"""Offline model-contract checks using synthetic generic rule fixtures, never runtime defaults."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import geometry
from structures import MODEL_DEFINITIONS, generate_model
from test_geometry import assert_grid_octilinear


def profile(name='generic_d'):
    return json.loads((Path(__file__).resolve().parent / 'fixtures' / (name + '.json')).read_text())


def layers_for(tech, model='differential_inductor'):
    metals = sorted(tech['layers'], key=lambda rule: rule['order'])
    return {'body': metals[-2]['name'], 'crossing': metals[-3]['name'], 'tap': metals[-4]['name'],
            'pgs': metals[0]['name'], 'ring': metals[-1]['name'], 'ground_route': metals[1]['name']}


class StructureTests(unittest.TestCase):
    def build(self, model='differential_inductor', parameters=None, technology=None, layers=None):
        tech = technology or profile()
        return generate_model(parameters or {}, layers or layers_for(tech, model), tech, model)

    def assert_ok(self, result):
        self.assertTrue(result['ok'], result['errors'])

    def test_only_the_differential_inductor_is_built_in(self):
        self.assertEqual([definition['id'] for definition in MODEL_DEFINITIONS], ['differential_inductor'])
        result = self.build('transformer_unknown')
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'][0]['code'], 'unsupported_model')

    def test_every_exported_mask_path_and_ring_tile_uses_integer_grid_octilinear_edges(self):
        for library in ('generic_d', 'generic_a', 'generic_b', 'generic_c'):
            tech = profile(library)
            for tap in (False, True):
                for pgs in ('square', 'octagon', 'circle'):
                    for ring in ('square', 'octagon'):
                        with self.subTest(library=library, tap=tap, pgs=pgs, ring=ring):
                            result = self.build(parameters={'pgs_shape': pgs, 'ring_shape': ring, 'center_tap': tap,
                                'pgs_connect_ring': True, 'ring_width': 7}, technology=tech)
                            self.assert_ok(result)
                            assert_grid_octilinear(self, result, tech)
                            self.assertEqual(result['geometry']['metrics']['winding_components'], {'L': 1})

    def test_unequal_turn_counts_widths_and_fractional_sizes_keep_final_manhattan_or_45_edges(self):
        cases = [{'turns': 1}, {'turns': 3, 'inner_diameter': 110.03, 'width': 8.01, 'spacing': 8.01},
                 {'turns': 8, 'ring_extent': 220}, {'turns': 4, 'width': 9.01, 'center_tap': True, 'ring_extent': 180}]
        tech = profile()
        for parameters in cases:
            with self.subTest(parameters=parameters):
                result = self.build(parameters=parameters)
                self.assert_ok(result)
                assert_grid_octilinear(self, result, tech)

    def test_defaults_are_connected_isolated_and_legal_on_all_four_generic_profiles(self):
        for library in ('generic_d', 'generic_a', 'generic_b', 'generic_c'):
            tech = profile(library)
            for tap in (False, True):
                with self.subTest(library=library, tap=tap):
                    result = self.build(parameters={'center_tap': tap}, technology=tech)
                    self.assert_ok(result)
                    masks = result['geometry']
                    self.assertEqual(masks['metrics']['winding_components'], {'L': 1})
                    self.assertEqual(masks['metrics']['octagonal_center'], [0, 0])
                    self.assertEqual(len([p for p in masks['ports'] if p['net'] != 'GND']), 3 if tap else 2)
                    for port in masks['ports']:
                        points = geometry._rect([port['bbox'][0][0], port['bbox'][0][1], port['bbox'][1][0], port['bbox'][1][1]])
                        self.assertTrue(any(poly['net'] == port['net'] and poly['layer'] == port['layer'] and
                            all(geometry._point_inside(point, poly['points']) for point in points)
                            for poly in masks['polygons']), port)

    def test_single_turn_inductor_needs_no_underpass_but_multiturn_has_real_vias(self):
        one = self.build(parameters={'turns': 1})
        multi = self.build(parameters={'turns': 3})
        self.assert_ok(one)
        self.assert_ok(multi)
        self.assertEqual([via for via in one['geometry']['vias'] if via['part'] == 'crossing'], [])
        self.assertTrue(multi['geometry']['vias'])
        half_turns = [poly for poly in multi['geometry']['polygons'] if poly.get('feature') == 'symmetric_half_turn']
        self.assertEqual(len(half_turns), 6)
        native_names = {via['name'] for via in profile()['vias']}
        self.assertTrue(all(via['via_def'] in native_names for via in multi['geometry']['vias']))

    def test_center_tap_adds_a_third_port_on_its_own_metal(self):
        tech = profile()
        roles = layers_for(tech)
        plain = self.build(parameters={'center_tap': False})
        tapped = self.build(parameters={'center_tap': True})
        self.assert_ok(plain)
        self.assert_ok(tapped)
        self.assertNotIn('CTAP', {port['name'] for port in plain['geometry']['ports']})
        tap_port = next(port for port in tapped['geometry']['ports'] if port['name'] == 'CTAP')
        self.assertEqual(tap_port['layer'], roles['tap'])
        self.assertTrue(any(roles['tap'] in (via['lower'], via['upper']) for via in tapped['geometry']['vias']))

    def test_eight_turn_winding_stays_continuous_without_shortcut_contacts(self):
        result = self.build(parameters={'turns': 8, 'ring_extent': 200})
        self.assert_ok(result)
        masks = result['geometry']
        self.assertEqual(masks['metrics']['winding_components'], {'L': 1})
        turns = {}
        for poly in masks['polygons']:
            if poly.get('feature') == 'symmetric_half_turn' and poly['side'] == 'left':
                turns[poly['turn_index']] = poly
        self.assertEqual(len(turns), 8)
        for index in range(1, 8):
            self.assertGreater(geometry.polygon_distance(turns[index]['points'], turns[index - 1]['points']), 7.98)

    def test_missing_return_via_and_turn_connection_disconnect_the_winding(self):
        result = self.build(parameters={'turns': 3})
        self.assert_ok(result)
        for remove in ('via', 'connector'):
            masks = copy.deepcopy(result['geometry'])
            if remove == 'via':
                masks['vias'].pop(0)
            else:
                index = next(i for i, poly in enumerate(masks['polygons']) if poly.get('feature') == 'center_join')
                masks['polygons'].pop(index)
            errors, components = geometry.validate_geometry(masks, profile(), expected_windings=['L'])
            self.assertGreater(components['L'], 1)
            self.assertTrue(any(error['code'] == 'disconnected_winding' for error in errors))

    def test_bad_turn_counts_layer_choices_and_dimensions_report_english_fields(self):
        for params in ({'turns': 0}, {'turns': 1.25}, {'turns': True}, {'turns': 9}, {'width': True},
                       {'spacing': 0.1, 'rule_policy': 'strict'}, {'inner_diameter': float('nan')}, {'terminal_gap': 250},
                       {'center_tap': 'yes'}):
            with self.subTest(parameters=params):
                result = self.build(parameters=params)
                self.assertFalse(result['ok'])
                self.assertTrue(any(error['fields'] for error in result['errors']))
                self.assertTrue(all(all(ord(char) < 128 for char in error['message']) for error in result['errors']))
        tech = profile()
        roles = layers_for(tech)
        roles['crossing'] = roles['body']
        result = self.build(technology=tech, layers=roles)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'][0]['code'], 'same_underpass_layer')

    def test_real_width_limits_and_missing_native_chain_are_rejected(self):
        oversized = self.build(parameters={'width': 14, 'rule_policy': 'strict'})
        self.assertFalse(oversized['ok'])
        self.assertTrue({'metal_width', 'landing_width'} & {error['code'] for error in oversized['errors']})
        missing = profile()
        missing['vias'] = []
        result = self.build(technology=missing)
        self.assertFalse(result['ok'])
        self.assertTrue(any(error['code'] == 'no_native_via_chain' for error in result['errors']))

    def test_below_rule_spacing_is_rejected_when_strict_and_only_warned_otherwise(self):
        tech = profile()
        strict = self.build(parameters={'turns': 3, 'spacing': 1, 'terminal_gap': 1, 'rule_policy': 'strict'})
        self.assertFalse(strict['ok'])
        self.assertTrue({'turn_spacing', 'terminal_spacing'} <= {error['code'] for error in strict['errors']}, strict['errors'])
        relaxed = self.build(parameters={'turns': 3, 'spacing': 1, 'terminal_gap': 1, 'rule_policy': 'warn'})
        self.assert_ok(relaxed)
        self.assertTrue({'turn_spacing', 'terminal_spacing'} <= {warning['code'] for warning in relaxed['warnings']})
        _, components = geometry.validate_geometry(relaxed['geometry'], tech, expected_windings=['L'])
        self.assertEqual(components['L'], 1)

    def test_crowded_ground_stack_collision_is_rejected(self):
        for library in ('generic_d', 'generic_a', 'generic_b', 'generic_c'):
            with self.subTest(library=library):
                result = self.build(parameters={'pgs_connect_ring': True, 'ring_width': 7}, technology=profile(library))
                self.assert_ok(result)
        tech = profile('generic_d')
        # A ring on the winding metal that is close to the leads touches them.
        roles = dict(layers_for(tech), ring=layers_for(tech)['body'])
        crowded = self.build(parameters={'pgs_connect_ring': True, 'ring_width': 7, 'ring_extent': 103, 'leg_length': 100},
                             technology=tech, layers=roles)
        self.assertFalse(crowded['ok'])
        self.assertTrue(any(error['code'] in ('same_layer_short', 'metal_spacing') for error in crowded['errors']))


if __name__ == '__main__':
    unittest.main()
