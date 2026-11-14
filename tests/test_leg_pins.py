"""Leg pin layouts: leg discovery, placement, scaling and pin-assigned metrics."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import backend
import emx_adapter
import emx_campaign
import emx_search
import leg_pins
from pack_support import EXAMPLE_MODEL, PAIR_MODEL, install_example_pack, register_pair_transformer

TECH = json.loads((ROOT / 'tests' / 'fixtures' / 'generic_a.json').read_text())
# The built-in differential inductor (TAP off/on, several turns), the example
# structure pack (both lead variants) and a synthetic two-winding test model.
CASES = [('differential_inductor', 'layout', {}, ['L1', 'L2']),
         ('differential_inductor', 'layout', {'center_tap': True}, ['L1', 'L2', 'CTAP']),
         ('differential_inductor', 'layout', {'turns': 3, 'center_tap': True}, ['L1', 'L2', 'CTAP']),
         (EXAMPLE_MODEL, 'compact', {}, ['L1', 'L2', 'CTAP']),
         (EXAMPLE_MODEL, 'wide', {}, ['L1', 'L2', 'CTAP']),
         (PAIR_MODEL, 'layout', {}, ['P1', 'P2', 'S1', 'S2'])]


class LegPinTestCase(unittest.TestCase):
    def setUp(self):
        install_example_pack(self)
        register_pair_transformer(self)
        entry = dict(name='Probe', available=True, layers=TECH['layers'], _tech=TECH, warnings=[],
                     model_default_layers={m['id']: backend.layer_defaults(TECH, m['id']) for m in backend.models()})
        patcher = mock.patch.dict(backend.LIBRARIES, {'Probe': entry}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def design(self, model, structure='layout', layout=None, **parameters):
        design = dict(model=model, structure=structure, library='Probe', cell='Pins', parameters=parameters, layers={})
        if layout is not None:
            design['leg_pins'] = layout
        return design

    def preview(self, *args, **kwargs):
        return backend.preview(self.design(*args, **kwargs))


class LegDiscoveryTests(LegPinTestCase):
    def test_every_model_exposes_one_leg_per_default_signal_pin(self):
        for model, structure, parameters, expected in CASES:
            with self.subTest(model=model, structure=structure):
                result = self.preview(model, structure, **parameters)
                self.assertTrue(result['ok'], result['errors'])
                found = leg_pins.legs(result['geometry'])
                self.assertEqual([leg['id'] for leg in found], expected)
                for leg in found:
                    port = leg['default_port']
                    end = leg['centerline'][-1]
                    self.assertTrue(port['bbox'][0][0] - 1e-6 <= end[0] <= port['bbox'][1][0] + 1e-6)
                    self.assertGreater(leg['length'], 0)

    def test_default_layout_is_a_no_op(self):
        for model, structure, parameters, _ in CASES:
            with self.subTest(model=model, structure=structure):
                plain = self.preview(model, structure, **parameters)
                layout = leg_pins.default_layout(plain['geometry'])
                again = self.preview(model, structure, layout=layout, **parameters)
                self.assertEqual(again['geometry']['ports'], plain['geometry']['ports'])
                self.assertFalse(leg_pins.requires_pin_mapping(layout))


class PlacementTests(LegPinTestCase):
    def test_multiple_named_pins_per_leg_with_independent_counts(self):
        layout = {'P1': [dict(name='pin_1', ratio=0, length=1), dict(name='pin_1b', ratio=0.5, length=1)],
                  'P2': [dict(name='pin_2', ratio=0, length=1)], 'S1': [], 'S2': [dict(name='S2', ratio=0, length=2)]}
        result = self.preview(PAIR_MODEL, layout=layout)
        self.assertTrue(result['ok'], result['errors'])
        ports = {p['name']: p for p in result['geometry']['ports']}
        self.assertNotIn('P1', ports)
        self.assertNotIn('S1', ports)
        self.assertEqual(ports['pin_1']['leg'], 'P1')
        self.assertEqual(ports['pin_1b']['leg'], 'P1')
        box = ports['pin_1']['bbox']
        self.assertAlmostEqual(box[1][1] - box[0][1], 1.0)            # length along the vertical leg
        leg = next(l for l in leg_pins.legs(self.preview(PAIR_MODEL)['geometry']) if l['id'] == 'P1')
        horizontal = abs(leg['centerline'][-1][1] - leg['centerline'][-2][1]) < 1e-9
        self.assertAlmostEqual(box[1][int(horizontal)] - box[0][int(horizontal)], leg['width'])  # width follows the leg
        self.assertIsNone(ports['pin_1']['winding'])
        self.assertEqual(ports['S2']['winding'], 'S')                  # default name keeps its role
        self.assertTrue(leg_pins.requires_pin_mapping(layout))
        self.assertIn('GND_PGS', ports)

    def test_pins_keep_their_relative_position_when_the_leg_length_changes(self):
        layout = {'L2': [dict(name='a', ratio=0, length=1), dict(name='b', ratio=0.25, length=1),
                         dict(name='c', ratio=0.6, length=2)]}
        spans = []
        for leg_length in (45, 90):
            result = self.preview('differential_inductor', layout=layout, leg_length=leg_length)
            self.assertTrue(result['ok'], result['errors'])
            total = result['geometry']['metrics']['leg_pins']['legs']['L2']
            distances = {p['name']: p['leg_distance'] for p in result['geometry']['ports'] if p.get('leg') == 'L2'}
            self.assertEqual(distances['a'], 0)
            self.assertAlmostEqual(distances['b'] / total, 0.25, places=6)
            self.assertAlmostEqual(distances['c'] / total, 0.6, places=6)
            spans.append(distances['c'] - distances['b'])
        self.assertGreater(spans[1], spans[0] * 1.5)

    def test_access_direction_faces_the_free_end(self):
        result = self.preview(PAIR_MODEL, layout={'S2': [dict(name='top_pin', ratio=0.3, length=1)],
                                                  'P1': [dict(name='bottom_pin', ratio=0.3, length=1)]})
        ports = {p['name']: p for p in result['geometry']['ports']}
        self.assertEqual(ports['top_pin']['access_direction'], 'top')
        self.assertEqual(ports['bottom_pin']['access_direction'], 'bottom')

    def test_errors_block_the_layout(self):
        cases = {
            'leg_pin_overlap': {'P1': [dict(name='a', ratio=0, length=2), dict(name='b', ratio=0.01, length=2)]},
            'leg_pin_too_long': {'P1': [dict(name='a', ratio=0, length=10000)]},
            'leg_pin_unknown_leg': {'Q9': [dict(name='a', ratio=0, length=1)]},
            'leg_pin_reserved_name': {'P1': [dict(name='GND_RING', ratio=0, length=1)]},
            'leg_pin_foreign_default': {'P1': [dict(name='S1', ratio=0, length=1)]},
            'leg_pin_net_conflict': {'P1': [dict(name='x', ratio=0, length=1)], 'S1': [dict(name='x', ratio=0, length=1)]},
            'leg_pin_kind_conflict': {'L1': [dict(name='x', ratio=0, length=1)], 'CTAP': [dict(name='x', ratio=0, length=1)]},
        }
        for code, layout in cases.items():
            with self.subTest(code=code):
                model = 'differential_inductor' if code == 'leg_pin_kind_conflict' else PAIR_MODEL
                result = self.preview(model, layout=layout, **({'center_tap': True} if model == 'differential_inductor' else {}))
                self.assertFalse(result['ok'])
                self.assertIn(code, [e['code'] for e in result['errors']])

    def test_rectangles_overlapping_at_a_bend_are_rejected(self):
        # Review T-0192: distinct path intervals can still overlap physically at a corner.
        plain = self.preview('differential_inductor')
        leg = next(l for l in leg_pins.legs(plain['geometry']) if l['id'] == 'L1')
        segments = leg_pins._segments_from_end(leg['centerline'])
        bend = segments[0][1]
        layout = {'L1': [dict(name='a', ratio=(bend - 1) / leg['length'], length=1),
                         dict(name='b', ratio=bend / leg['length'], length=1)]}
        result = self.preview('differential_inductor', layout=layout)
        self.assertFalse(result['ok'])
        self.assertIn('leg_pin_overlap', [e['code'] for e in result['errors']])

    def test_pins_crossing_a_bend_are_rejected_not_moved(self):
        plain = self.preview('differential_inductor')
        leg = next(l for l in leg_pins.legs(plain['geometry']) if l['id'] == 'L1')
        bend = leg_pins._segments_from_end(leg['centerline'])[0][1]
        result = self.preview('differential_inductor', layout={'L1': [dict(name='a', ratio=(bend - 0.5) / leg['length'], length=2)]})
        self.assertFalse(result['ok'])
        self.assertIn('leg_pin_segment', [e['code'] for e in result['errors']])
        port, problem = leg_pins.place(leg, dict(name='a', ratio=10 / leg['length'], length=2))
        self.assertIsNone(problem)
        self.assertAlmostEqual(port['leg_distance'], 10, places=6)   # placed exactly where stored

    def test_repeated_names_warn_but_generate(self):
        layout = {'P1': [dict(name='p', ratio=0, length=1)], 'P2': [dict(name='p', ratio=0, length=1)]}
        result = self.preview(PAIR_MODEL, layout=layout)
        self.assertTrue(result['ok'], result['errors'])
        self.assertIn('leg_pin_duplicate_name', [w['code'] for w in result['warnings']])
        contract = emx_adapter.port_contract(result['geometry'])
        terminal = next(t for t in contract['terminals'] if t['name'] == 'p')
        self.assertEqual(len(terminal['regions']), 2)

    def test_invalid_stored_layouts_are_rejected_on_normalize(self):
        for layout in ({'P1': 'x'}, {'P1': [dict(name='1bad', ratio=0, length=1)]},
                       {'P1': [dict(name='a', ratio=2, length=1)]}, {'P1': [dict(name='a', ratio=0, length=0)]},
                       {'P1': [dict(name='a', ratio=True, length=1)]}):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                backend.normalize_design(self.design(PAIR_MODEL, layout=layout))

    def test_normalized_design_round_trips_the_layout(self):
        layout = {'P1': [dict(name='pin_1', ratio=0.125, length=1.5)]}
        normalized = backend.normalize_design(self.design(PAIR_MODEL, layout=layout))
        self.assertEqual(normalized['leg_pins'], layout)
        self.assertNotIn('leg_pins', backend.normalize_design(self.design(PAIR_MODEL)))

    def test_emx_port_contract_drives_every_custom_pin(self):
        layout = {'P1': [dict(name='pin_1', ratio=0, length=1), dict(name='pin_x', ratio=0.5, length=1)],
                  'P2': [dict(name='pin_2', ratio=0, length=1)],
                  'S1': [dict(name='pin_3', ratio=0, length=1)], 'S2': [dict(name='pin_4', ratio=0, length=1)]}
        result = self.preview(PAIR_MODEL, layout=layout)
        contract = emx_adapter.port_contract(result['geometry'])
        self.assertEqual(contract['signals'], ['pin_1', 'pin_x', 'pin_2', 'pin_3', 'pin_4'])
        self.assertEqual(leg_pins.signal_pin_names(result['geometry']), contract['signals'])


class PinMetricTests(unittest.TestCase):
    def network(self):
        snp = ROOT / 'tests' / 'fixtures' / 'ideal_coupled.s4p'
        lines = snp.read_text().splitlines()
        unit = next(line for line in lines if line.startswith('#')).split()[1].upper()
        scale = {'GHZ': 1e9, 'MHZ': 1e6, 'KHZ': 1e3, 'HZ': 1}[unit]
        frequencies = [float(line.split()[0]) * scale for line in lines if line and line[0].isdigit()]
        return emx_adapter.read_touchstone(snp, 4, frequencies)

    def ports(self):
        signals = ['P1', 'P2', 'S1', 'S2']
        return dict(signals=signals, grounds=[], protocol='fixed_ground_pins',
                    terminals=[dict(name=n, winding=n[0], endpoint='positive' if n[1] == '1' else 'negative') for n in signals])

    def test_pin_assigned_metrics_match_the_built_in_winding_pairs(self):
        goals = [dict(metric='Lp', pins=['P1', 'P2']), dict(metric='Ls', pins=['S1', 'S2']),
                 dict(metric='Qp', pins=['P1', 'P2']), dict(metric='k', pins=['P1', 'P2', 'S1', 'S2'])]
        rows = emx_campaign._metrics(emx_adapter.summarize_network(self.network(), self.ports(), emx_search.metric_specs(goals, [])))
        for row in rows:
            self.assertAlmostEqual(row['Lp'], row['Lp[P1,P2]'], places=9)
            self.assertAlmostEqual(row['Ls'], row['Ls[S1,S2]'], places=9)
            self.assertAlmostEqual(row['Qp'], row['Qp[P1,P2]'], places=9)
            self.assertAlmostEqual(row['k'], row['k[P1,P2,S1,S2]'], places=12)
        swapped = emx_adapter.summarize_network(self.network(), self.ports(),
                                                [dict(key='k[P1,P2,S2,S1]', metric='k', pins=['P1', 'P2', 'S2', 'S1'])])
        self.assertAlmostEqual(swapped['samples'][0]['pin_metrics']['k[P1,P2,S2,S1]'], -rows[0]['k'], places=12)

    def test_same_metric_on_different_pins_is_evaluated_separately(self):
        goals = [dict(metric='L', pins=['P1', 'P2'], frequency_hz=0, direction='maximize'),
                 dict(metric='L', pins=['S1', 'S2'], frequency_hz=0, direction='maximize')]
        specs = emx_search.metric_specs(goals, [])
        self.assertEqual([s['key'] for s in specs], ['L[P1,P2]', 'L[S1,S2]'])
        rows = emx_campaign._metrics(emx_adapter.summarize_network(self.network(), self.ports(), specs))
        for goal in goals:
            goal['frequency_hz'] = rows[0]['frequency_hz']
        result = emx_search.evaluate_response(rows, goals, [])
        self.assertAlmostEqual(result['objective_values'][0], rows[0]['Lp'], places=9)
        self.assertAlmostEqual(result['objective_values'][1], rows[0]['Ls'], places=9)

    def test_unknown_pins_are_reported_not_guessed(self):
        summary = emx_adapter.summarize_network(self.network(), self.ports(), [dict(key='L[X,P1]', metric='L', pins=['X', 'P1'])])
        self.assertIn('L[X,P1]', summary['pin_metric_errors'])
        self.assertNotIn('pin_metrics', summary['samples'][0])

    def test_keys_and_pin_checks(self):
        self.assertEqual(emx_search.metric_key(dict(metric='Lp', pins=['a', 'b'])), 'Lp[a,b]')
        self.assertEqual(emx_search.metric_key(dict(metric='Lp')), 'Lp')
        self.assertEqual(emx_search.parse_metric_key('k[a, b,c,d]'), ('k', ['a', 'b', 'c', 'd']))
        self.assertEqual(emx_search.parse_metric_key('Qs'), ('Qs', None))
        for metric, pins in (('Lp', ['a']), ('k', ['a', 'b', 'c']), ('Lp', ['a', 'a']), ('k', ['a', 'b', 'c', 'c']), ('foo', ['a', 'b'])):
            with self.subTest(metric=metric, pins=pins), self.assertRaises(ValueError):
                emx_search.check_pins(metric, pins)
        with self.assertRaisesRegex(ValueError, 'not in the layout'):
            emx_search.check_pins('L', ['a', 'z'], ['a', 'b'])

    def test_campaign_goals_require_pins_for_custom_layouts(self):
        goal = dict(metric='Lp', frequency_hz=1e9, direction='maximize')
        emx_campaign._validate_goals([goal], [], ['Lp', 'Ls', 'k'])
        with self.assertRaisesRegex(ValueError, 'assign pins'):
            emx_campaign._validate_goals([goal], [], ['Lp'], ['a', 'b'], True)
        emx_campaign._validate_goals([dict(goal, pins=['a', 'b'])], [], ['Lp'], ['a', 'b'], True)
        emx_campaign._validate_goals([dict(goal, metric='k', pins=['a', 'b', 'c', 'd'])], [], ['L', 'Q'], list('abcd'), True)
        with self.assertRaises(ValueError):
            emx_campaign._validate_goals([dict(goal, pins=['a', 'q'])], [], ['Lp'], ['a', 'b'], True)


if __name__ == '__main__':
    unittest.main()


class StackedLegKeyTests(unittest.TestCase):
    def test_legs_on_different_metals_with_the_same_centerline_are_both_found(self):
        line = [[0.0, 0.0], [0.0, -40.0]]
        geometry = dict(
            polygons=[dict(layer='MA', part='legs', net='P', width=4.0, centerline=line, points=[]),
                      dict(layer='MB', part='legs', net='S', width=4.0, centerline=line, points=[])],
            ports=[dict(name='P1', layer='MA', net='P', part='legs', kind='signal', bbox=[[-2, -40], [2, -36]]),
                   dict(name='S1', layer='MB', net='S', part='legs', kind='signal', bbox=[[-2, -40], [2, -36]])])
        self.assertEqual([leg['id'] for leg in leg_pins.legs(geometry)], ['P1', 'S1'])

