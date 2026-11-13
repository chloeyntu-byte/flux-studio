"""Independent passive-mask checks against the four synthetic technology fixtures.

This is not a foundry DRC or electromagnetic validation.  All layers, vias and rules in these fixtures are fictional.
"""
import unittest

import geometry
from backend import layer_defaults
from structures import generate_model
from test_geometry import assert_grid_octilinear
from test_structures import layers_for, profile


PROFILES = ('generic_a', 'generic_b', 'generic_c', 'generic_d')
MODELS = ('differential_inductor',)


class PassiveModelMatrixTests(unittest.TestCase):
    def build(self, model, tech, parameters=None):
        params = {'pgs_extent': 10, 'pgs_spacing': 8, 'ring_extent': 500}
        params.update(parameters or {})
        result = generate_model(params, layer_defaults(tech, model), tech, model)
        self.assertTrue(result['ok'], (model, result['errors']))
        return result

    def test_default_masks_ports_and_native_vias(self):
        for library in PROFILES:
            tech = profile(library)
            via_names = {via['name'] for via in tech['vias']}
            for model in MODELS:
                with self.subTest(library=library, model=model):
                    result = self.build(model, tech)
                    masks = result['geometry']
                    assert_grid_octilinear(self, result, tech)
                    expected = ['L']
                    errors, components = geometry.validate_geometry(masks, tech, expected_windings=expected, rule_policy='warn', warnings=[])
                    self.assertEqual(errors, [])
                    self.assertEqual(components, {net: 1 for net in expected})
                    self.assertTrue(all(v['via_def'] in via_names for v in masks['vias']))
                    for ground in (p for p in masks['ports'] if p['net'] == 'GND'):
                        self.assertEqual(ground.get('kind'), 'ground')
                        self.assertIn(ground.get('access_direction'), ('bottom', 'top', 'left', 'right'))
                    ports = [port for port in masks['ports'] if port['net'] in expected]
                    self.assertEqual(len([p for p in ports if p.get('kind') == 'signal']), 2 * len(expected))
                    directions = masks['metrics'].get('terminal_directions', {})
                    for port in ports:
                        if port.get('kind') == 'signal' and port['net'] in directions:
                            self.assertEqual(port.get('access_direction'), directions[port['net']])
                    for port in ports:
                        self.assertIn(port['kind'], ('signal', 'tap'))
                        self.assertIn(port['winding'], expected)
                        self.assertIn(port['endpoint'], ('positive', 'negative', 'center'))
                        self.assertIn(port['access_direction'], ('bottom', 'top', 'left', 'right'))
                        rect = geometry._rect([*port['bbox'][0], *port['bbox'][1]])
                        self.assertTrue(any(p['net'] == port['net'] and p['layer'] == port['layer'] and
                                            all(geometry._point_inside(q, p['points']) for q in rect)
                                            for p in masks['polygons']), port)
                    # Connectivity alone cannot detect a short between two turns.
                    tracks = [p for p in masks['polygons'] if p.get('feature') == 'symmetric_half_turn']
                    for index, left in enumerate(tracks):
                        for right in tracks[:index]:
                            if (left['net'] == right['net'] and left['layer'] == right['layer'] and
                                    left.get('turn_index') != right.get('turn_index')):
                                self.assertGreater(geometry.polygon_distance(left['points'], right['points']), 0)
                    for landing in (p for p in masks['polygons'] if p.get('landing') and p['net'] in expected):
                        for other in masks['polygons']:
                            if other['net'] in expected and other['net'] != landing['net'] and other['layer'] == landing['layer']:
                                self.assertGreater(geometry.polygon_distance(landing['points'], other['points']), 0)

    def test_differential_tracks_mirror_and_tap_hits_join_midpoint(self):
        for turns in (1, 2, 3, 4, 8):
            with self.subTest(turns=turns):
                tech = profile('generic_d')
                masks = self.build('differential_inductor', tech,
                                   {'turns': turns, 'center_tap': True})['geometry']
                arcs = [p for p in masks['polygons'] if p.get('feature') == 'symmetric_half_turn']
                self.assertEqual(len(arcs), 2 * turns)
                for index in range(turns):
                    left = next(p for p in arcs if p['turn_index'] == index and p['side'] == 'left')
                    right = next(p for p in arcs if p['turn_index'] == index and p['side'] == 'right')
                    self.assertEqual([[-x, y] for x, y in left['centerline']], right['centerline'])
                join = next(p for p in masks['polygons'] if p.get('feature') == 'center_join')
                line = join['centerline']
                midpoint = [(line[0][i] + line[-1][i]) / 2 for i in (0, 1)]
                self.assertEqual(midpoint, masks['metrics']['tap_midpoint'])
                self.assertTrue(any(v['net'] == 'L' and v['origin'] == midpoint for v in masks['vias']))

    def test_tap_and_ground_are_independent_and_connected(self):
        for library in PROFILES:
            for model in MODELS:
                with self.subTest(library=library, model=model):
                    tech = profile(library)
                    result = self.build(model, tech, {'center_tap': True, 'pgs_connect_ring': True,
                                                       'ring_width': 7, 'ring_extent': 103})
                    masks = result['geometry']
                    errors, components = geometry.validate_geometry(
                        masks, tech, expected_windings=['L'], rule_policy='warn', warnings=[])
                    self.assertEqual(errors, [])
                    self.assertEqual(components['L'], 1)
                    self.assertEqual(sum(p.get('kind') == 'tap' for p in masks['ports']), 1)
                    self.assertTrue(masks['metrics']['pgs_connect_ring'])
                    for poly in (p for p in masks['polygons'] if p['net'] == 'L'):
                        for ground in (p for p in masks['polygons'] if p['net'] == 'GND' and p['layer'] == poly['layer']):
                            self.assertGreater(geometry.polygon_distance(poly['points'], ground['points']), 0)

    def test_turn_counts_across_four_technologies_with_and_without_tap(self):
        for library in PROFILES:
            for turns in (1, 2, 3, 8):
                for tap in (False, True):
                    with self.subTest(library=library, turns=turns, tap=tap):
                        tech = profile(library)
                        result = self.build('differential_inductor', tech, {'turns': turns, 'center_tap': tap})
                        masks = result['geometry']
                        assert_grid_octilinear(self, result, tech)
                        errors, components = geometry.validate_geometry(masks, tech, expected_windings=['L'], rule_policy='warn', warnings=[])
                        self.assertEqual(errors, [])
                        self.assertEqual(components['L'], 1)
                        ports = {p['name']: p for p in masks['ports']}
                        self.assertEqual(ports['L1']['access_direction'], 'bottom')
                        self.assertEqual(ports['L2']['access_direction'], 'bottom')
                        self.assertEqual(sum(p.get('kind') == 'tap' for p in masks['ports']), int(tap))

    def test_tap_is_an_explicit_option(self):
        for library in PROFILES:
            for model in MODELS:
              with self.subTest(library=library, model=model):
                tech = profile(library)
                off = self.build(model, tech, {'center_tap': False})
                on = self.build(model, tech, {'center_tap': True})
                ports_off = [p for p in off['geometry']['ports'] if p.get('winding') == 'L']
                ports_on = [p for p in on['geometry']['ports'] if p.get('winding') == 'L']
                self.assertEqual(sum(p.get('kind') == 'tap' for p in ports_off), 0)
                taps = [p for p in ports_on if p.get('kind') == 'tap']
                self.assertEqual(len(taps), 1)
                self.assertEqual(taps[0]['endpoint'], 'center')


if __name__ == '__main__':
    unittest.main()
