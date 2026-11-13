"""Independent shields across geometry, saved designs, UI and EMX port contracts."""
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import backend
import emx_adapter
import geometry
from structures import generate_model
from test_structures import profile, layers_for
from test_geometry import assert_grid_octilinear
from test_ui_rule_warnings import ui, Variable
from pack_support import install_example_pack


class OptionalShieldsTests(unittest.TestCase):
    def build(self, model='differential_inductor', parameters=None, tech=None, layers=None):
        tech = tech or profile()
        layers = layers or backend.layer_defaults(tech, model)
        return generate_model(parameters or {}, layers, tech, model)

    def test_all_four_generic_stacks_taps_and_shield_combinations(self):
        for pdk, tap, pgs, ring in itertools.product(
                ('generic_a', 'generic_d', 'generic_b', 'generic_c'), (False, True), (False, True), (False, True)):
            with self.subTest(pdk=pdk, tap=tap, pgs=pgs, ring=ring):
                tech = profile(pdk)
                r = self.build('differential_inductor', dict(center_tap=tap, pgs_enabled=pgs, ring_enabled=ring), tech)
                self.assertTrue(r['ok'], r['errors'])
                g = r['geometry']
                assert_grid_octilinear(self, r, tech)
                self.assertEqual(any(p['part'] == 'pgs' for p in g['polygons']), pgs)
                self.assertEqual(any(p['part'] == 'ring' for p in g['polygons']), ring)
                expected = (['GND_PGS'] if pgs else []) + (['GND_RING'] if ring else [])
                contract = emx_adapter.port_contract(g)
                self.assertEqual(contract['grounds'], expected)
                self.assertEqual(len(contract['signals']), 3 if tap else 2)
                self.assertEqual(len(contract['definitions']), len(contract['signals']) + len(expected))
                self.assertEqual(g['metrics']['ring_inner_radius'] is not None, ring)
                self.assertEqual(set(g['metrics']['winding_components']), {'L'})

    def test_disabled_shields_ignore_stale_dimensions_layers_and_rules(self):
        tech = profile()
        for rule in tech['layers']:
            if rule['name'] == 'M1': rule.update(min_width=None, min_spacing=None)
        layers = backend.layer_defaults(tech)
        layers.update(pgs='missing_pgs', ring='missing_ring', ground_route='missing_route')
        p = dict(pgs_enabled=False, ring_enabled=False, pgs_connect_ring=True,
                 pgs_width=None, pgs_spacing=-100, pgs_extent='bad', pgs_shape='bad',
                 ring_extent=None, ring_width='bad', ring_shape='bad', ground_route_width=None)
        result = self.build(parameters=p, tech=tech, layers=layers)
        self.assertTrue(result['ok'], result['errors'])
        self.assertIn('ground_link_inactive', [w['code'] for w in result['warnings']])
        self.assertFalse(result['geometry']['metrics']['pgs_connect_ring'])
        self.assertFalse(any(w.get('layer') == 'M1' for w in result['warnings']))
        p['pgs_enabled'] = True
        self.assertFalse(self.build(parameters=p, tech=tech, layers=layers)['ok'])

    def test_one_shield_does_not_validate_the_other_or_draw_link(self):
        for pgs, ring in ((False, True), (True, False)):
            p = dict(pgs_enabled=pgs, ring_enabled=ring, pgs_connect_ring=True, ground_route_width=None)
            layers = backend.layer_defaults(profile())
            layers['ground_route'] = ''
            if not pgs:
                p.update(pgs_width=None, pgs_shape='bad'); layers['pgs'] = ''
            if not ring:
                p.update(ring_width=None, ring_extent=None); layers['ring'] = ''
            r = self.build(parameters=p, layers=layers)
            self.assertTrue(r['ok'], r['errors'])
            self.assertFalse(any(q.get('feature') == 'ground_route' for q in r['geometry']['polygons']))
            self.assertFalse(any(v['part'] == 'pgs' for v in r['geometry']['vias']))

    def test_link_is_restored_only_with_both_shields(self):
        p = dict(pgs_enabled=True, ring_enabled=True, pgs_connect_ring=True, ring_width=7)
        r = self.build(parameters=p)
        self.assertTrue(r['ok'], r['errors'])
        self.assertTrue(r['geometry']['metrics']['pgs_connect_ring'])
        self.assertTrue(any(q.get('feature') == 'ground_route' for q in r['geometry']['polygons']))
        self.assertTrue(any(v['part'] == 'pgs' for v in r['geometry']['vias']))
        p['pgs_enabled'] = False
        self.assertTrue(self.build(parameters=p)['ok'])
        p['pgs_enabled'] = True
        self.assertEqual(self.build(parameters=p)['geometry'], r['geometry'])

    def test_optional_tap_tracks_effective_ground_connection(self):
        for model, pgs, ring in itertools.product(('differential_inductor',), (False, True), (False, True)):
            p = dict(center_tap=True, pgs_enabled=pgs, ring_enabled=ring,
                     pgs_connect_ring=True, ring_width=7)
            r = self.build(model, p)
            self.assertTrue(r['ok'], (model, pgs, ring, r['errors']))
            c = emx_adapter.port_contract(r['geometry'])
            self.assertEqual(len(c['signals']), 3)
            self.assertEqual(sum(t['kind'] == 'tap' for t in c['terminals']), 1)
            if not (pgs and ring):
                unlinked = self.build(model, dict(p, pgs_connect_ring=False))
                body = lambda x: [q for q in x['geometry']['polygons'] if q['net'] != 'GND']
                self.assertEqual(body(r), body(unlinked))

    def test_enable_flags_require_actual_booleans(self):
        for name, value in itertools.product(('pgs_enabled', 'ring_enabled'), ('false', 0, 1, None)):
            r = self.build(parameters={name: value})
            self.assertFalse(r['ok'])
            self.assertTrue(any(name in e.get('fields', []) for e in r['errors']))

    def test_old_design_defaults_and_saved_switches_keep_drafts(self):
        install_example_pack(self)
        for definition in backend.models():
            model = definition['id']
            self.assertTrue(definition['defaults']['pgs_enabled'])
            old = backend.normalize_design(dict(model=model, parameters={}))
            self.assertTrue(old['parameters']['pgs_enabled'])
            self.assertTrue(old['parameters']['ring_enabled'])
            design = dict(old, parameters=dict(old['parameters'], pgs_enabled=False,
                          ring_enabled=True, pgs_width=0.7), layers=dict(pgs='CustomPGS', ring='CustomRing'))
            with tempfile.TemporaryDirectory() as d, patch.object(backend, 'PRESETS', Path(d)):
                backend.save_preset('Optional shields', design)
                loaded = backend.load_preset('Optional shields')['design']
                self.assertFalse(loaded['parameters']['pgs_enabled'])
                self.assertTrue(loaded['parameters']['ring_enabled'])
                self.assertEqual(loaded['parameters']['pgs_width'], 0.7)
                self.assertEqual(loaded['layers']['pgs'], 'CustomPGS')

    def test_no_ground_contacts_preserves_signals_and_implicit_reference(self):
        g = self.build(parameters=dict(pgs_enabled=False, ring_enabled=False))['geometry']
        c = emx_adapter.port_contract(g)
        self.assertEqual(c['grounds'], [])
        self.assertEqual(len(c['signals']), 2)
        self.assertTrue(all(t['driven'] for t in c['definitions']))
        self.assertIn('no physical ground', c['reference'])
        with self.assertRaises(ValueError):
            emx_adapter.port_contract(g, dict(ground_ports=['GND_PGS']))
        with self.assertRaises(ValueError):
            emx_adapter.port_contract(g, dict(ground_protocol='local_returns', return_map={}))


