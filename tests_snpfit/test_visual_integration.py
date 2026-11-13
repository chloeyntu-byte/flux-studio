"""Cross-layer presentation/symbol boundaries, with no GUI, fitting or EDA run.

Accepted snapshots below are explicit routing fixtures, not numerical accuracy
evidence. Native process work is mocked at the public build_symbol boundary.
"""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from snpfit import studio_engine as engine, studio_state as state
from snpfit.delivery import deliver
from snpfit.network import Network, write_touchstone
from snpfit.studio_gui import AnalysisPage, App
from snpfit.winding_view import local_q_text


class Value:
    def __init__(self, value=None):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class VisualIntegration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        (self.workspace/'cds.lib').write_text('')
        self.source_path = self.root/'source.s2p'
        frequencies = np.geomspace(1e9, 10e9, 12)
        write_touchstone(Network(frequencies, np.zeros((12, 2, 2), complex), 50.), self.source_path)
        config = dict(state.default_config(), source_path=str(self.source_path),
                      model_id='inductor_dm_gnd', port_order='1,2', ports_confirmed=True,
                      fmin_ghz=1., fmax_ghz=10., l_pct=5., q_pct=20.,
                      workspace=str(self.workspace), library='visual_test', cell='winding')
        self.cfg, self.source, _, _ = engine._prepare(config)
        self.circuit = {'name': 'routing_fixture', 'ports': ['P', 'N'], 'reference': 'REF',
                        'metadata': {'model_id': 'inductor_dm_gnd', 'export_reference': True},
                        'elements': [
                            {'kind': 'res', 'name': 'rw0_0', 'n1': 'P', 'n2': 'rnode', 'value': 2.},
                            {'kind': 'ind', 'name': 'lw0_0', 'n1': 'rnode', 'n2': 'N', 'value': 1e-9},
                            {'kind': 'cap', 'name': 'cg0', 'n1': 'P', 'n2': 'REF', 'value': 1e-14}]}
        self.baseline = {
            'schema': 1, 'engine': engine.ENGINE_VERSION, 'id': 'verified-routing-fixture',
            'stage': 'native', 'status': 'VERIFIED', 'passed': True, 'native_verified': True,
            'circuit': copy.deepcopy(self.circuit), 'source': copy.deepcopy(self.source),
            'request': copy.deepcopy(self.cfg),
            'compatibility_fingerprint': engine.compatibility_fingerprint(self.cfg, self.source),
            'acceptance_fingerprint': engine.acceptance_fingerprint(self.cfg, self.source),
            'metrics': {'joint': {'passed': True}, 'sentinel': 'pre-existing numeric metrics'},
            'curves': {'sentinel': [1., 3., 7.]}, 'warnings': [],
            'native': {'library': 'visual_test', 'cell': 'winding', 'workspace': str(self.workspace),
                       'library_path': str(self.root/'native-library'),
                       'schematic_sha256': 'unchanged-schematic-hash', 'netlist_sha256': 'unchanged-netlist-hash',
                       'schematic_ports': ['P', 'N', 'REF']},
            'visual_plan': engine._visual_plan(self.circuit, self.cfg, self.source)}

    def tearDown(self):
        self.folder.cleanup()

    def test_drawing_settings_do_not_change_fit_or_acceptance_identity(self):
        before = json.dumps(self.baseline, sort_keys=True)
        for settings in ({'q_annotation_ghz': '2'}, {'visual_reference': 'template'},
                         {'q_annotation_ghz': '9', 'visual_reference': 'auto'},
                         {'native_transformer_style': 'coplanar'}, {'native_transformer_style': 'stacked'}):
            cfg = engine.normalize_config({**self.cfg, **settings})
            self.assertEqual(engine.compatibility_fingerprint(cfg, self.source), self.baseline['compatibility_fingerprint'])
            self.assertEqual(engine.acceptance_fingerprint(cfg, self.source), self.baseline['acceptance_fingerprint'])
            engine._check_baseline(self.baseline, cfg, self.source, accepted=True)
        changed = {**self.cfg, 'l_pct': 6.}
        self.assertNotEqual(engine.acceptance_fingerprint(changed, self.source), self.baseline['acceptance_fingerprint'])
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), before)

    def test_native_transformer_style_is_strict_and_drawing_only(self):
        frozen = copy.deepcopy((self.circuit, self.cfg, self.source, self.baseline))
        default = engine._visual_plan(self.circuit, self.cfg, self.source)
        self.assertEqual(default['native_transformer_style'], 'coplanar')
        for style in ('coplanar', 'stacked'):
            config = engine.normalize_config({**self.cfg, 'native_transformer_style': style})
            with self.subTest(style=style):
                plan = engine._visual_plan(self.circuit, config, self.source)
                self.assertEqual(plan['native_transformer_style'], style)
                self.assertEqual(plan['paths'], default['paths'])
                self.assertEqual(plan['placements'], default['placements'])
                self.assertEqual(plan['pins'], default['pins'])
                self.assertEqual(plan['symbol'], default['symbol'])
                self.assertEqual(engine.compatibility_fingerprint(config, self.source),
                                 self.baseline['compatibility_fingerprint'])
                self.assertEqual(engine.acceptance_fingerprint(config, self.source),
                                 self.baseline['acceptance_fingerprint'])
                engine._check_baseline(self.baseline, config, self.source, accepted=True)
        for invalid in ('', 'auto', 'Stacked', 'side_by_side', None, True, 1, [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'coplanar or stacked'):
                engine._visual_plan(self.circuit, {**self.cfg, 'native_transformer_style': invalid}, self.source)
        self.assertEqual((self.circuit, self.cfg, self.source, self.baseline), frozen)

    def test_annotation_frequency_rejects_out_of_band_and_auto_uses_geometric_center(self):
        before = copy.deepcopy(self.circuit)
        for raw in ('0', '.99', '10.01', 'nan', 'inf', 'not a frequency'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                engine._visual_plan(self.circuit, {**self.cfg, 'q_annotation_ghz': raw}, self.source)
        plan = engine._visual_plan(self.circuit, {**self.cfg, 'q_annotation_ghz': ''}, self.source)
        self.assertAlmostEqual(plan['q_frequency_hz'], math.sqrt(10.)*1e9)
        explicit = engine._visual_plan(self.circuit, {**self.cfg, 'q_annotation_ghz': '2'}, self.source)
        self.assertEqual(explicit['q_frequency_hz'], 2e9)
        self.assertEqual(self.circuit, before)

    def test_symbol_routes_only_to_builder_and_preserves_numeric_result(self):
        baseline = copy.deepcopy(self.baseline)
        frozen = json.dumps(baseline, sort_keys=True)
        symbol = {'symbol_created': True, 'symbol_verified': True,
                  'symbol_ports': ['P', 'N', 'REF'], 'schematic_and_netlist_unchanged': True}
        config = {**self.cfg, 'q_annotation_ghz': '2'}
        with patch('snpfit.native.build_symbol', return_value=symbol) as build, \
                patch('snpfit.native.replay_native', side_effect=AssertionError('No symbol replay')), \
                patch('snpfit.studio_engine._assess', side_effect=AssertionError('No reassessment')), \
                patch('snpfit.studio_engine._parameter_fit', side_effect=AssertionError('No optimizer')), \
                patch('snpfit.studio_engine.quick_initialize', side_effect=AssertionError('No initializer')), \
                patch('subprocess.Popen', side_effect=AssertionError('No native process in boundary test')):
            result = engine.run_action('symbol', config, baseline, runtime=self.root/'runs')
        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.args[1], self.circuit)
        self.assertEqual(build.call_args.args[2]['q_frequency_hz'], 2e9)
        self.assertEqual(json.dumps(baseline, sort_keys=True), frozen)
        for key in ('metrics', 'curves', 'circuit', 'source', 'passed', 'native_verified', 'status',
                    'compatibility_fingerprint', 'acceptance_fingerprint'):
            self.assertEqual(result[key], baseline[key], key)
        self.assertEqual(result['stage'], 'symbol')
        self.assertEqual(result['parent_snapshot_id'], baseline['id'])
        self.assertNotEqual(result['id'], baseline['id'])
        self.assertTrue(result['symbol_created'])
        self.assertEqual(result['native']['schematic_sha256'], 'unchanged-schematic-hash')
        self.assertEqual(result['native']['netlist_sha256'], 'unchanged-netlist-hash')
        self.assertEqual(result['native']['symbol'], symbol)
        self.assertEqual(json.loads(Path(result['run_dir'], 'result.json').read_text())['metrics'], baseline['metrics'])

    def test_symbol_refuses_unverified_stale_or_changed_target_before_builder(self):
        cases = []
        not_verified = copy.deepcopy(self.baseline); not_verified['native_verified'] = False
        cases.append((self.cfg, not_verified, 'verify'))
        cases.append(({**self.cfg, 'l_pct': 6}, self.baseline, 'targets changed'))
        for key, value in (('workspace', str(self.root/'other')), ('library', 'another_library'), ('cell', 'another_cell')):
            cases.append(({**self.cfg, key: value}, self.baseline, 'Restore its workspace'))
        for cfg, baseline, message in cases:
            with self.subTest(message=message, cfg=cfg), \
                    patch('snpfit.native.build_symbol') as build, \
                    patch('snpfit.studio_engine._assess', side_effect=AssertionError('No reassessment')), \
                    self.assertRaisesRegex(ValueError, message):
                engine.run_action('symbol', cfg, baseline, runtime=self.root/'runs')
            build.assert_not_called()

    def test_pre_cancelled_symbol_starts_no_builder_and_retains_old_result(self):
        event = threading.Event(); event.set()
        before = json.dumps(self.baseline, sort_keys=True)
        with patch('snpfit.native.build_symbol') as build:
            result = engine.run_action('symbol', self.cfg, self.baseline, runtime=self.root/'runs', cancel=event)
        build.assert_not_called()
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertFalse(result['passed']); self.assertFalse(result['native_verified'])
        self.assertEqual(result['circuit'], self.baseline['circuit'])
        self.assertEqual(result['curves'], self.baseline['curves'])
        self.assertEqual(json.dumps(self.baseline, sort_keys=True), before)

    def test_legacy_delivery_cannot_relabel_a_symbol_snapshot(self):
        with patch('snpfit.native.build_native') as build, patch('snpfit.native.replay_native') as replay:
            with self.assertRaisesRegex(ValueError, 'studio-action native'):
                deliver({'stage': 'symbol', 'passed': True, 'circuit': self.circuit}, '/unused', 'unused', 'unused')
        build.assert_not_called(); replay.assert_not_called()

    def test_q_edit_keeps_numeric_acceptance_current_and_display_formats_status(self):
        page = AnalysisPage.__new__(AnalysisPage)
        page._populating = False; page._revision = 7
        page.app = SimpleNamespace(dirty=False)
        page.vars = {'q_annotation_ghz': Value('2')}
        page.data = {'config': dict(self.cfg, q_annotation_ghz=''),
                     'results': {'current': copy.deepcopy(self.baseline)},
                     'identity_stale': False, 'results_stale': False, 'view': {}}
        page._update_strategy = Mock(); page.refresh_buttons = Mock()
        frozen = copy.deepcopy(page.data['results'])
        AnalysisPage._config_changed(page, 'q_annotation_ghz')
        self.assertFalse(page.data['identity_stale']); self.assertFalse(page.data['results_stale'])
        self.assertEqual(page.data['results'], frozen)
        self.assertEqual(page.data['config']['q_annotation_ghz'], '2')
        self.assertTrue(page.app.dirty)
        self.assertEqual(local_q_text({'q': None, 'status': 'lossless'}), 'Infinity')
        self.assertEqual(local_q_text({'q': None, 'status': 'unassigned'}), 'N/A')
        self.assertEqual(local_q_text({'q': -.25, 'status': 'non_inductive'}), '-0.25')
        self.assertIn('e+', local_q_text({'q': 2.85e7, 'status': 'finite'}))

    def test_ui_symbol_dispatch_transfers_frozen_action_config_and_baseline(self):
        page = SimpleNamespace(id='page', data={'results_stale': False, 'identity_stale': False})
        page._input_error = Mock(return_value=None); page.sync = Mock(return_value=copy.deepcopy(self.cfg))
        page.selected_snapshot = Mock(return_value=copy.deepcopy(self.baseline))
        page._visual_refresh = Mock(return_value=True); page.set_status = Mock(); page.append_log = Mock()
        app = App.__new__(App); app.page = lambda _: page; app.job = None
        app.runtime = self.root/'ui-runs'; app._dispatch = Mock(return_value=True)
        self.assertTrue(App.start_action(app, 'symbol', page.id))
        args = app._dispatch.call_args.args
        self.assertEqual(args[0:2], ('symbol', page.id))
        captured_config, captured_baseline = copy.deepcopy(args[3]), copy.deepcopy(args[4])
        page.sync.return_value['cell'] = 'later_edit'
        page.selected_snapshot.return_value['metrics']['sentinel'] = 'later_edit'
        with patch('snpfit.studio_gui.engine.run_action', return_value={'stage': 'symbol'}) as run:
            self.assertEqual(args[2](threading.Event(), Mock()), {'stage': 'symbol'})
        self.assertEqual(run.call_args.args[0], 'symbol')
        self.assertEqual(run.call_args.args[1], captured_config)
        self.assertEqual(run.call_args.kwargs['baseline'], captured_baseline)

    def test_symbol_completion_updates_selected_aliases_and_queues_symbol_preview(self):
        baseline = copy.deepcopy(self.baseline)
        completed = dict(copy.deepcopy(baseline), id='new-symbol', stage='symbol', symbol_created=True)
        completed['native']['symbol'] = {'symbol_created': True}
        status = Value('')
        page = SimpleNamespace(id='one', data={'title': 'One', 'results': {
            'current': baseline, 'upgraded': copy.deepcopy(baseline), 'before_upgrade': {'id': 'older'}},
            'results_stale': False, 'identity_stale': False},
            views=Mock(), circuits=SimpleNamespace(tabs=Mock()), refresh=Mock(),
            status_var=status, append_log=Mock())
        page.set_status = lambda message, level='info': status.set(message)
        app = App.__new__(App); app.global_status = Value(''); app.dirty = False
        App._apply_result(app, {'action': 'symbol', 'baseline': baseline}, page, completed)
        self.assertEqual(page.data['results']['current']['id'], 'new-symbol')
        self.assertEqual(page.data['results']['upgraded']['id'], 'new-symbol')
        self.assertEqual(page.data['results']['before_upgrade']['id'], 'older')
        page.views.select.assert_called_with(1)
        self.assertEqual(page.data['view']['circuit_panel'], 1)
        page.refresh.assert_called_once()
        self.assertIn('verified schematic', status.get())

    def test_hidden_native_style_survives_known_key_request_import_and_session(self):
        self.assertEqual(state.default_config()['native_transformer_style'], 'coplanar')
        for style in ('coplanar', 'stacked'):
            with self.subTest(style=style):
                page = state.new_page('Imported transformer')
                page['config'].update(self.cfg)
                initial = {'native_transformer_style': style, 'unused_request_key': 'ignored'}
                config = page['config']
                # Match the existing GUI main import without adding a widget.
                config.update({key:value for key,value in initial.items() if key in config})
                self.assertEqual(config['native_transformer_style'], style)
                self.assertNotIn('unused_request_key', config)
                path = self.root/('native-style-'+style+'.json')
                state.save_session(path, [page], page['id'])
                restored = state.load_session(path)['pages'][0]
                self.assertEqual(restored['config']['native_transformer_style'], style)
                plan = engine._visual_plan(self.circuit, restored['config'], self.source)
                self.assertEqual(plan['native_transformer_style'], style)

    def test_session_roundtrip_preserves_visual_fields_and_native_qualification(self):
        page = state.new_page('Visual state')
        page.update(config={**self.cfg, 'visual_reference': 'template', 'q_annotation_ghz': '2'},
                    source=copy.deepcopy(self.source), source_valid=True)
        page['results']['current'] = copy.deepcopy(self.baseline)
        page['view'].update(winding_axis_limits=[[-10., 10., -20., 20.]],
                            symbol_axis_limits=[[-2., 2., -3., 3.]])
        path = self.root/'session.json'
        state.save_session(path, [page], page['id'])
        loaded = state.load_session(path)['pages'][0]
        self.assertEqual(loaded['config']['visual_reference'], 'template')
        self.assertEqual(loaded['config']['q_annotation_ghz'], '2')
        self.assertEqual(loaded['results']['current']['metrics'], self.baseline['metrics'])
        self.assertTrue(loaded['results']['current']['native_verified'])
        self.assertEqual(loaded['view']['symbol_axis_limits'], page['view']['symbol_axis_limits'])


if __name__ == '__main__':
    unittest.main()
