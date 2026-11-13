"""Exact native environment restoration without executing Cadence or touching OA."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import runtime_config as runtime

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('flux_native_context', ROOT / 'cadence/plugin_context.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
KEYS = runtime.NATIVE_ISOLATION_KEYS


def markers(states):
    result = {'TFGEN_ORIGINAL_PYTHON_ENV_READY': '1'}
    for key, value in states.items():
        name = 'TFGEN_ORIGINAL_' + key
        result[name + '_SET'] = '0' if value is None else '1'
        result[name] = '' if value is None else value
    return result


class NativeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flux native's environment ")
        self.folder = Path(self.temp.name)
        self.workspace = self.folder / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'cds.lib').write_text('DEFINE ExampleDesign design\n')
        self.binary = self.folder / 'tools/bin/virtuoso'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text('#!/bin/sh\nexit 99\n')
        self.binary.chmod(0o755)
        self.environment = patch.dict(os.environ, {'PATH': str(self.binary.parent)}, clear=True)
        self.environment.start()
        self.patches = [patch.object(runtime, 'discover_source_process', return_value=None),
                        patch.object(runtime.os, 'getuid', return_value=1234),
                        patch.object(runtime.os, 'geteuid', return_value=1234),
                        patch.object(runtime.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='design_user', pw_uid=1234)),
                        patch.object(runtime.pwd, 'getpwnam', return_value=SimpleNamespace(pw_name='design_user', pw_uid=1234))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.environment.stop()
        self.temp.cleanup()

    def source(self, states):
        return dict(uid=1234, pid=4242, executable=str(self.binary),
                    workspace=str(self.workspace), environment=states)

    def discover(self, **kwargs):
        return runtime.discover_runtime(workspace=str(self.workspace), **kwargs)

    def test_each_key_restores_unset_empty_and_exact_nonempty_after_gui_removal(self):
        for key in KEYS:
            for value in (None, '', ' original value : path with spaces '):
                with self.subTest(key=key, value=value):
                    base = dict((item, 'gui-' + item) for item in KEYS)
                    base.update(PATH='/base/path', UNRELATED='unchanged')
                    before = dict(base)
                    captured = dict((item, None) for item in KEYS)
                    captured[key] = value
                    native = runtime.native_environment(base, captured)
                    self.assertEqual(base, before)
                    self.assertEqual(native['UNRELATED'], 'unchanged')
                    for item in KEYS:
                        if item == key and value is not None:
                            self.assertIn(item, native)
                            self.assertEqual(native[item], value)
                        else:
                            self.assertNotIn(item, native)

    def test_complete_original_markers_restore_all_three_states_without_editor(self):
        for value in (None, '', 'original-private-fixture'):
            states = dict((key, value) for key in KEYS)
            gui = dict((key, 'gui-only-fixture') for key in KEYS)
            with self.subTest(value=value), patch.dict(os.environ, dict(gui, **markers(states))):
                snapshot = self.discover()
                for key in KEYS:
                    self.assertIn(key, snapshot['environment'])
                    self.assertEqual(snapshot['environment'][key], value)
                child = runtime.native_environment(os.environ, snapshot['environment'])
                self.assertFalse(any(child.get(key) == 'gui-only-fixture' for key in KEYS))
                self.assertNotIn('original-private-fixture', json.dumps(runtime.public_runtime(snapshot)))

    def test_current_editor_rows_override_source_proc_and_original_markers(self):
        original = dict((key, 'original') for key in KEYS)
        source = self.source(dict((key, 'proc-initial') for key in KEYS))
        for value in (None, '', ' current editor state '):
            current = dict((key, value) for key in KEYS)
            with self.subTest(value=value), patch.dict(os.environ, markers(original)):
                snapshot = self.discover(context={'source_process': source, 'environment': current})
                for key in KEYS:
                    self.assertEqual(snapshot['environment'][key], value)

    def test_legacy_missing_rows_keep_original_states_instead_of_becoming_unset(self):
        original = dict((key, '' if index % 2 else 'original-' + key)
                        for index, key in enumerate(KEYS))
        partial = {'LD_LIBRARY_PATH': '/legacy-editor-loader'}
        with patch.dict(os.environ, markers(original)), patch.object(runtime, '_process', return_value=None):
            snapshot = self.discover(context={'source_process': self.source(partial), 'environment': partial})
        self.assertEqual(snapshot['environment']['LD_LIBRARY_PATH'], '/legacy-editor-loader')
        for key in KEYS[1:]:
            self.assertEqual(snapshot['environment'][key], original[key])

    def test_matching_live_editor_completes_old_snapshot_but_mismatched_pid_does_not(self):
        partial = self.source({'LD_LIBRARY_PATH': '/old-captured-loader'})
        live_states = dict((key, None if index % 3 == 0 else ('' if index % 3 == 1 else 'editor-' + key))
                           for index, key in enumerate(KEYS))
        live = self.source(live_states)
        with patch.object(runtime, '_process', return_value=live):
            captured = runtime._source_environment(partial)
        self.assertEqual(captured['LD_LIBRARY_PATH'], '/old-captured-loader')
        for key in KEYS[1:]:
            self.assertIn(key, captured)
            self.assertEqual(captured[key], live_states[key])
        for field, mismatch in [('uid', 9999), ('executable', '/unrelated/virtuoso'),
                                ('workspace', '/unrelated/workspace')]:
            with self.subTest(field=field), patch.object(runtime, '_process', return_value=dict(live, **{field: mismatch})):
                self.assertEqual(runtime._source_environment(partial), partial['environment'])

    def test_legacy_loader_fallback_cannot_restore_other_gui_injections(self):
        gui = dict((key, 'gui-only') for key in KEYS)
        gui['TFGEN_CAPTURED_LD_LIBRARY_PATH'] = '/legacy-cadence-loader'
        with patch.dict(os.environ, gui):
            snapshot = self.discover()
        child = runtime.native_environment(os.environ, snapshot['environment'])
        self.assertEqual(child['LD_LIBRARY_PATH'], '/legacy-cadence-loader')
        for key in KEYS[1:]:
            self.assertNotIn(key, child)

    def test_invalid_capture_and_incomplete_original_protocol_are_refused_without_values(self):
        for invalid in ('private\x00value', 7, ['private-value']):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaisesRegex(ValueError, '^Invalid captured native environment field: PYTHONHOME\\.$'):
                    runtime.native_environment({}, {'PYTHONHOME': invalid})
        incomplete = markers(dict((key, None) for key in KEYS))
        del incomplete['TFGEN_ORIGINAL_TK_LIBRARY_SET']
        with self.assertRaisesRegex(ValueError, '^Incomplete original native environment state: TK_LIBRARY\\.$'):
            runtime.original_python_environment(incomplete)

    def test_native_unset_also_removes_an_inherited_whitelisted_value(self):
        native = runtime.native_environment({'CDS_LIC_FILE': 'inherited-fixture', 'PATH': '/base'},
                                            {'CDS_LIC_FILE': None, 'PATH': '/editor'})
        self.assertNotIn('CDS_LIC_FILE', native)
        self.assertEqual(native['PATH'], '/editor')

    def test_proc_snapshot_records_absence_empty_and_space_without_other_environment(self):
        for value in (None, '', ' '):
            raw = b'PATH=/editor/path\0UNRELATED_PRIVATE=ignored\0'
            if value is not None:
                raw += b'PYTHONHOME=' + value.encode('utf-8') + b'\0'

            class FakeFolder:
                def __truediv__(self, _pid):
                    return self

                def joinpath(folder, name):
                    if name == 'status':
                        return SimpleNamespace(read_text=lambda: 'Uid:\t1234\t1234\t1234\t1234\n')
                    if name == 'exe':
                        return SimpleNamespace(resolve=lambda **kwargs: self.binary)
                    if name == 'cwd':
                        return SimpleNamespace(resolve=lambda **kwargs: self.workspace)
                    if name == 'cmdline':
                        return SimpleNamespace(read_bytes=lambda: b'virtuoso\0-64\0')
                    if name == 'environ':
                        return SimpleNamespace(read_bytes=lambda: raw)
                    raise AssertionError(name)

            actual_path = runtime.Path
            with self.subTest(value=value), patch.object(runtime, 'Path', side_effect=lambda name: FakeFolder() if name == '/proc' else actual_path(name)):
                captured = runtime._process(4242)['environment']
            self.assertEqual(captured['PYTHONHOME'], value)
            self.assertNotIn('UNRELATED_PRIVATE', captured)
            for key in KEYS:
                self.assertIn(key, captured)
                if key != 'PYTHONHOME':
                    self.assertIsNone(captured[key])


class PluginEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flux context's states ")
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)
        self.workspace = self.folder / 'workspace'
        self.workspace.mkdir()
        self.mapping = self.workspace / 'cds.lib'
        self.mapping.write_text('DEFINE ExampleDesign design\n')
        self.fields = dict(library='ExampleDesign', cell='ExampleCell', view='layout', technology='ExampleTech',
            library_path=str(self.workspace / 'design'), view_path=str(self.workspace / 'design/cell/layout'),
            workspace=str(self.workspace), cds_lib=str(self.mapping))

    def tearDown(self):
        self.temp.cleanup()

    def tsv(self, states):
        rows = [plugin.HEADER]
        for kind, values in [('C', self.fields), ('E', states)]:
            rows.extend('%s\t%s\t%s' % (kind, json.dumps(key), 'nil' if value is None else json.dumps(value))
                        for key, value in values.items())
        return '\n'.join(rows) + '\n'

    def private_tsv(self, states):
        path = self.folder / 'context.tsv'
        path.write_text(self.tsv(states))
        path.chmod(0o600)
        return path

    def test_decode_keeps_explicit_nil_empty_space_and_missing_rows_distinct(self):
        for value in (None, '', ' '):
            context = plugin.decode_context(self.tsv({'PYTHONHOME': value}))
            self.assertIn('PYTHONHOME', context['environment'])
            self.assertEqual(context['environment']['PYTHONHOME'], value)
            self.assertNotIn('TCL_LIBRARY', context['environment'])

    def test_process_completion_does_not_overwrite_current_editor_unset_or_empty(self):
        states = {'LD_LIBRARY_PATH': None, 'PYTHONHOME': '', 'PYTHONPATH': ' '}
        path = self.private_tsv(states)
        proc = dict(pid=1234, uid=os.getuid(), executable='/fixture/virtuoso', workspace=str(self.workspace),
                    environment=dict((key, '/proc-value') for key in KEYS))
        with patch.object(plugin, 'discover_source_process', return_value=proc):
            target = plugin.convert_context(path)
        result = json.loads(target.read_text())
        for key, value in states.items():
            self.assertEqual(result['environment'][key], value)
        self.assertEqual(result['environment']['TK_LIBRARY'], '/proc-value')
        before, info = target.read_bytes(), target.stat()
        with patch.object(plugin, 'discover_source_process', side_effect=AssertionError('replay should not resnapshot')):
            self.assertEqual(plugin.convert_context(path, reuse_existing=True), target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, info.st_mtime_ns)
        with self.assertRaises(FileExistsError):
            with patch.object(plugin, 'discover_source_process', return_value=None):
                plugin.convert_context(path)

    def test_legacy_json_missing_unknown_rows_is_reused_without_rewriting(self):
        path = self.private_tsv({'LD_LIBRARY_PATH': '/legacy-loader'})
        existing = plugin.decode_context(path.read_text())
        target = self.folder / 'context.json'
        target.write_text(json.dumps(existing))
        target.chmod(0o600)
        before = target.read_bytes()
        self.assertEqual(plugin.convert_context(path, reuse_existing=True), target)
        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn('PYTHONHOME', json.loads(target.read_text())['environment'])

    def test_replay_with_a_different_explicit_editor_state_is_rejected(self):
        path = self.private_tsv({'PYTHONHOME': None})
        existing = plugin.decode_context(path.read_text())
        existing['environment']['PYTHONHOME'] = '/different-fixture'
        target = self.folder / 'context.json'
        target.write_text(json.dumps(existing))
        target.chmod(0o600)
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            plugin.convert_context(path, reuse_existing=True)
        self.assertEqual(target.read_bytes(), before)

    def test_explicit_editor_nil_does_not_match_a_legacy_json_missing_that_key(self):
        for key in KEYS:
            with self.subTest(key=key):
                path = self.private_tsv({key: None})
                existing = plugin.decode_context(path.read_text())
                del existing['environment'][key]
                target = self.folder / 'context.json'
                target.write_text(json.dumps(existing))
                target.chmod(0o600)
                before = target.read_bytes()
                with self.assertRaisesRegex(ValueError, 'fresh context'):
                    plugin.convert_context(path, reuse_existing=True)
                self.assertEqual(target.read_bytes(), before)


class FirstCleanProtocolTests(unittest.TestCase):
    def test_a_second_launcher_keeps_first_capture_including_present_empty(self):
        states = dict((key, None if index % 3 == 0 else ('' if index % 3 == 1 else 'original-' + key))
                      for index, key in enumerate(KEYS))
        child_script = ('source "$1"; tfgen_clean_python_environment; '
                        'for key in "${@:2}"; do '
                        'name="TFGEN_ORIGINAL_${key}"; state="${name}_SET"; '
                        'printf "%s\\0%s\\0%s\\0" "$key" "${!state}" "${!name-}"; done')
        script = ('source "$1"; tfgen_clean_python_environment; '
                  'for key in "${@:2}"; do export "$key=gui-second-launch"; done; '
                  'exec /bin/bash -c ' + shlex.quote(child_script) + ' second "$@"')
        environment = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp'}
        environment.update((key, value) for key, value in states.items() if value is not None)
        result = subprocess.run(['/bin/bash', '-c', script, 'first', str(ROOT / 'scripts/select_python.sh')] + list(KEYS),
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        fields = result.stdout.decode('utf-8').split('\x00')
        self.assertEqual(fields[-1], '')
        captured = {fields[i]: (fields[i + 1], fields[i + 2]) for i in range(0, len(fields) - 1, 3)}
        for key, value in states.items():
            self.assertEqual(captured[key], ('0' if value is None else '1', '' if value is None else value))


class NativeChildIntegrationTests(unittest.TestCase):
    def test_oa_runner_subprocess_boundary_receives_restored_states_without_gui_values(self):
        import cadence
        with tempfile.TemporaryDirectory(prefix='flux-inert-native-runner-') as temporary:
            directory = Path(temporary)
            for value in (None, '', 'original-native-fixture'):
                received = []

                def popen(args, **kwargs):
                    received.append(kwargs['env'])
                    return SimpleNamespace(returncode=0, communicate=lambda **ignored: ('TFGEN_PDK_SUCCESS\n', None))

                captured = dict((key, value) for key in KEYS)
                gui = dict((key, 'gui-only-fixture') for key in KEYS)
                with self.subTest(value=value), patch.dict(os.environ, gui), \
                        patch.object(cadence, 'ensure_runtime', return_value={'helper_dirs': []}), \
                        patch.object(cadence, 'RUNTIME_ENV', captured), \
                        patch.object(cadence, 'VIRTUOSO', '/inert/virtuoso'), \
                        patch.object(cadence, 'ENV_FILE', None), \
                        patch.object(cadence.os, 'geteuid', return_value=1234), \
                        patch.object(cadence.subprocess, 'Popen', side_effect=popen):
                    cadence.run_skill('printf("TFGEN_PDK_SUCCESS\\n")\n', directory, directory)
                self.assertEqual(len(received), 1)
                child = received[0]
                self.assertNotIn(None, child.values())
                for key in KEYS:
                    if value is None:
                        self.assertNotIn(key, child)
                    else:
                        self.assertIn(key, child)
                        self.assertEqual(child[key], value)
                self.assertFalse(any(child.get(key) == 'gui-only-fixture' for key in KEYS))


if __name__ == '__main__':
    unittest.main()