class OptionalShieldsUITests(unittest.TestCase):
    def setUp(self):
        popup = patch.object(app.messagebox, "showwarning")
        popup.start()
        self.addCleanup(popup.stop)

    def controller(self):
        c = ui()
        c.configure_library('ExampleDesign', apply_defaults=True)
        return c

    def test_switches_stay_draft_until_apply_and_restore_settings(self):
        c = self.controller()
        c.applied_design = copy.deepcopy(c.collect_design())
        c.geometry = {'ports': [{'name':'GND_PGS'}]}
        c.vars['pgs_enabled'].set(False); c.vars['ring_enabled'].set(False)
        c.vars['pgs_width'].set('bad'); c.vars['ring_width'].set('bad')
        c.vars['pgs'].set('missing'); c.vars['ring'].set('missing')
        draft = c.collect_design()
        self.assertEqual(c.local_errors(draft), [])
        self.assertTrue(c.applied_design['parameters']['pgs_enabled'])
        self.assertEqual(c.geometry['ports'], [{'name':'GND_PGS'}])
        self.assertEqual(draft['layers']['pgs'], 'missing')
        c.apply_parameters()
        self.assertFalse(c.applied_design['parameters']['pgs_enabled'])
        c.vars['pgs_enabled'].set(True)
        self.assertTrue(c.local_errors(c.collect_design()))

    def test_inactive_controls_and_link_follow_two_independent_switches(self):
        c = self.controller()
        c.vars['pgs_connect_ring'].set(True)
        for pgs, ring in itertools.product((False, True), repeat=2):
            c.vars['pgs_enabled'].set(pgs); c.vars['ring_enabled'].set(ring)
            c.update_controls()
            roles = c.active_model_roles()
            self.assertEqual('pgs' in roles, pgs)
            self.assertEqual('ring' in roles, ring)
            self.assertEqual('ground_route' in roles, pgs and ring)
            self.assertEqual(c.ground_checkbox.options['state'] == 'disabled', not (pgs and ring))
            self.assertEqual(c.field_widgets['pgs'].options['state'] == 'disabled', not pgs)
            self.assertEqual(c.field_widgets['ring'].options['state'] == 'disabled', not ring)

    def test_old_v2_import_injects_enabled_defaults(self):
        c = self.controller()
        design = c.collect_design()
        design['parameters'].pop('pgs_enabled'); design['parameters'].pop('ring_enabled')
        loaded = c.validate_imported(dict(schema='tfgen.design.v2', design=design))
        self.assertTrue(loaded['parameters']['pgs_enabled'])
        self.assertTrue(loaded['parameters']['ring_enabled'])

    def test_model_round_trip_preserves_disabled_shields_and_drafts(self):
        c = self.controller()
        c.vars['pgs_enabled'].set(False);c.vars['ring_enabled'].set(False)
        c.vars['pgs_width'].set('0.7')
        c.switch_model('example_tapped_inductor'); c.switch_model('differential_inductor')
        d = c.collect_design()
        self.assertFalse(d['parameters']['pgs_enabled'])
        self.assertFalse(d['parameters']['ring_enabled'])
        self.assertEqual(d['parameters']['pgs_width'], 0.7)
