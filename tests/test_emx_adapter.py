"""EMX job/pin contracts without requiring a license or installed solver."""
import copy
import json
import math
import os
import pwd
import subprocess
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import emx_adapter as emx


def record(kind, datatype, data=b''):
    if len(data) % 2:
        data += b'\0'
    return struct.pack('>HBB', len(data) + 4, kind, datatype) + data


def real8(value):
    exponent = 0
    while value and value < 1 / 16:
        value *= 16
        exponent -= 1
    while value >= 1:
        value /= 16
        exponent += 1
    return bytes([64 + exponent]) + int(round(value * (1 << 56))).to_bytes(7, 'big')


def gds(port_contract, top='Fixture', wrong_layer=False, omit_last=False):
    data = record(0, 2, struct.pack('>h', 600))
    data += record(1, 2, b'\0' * 24) + record(2, 6, b'FixtureLib')
    data += record(3, 5, real8(.001) + real8(1e-9))
    data += record(5, 2, b'\0' * 24) + record(6, 6, top.encode('ascii'))
    terminals = port_contract['terminals'][:-1] if omit_last else port_contract['terminals']
    for terminal in terminals:
        for index, region in enumerate(terminal['regions']):
            a, b = region['bbox']
            points = [(a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1]), (a[0], a[1])]
            xy = [round(v * 1000) for point in points for v in point]
            data += record(8, 0) + record(13, 2, struct.pack('>h', 99 if wrong_layer else region.get('gds_layer', 39)))
            data += record(14, 2, struct.pack('>h', region.get('gds_datatype', 60)))
            data += record(16, 3, struct.pack('>' + 'i' * len(xy), *xy))
            direction = emx._DIRECTIONS.get(region.get('access_direction'), 'TBLR')
            prop = '{} {} {} inputOutput'.format(direction, index, terminal['name']).encode('ascii')
            data += record(43, 2, struct.pack('>h', 1)) + record(44, 6, prop) + record(17, 0)
    return data + record(7, 0) + record(4, 0)


def geometry():
    ports = []
    for name, x, kind, endpoint in (('L1', -4, 'signal', 'positive'), ('L2', 4, 'signal', 'negative'),
                                    ('TAP', 0, 'tap', 'center'), ('GND_PGS', -10, 'ground', None),
                                    ('GND_RING', 10, 'ground', None)):
        ports.append(dict(name=name, layer='M9', bbox=[[x - .5, -10], [x + .5, -9]], net='L' if kind != 'ground' else 'GND',
                          kind=kind, endpoint=endpoint, winding='L' if kind != 'ground' else None, access_direction='bottom'))
    return dict(ports=ports, polygons=[dict(part='body', width=1)], vias=[])


class EMXAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="emx handoff's ")
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.library = self.workspace / 'Design'; self.library.mkdir()
        view = self.library / 'Fixture/layout'; view.mkdir(parents=True)
        (view / 'layout.oa').write_bytes(b'owned native source fixture')
        self.mapping = self.workspace / 'cds.lib'; self.mapping.write_text('DEFINE Design ./Design\n')
        self.install = self.root / 'software prefix'; self.install.mkdir()
        self.binary = self.install / 'emx'; self.binary.write_text('#!/bin/sh\nexit 0\n'); self.binary.chmod(0o755)
        self.strmout = self.install / 'strmout'; shutil.copy2(str(self.binary), str(self.strmout))
        self.process = self.install / 'stack.proc'; self.process.write_text('define m9=L39T60\nconductor 1 1e7 m9\n')
        self.layer_map = self.install / 'layer_map.txt'; self.layer_map.write_text('M9 drawing 39 60\n')
        self.config_file = self.install / 'emxconfig.il'
        self.config_file.write_text('EMX_path="."\nEMX_layer_table=\'(("ActualTech" "layer_map.txt"))\n'
                                   'EMX_process_path=\'(("ActualTech" "."))\n'
                                   'EMX_process_name=\'(("ActualTech" "stack.proc"))\n')
        self.runtime = dict(workspace=str(self.workspace), cds_lib=str(self.mapping), virtuoso=str(self.install / 'virtuoso'),
                            user='fixture_user', env_file=None, helper_dirs=[], environment={})
        self.source = dict(library='Design', cell='Fixture', view='layout', technology='ActualTech',
                           workspace=str(self.workspace), cds_lib=str(self.mapping), library_path=str(self.library))
        self.overrides = dict(config_file=str(self.config_file), strmout_executable=str(self.strmout))

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **updates):
        overrides = dict(self.overrides, **updates)
        return emx.discover_config('ActualTech', self.runtime, overrides, environment={})

    def job(self, **updates):
        return emx.prepare_job(self.source, geometry(), self.config(**updates), self.root / 'private job')

    def test_exact_native_technology_and_relative_prefix_configuration(self):
        config = self.config()
        self.assertEqual(config['process_file'], str(self.process))
        self.assertEqual(config['emx_executable'], str(self.binary))
        self.assertEqual(config['layer_map'], str(self.layer_map))
        self.assertEqual(config['process_source'], 'exact attached-technology association')

    def test_unknown_technology_never_uses_other_pdk_or_generic_first_file(self):
        with self.assertRaisesRegex(ValueError, 'No named EMX process association'):
            emx.discover_config('NewTech', self.runtime, self.overrides, environment={})
        self.config_file.write_text('EMX_path="."\nEMX_process_path="."\nEMX_process_name=nil\n')
        with self.assertRaisesRegex(ValueError, 'No exact EMX process association'):
            self.config()

    def test_explicit_invalid_binary_does_not_silently_use_valid_path(self):
        with self.assertRaisesRegex(ValueError, 'executable is unavailable'):
            self.config(emx_executable=str(self.root / 'absent python-like choice'))

    def test_configuration_does_not_evaluate_skill_expressions(self):
        marker = self.root / 'must not exist'
        self.config_file.write_text('EMX_path=system("touch {}")\n'.format(marker))
        with self.assertRaises(ValueError):
            self.config()
        self.assertFalse(marker.exists())

    def test_prefix_relocation_rebinds_config_and_process_without_cache(self):
        new = self.root / 'moved software prefix'; shutil.move(str(self.install), str(new))
        config = emx.discover_config('ActualTech', self.runtime, dict(config_file=str(new / 'emxconfig.il'),
                                      strmout_executable=str(new / 'strmout')), environment={})
        self.assertEqual(config['process_file'], str(new / 'stack.proc'))
        self.assertEqual(config['emx_executable'], str(new / 'emx'))

    def test_adjacent_install_is_discovered_without_manual_overrides(self):
        vendor = self.root / 'vendor with spaces'; install = vendor / 'IC release'; binary = install / 'tools.lnx86/dfII/bin/64bit/virtuoso'
        binary.parent.mkdir(parents=True); binary.write_text('fixture'); binary.chmod(0o755)
        cfg = vendor / 'EMX release/virtuoso_ui/emxinterface/emxskill/emxconfig.il'; cfg.parent.mkdir(parents=True)
        cfg.write_text('EMX_path={}\nEMX_layer_table=\'((\"ActualTech\" {}))\nEMX_process_path=\'((\"ActualTech\" {}))\nEMX_process_name=\'((\"ActualTech\" \"stack.proc\"))\n'.format(json.dumps(str(self.install)),json.dumps(str(self.layer_map)),json.dumps(str(self.install))))
        runtime = dict(self.runtime, virtuoso=str(binary))
        found = emx.discover_config('ActualTech', runtime, {'strmout_executable':str(self.strmout)}, environment={})
        self.assertEqual(found['config_file'], str(cfg))
        self.assertEqual(found['emx_executable'], str(self.binary))
        self.assertIn('adjacent product', found['config_source'])
        self.assertEqual(found['config_sha256'], emx._hash(cfg))
        with self.assertRaisesRegex(ValueError, 'No named EMX process association'):
            emx.discover_config('OtherTechnology', runtime, {'strmout_executable':str(self.strmout)}, environment={})
        other = vendor / 'EMX second/virtuoso_ui/emxinterface/emxskill/emxconfig.il'; other.parent.mkdir(parents=True); other.write_text(cfg.read_text())
        with self.assertRaisesRegex(ValueError, 'Several EMX configurations.*TFGEN_EMX_CONFIG'):
            emx.discover_config('ActualTech', runtime, {'strmout_executable':str(self.strmout)}, environment={})
        with self.assertRaisesRegex(ValueError, 'executable.*unavailable'):
            emx.discover_config('ActualTech', runtime, {'emx_executable':str(self.root/'missing'), 'config_file':str(cfg), 'strmout_executable':str(self.strmout)}, environment={})

    def test_stale_implicit_config_is_reported_without_silent_process_switch(self):
        stale = self.workspace / 'emxconfig.il'
        stale.write_text('EMX_path="/missing stale install"\n')
        with self.assertRaisesRegex(ValueError, 'configuration.*missing stale install'):
            emx.discover_config('ActualTech', self.runtime, {'strmout_executable':str(self.strmout)}, environment={})

    def test_config_bare_program_is_resolved_against_native_path(self):
        cfg = self.root / 'selected config.il'
        cfg.write_text('EMX_program="emx"\nEMX_layer_table=\'((\"ActualTech\" {}))\nEMX_process_path=\'((\"ActualTech\" {}))\nEMX_process_name=\'((\"ActualTech\" \"stack.proc\"))\n'.format(json.dumps(str(self.layer_map)),json.dumps(str(self.install))))
        found = emx.discover_config('ActualTech', self.runtime, {'config_file':str(cfg),'strmout_executable':str(self.strmout)}, environment={'PATH':str(self.install)})
        self.assertEqual(found['emx_executable'], str(self.binary))

    def test_empty_native_path_does_not_fall_back_to_gui_path(self):
        cfg = self.root / 'path-only-config.il'
        cfg.write_text('EMX_program="emx"\n')
        runtime = dict(self.runtime, environment={'PATH': ''})
        with self.assertRaisesRegex(ValueError, 'native PATH program'):
            emx.discover_config('ActualTech', runtime, {'config_file':str(cfg), 'strmout_executable':str(self.strmout)}, environment={'PATH':str(self.install)})

    def test_native_environment_hints_are_child_only_and_limited(self):
        if os.geteuid() != 0: user = pwd.getpwuid(os.geteuid())
        else:
            try: user = pwd.getpwnam('nobody')
            except KeyError: self.skipTest('native user unavailable')
        self.root.chmod(0o755); self.install.chmod(0o755)
        env_file = self.root / 'native env with spaces.sh'
        import shlex
        env_file.write_text('export EMX_path={}\nexport EMX_interface_path={}\nexport FLUX_SECRET_FIXTURE=never_publish\n'.format(shlex.quote(str(self.install)),shlex.quote(str(self.root / 'ui with spaces'))))
        runtime = dict(self.runtime, user=user.pw_name, env_file=str(env_file), helper_dirs=[], environment={})
        before = dict(os.environ)
        hints = emx._native_tool_hints(runtime)
        self.assertEqual(hints['emx_path'], str(self.install))
        self.assertEqual(hints['interface'], str(self.root / 'ui with spaces'))
        self.assertEqual(set(hints), {'emx_path','interface','path'})
        self.assertNotIn('never_publish', repr(hints))
        self.assertEqual(dict(os.environ), before)

    def test_tap_is_signal_and_ground_contacts_are_defined_but_not_driven(self):
        ports = emx.port_contract(geometry())
        self.assertEqual(ports['signals'], ['L1', 'L2', 'TAP'])
        self.assertEqual(ports['grounds'], ['GND_PGS', 'GND_RING'])
        self.assertEqual(len(ports['definitions']), 5)
        self.assertEqual(ports['included'], ['P000', 'P001', 'P002'])
        self.assertFalse(any(row['driven'] for row in ports['definitions'] if row['pin'].startswith('GND')))

    def test_tap_cannot_be_silently_omitted_and_explicit_grounding_is_possible(self):
        with self.assertRaisesRegex(ValueError, 'including TAP'):
            emx.port_contract(geometry(), dict(signal_ports=['L1', 'L2']))
        ports = emx.port_contract(geometry(), dict(ground_ports=['GND_PGS', 'GND_RING', 'TAP']))
        self.assertEqual(ports['signals'], ['L1', 'L2'])

    def test_local_return_remains_distinct_from_fixed_ground_protocol(self):
        returned = {name: 'GND_PGS' for name in ('L1', 'L2', 'TAP')}
        ports = emx.port_contract(geometry(), dict(ground_protocol='local_returns', return_map=returned))
        self.assertEqual(len(ports['definitions']), 3)
        self.assertTrue(all(row['label'].endswith(':GND_PGS') for row in ports['definitions']))
        with self.assertRaisesRegex(ValueError, 'Each local-return signal'):
            emx.port_contract(geometry(), dict(ground_protocol='local_returns'))

    def test_prepare_is_pure_and_freezes_oa_map_proc_and_real_pin_order(self):
        job = self.job(frequencies_hz=[2e9, 1e9])
        self.assertFalse(Path(job['run_dir']).exists())
        self.assertEqual(job['frequencies_hz'], [1e9, 2e9])
        self.assertTrue(any(key.startswith('source_oa_') for key in job['inputs']))
        self.assertIn('--cadence-pins=1', job['emx_argv'])
        self.assertIn('--3d=-*', job['emx_argv'])
        for option in ('-e', '-t', '-v'):
            self.assertEqual(job['emx_argv'][job['emx_argv'].index(option) + 1], '0.5')
        self.assertIn('--parallel=2', job['emx_argv'])
        self.assertIn('--port=P002=TAP', job['emx_argv'])
        self.assertIn('--include=P002', job['emx_argv'])
        self.assertNotIn('--include=P003', job['emx_argv'])
        self.assertTrue(job['outputs']['touchstone'].endswith('response.s3p'))
        self.assertEqual(job['ports']['terminals'][0]['regions'][0]['gds_datatype'], 60)

    def test_manual_inputs_and_options_are_frozen_in_argv_without_running_emx(self):
        manual = self.workspace / 'manual tools'
        manual.mkdir()
        binary = manual / 'emx'; binary.write_text('#!/bin/sh\nexit 99\n'); binary.chmod(0o755)
        process = manual / 'custom.proc'; process.write_text('fixture process')
        for parallel in (1, 2, 4):
            with self.subTest(parallel=parallel), patch('emx_adapter.subprocess.Popen', side_effect=AssertionError('must not execute')):
                config = self.config(emx_executable='./manual tools/emx', process_file='./manual tools/custom.proc',
                                     parallel=parallel, options_text='--3d=M9 --verbose=1')
                job = emx.prepare_job(self.source, geometry(), config, self.root / ('job' + str(parallel)))
                argv = job['emx_argv']
                self.assertEqual(argv[0], str(binary))
                self.assertEqual(argv[3], str(process))
                self.assertIn('--parallel=' + str(parallel), argv)
                self.assertEqual([a for a in argv if a.startswith('--3d=')], ['--3d=M9'])
                self.assertIn('--verbose=1', argv)
        with self.assertRaises(ValueError):
            self.config(process_file='./manual tools/missing.proc')

    def test_new_intermediate_run_parents_are_private_and_assigned_to_native_user(self):
        nested = self.root / 'new native parent/emx runs/case'
        job = emx.prepare_job(self.source, geometry(), self.config(), nested)
        user = SimpleNamespace(pw_name='fixture_user', pw_uid=12345, pw_gid=23456)
        assigned = []
        def assignment(descriptor, uid, gid):
            assigned.append((Path(os.readlink('/proc/self/fd/' + str(descriptor))), uid, gid))
        with patch.object(emx.pwd, 'getpwnam', return_value=user), patch.object(emx.os, 'geteuid', return_value=0), \
                patch.object(emx, '_native_can_access', return_value=True), patch.object(emx.os, 'fchown', side_effect=assignment):
            emx._create_run(job, self.runtime)
        self.assertEqual([row[0] for row in assigned[:4]], [nested.parent.parent, nested.parent, nested, nested / 'cds.lib'])
        self.assertEqual(assigned[4][0], nested / 'job.json.tmp')
        self.assertEqual(assigned[4][1:], (nested.stat().st_uid, nested.stat().st_gid))
        self.assertTrue(all((uid, gid) == (12345, 23456) for _, uid, gid in assigned[:4]))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path, _, _ in assigned[:3]))
        self.assertTrue((nested / 'job.json').is_file())

    def test_native_user_cannot_traverse_existing_private_parent_without_changing_it(self):
        blocked = self.root / 'administrator private'; blocked.mkdir(mode=0o700)
        job = emx.prepare_job(self.source, geometry(), self.config(), blocked / 'native/case')
        user = SimpleNamespace(pw_name='fixture_user', pw_uid=12345, pw_gid=23456)
        original = blocked.stat()
        def accessible(path, user, mode):
            return blocked != path and blocked not in path.parents
        with patch.object(emx.pwd, 'getpwnam', return_value=user), patch.object(emx.os, 'geteuid', return_value=0), \
                patch.object(emx, '_native_can_access', side_effect=accessible), patch.object(emx.os, 'fchown') as chown:
            with self.assertRaisesRegex(ValueError, 'cannot traverse existing EMX parent.*administrator private.*Existing permissions were unchanged'):
                emx._create_run(job, self.runtime)
            chown.assert_not_called()
        self.assertFalse((blocked / 'native').exists())
        self.assertEqual((blocked.stat().st_uid, blocked.stat().st_mode), (original.st_uid, original.st_mode))

    def test_symlink_run_parent_rejected_at_prepare_and_if_introduced_later(self):
        actual = self.root / 'actual'; actual.mkdir()
        link = self.root / 'linked'; link.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'must not traverse a symbolic link'):
            emx.prepare_job(self.source, geometry(), self.config(), link / 'case')
        pending = self.root / 'pending'
        job = emx.prepare_job(self.source, geometry(), self.config(), pending / 'case')
        pending.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'must not traverse a symbolic link'):
            emx._create_run(job, self.runtime)
        self.assertFalse((actual / 'case').exists())

    def test_prepare_rejects_uncreated_source_missing_map_and_changed_technology(self):
        wrong = dict(self.source, cell='NotCreated')
        with self.assertRaisesRegex(ValueError, 'existing bounded OA'):
            emx.prepare_job(wrong, geometry(), self.config(), self.root / 'run')
        with self.assertRaisesRegex(ValueError, 'different attached technologies'):
            emx.prepare_job(dict(self.source, technology='OtherTech'), geometry(), self.config(), self.root / 'run')
        self.layer_map.write_text('M8 drawing 38 40\n')
        with self.assertRaisesRegex(ValueError, 'does not contain pin LPP'):
            self.job()

    def test_native_gds_property_rectangles_directions_and_real_datatypes_match(self):
        job = self.job(); file = self.root / 'pins.gds'
        file.write_bytes(gds(job['ports']) + b'\0' * 2048)
        audit = emx.audit_gds(file, job['ports'], 'Fixture')
        self.assertEqual(audit['native_pin_count'], 5)
        self.assertEqual({(p['layer'], p['datatype']) for p in audit['native_pins']}, {(39, 60)})
        self.assertEqual({p['direction'] for p in audit['native_pins']}, {'B'})

    def test_missing_native_property_or_wrong_layer_direction_or_geometry_is_rejected(self):
        job = self.job(); file = self.root / 'pins.gds'
        for mode in ('missing', 'layer', 'direction', 'bbox', 'truncated'):
            with self.subTest(mode=mode):
                data = gds(job['ports'], wrong_layer=mode == 'layer', omit_last=mode == 'missing')
                if mode == 'direction':
                    data = data.replace(b'B 0 L1 inputOutput', b'T 0 L1 inputOutput')
                if mode == 'bbox':
                    altered = copy.deepcopy(job['ports']); altered['terminals'][0]['regions'][0]['bbox'][0][0] -= 1
                    data = gds(altered)
                if mode == 'truncated':
                    data = data[:-5]
                file.write_bytes(data)
                with self.assertRaises(ValueError):
                    emx.audit_gds(file, job['ports'], 'Fixture')

    def test_repeated_native_regions_are_preserved_not_first_pin_only(self):
        geom = geometry(); extra = dict(geom['ports'][3], bbox=[[-11, 10], [-10, 11]])
        geom['ports'].append(extra)
        config = self.config(); job = emx.prepare_job(self.source, geom, config, self.root / 'run')
        file = self.root / 'pins.gds'; file.write_bytes(gds(job['ports']))
        self.assertEqual(emx.audit_gds(file, job['ports'], 'Fixture')['native_pin_count'], 6)
        self.assertEqual(len(job['ports']['signals']), 3)

    def test_touchstone_two_port_order_units_and_exact_frequency_completeness(self):
        file = self.root / 'response.s2p'
        file.write_text('! native solver fixture\n# GHz S RI R 50\n1 .1 0 .3 0 .2 0 .4 0\n')
        result = emx.read_touchstone(file, 2, [1e9])
        self.assertEqual(result['rows'][0]['s'], [[.1 + 0j, .2 + 0j], [.3 + 0j, .4 + 0j]])
        with self.assertRaisesRegex(ValueError, 'every exact requested frequency'):
            emx.read_touchstone(file, 2, [1e9, 2e9])

    def test_native_frequency_roundtrip_and_printed_precision_matching(self):
        wanted = 1234567890.1234
        job = self.job(frequencies_hz=[wanted])
        self.assertEqual(float(job['emx_argv'][-1]), wanted)
        path = self.root / 'rounded.s2p'
        for unit, token, representation in (('Hz', '1.234568e+09', 'RI'), ('GHz', '1.234568', 'MA')):
            with self.subTest(unit=unit, representation=representation):
                path.write_text('# {} S {} R 50\n{} .1 0 .3 0 .2 0 .4 0\n'.format(unit, representation, token))
                row = emx.read_touchstone(path, 2, [wanted])['rows'][0]
                self.assertEqual(row['frequency_hz'], wanted)
                self.assertEqual(row['requested_frequency_hz'], wanted)
                self.assertEqual(row['reported_frequency_hz'], 1234568000.0)
                self.assertEqual(row['frequency_token'], token)
                self.assertEqual(row['frequency_resolution_hz'], 1000.0)
                raw = emx.read_touchstone(path, 2)['rows'][0]
                self.assertEqual(raw['frequency_hz'], 1234568000.0)
                self.assertNotIn('requested_frequency_hz', raw)

    def test_frequency_print_precision_rejects_ambiguous_wrong_or_unordered(self):
        path = self.root / 'points.s2p'
        def sample(token): return token + ' .1 0 .3 0 .2 0 .4 0\n'
        path.write_text('# Hz S RI R 50\n' + sample('1.234568e+09') * 2)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            emx.read_touchstone(path, 2, [1234567890.1234, 1234567999.0])
        path.write_text('# Hz S RI R 50\n' + sample('1.234569e+09'))
        with self.assertRaisesRegex(ValueError, 'exact requested'):
            emx.read_touchstone(path, 2, [1234567890.1234])
        path.write_text('# Hz S RI R 50\n' + sample('2.000000e+09') + sample('1.000000e+09'))
        with self.assertRaisesRegex(ValueError, 'in order'):
            emx.read_touchstone(path, 2, [1e9, 2e9])
        with self.assertRaisesRegex(ValueError, 'point count'):
            emx.read_touchstone(path, 2, [1e9])

    def test_reported_frequency_metadata_survives_summary(self):
        frequency = 1234567890.1234
        sample = dict(frequency_hz=frequency, requested_frequency_hz=frequency,
                      reported_frequency_hz=1234568000.0, **{'frequency_token': '1.234568e+09'}, frequency_resolution_hz=1000.0,
                      s=[[.1+0j, .2+0j], [.2+0j, .1+0j]])
        network = dict(port_count=2, reference_ohms=50, rows=[sample])
        summary = emx.summarize_network(network, emx.port_contract(dict(ports=geometry()['ports'][:2])))
        for key in ('frequency_hz','requested_frequency_hz','reported_frequency_hz','frequency_token','frequency_resolution_hz'):
            self.assertEqual(summary['samples'][0][key], sample[key])

    def test_touchstone_three_port_row_order_and_wrapped_records(self):
        file = self.root / 'response.s3p'
        file.write_text('# MHz S RI R 50\n1000 1 0 2 0 3 0\n4 0 5 0 6 0\n7 0 8 0 9 0\n')
        result = emx.read_touchstone(file, 3)
        self.assertEqual(result['rows'][0]['s'][1][2], 6 + 0j)
        self.assertEqual(result['rows'][0]['s'][2][0], 7 + 0j)

    def test_header_only_partial_nonfinite_and_duplicate_frequency_are_not_success(self):
        file = self.root / 'response.s2p'
        for text in ('# Hz S RI R 50\n', '# Hz S RI R 50\n1 0 0\n',
                     '# Hz S RI R 50\n1 nan 0 0 0 0 0 0 0\n',
                     '# Hz S RI R 50\n1 0 0 0 0 0 0 0 0\n1 0 0 0 0 0 0 0 0\n'):
            file.write_text(text)
            with self.assertRaises(ValueError):
                emx.read_touchstone(file, 2)

    def test_network_summary_retains_two_terminal_differential_inductance(self):
        frequency, inductance, resistance = 1e9, 2e-9, 3
        zd = resistance + 2j * math.pi * frequency * inductance
        differential = (zd / 2 - 50) / (zd / 2 + 50)
        common = .5
        s11, s12 = (common + differential) / 2, (common - differential) / 2
        ports = emx.port_contract(dict(ports=geometry()['ports'][:2]))
        network = dict(port_count=2, reference_ohms=50, rows=[dict(frequency_hz=frequency, s=[[s11, s12], [s12, s11]])])
        sample = emx.summarize_network(network, ports)['samples'][0]['windings']['L']
        self.assertAlmostEqual(sample['inductance_h'] / inductance, 1, places=10)
        self.assertAlmostEqual(sample['resistance_ohms'], resistance, places=10)

    def test_gui_dependencies_do_not_leak_to_native_child_unset_empty_value(self):
        base = dict(LD_LIBRARY_PATH='gui lib', TCL_LIBRARY='gui Tcl', TK_LIBRARY='gui Tk', PYTHONPATH='gui modules')
        for loader in (None, '', 'native library'):
            with self.subTest(loader=loader), patch.dict(os.environ, base), patch.object(emx.os, 'geteuid', return_value=1000):
                snapshot = dict(self.runtime, environment=dict(LD_LIBRARY_PATH=loader, TCL_LIBRARY=None, TK_LIBRARY='', PYTHONPATH=None))
                _argv, env = emx._native_command(['/bin/true'], snapshot)
                self.assertEqual(env.get('LD_LIBRARY_PATH'), loader)
                self.assertEqual('LD_LIBRARY_PATH' in env, loader is not None)
                self.assertNotIn('TCL_LIBRARY', env)
                self.assertNotIn('PYTHONPATH', env)
                self.assertEqual(env['TK_LIBRARY'], '')
                self.assertEqual(os.environ['LD_LIBRARY_PATH'], 'gui lib')

    def test_frozen_source_change_prevents_new_native_process(self):
        job = self.job()
        (self.library / 'Fixture/layout/layout.oa').write_bytes(b'changed source winding')
        with patch.object(emx.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'Frozen EMX input changed'):
                emx.export_layout(job, self.runtime)
            popen.assert_not_called()

    def test_source_probe_reports_last_marker_or_native_initialization(self):
        for last in (None, 'technology_verified'):
            with self.subTest(last=last):
                job = self.job(); directory = Path(job['run_dir']); directory.mkdir()
                def write_input(path, value, runtime): Path(path).write_text(value)
                def stage(job, name, argv, runtime, timeout, cancel):
                    job['stages'].append({'stage':'source', 'status':'timed_out'})
                    if last: (directory / 'source-progress.log').write_text('script_started\n' + last + '\n')
                    raise RuntimeError('source timed_out')
                with patch.object(emx, '_write_native_input', side_effect=write_input), patch.object(emx, '_run_stage', side_effect=stage):
                    with self.assertRaisesRegex(RuntimeError, last or 'native initialization'):
                        emx._probe_source(job, self.runtime)
                script = (directory / 'probe.il').read_text()
                for marker in ('script_started', 'library_verified', 'technology_verified', 'view_verified', 'view_opened', 'pins_read'):
                    self.assertIn(marker, script)
                self.assertEqual(job['stages'][-1]['last_source_marker'], last or 'not started (native initialization)')
                shutil.rmtree(str(directory))

    def test_owned_child_timeout_has_terminal_status_and_no_relaunch(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
            with self.assertRaisesRegex(RuntimeError, 'timed_out'):
                emx._run_stage(job, 'fixture', [sys.executable, '-c', 'import time; time.sleep(20)'], self.runtime, .1)
        stage = job['stages'][0]
        self.assertEqual(stage['status'], 'timed_out')
        self.assertIsNotNone(stage['exit_code'])
        self.assertEqual(len(job['stages']), 1)
        self.assertEqual(json.loads((Path(job['run_dir']) / 'job.json').read_text())['stages'][0]['pid'], stage['pid'])

    def test_native_inputs_owned_by_native_user_with_restrictive_umask(self):
        user = pwd.getpwnam('nobody') if os.geteuid() == 0 else pwd.getpwuid(os.geteuid())
        if user.pw_uid == 0:
            self.skipTest('requires a non-root native account')
        self.root.chmod(0o755)
        job = self.job()
        payload = self.root / 'job-input.json'
        payload.write_text(json.dumps(job))
        code = """import json, os, sys
from unittest.mock import patch
import emx_adapter as emx
os.umask(0o077)
job = json.load(open(sys.argv[1]))
runtime = {'user': sys.argv[2], 'virtuoso': '/bin/true'}
emx._create_run(job, runtime)
with patch.object(emx, '_run_stage', side_effect=RuntimeError('probe created')):
    try: emx._probe_source(job, runtime)
    except RuntimeError as exc:
        assert str(exc).startswith('probe created')
"""
        subprocess.run([sys.executable, '-c', code, str(payload), user.pw_name], check=True)
        directory = Path(job['run_dir'])
        for name in ('cds.lib', 'probe.il'):
            path = directory / name
            self.assertEqual(path.stat().st_uid, user.pw_uid)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            if os.geteuid() == 0:
                subprocess.run(['runuser', '-u', user.pw_name, '--', '/usr/bin/test', '-r', str(path)], check=True)
        victim = directory / 'victim'; victim.write_text('unchanged')
        link = directory / 'link'; link.symlink_to(victim)
        for path in (link, directory / 'cds.lib'):
            with self.assertRaises(FileExistsError):
                emx._write_native_input(path, 'overwrite', {'user': user.pw_name})
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_owned_orphan_ignoring_term_is_killed_for_timeout_cancel_and_exit(self):
        code = """import os, signal, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open('child-ready', 'w').write(str(os.getpid()))
    while True: time.sleep(1)
while not os.path.exists('child-ready'): time.sleep(.01)
if MODE == 'exit': os._exit(0)
while True: time.sleep(1)
"""
        for mode in ('timed_out', 'cancelled', 'exit'):
            with self.subTest(mode=mode):
                job = self.job()
                directory = Path(job['run_dir'])
                directory.mkdir()
                event = threading.Event()
                timer = threading.Timer(.6, event.set) if mode == 'cancelled' else None
                if timer: timer.start()
                source = 'MODE = {!r}\n'.format(mode) + code
                try:
                    with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
                        with self.assertRaises(RuntimeError):
                            emx._run_stage(job, 'fixture', [sys.executable, '-c', source], self.runtime,
                                           .6 if mode == 'timed_out' else 10, event)
                finally:
                    if timer: timer.cancel()
                record = job['stages'][-1]
                self.assertEqual(record['status'], 'orphaned_children' if mode == 'exit' else mode)
                self.assertTrue(record['cleanup']['kill_sent'])
                self.assertTrue(record['cleanup']['verified_stopped'])
                self.assertEqual(emx._group_members(record['process_group'])[0], [])
                shutil.rmtree(str(directory))

    def test_keyboard_interrupt_cleans_only_owned_group_and_records_terminal_state(self):
        job = self.job(); directory = Path(job['run_dir']); directory.mkdir()
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"
        def interrupt(_observer, final=False):
            if not final:
                time.sleep(.3)
                raise KeyboardInterrupt()
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))), \
             patch.object(emx._ResourceObserver, 'sample', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                emx._run_stage(job, 'fixture', [sys.executable, '-c', code], self.runtime, 10)
        record = job['stages'][-1]
        self.assertEqual(record['status'], 'interrupted')
        self.assertTrue(record['cleanup']['verified_stopped'])
        self.assertEqual(emx._group_members(record['process_group'])[0], [])

    def test_cleanup_failure_cannot_report_success(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        failure = {'process_group': 123, 'verified_stopped': False, 'live': [123], 'zombies': []}
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))), \
             patch.object(emx, '_cleanup_group', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'cleanup_failed'):
                emx._run_stage(job, 'fixture', ['/bin/true'], self.runtime, 10)
        self.assertEqual(job['stages'][-1]['status'], 'cleanup_failed')

    @unittest.skipUnless(os.geteuid() == 0, 'requires root for ownership check')
    def test_resource_log_owned_by_native_directory_user(self):
        try:
            account = pwd.getpwnam('nobody')
        except KeyError:
            self.skipTest('native test account unavailable')
        job = self.job(); directory = Path(job['run_dir']); directory.mkdir()
        os.chown(str(directory), account.pw_uid, account.pw_gid)
        old = os.umask(0o077)
        try:
            with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
                result = emx._run_stage(job, 'fixture', ['/bin/true'], self.runtime, 10)
        finally:
            os.umask(old)
        resource = Path(result['resources']['jsonl'])
        self.assertEqual(resource.stat().st_uid, account.pw_uid)
        self.assertEqual(resource.stat().st_mode & 0o777, 0o600)

    def test_resource_sampling_owned_fork_cpu_memory_and_io(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        code = """import os, time
pid = os.fork()
if pid == 0:
    data = bytearray(12 * 1024 * 1024)
    with open('resource-fixture.bin', 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    until = time.monotonic() + 1.5
    while time.monotonic() < until: pass
    os._exit(0)
os.waitpid(pid, 0)
"""
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
            result = emx._run_stage(job, 'fixture', [sys.executable, '-c', code], self.runtime, 10)
        resources = result['resources']
        samples = [json.loads(line) for line in Path(resources['jsonl']).read_text().splitlines()]
        self.assertGreaterEqual(resources['max_sampled_process_count'], 2)
        self.assertGreater(resources['max_sampled_rss_sum_bytes'], 12 * 1024 * 1024)
        self.assertGreater(resources['max_sampled_cpu_user_seconds'], .1)
        self.assertGreaterEqual(resources['max_sampled_write_bytes'], 12 * 1024 * 1024)
        self.assertGreaterEqual(resources['last_task_disk']['logical_bytes'], 12 * 1024 * 1024)
        self.assertTrue(samples[-1]['final'])
        self.assertEqual(result['status'], 'completed')

    def test_resource_sampling_failure_does_not_mask_solver_failure(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))), \
             patch.object(emx, '_resource_sample', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'failed; exit 7'):
                emx._run_stage(job, 'fixture', [sys.executable, '-c', 'raise SystemExit(7)'], self.runtime, 10)
        self.assertEqual(job['stages'][-1]['status'], 'failed')
        self.assertGreater(job['stages'][-1]['resources']['errors'], 0)

    def test_resource_sample_process_gone_and_fast_exit(self):
        sample = emx._resource_sample(2147483647, str(self.root), True)
        self.assertEqual(sample['process_count'], 0)
        job = self.job(); Path(job['run_dir']).mkdir()
        with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
            result = emx._run_stage(job, 'fixture', ['/bin/true'], self.runtime, 10)
        samples = [json.loads(line) for line in Path(result['resources']['jsonl']).read_text().splitlines()]
        self.assertTrue(samples[-1]['final'])

    def test_cancel_cleans_up_owned_fork_process_group(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        cancelled = threading.Event()
        timer = threading.Timer(.5, cancelled.set); timer.start()
        code = "import os,time; os.fork(); time.sleep(20)"
        try:
            with patch.object(emx, '_native_command', side_effect=lambda argv, _runtime: (argv, dict(os.environ))):
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    emx._run_stage(job, 'fixture', [sys.executable, '-c', code], self.runtime, 10, cancelled)
        finally:
            timer.cancel()
        record = job['stages'][-1]
        self.assertEqual(record['status'], 'cancelled')
        self.assertGreaterEqual(record['resources']['samples'], 1)
        # A killed, unreaped child can briefly remain as a zombie; it consumes no CPU.
        for entry in os.scandir('/proc'):
            if not entry.name.isdigit():
                continue
            try:
                raw = Path(entry.path, 'stat').read_text()
                fields = raw[raw.rfind(')') + 2:].split()
                if int(fields[2]) == record['process_group']:
                    self.assertEqual(fields[0], 'Z')
            except (OSError, ValueError, IndexError):
                pass

    def test_cancel_before_stage_does_not_launch_a_native_process(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        cancelled = threading.Event(); cancelled.set()
        with patch.object(emx.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'cancelled before process launch'):
                emx._run_stage(job, 'fixture', ['/bin/true'], self.runtime, 30, cancelled)
            popen.assert_not_called()
        self.assertEqual(job['stages'][0]['status'], 'cancelled')

    def test_process_launch_failure_has_terminal_stage_status(self):
        job = self.job(); Path(job['run_dir']).mkdir()
        with patch.object(emx.subprocess, 'Popen', side_effect=OSError('controlled missing loader')):
            with self.assertRaisesRegex(RuntimeError, 'could not launch.*controlled missing loader'):
                emx._run_stage(job, 'fixture', ['/bin/true'], self.runtime, 30)
        self.assertEqual(job['stages'][0]['status'], 'launch_failed')
        self.assertIsNone(job['stages'][0]['exit_code'])

    def test_preexisting_run_directory_and_invalid_resource_options_are_rejected(self):
        run = self.root / 'private job'; run.mkdir()
        with self.assertRaisesRegex(ValueError, 'new private task directory'):
            self.job()
        run.rmdir()
        for updates in (dict(frequencies_hz=[]), dict(frequencies_hz=[float('nan')]), dict(parallel=0), dict(timeout_seconds=0)):
            with self.assertRaises(ValueError):
                self.job(**updates)


if __name__ == '__main__':
    unittest.main()
