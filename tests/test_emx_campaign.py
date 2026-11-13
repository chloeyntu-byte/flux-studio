import copy
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import emx_adapter
import emx_campaign


class SettingsTests(unittest.TestCase):
    def test_defaults_and_explicit_options(self):
        s = emx_adapter.resolve_settings({'extra_options': '--verbose=1 --log-level=2'})
        self.assertEqual((s['edge_um'], s['thickness_um'], s['via_merge_um'], s['three_d'], s['parallel']), (.5, .5, .5, False, 2))
        self.assertIsNone(s['memory_mb'])
        s2 = emx_adapter.resolve_settings({'extra_options': "--verbose 1 --unknown-option 'two words'"})
        self.assertEqual(s2['extra_argv'], ['--verbose=1', '--unknown-option=two words'])
        self.assertEqual(s['extra_argv'], ['--verbose=1', '--log-level=2'])

    def test_identity_and_managed_setting_cannot_be_overridden(self):
        for flag in ('--port=P=evil', '--s-file=bad', '--process=bad', '-e0.1', '-t', '--3d=*', '--parallel=50', 'other.gds', '--', '--sweep', '--sweep-stepsize=1e9', '--no-cadence-pins', '-p1'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                emx_adapter.resolve_settings({'extra_options': flag})

    def test_modern_selector_precedence_and_managed_options(self):
        for selector in ('-*', '*', 'M9', 'm5,m6', '-m1'):
            settings = emx_adapter.resolve_settings({'options_text': '--3d=' + selector + ' --verbose=1',
                                                     'three_d': False, 'extra_options': '--legacy'})
            self.assertEqual(settings['three_d_selector'], selector)
            self.assertEqual(settings['extra_argv'], ['--verbose=1'])
        self.assertEqual(emx_adapter.resolve_settings({'three_d': True})['three_d_selector'], '*')
        for flag in ('--3d=* --3d=M9', '--3d=', '--3d', '--port=P0=x', '--include=P0',
                     '--cadence-pins=0', '--no-cadence-pins', '--frequency=2', '--parallel=4',
                     '--port-x', '--include-all', '-xP0', '-CxP0', '-Iall', '-3*', '-ftouchstone',
                     '-sbad', '-ybad', '-l1', '--mode=P0-P1', '--collapse=P0,P1',
                     '--signal-ground-separator=;', '--multi-sweep=1e9-2e9',
                     '--discrete-frequency=3e9', '--definitions-file=other.proc'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                emx_adapter.resolve_settings({'options_text': flag})

    def test_bad_options_and_resources(self):
        for options in ({'edge_um': float('nan')}, {'parallel': 0}, {'memory_mb': 4}, {'timeout_seconds': 0}, {'extra_options': '"bad'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                emx_adapter.resolve_settings(options)


class MetricMappingTests(unittest.TestCase):
    def test_frequency_evidence_reaches_campaign_metrics(self):
        source = {'frequency_hz':1234567890.1234, 'requested_frequency_hz':1234567890.1234,
                  'reported_frequency_hz':1234568000.0, 'frequency_token':'1.234568e+09',
                  'frequency_resolution_hz':1000.0, 'windings':{}}
        row = emx_campaign._metrics({'samples':[source]})[0]
        for key in ('frequency_hz','requested_frequency_hz','reported_frequency_hz','frequency_token','frequency_resolution_hz'):
            self.assertEqual(row[key], source[key])

    def test_inductor_and_transformer_do_not_fabricate_metrics(self):
        summary = {'samples': [
            {'frequency_hz': 1e9, 'windings': {'L': {'inductance_h': 2e-9, 'q': 8}}},
            {'frequency_hz': 2e9, 'windings': {'P': {'inductance_h': 3e-9, 'q': 10},
                                               'S': {'inductance_h': 4e-9, 'q': None}}, 'coupling_k': -.6}]}
        first, second = emx_campaign._metrics(summary)
        self.assertAlmostEqual(first['L'], 2)
        self.assertEqual(first['Q'], 8)
        self.assertNotIn('Lp', first)
        self.assertAlmostEqual(second['Lp'], 3)
        self.assertAlmostEqual(second['Ls'], 4)
        self.assertEqual(second['Qp'], 10)
        self.assertNotIn('Qs', second)
        self.assertEqual(second['k'], -.6)
        self.assertNotIn('L', second)


class ResultNamingTests(unittest.TestCase):
    def test_single_candidate_has_dimensions_and_turn_count(self):
        source = {'fields': [{'name':'inner_diameter','part':'body'}, {'name':'width','part':'body'},
                             {'name':'spacing','part':'body'}, {'name':'turns','part':'body'},
                             {'name':'pgs_width','part':'pgs'}]}
        row = {'id':'c00001_abc123', 'values':{}, 'design':{'model':'differential_inductor',
                'parameters':{'inner_diameter':100, 'width':8, 'spacing':8, 'turns':2, 'pgs_width':.4}}}
        name = emx_campaign._snp_name(source, row, 2)
        for token in ('inner_diameter100', 'width8', 'spacing8', 'turns2', 'c00001_abc123', '.s2p'):
            self.assertIn(token, name)
        self.assertNotIn('pgs_width', name)

    def test_variant_structure_and_selected_axes_are_retained(self):
        source = {'fields':[{'name':'structure','part':'body'}, {'name':'inner_diameter','part':'body'}]}
        row = {'id':'c00002_abc123', 'values':{'inner_diameter':120}, 'design':{'model':'example_tapped_inductor',
                    'structure':'wide', 'parameters':{'inner_diameter':120, 'width':8}}}
        name = emx_campaign._snp_name(source, row, 4)
        self.assertIn('example_tapped_inductor_wide_inner_diameter120', name)
        self.assertNotIn('width8', name)
        self.assertTrue(name.endswith('_c00002_abc123.s4p'))

class CampaignTests(unittest.TestCase):
    def test_objective_frequency_does_not_change_solver_frequencies(self):
        source = dict(fields=[dict(name='w', type='float', min=1, max=8)], design={'model':'fixture', 'parameters':{'w':1}}, identity={'library':'L'}, runtime={})
        frequencies = [1e9, 5e9, 10e9, 20e9, 30e9]
        config = dict(mode='optimize', axes=[dict(name='w', sampling='step', minimum=1, maximum=2, step=1)], emx={'frequencies_hz':frequencies},
                      objectives=[dict(metric='L', frequency_hz=20e9, direction='maximize')])
        campaign = emx_campaign.Campaign(source, config)
        self.assertEqual(campaign.emx['frequencies_hz'], frequencies)
        self.assertEqual(campaign.settings['frequencies_hz'], frequencies)
        config['objectives'][0]['frequency_hz'] = 21e9
        with self.assertRaisesRegex(ValueError, 'Available frequencies'):
            emx_campaign.Campaign(source, config)
        self.assertEqual(config['emx']['frequencies_hz'], frequencies)

    def _candidate_isolation_run(self, mode, reject_first=False, timeout_first=False):
        source = dict(fields=[dict(name='w', type='float', min=1, max=8)],
                      design={'model':'fixture', 'parameters':{'w':1}}, identity={'library':'L'}, runtime={})
        config = dict(mode=mode, axes=[dict(name='w', sampling='step', minimum=1, maximum=8, step=1)],
                      emx={'frequencies_hz':[1e9, 2e9]}, output_dir='/tmp', concurrency=2)
        if mode == 'optimize':
            config.update(optimizer={'population':4, 'generations':2, 'seed':1},
                          objectives=[dict(metric='L', frequency_hz=1e9, direction='maximize')])
        c = emx_campaign.Campaign(source, config)
        calls = []
        failure = {'part':'crossing', 'code':'via_array_fit', 'fields':['w'], 'message':'via cannot fit'}
        def preview(design, source):
            calls.append(design['parameters']['w'])
            if reject_first and len(calls) == 1:
                return {'ok':False, 'errors':[failure]}
            return {'ok':True}
        def generate(design, source):
            if timeout_first and len(calls) == 1:
                raise RuntimeError('Cadence batch timed out. See this task\'s logs.')
            return {'ok':True}
        def prepare_job(generated, **kw):
            return {'source':dict(library='L', cell='C', view='layout', technology='T', workspace='W', cds_lib='D', library_path='P'),
                    'inputs':{}, 'ports':{'signals':['a','b']}, 'effective_settings':{}, 'stream_argv':[],
                    'emx_argv':[], 'outputs':{}, 'run_dir':str(kw['run_dir'])}
        def solve(row, job, started):
            row.update(status='completed', metrics=[{'frequency_hz':1e9, 'L':2, 'Q':4},
                                                    {'frequency_hz':2e9, 'L':3, 'Q':5}],
                       objectives=[-2] if mode == 'optimize' else [], objective_values=[2] if mode == 'optimize' else [],
                       violation=0.0, feasible=True)
            return row
        with tempfile.TemporaryDirectory() as tmp:
            def open_output(): c.output_dir = tmp
            def export(job, *args): Path(job['run_dir']).mkdir()
            with patch.object(c, '_open_output', open_output), patch.object(c, '_solve', solve), \
                 patch('emx_campaign.backend.preview_in_context', side_effect=preview), \
                 patch('emx_campaign.backend.generate_in_context', side_effect=generate), \
                 patch('emx_campaign.backend.prepare_emx_job', side_effect=prepare_job), \
                 patch('emx_campaign.emx_adapter.export_layout', side_effect=export):
                rows = c.run()
            stored = json.loads((Path(tmp) / 'index.json').read_text())
            completion = json.loads((Path(tmp) / 'completion.json').read_text())
        return c, rows, stored, completion, failure

    def test_invalid_first_sweep_continues_with_structured_reason(self):
        c, rows, stored, completion, failure = self._candidate_isolation_run('sweep', reject_first=True)
        self.assertEqual(rows[0]['status'], 'rejected')
        self.assertEqual(stored[0]['errors'], [failure])
        self.assertEqual([r['status'] for r in rows[1:]], ['completed'] * 7)
        self.assertEqual(completion['status'], 'partial')
        self.assertEqual(rows[1]['metrics'][1]['Q'], 5)
        self.assertFalse(any(e['type'] == 'error' for e in c.events.queue))

    def test_mixed_optimizer_continues_next_generation(self):
        c, rows, stored, completion, failure = self._candidate_isolation_run('optimize', reject_first=True)
        self.assertEqual([r['generation'] for r in rows], [0]*4 + [1]*4)
        self.assertEqual(rows[0]['errors'], [failure])
        self.assertEqual(len(c._optimizer.history), 7)
        self.assertEqual(c._optimizer.generation, 2)
        self.assertEqual(completion['status'], 'partial')
        self.assertEqual([e['generation'] for e in c.events.queue if e['type']=='progress'], [0]*5 + [1]*4)

    def test_native_timeout_is_failed_and_later_candidates_continue(self):
        c, rows, stored, completion, _ = self._candidate_isolation_run('sweep', timeout_first=True)
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertIn('timed out', rows[0]['error'])
        self.assertEqual(rows[0]['errors'], [])
        self.assertEqual(rows[1]['status'], 'completed')
        self.assertEqual(completion['status'], 'partial')

    def test_structure_switch_preserves_candidate_specific_geometry_errors(self):
        source = dict(fields=[dict(name='structure', type='enum', options=['compact', 'wide'])],
                      design={'model':'fixture', 'structure':'compact'}, identity={}, runtime={})
        c = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        reasons = {'compact': {'part':'body', 'code':'same_layer_short', 'fields':[], 'message':'nets touch'},
                   'wide': {'part':'crossing', 'code':'via_array_fit', 'fields':['width'], 'message':'via cannot fit'}}
        with patch('emx_campaign.backend.preview_in_context', side_effect=lambda design, source: {'ok':False, 'errors':[reasons[design['structure']]]}):
            for index, structure in enumerate(('compact', 'wide'), 1):
                row, job, _ = c._prepare({'structure':structure}, index, 0)
                self.assertIsNone(job)
                self.assertEqual(row['design']['structure'], structure)
                self.assertEqual(row['errors'], [reasons[structure]])
                self.assertEqual(row['status'], 'rejected')

    def test_serial_export_bounded_solver_and_frozen_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = dict(fields=[dict(name='w', type='float', current=1, min=1, max=4)],
                          design=dict(model='fixture', parameters={'w': 1}), identity={'library': 'L'},
                          runtime={'user': pwd.getpwuid(os.geteuid()).pw_name})
            config = dict(mode='sweep', axes=[dict(name='w', enabled=True, sampling='linear', minimum=1, maximum=4, count=4)],
                          emx={'frequencies_hz':[1e9]}, output_dir=tmp, concurrency=2)
            campaign = emx_campaign.Campaign(source, config)
            source['design']['model'] = 'changed'
            active = [0]; peak = [0]; serial = [0]; violations = []; lock = threading.Lock()
            def open_output():
                campaign.output_dir = tmp
                (Path(tmp) / 'results').mkdir()
            def prepare(values, index, generation):
                with lock:
                    serial[0] += 1
                    if serial[0] != 1: violations.append('concurrent native')
                time.sleep(.02)
                with lock: serial[0] -= 1
                row = dict(id=str(index), index=index, generation=generation, status='rejected', values=values,
                           design=copy.deepcopy(campaign.source['design']), source={}, job_dir=None, log_path=None,
                           snp_path=None, metrics=[], objectives=[], objective_values=[], violation=None,
                           feasible=False, warnings=[], error=None, elapsed_seconds=0)
                return row, {'id': index}, time.monotonic()
            def solve(row, job, started):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(.1)
                with lock: active[0] -= 1
                row.update(status='completed', feasible=True, violation=0.0)
                return row
            with patch.object(campaign, '_open_output', open_output), patch.object(campaign, '_prepare', prepare), patch.object(campaign, '_solve', solve):
                rows = campaign.run()
            self.assertEqual(len(rows), 4)
            self.assertEqual(peak[0], 2)
            self.assertEqual(violations, [])
            self.assertEqual(rows[0]['design']['model'], 'fixture')
            self.assertEqual(json.loads((Path(tmp) / 'completion.json').read_text())['status'], 'completed')
            self.assertEqual([event['type'] for event in list(campaign.events.queue)].count('candidate'), 4)

    def test_warnings_survive_preparation_and_solver(self):
        source = dict(fields=[], design={'model': 'fixture'}, identity={'library': 'L'}, runtime={})
        campaign = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        with tempfile.TemporaryDirectory() as tmp:
            campaign.output_dir = tmp
            preview_warning = {'code': 'width', 'message': 'narrow'}
            generated_warning = {'code': 'space', 'message': 'close'}
            generated = {'ok': True, 'run_dir': tmp, 'warnings': [preview_warning, generated_warning]}
            jobdir = Path(tmp) / 'job'; jobdir.mkdir()
            data = jobdir / 'result.s2p'; data.write_text('data')
            job = {'source': {'library':'L', 'cell':'C', 'view':'layout', 'technology':'T',
                              'workspace':'W', 'cds_lib':'D', 'library_path':'P'},
                   'inputs': {}, 'ports': {'signals':['a','b']}, 'effective_settings': {'edge_um': .5, 'frequencies_hz':[1e9]}, 'run_dir': str(Path(tmp) / 'unused'),
                   'stream_argv':[], 'emx_argv':[], 'frequencies_hz':[1e9], 'outputs':{'touchstone':str(data)},
                   'warnings':['process warning'], 'stages':[]}
            (Path(tmp) / 'results').mkdir()
            with patch('emx_campaign.backend.preview_in_context', return_value={'ok':True, 'warnings':[preview_warning]}), \
                 patch('emx_campaign.backend.generate_in_context', return_value=generated), \
                 patch('emx_campaign.backend.prepare_emx_job', side_effect=lambda *a, **kw: dict(job, run_dir=str(kw['run_dir']))), \
                 patch('emx_campaign.emx_adapter.export_layout', side_effect=lambda *args: Path(args[0]['run_dir']).mkdir()), \
                 patch('emx_campaign.emx_adapter.run_job', side_effect=lambda job, *a: job.update(network_summary={'samples': []})):
                row, job, started = campaign._prepare({}, 1, 0)
                row = campaign._solve(row, job, started)
            replay = json.loads((Path(job['run_dir']) / 'manual-replay.json').read_text())
            self.assertEqual(replay['effective_settings']['frequencies_hz'], [1e9])
            self.assertEqual(replay['ports']['signals'], ['a', 'b'])
            self.assertEqual(row['warnings'][:2], [preview_warning, generated_warning])
            self.assertIn('Cell cleanup preparation failed:', row['warnings'][2])
            self.assertEqual(row['warnings'][3], 'process warning')
            self.assertEqual(row['retention']['state'], 'prepare_failed')
            self.assertEqual(row['status'], 'completed')
            stages = [e['stage'] for e in list(campaign.events.queue) if e['type'] == 'state']
            self.assertEqual(stages, ['preview', 'generate', 'export', 'solver'])

    def test_all_rejected_has_failed_status_counts_and_real_log(self):
        source = dict(fields=[], design={'model': 'fixture'}, identity={}, runtime={})
        campaign = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        with tempfile.TemporaryDirectory() as tmp:
            def open_output(): campaign.output_dir = tmp
            with patch.object(campaign, '_open_output', open_output), \
                 patch('emx_campaign.backend.preview_in_context', return_value={'ok':False, 'errors':['bad geometry'], 'warnings':[{'code':'spacing'}]}):
                rows = campaign.run()
            events = list(campaign.events.queue)
            initial = next(e for e in events if e['type'] == 'progress')
            self.assertEqual((initial['completed'], initial['total'], initial['total_expected']), (0, 1, '1'))
            complete = events[-1]
            self.assertEqual(complete['status'], 'failed')
            self.assertFalse(any(e['type'] == 'error' for e in events))
            self.assertEqual(complete['counts'], {'completed':0, 'failed':0, 'rejected':1, 'cancelled':0})
            self.assertIn('bad geometry', Path(rows[0]['log_path']).read_text())
            receipt = json.loads((Path(tmp) / (rows[0]['id'] + '-candidate.json')).read_text())
            self.assertEqual(receipt['warnings'], [{'code':'spacing'}])

    def test_unwritable_output_failure_does_not_invent_log_path(self):
        source = dict(fields=[], design={}, identity={}, runtime={})
        campaign = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        with patch.object(campaign, '_open_output', side_effect=OSError('permission denied')):
            campaign.run()
        error = list(campaign.events.queue)[0]
        self.assertEqual(error['type'], 'error')
        self.assertIsNone(error['log_path'])
        self.assertIn('Unable to save', error['message'])

    def test_cancellation_after_preview_never_announces_or_starts_generation(self):
        source = dict(fields=[], design={'model':'fixture'}, identity={}, runtime={})
        c = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        def preview(*args):
            c.cancel()
            return {'ok':True}
        with patch('emx_campaign.backend.preview_in_context', side_effect=preview), \
             patch('emx_campaign.backend.generate_in_context') as generate:
            row, job, _ = c._prepare({}, 1, 0)
        self.assertEqual(row['status'], 'cancelled')
        self.assertIsNone(job)
        generate.assert_not_called()
        self.assertEqual([e['stage'] for e in list(c.events.queue) if e['type']=='state'], ['preview'])

    def test_solver_limit_is_shared_between_campaigns(self):
        source = dict(fields=[], design={}, identity={}, runtime={})
        config = dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp')
        campaigns = [emx_campaign.Campaign(source, config) for _ in range(4)]
        active, peak = [0], [0]
        mutex = threading.Lock()
        def solve(row, job, started):
            with mutex:
                active[0] += 1
                peak[0] = max(active[0], peak[0])
            time.sleep(.08)
            with mutex:
                active[0] -= 1
            return row
        threads = []
        for campaign in campaigns:
            campaign._solve = solve
            thread = threading.Thread(target=campaign._solve_guarded, args=({}, {}, time.monotonic()))
            threads.append(thread)
            thread.start()
        for thread in threads: thread.join()
        self.assertEqual(peak[0], 2)

    @unittest.skipUnless(os.geteuid() == 0, 'requires root to verify native ownership')
    def test_output_files_belong_to_native_user_under_restrictive_umask(self):
        try:
            user = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('native test account unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp).chmod(0o700)
            os.chown(tmp, user.pw_uid, user.pw_gid)
            source = dict(fields=[], design={}, identity={'library': 'L'}, runtime={'user': user.pw_name})
            campaign = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir=tmp))
            old = os.umask(0o077)
            try:
                campaign._open_output()
                campaign._save()
            finally:
                os.umask(old)
            directory = Path(campaign.output_dir)
            for path in (directory, directory / 'results', directory / 'campaign.json', directory / 'index.json', directory / 'index.csv'):
                self.assertEqual(path.stat().st_uid, user.pw_uid)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / 'campaign.json').stat().st_mode & 0o777, 0o600)

    def test_goal_preflight_and_frequency_validation(self):
        source = dict(fields=[], design={}, identity={}, runtime={}, definition={'emx_metrics':['L', 'Q']})
        base = dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp')
        objective = {'metric':'L', 'frequency_hz': 1e9, 'direction':'maximize'}
        campaign = emx_campaign.Campaign(source, dict(base, objectives=[objective]))
        self.assertEqual(campaign.emx['frequencies_hz'], [1e9])
        self.assertEqual(campaign.settings['frequencies_hz'], [1e9])
        for bad in (dict(objective, metric='Lp'), dict(objective, scale=0),
                    dict(objective, frequency_hz=float('nan')), dict(objective, direction='target'),
                    dict(objective, direction='bogus')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                emx_campaign.Campaign(source, dict(base, objectives=[bad]))
        for bad in ({'metric':'Q', 'frequency_hz':1e9, 'operator':'between', 'limit':[2,1]},
                    {'metric':'Q', 'frequency_hz':1e9, 'operator':'>=', 'limit':float('inf')}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                emx_campaign.Campaign(source, dict(base, constraints=[bad]))
        # An unannotated custom model is not presumed to be an inductor or transformer.
        del source['definition']['emx_metrics']
        emx_campaign.Campaign(source, dict(base, objectives=[dict(objective, metric='custom')]))

    def test_nested_output_and_rejection_of_unwritable_existing_ancestor(self):
        if os.geteuid() != 0: self.skipTest('requires root and native test user')
        try: user = pwd.getpwnam('nobody')
        except KeyError: self.skipTest('native test account unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            os.chown(tmp, user.pw_uid, user.pw_gid); os.chmod(tmp, 0o700)
            source = dict(fields=[], design={}, identity={}, runtime={'user':user.pw_name})
            out = Path(tmp) / 'new' / 'deep' / 'campaign'
            c = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir=str(out)))
            c._open_output()
            for path in (out.parent.parent, out.parent, out):
                self.assertEqual(path.stat().st_uid, user.pw_uid)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            blocked = Path(tmp) / 'blocked'; blocked.mkdir(); os.chown(str(blocked), 0, 0); blocked.chmod(0o700)
            c2 = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir=str(blocked / 'deep')))
            before = blocked.stat()
            with self.assertRaisesRegex(ValueError, 'not writable'):
                c2._open_output()
            self.assertEqual((blocked.stat().st_uid, blocked.stat().st_mode), (before.st_uid, before.st_mode))
            self.assertFalse((blocked / 'deep').exists())
            link = Path(tmp) / 'link'; link.symlink_to(out, target_is_directory=True)
            c3 = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir=str(link / 'nested')))
            with self.assertRaisesRegex(ValueError, 'symbolic link'): c3._open_output()

    def test_interrupt_signals_workers_before_pool_wait_and_reuse_rejected(self):
        source = dict(fields=[], design={}, identity={}, runtime={})
        c = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        started = threading.Event(); stopped = threading.Event()
        def worker():
            started.set()
            if c.cancel_event.wait(2): stopped.set()
        with self.assertRaises(KeyboardInterrupt):
            with c._worker_pool() as pool:
                pool.submit(worker)
                self.assertTrue(started.wait(1))
                raise KeyboardInterrupt()
        self.assertTrue(stopped.is_set())
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(c, '_open_output', side_effect=lambda: setattr(c, 'output_dir', tmp)):
                c.run()
            with self.assertRaisesRegex(RuntimeError, 'already started'): c.run()
            with self.assertRaisesRegex(RuntimeError, 'already started'): c.start()

    def test_optimizer_preflight_is_before_native_work(self):
        source = dict(fields=[dict(name='w', type='float', current=1, min=1, max=3)], design={}, identity={}, runtime={})
        config = dict(mode='optimize', axes=[dict(name='w', enabled=True, sampling='linear', minimum=1, maximum=3, count=3)],
                      emx={'frequencies_hz':[1e9]}, objectives=[dict(metric='L', frequency_hz=1e9, direction='maximize')], output_dir='/tmp')
        for options in ({'algorithm':'bad'}, {'algorithm':'de','population':2}, {'seed':'1'}, {'generations':0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                emx_campaign.Campaign(source, dict(config, optimizer=options))
        c = emx_campaign.Campaign(source, dict(config, optimizer={'algorithm':'de','population':4}))
        self.assertIn('Fewer than four', c._preflight_warnings[0])

    def test_cancel_does_not_report_completion(self):
        source = dict(fields=[], design={}, identity={}, runtime={})
        campaign = emx_campaign.Campaign(source, dict(mode='sweep', axes=[], emx={'frequencies_hz':[1e9]}, output_dir='/tmp'))
        with tempfile.TemporaryDirectory() as tmp:
            def open_output(): campaign.output_dir = tmp
            campaign.cancel()
            with patch.object(campaign, '_open_output', open_output): campaign.run()
            complete = list(campaign.events.queue)[-1]
            self.assertEqual(complete['status'], 'cancelled')


if __name__ == '__main__': unittest.main()
