"""Portable-runtime and exact-library authorization tests, without Cadence or GUI execution."""
import json
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import runtime_config as runtime


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flux-runtime-pure-')
        self.folder = Path(self.temp.name)
        self.workspace = self.folder / 'workspace'
        self.workspace.mkdir()
        self.mapping = self.workspace / 'cds.lib'
        self.mapping.write_text('DEFINE ExampleDesign design\n')
        self.install = self.folder / 'relative-install'
        self.binary = self.install / 'tools.lnx86/dfII/bin/64bit/virtuoso'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text('#!/bin/sh\nexit 97\n')
        self.binary.chmod(0o755)
        (self.install / 'tools/bin').mkdir(parents=True)
        (self.install / 'tools.lnx86/bin').mkdir(parents=True)
        (self.install / 'bin').mkdir()
        self.link = self.install / 'bin/virtuoso'
        self.link.symlink_to('../tools.lnx86/dfII/bin/64bit/virtuoso')
        self.users = {1234: SimpleNamespace(pw_uid=1234, pw_name='design_user'),
                      9999: SimpleNamespace(pw_uid=9999, pw_name='another_user'),
                      0: SimpleNamespace(pw_uid=0, pw_name='root')}
        self.patches = [
            patch.dict(os.environ, {'PATH': str(self.link.parent)}, clear=True),
            patch.object(runtime.os, 'getuid', return_value=1234),
            patch.object(runtime.os, 'geteuid', return_value=1234),
            patch.object(runtime.pwd, 'getpwuid', side_effect=lambda uid: self.users[uid]),
            patch.object(runtime.pwd, 'getpwnam', side_effect=lambda name: next(user for user in self.users.values() if user.pw_name == name)),
            patch.object(runtime, 'discover_source_process', return_value=None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def discover(self, **kwargs):
        return runtime.discover_runtime(workspace=str(self.workspace), **kwargs)

    def test_path_resolution_uses_selected_installation_helpers_and_no_env_file(self):
        discovered = self.discover()
        self.assertEqual(discovered['virtuoso'], str(self.binary.resolve()))
        self.assertEqual(discovered['helper_dirs'], [str(self.install / 'tools/bin'),
                        str(self.install / 'tools.lnx86/bin'), str(self.install / 'tools.lnx86/dfII/bin'), str(self.binary.parent)])
        self.assertIsNone(discovered['env_file'])
        self.assertEqual(discovered['source'], 'launch PATH')
        self.assertEqual(discovered['user'], 'design_user')

    def test_relative_mapping_is_anchored_to_selected_workspace(self):
        custom = self.workspace / "config's folder" / 'selected.lib'
        custom.parent.mkdir()
        custom.write_text('DEFINE PortableDesign ../design\n')
        unrelated = self.folder / 'unrelated launch directory'
        unrelated.mkdir()
        previous = os.getcwd()
        try:
            os.chdir(str(unrelated))
            discovered = self.discover(cds_lib="config's folder/selected.lib")
        finally:
            os.chdir(previous)
        self.assertEqual(discovered['cds_lib'], str(custom))

    def test_workspace_is_found_from_nested_project_without_tfgen_overrides(self):
        project = self.workspace / 'project/src'
        project.mkdir(parents=True)
        with patch.object(runtime.os, 'getcwd', return_value=str(project)):
            discovered = runtime.discover_runtime()
        self.assertEqual(discovered['workspace'], str(self.workspace))
        self.assertEqual(discovered['cds_lib'], str(self.mapping))
        self.assertFalse(any(key.startswith('TFGEN_') for key in os.environ))

    def test_same_user_editor_snapshot_preserves_private_child_environment(self):
        snapshot = dict(uid=1234, pid=42, executable=str(self.binary), workspace=str(self.workspace),
                        environment=dict(PATH=str(self.link.parent), **{'LM_LICENSE_FILE': 'private-license-marker'}, LD_LIBRARY_PATH='private-loader-marker'))
        discovered = self.discover(context={'source_process': snapshot})
        self.assertEqual(discovered['source'], 'running Virtuoso')
        self.assertEqual(discovered['environment']['LM_LICENSE_FILE'], 'private-license-marker')
        public = json.dumps(runtime.public_runtime(discovered))
        self.assertNotIn('private-license-marker', public)
        self.assertNotIn('private-loader-marker', public)
        self.assertNotIn('environment', runtime.public_runtime(discovered))

    def test_other_user_editor_snapshot_is_refused(self):
        snapshot = dict(uid=9999, pid=42, executable=str(self.binary), workspace=str(self.workspace), environment={})
        with self.assertRaisesRegex(ValueError, 'different user'):
            self.discover(context={'source_process': snapshot})

    def test_bad_explicit_binary_does_not_silently_fall_back_to_path(self):
        with patch.dict(os.environ, {'TFGEN_VIRTUOSO': str(self.folder / 'missing-virtuoso')}):
            with self.assertRaisesRegex(ValueError, 'usable Virtuoso'):
                self.discover()

    def test_gui_library_path_never_leaks_to_cadence_and_source_context_still_takes_precedence(self):
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/gui-only-libraries',
                                     'TFGEN_CAPTURED_LD_LIBRARY_PATH': ''}):
            self.assertIsNone(self.discover()['environment']['LD_LIBRARY_PATH'])
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/gui-only-libraries',
                                     'TFGEN_CAPTURED_LD_LIBRARY_PATH': '/original-cadence-libraries'}):
            self.assertEqual(self.discover()['environment']['LD_LIBRARY_PATH'], '/original-cadence-libraries')
            source = dict(uid=1234, pid=42, executable=str(self.binary), workspace=str(self.workspace),
                          environment={'LD_LIBRARY_PATH': '/source-editor-libraries'})
            self.assertEqual(self.discover(context={'source_process': source})['environment']['LD_LIBRARY_PATH'],
                             '/source-editor-libraries')
            self.assertEqual(self.discover(context={'source_process': source,
                'environment': {'LD_LIBRARY_PATH': '/captured-layout-libraries'}})['environment']['LD_LIBRARY_PATH'],
                             '/captured-layout-libraries')

    def test_env_file_is_only_optional_explicit_configuration(self):
        self.assertIsNone(self.discover()['env_file'])
        with patch.dict(os.environ, {'TFGEN_CADENCE_ENV': str(self.folder / 'missing.sh')}):
            with self.assertRaisesRegex(ValueError, 'TFGEN_CADENCE_ENV'):
                self.discover()

    def test_root_without_usable_design_identity_is_refused(self):
        root_owned_project = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0), parent=self.folder)
        with patch.object(runtime.os, 'geteuid', return_value=0), patch.object(runtime, 'ROOT', root_owned_project):
            with self.assertRaisesRegex(ValueError, 'design user'):
                self.discover()
        with patch.object(runtime.os, 'geteuid', return_value=0), patch.dict(os.environ, {'TFGEN_RUN_USER': 'root'}):
            with self.assertRaisesRegex(ValueError, 'design user'):
                self.discover()

    def test_root_can_resolve_exact_source_owner_without_fixed_username(self):
        snapshot = dict(uid=1234, pid=42, executable=str(self.binary), workspace=str(self.workspace), environment={})
        with patch.object(runtime.os, 'geteuid', return_value=0):
            discovered = self.discover(context={'source_process': snapshot})
        self.assertEqual(discovered['user'], 'design_user')

    def test_process_reader_rejects_wrong_uid_before_reading_environment(self):
        reads = []

        class FakeFolder:
            def __truediv__(self, _pid):
                return self

            def joinpath(self, name):
                if name == 'status':
                    return SimpleNamespace(read_text=lambda: 'Uid:\t9999\t9999\t9999\t9999\n')
                if name == 'environ':
                    return SimpleNamespace(read_bytes=lambda: reads.append('environment'))
                raise AssertionError('Wrong-UID process data was accessed: ' + name)

        original_path = runtime.Path
        with patch.object(runtime, 'Path', side_effect=lambda value: FakeFolder() if value == '/proc' else original_path(value)):
            self.assertIsNone(runtime._process(42))
        self.assertEqual(reads, [])

    def test_process_reader_accepts_same_uid_editor_and_filters_child_env(self):
        class FakeFolder:
            def __truediv__(self, _pid):
                return self

            def joinpath(folder, name):
                if name == 'status':
                    return SimpleNamespace(read_text=lambda: 'Uid:\t1234\t1234\t1234\t1234\n')
                if name == 'exe':
                    return SimpleNamespace(resolve=lambda **_kwargs: self.binary)
                if name == 'cwd':
                    return SimpleNamespace(resolve=lambda **_kwargs: self.workspace)
                if name == 'cmdline':
                    return SimpleNamespace(read_bytes=lambda: b'virtuoso\0-64\0')
                if name == 'environ':
                    return SimpleNamespace(read_bytes=lambda: b'PATH=/test/path\0CDS_LIC_FILE' b'=private\0LD_PRELOAD=ignored\0UNRELATED=ignored\0')
                raise AssertionError(name)

        original_path = runtime.Path
        with patch.object(runtime, 'Path', side_effect=lambda value: FakeFolder() if value == '/proc' else original_path(value)):
            process = runtime._process(42)
        self.assertEqual(process['uid'], 1234)
        self.assertEqual(process['environment'], dict(PATH='/test/path', **{'CDS_LIC_FILE': 'private'},
            LD_LIBRARY_PATH=None, LD_PRELOAD='ignored', PYTHONHOME=None, PYTHONPATH=None,
            TCL_LIBRARY=None, TK_LIBRARY=None, QT_PLUGIN_PATH=None))


class ExactLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flux-library-pure-')
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.library = self.workspace / 'design'
        self.library.mkdir()
        self.mapping = self.workspace / 'cds.lib'
        self.mapping.write_text('DEFINE PortableDesign ' + str(self.library) + '\n')
        self.technology = self.root / 'technology/RealTech'
        self.technology.mkdir(parents=True)
        self.record = dict(library='PortableDesign', library_path=str(self.library), technology='RealTech',
                           technology_path=str(self.technology), cds_lib=str(self.mapping), workspace=str(self.workspace))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def permit(self, path=None, record=None, context=None):
        return runtime.permitted_library(str(path or self.library), str(self.workspace), record or self.record, context)

    def test_exact_probed_design_library_in_tmp_is_permitted(self):
        self.assertEqual(self.permit(), self.library)
        self.assertEqual(self.library.parent, self.workspace)

    def test_unprobed_sibling_library_and_stale_context_binding_are_refused(self):
        sibling = self.workspace / 'unprobed'
        sibling.mkdir()
        with self.assertRaisesRegex(ValueError, 'path changed'):
            self.permit(path=sibling)
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.permit(context={'library_path': str(sibling)})

    def test_named_technology_library_is_refused(self):
        record = dict(self.record, library='RealTech')
        with self.assertRaisesRegex(ValueError, 'Technology libraries'):
            self.permit(record=record)

    def test_technology_alias_and_symlink_into_technology_are_refused(self):
        alias = self.workspace / 'aliased-pdk'
        alias.symlink_to(self.technology)
        record = dict(self.record, library='NotNamedLikeATech', library_path=str(alias))
        with self.assertRaisesRegex(ValueError, 'technology library'):
            self.permit(path=alias, record=record)

    def test_foundry_directory_alias_is_refused(self):
        foundry = self.root / 'pdks/not_named_tech'
        foundry.mkdir(parents=True)
        alias = self.workspace / 'design_alias'
        alias.symlink_to(foundry)
        record = dict(self.record, library_path=str(alias))
        with self.assertRaisesRegex(ValueError, 'Foundry installation'):
            self.permit(path=alias, record=record)

    def test_selected_cadence_installation_library_is_refused(self):
        install = self.root / 'cadence_install'
        binary = install / 'tools.lnx86/dfII/bin/64bit/virtuoso'
        binary.parent.mkdir(parents=True)
        target = install / 'share/cdssetup/vendor_library'
        target.mkdir(parents=True)
        record = dict(self.record, library_path=str(target))
        with patch.object(runtime, 'LAST_RUNTIME', {'virtuoso': str(binary)}):
            with self.assertRaisesRegex(ValueError, 'Cadence installation'):
                self.permit(path=target, record=record)

    def test_missing_workspace_mapping_is_refused(self):
        self.mapping.unlink()
        with self.assertRaisesRegex(ValueError, 'workspace library mapping'):
            self.permit()

    def test_optional_explicit_design_root_bounds_resolved_target(self):
        with patch.dict(os.environ, {'TFGEN_DESIGN_ROOT': str(self.workspace)}):
            self.assertEqual(self.permit(), self.library)
        with patch.dict(os.environ, {'TFGEN_DESIGN_ROOT': str(self.technology)}):
            with self.assertRaisesRegex(ValueError, 'TFGEN_DESIGN_ROOT'):
                self.permit()
        with patch.dict(os.environ, {'TFGEN_DESIGN_ROOT': '/'}):
            with self.assertRaisesRegex(ValueError, 'TFGEN_DESIGN_ROOT'):
                self.permit()

    def test_symlink_cannot_escape_optional_explicit_design_root(self):
        outside = self.root / 'outside_design'
        outside.mkdir()
        alias = self.workspace / 'design_link'
        alias.symlink_to(outside)
        record = dict(self.record, library_path=str(alias))
        with patch.dict(os.environ, {'TFGEN_DESIGN_ROOT': str(self.workspace)}):
            with self.assertRaisesRegex(ValueError, 'TFGEN_DESIGN_ROOT'):
                self.permit(path=alias, record=record)


class StartupFeedbackTests(unittest.TestCase):
    """Shell contracts use inert interpreter/app stubs; native Tk is checked separately."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flux startup's tests ")
        self.folder = Path(self.temp.name)
        self.project = self.folder / "moved project's directory"
        self.project.mkdir()
        self.original = Path(__file__).resolve().parents[1]
        for relative in ('launch.sh', 'cadence/launch_from_layout.sh',
                         'scripts/startup.sh', 'scripts/select_python.sh', 'scripts/python_runtime.py'):
            destination = self.project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.original / relative, destination)
        self.private = self.folder / "source's private directory"
        self.private.mkdir(mode=0o700)
        self.tsv = self.private / 'context.tsv'
        self.tsv.write_text('inert shell fixture; no Cadence or OA access\n')
        self.tsv.chmod(0o600)
        self.app_report = self.folder / 'app-report.json'
        self.probe_report = self.folder / 'probe-report.jsonl'
        self.shell_probe_report = self.folder / 'shell-probe-fields.txt'
        self.python = self.folder / "interpreter's directory" / 'python'
        self.python.parent.mkdir()
        # Simulate Python probes, then use the test interpreter for the inert
        # app/converter. This exercises supervision without opening GUI windows.
        probe = ('import json,os,sys; from pathlib import Path; '
                 'keys=["LD_LIBRARY_PATH","LD_PRELOAD","PYTHONHOME","PYTHONPATH",'
                 '"TCL_LIBRARY","TK_LIBRARY","QT_PLUGIN_PATH"]; '
                 'p=Path(os.environ["TFGEN_TEST_PROBE_REPORT"]); '
                 'p.open("a").write(json.dumps({"argv":sys.argv[1:],'
                 '"environment":{k:os.environ.get(k) for k in keys}})+"\\n")')
        self.python.write_text('#!/bin/bash\nset -eu\n'
            'if [[ "${1-}" == -c || "${1-}" == - ]]; then\n'
            '  probe_fields=absent\n'
            '  for probe_key in LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH TCL_LIBRARY TK_LIBRARY QT_PLUGIN_PATH; do\n'
            '    if [[ -v "$probe_key" ]]; then probe_fields=present; fi\n'
            '  done\n'
            '  printf "%s\\n" "$probe_fields" >> "$TFGEN_TEST_SHELL_PROBE_REPORT"\n'
            '  ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(probe) + ' "$@"\n'
            '  if [[ -n "${TFGEN_TEST_PROBE_FAIL-}" ]]; then\n'
            '    printf "%s\\n" "Fixture interpreter error: dynamic-library marker" >&2\n'
            '    exit "$TFGEN_TEST_PROBE_FAIL"\n'
            '  fi\n  exit 0\nfi\n'
            'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
        self.python.chmod(0o755)
        (self.project / 'app.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\n'
            'keys=["LD_LIBRARY_PATH","LD_PRELOAD","PYTHONHOME","PYTHONPATH",'
            '"TCL_LIBRARY","TK_LIBRARY","QT_PLUGIN_PATH"]\n'
            'Path(os.environ["TFGEN_TEST_APP_REPORT"]).write_text(json.dumps('
            '{"argv":sys.argv[1:],"cwd":os.getcwd(),"caller":os.environ.get("TFGEN_LAUNCH_CWD"),'
            '"environment":{k:os.environ.get(k) for k in keys}}))\n'
            'sys.exit(int(os.environ.get("TFGEN_TEST_APP_EXIT","0")))\n')
        (self.project / 'cadence/plugin_context.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            'p=Path(sys.argv[-1]).parent/"context.json"\n'
            'if not p.exists():\n p.write_text("{}\\n"); p.chmod(0o600)\n'
            'print(p)\n')
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith('TFGEN_') and key not in
                            ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH',
                             'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')}
        self.environment.update(TFGEN_PYTHON=str(self.python), TMPDIR=str(self.folder),
            TFGEN_TEST_APP_REPORT=str(self.app_report), TFGEN_TEST_PROBE_REPORT=str(self.probe_report),
            TFGEN_TEST_SHELL_PROBE_REPORT=str(self.shell_probe_report))

    def tearDown(self):
        self.temp.cleanup()

    def launch(self, menu=False, overrides=None):
        env = dict(self.environment, **(overrides or {}))
        command = ['/bin/bash', str(self.project / ('cadence/launch_from_layout.sh' if menu else 'launch.sh'))]
        if menu:
            command.append(str(self.tsv))
        return subprocess.run(command, cwd=self.folder, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)

    def log_for(self, result):
        matches = re.findall(r'^Flux launch log: (.+)$', result.stderr, re.MULTILINE)
        self.assertTrue(matches, result.stderr)
        path = Path(matches[0])
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
        return path

    def logged_python_paths(self, log):
        # Bash quotes diagnostic paths with %q so spaces/apostrophes stay
        # unambiguous. Decode inert text rather than demand unescaped output.
        return {token[5:] for line in log.splitlines()
                if line.startswith('Flux launch stage=python-') and ' exit=' in line
                for token in shlex.split(line) if token.startswith('path=')}

    def test_invalid_explicit_python_is_logged_before_conversion_and_has_no_fallback(self):
        missing = str(self.folder / "missing interpreter's path")
        for menu in (False, True):
            with self.subTest(menu=menu):
                result = self.launch(menu, {'TFGEN_PYTHON': missing})
                self.assertNotEqual(result.returncode, 0)
                log = self.log_for(result)
                self.assertIn('stage=python_probe', result.stderr)
                self.assertIn('TFGEN_PYTHON', log.read_text())
                self.assertIn(missing, self.logged_python_paths(log.read_text()))
                self.assertFalse(self.app_report.exists())
                self.assertFalse((self.private / 'context.json').exists())

    def test_real_probe_error_text_and_exit_status_survive_supervisor_feedback(self):
        result = self.launch(overrides={'TFGEN_TEST_PROBE_FAIL': '42'})
        self.assertNotEqual(result.returncode, 0)
        log = self.log_for(result).read_text()
        self.assertIn('dynamic-library marker', log)
        self.assertIn('42', log)
        self.assertIn(str(self.python), self.logged_python_paths(log))
        self.assertIn('stage=python_probe', result.stderr)
        self.assertFalse(self.app_report.exists())

    def test_config_failure_is_not_suppressed_by_a_callers_conditional(self):
        (self.project / 'flux_config.sh').write_text('false\nexport TFGEN_PYTHON=/partial-success\n')
        command = ['bash', '-c', 'source "$1"; if tfgen_load_config "$2"; then '
                   'printf ACCEPTED; exit 0; else printf REJECTED; exit 79; fi',
                   'conditional-config', str(self.project / 'scripts/select_python.sh'), str(self.project)]
        result = subprocess.run(command, cwd=self.folder, env=self.environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertEqual(result.stdout, 'REJECTED')

    def test_config_syntax_crlf_and_invalid_export_fail_before_partial_values_are_applied(self):
        for data in (b'if then\n', b'export TFGEN_PYTHON=/wrong\r\n',
                     b'export 9INVALID=x\nexport TFGEN_PYTHON=/partial-success\n'):
            with self.subTest(data=data):
                (self.project / 'flux_config.sh').write_bytes(data)
                result = self.launch()
                self.assertNotEqual(result.returncode, 0)
                log = self.log_for(result).read_text()
                self.assertIn('stage=config_load', result.stderr)
                self.assertIn('flux_config.sh', log)
                self.assertFalse(self.app_report.exists())
                self.assertFalse(self.probe_report.exists())

    def test_probe_and_inert_app_share_the_clean_environment_and_paths_keep_apostrophes(self):
        stale = {key: '/stale/foreign-toolchain' for key in ('LD_LIBRARY_PATH', 'PYTHONHOME',
                 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')}
        result = self.launch(True, stale)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.log_for(result)
        app = json.loads(self.app_report.read_text())
        probes = [json.loads(line) for line in self.probe_report.read_text().splitlines()]
        self.assertTrue(probes)
        self.assertEqual(app['argv'], ['--context', str(self.private / 'context.json')])
        self.assertEqual(app['cwd'], str(self.project))
        self.assertEqual(app['caller'], str(self.folder))
        self.assertTrue(all(value is None for value in app['environment'].values()), app)
        self.assertTrue(all(probe['environment'] == app['environment'] for probe in probes))
        self.assertTrue(all(line == 'absent' for line in self.shell_probe_report.read_text().splitlines()),
                        'Normal startup must clean before its first Python probe')

    def test_inherited_python_overrides_local_config_and_explicit_tcl_paths_are_anchored(self):
        for name in ('tcl', 'tk', 'ld1', 'ld2', 'pythonpath'):
            (self.project / "toolkit's tree" / name).mkdir(parents=True, exist_ok=True)
        (self.project / "python's home").symlink_to(sys.prefix, target_is_directory=True)
        (self.project / 'flux_config.sh').write_text(
            'export TFGEN_PYTHON=/missing/configured-python\n'
            'export TFGEN_TCL_LIBRARY="toolkit\'s tree/tcl"\n'
            'export TFGEN_TK_LIBRARY="toolkit\'s tree/tk"\n'
            'export TFGEN_PYTHON_LD_LIBRARY_PATH="toolkit\'s tree/ld1:toolkit\'s tree/ld2"\n'
            'export TFGEN_PYTHONHOME="python\'s home"\n'
            'export TFGEN_PYTHONPATH="toolkit\'s tree/pythonpath"\n')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        app = json.loads(self.app_report.read_text())
        self.assertEqual(app['environment']['TCL_LIBRARY'], str(self.project / "toolkit's tree/tcl"))
        self.assertEqual(app['environment']['TK_LIBRARY'], str(self.project / "toolkit's tree/tk"))
        self.assertEqual(app['environment']['LD_LIBRARY_PATH'], ':'.join(str(self.project / "toolkit's tree" / name) for name in ('ld1', 'ld2')))
        self.assertEqual(app['environment']['PYTHONHOME'], str(self.project / "python's home"))
        self.assertEqual(app['environment']['PYTHONPATH'], str(self.project / "toolkit's tree/pythonpath"))
        log = self.log_for(result).read_text()
        self.assertNotIn('/missing/configured-python', log)
        self.assertIn('inherited', log)

    def test_replayed_context_preserves_prior_log_and_json_and_reports_its_new_log(self):
        first = self.launch(True)
        self.assertEqual(first.returncode, 0, first.stderr)
        log1 = self.log_for(first)
        before = log1.read_bytes()
        converted = self.private / 'context.json'
        original_json = converted.read_bytes()
        second = self.launch(True)
        self.assertEqual(second.returncode, 0, second.stderr)
        log2 = self.log_for(second)
        self.assertNotEqual(log1, log2)
        self.assertEqual(log1.read_bytes(), before)
        self.assertEqual(converted.read_bytes(), original_json)
        self.assertIn('Flux UI exited successfully.', log2.read_text())

    def test_unsafe_existing_log_is_rejected_without_changing_the_victim(self):
        victim = self.folder / 'victim.txt'
        victim.write_bytes(b'preserve this evidence')
        victim.chmod(0o600)
        log = self.private / 'launch.log'
        for kind in ('symlink', 'hardlink', 'public'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    log.symlink_to(victim)
                elif kind == 'hardlink':
                    os.link(victim, log)
                else:
                    log.write_text('public old evidence'); log.chmod(0o644)
                result = self.launch(True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('safe private regular file', result.stderr)
                self.assertEqual(victim.read_bytes(), b'preserve this evidence')
                self.assertFalse(self.app_report.exists())
                log.unlink()

    def test_public_context_is_rejected_and_app_failure_reaches_the_log_and_ipc_channel(self):
        self.private.chmod(0o755)
        result = self.launch(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('context directory must be private', result.stderr)
        self.assertFalse((self.private / 'launch.log').exists())
        self.assertFalse(self.app_report.exists())
        self.private.chmod(0o700)
        self.tsv.chmod(0o644)
        result = self.launch(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stage=context_permissions', result.stderr)
        self.log_for(result)
        self.assertFalse(self.app_report.exists())
        self.tsv.chmod(0o600)
        result = self.launch(True, {'TFGEN_TEST_APP_EXIT': '37'})
        self.assertEqual(result.returncode, 37)
        self.assertIn('stage=ui_run exit=37', result.stderr)
        self.assertIn('stage=ui_run exit=37', self.log_for(result).read_text())

    def test_completion_protocol_reports_supervisor_exit_and_isolates_child_output(self):
        app = self.project / 'app.py'
        source = app.read_text().replace('sys.exit(int(',
            'print("Flux launch result: exit=255",flush=True)\nsys.exit(int(')
        app.write_text('import time\ntime.sleep(float(os.environ.get("TFGEN_TEST_APP_DELAY","0")))\n'.replace(
            'import time', 'import os,time') +
            'for descriptor in (3,4):\n try: os.fstat(descriptor)\n except OSError: pass\n else: raise AssertionError("Supervisor descriptor leaked to app")\n' + source)
        for menu in (False, True):
            for status in (0, 37):
                with self.subTest(menu=menu, status=status):
                    result = self.launch(menu, {'TFGEN_TEST_APP_EXIT': str(status)})
                    self.assertEqual(result.returncode, status)
                    lines = re.findall(r'^Flux launch result: exit=(\d+)$', result.stderr, re.MULTILINE)
                    self.assertEqual(lines, [str(status)])
                    self.assertIn('Flux launch result: exit=255', self.log_for(result).read_text())
        failure = self.launch(True, {'TFGEN_TEST_PROBE_FAIL': '42'})
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(re.findall(r'^Flux launch result: exit=(\d+)$', failure.stderr, re.MULTILINE),
                         [str(failure.returncode)])
        self.assertIn('exit=42', self.log_for(failure).read_text())
        # Cadence IPC observes stdout EOF. Keep that channel open until the
        # supervising process ends even while app stdout goes to its log.
        import select
        environment = dict(self.environment, TFGEN_TEST_APP_DELAY='0.4')
        with subprocess.Popen(['/bin/bash', str(self.project / 'launch.sh')],
                cwd=self.folder, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            readable, _, _ = select.select([process.stdout], [], [], 0.15)
            self.assertFalse(readable, 'IPC stdout closed before the app completed')
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(stdout, '')
            self.assertIn('Flux launch result: exit=0', stderr)

    def test_diagnose_checks_startup_and_tk_without_invoking_the_application(self):
        (self.project / 'flux_doctor.py').write_text(
            "import sys\nassert sys.argv[1:] == ['--display']\n"
            "print('WARN [Video] Optional missing media tool')\n")
        inherited = {key: '/stale/foreign-toolchain' for key in ('LD_LIBRARY_PATH', 'PYTHONHOME',
                     'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')}
        environment = dict(self.environment, **inherited)
        result = subprocess.run(['/bin/bash', str(self.project / 'launch.sh'), '--diagnose'],
            cwd=self.folder, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Flux startup diagnostics passed.', result.stderr)
        log = self.log_for(result).read_text()
        self.assertIn('Flux startup diagnostics passed.', log)
        self.assertIn('WARN [Video] Optional missing media tool', result.stderr)
        self.assertIn('WARN [Video] Optional missing media tool', log)
        self.assertIn('original inherited environment (diagnostic only)', log)
        self.assertIn('only LD_LIBRARY_PATH cleared (diagnostic only)', log)
        fields = self.shell_probe_report.read_text().splitlines()
        self.assertEqual(fields[0], 'absent', 'Selection must probe the isolated environment first')
        self.assertGreaterEqual(fields.count('present'), 2, 'Raw and LD-only diagnostic comparisons must preserve other original fields')
        self.assertFalse(self.app_report.exists())
        self.assertNotIn('Flux UI starting:', log)
        self.assertTrue(self.probe_report.exists())

    def test_private_runtime_records_require_nul_termination_and_whole_tcl_tk_paths(self):
        tcl = self.folder / "Tcl data's directory : 8.6"
        tk = self.folder / "Tk data's directory : 8.6"
        ld1, ld2 = self.folder / 'library one', self.folder / "library's two"
        for path in (tcl, tk, ld1, ld2):
            path.mkdir()
        report = self.folder / 'runtime-overlay.json'
        record = self.private / 'runtime.env'
        base = ['TFGEN_RUNTIME_SCHEMA=tfgen.python.runtime.v1',
                'TFGEN_RUNTIME_INTERPRETER=' + str(self.python)]
        values = dict(LD_LIBRARY_PATH=str(ld1) + ':' + str(ld2),
                      TCL_LIBRARY=str(tcl), TK_LIBRARY=str(tk))
        python_report = ('import json,os,sys; from pathlib import Path; '
                         'Path(sys.argv[1]).write_text(json.dumps({k:os.environ.get(k) '
                         'for k in ("LD_LIBRARY_PATH","TCL_LIBRARY","TK_LIBRARY")}))')
        command = ['/bin/bash', '-c',
            'source "$1"; if tfgen_apply_runtime_environment "$2" "$3"; then '
            'printf ACCEPTED; else printf REJECTED; fi; "$4" -c "$5" "$6"',
            'runtime-record', str(self.project / 'scripts/select_python.sh'),
            str(record), str(self.python), sys.executable, python_report, str(report)]
        valid = base + [key + '=' + value for key, value in values.items()]
        payloads = [('\0'.join(valid) + '\0').encode(),
                    ('\0'.join(valid[:-1]) + '\0' + valid[-1]).encode()]
        for role in ('TCL_LIBRARY', 'TK_LIBRARY'):
            changed = dict(values, **{role: str(ld1) + ':' + str(ld2)})
            payloads.append(('\0'.join(base + [key + '=' + value for key, value in changed.items()]) + '\0').encode())
        for index, payload in enumerate(payloads):
            with self.subTest(case=index):
                record.write_bytes(payload); record.chmod(0o600)
                result = subprocess.run(command, cwd=self.folder, env=self.environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'ACCEPTED' if index == 0 else 'REJECTED')
                applied = json.loads(report.read_text())
                self.assertEqual(applied, values if index == 0 else dict.fromkeys(values),
                                 'Reject every malformed record before applying any overlay')

    def test_real_missing_libpython_overlay_survives_selection_into_parent_shell(self):
        if not shutil.which('gcc') or not shutil.which('readelf'):
            self.skipTest('ELF fixture requires existing gcc and readelf; nothing is installed')
        # A genuine ELF loader failure delegates to the existing real Python.
        # This fixture is neither a fabricated Python installation nor Tk result.
        prefix = self.folder / "ELF delegate's moved prefix"
        lib = prefix / 'lib'
        binary = prefix / 'bin/python'
        lib.mkdir(parents=True); binary.parent.mkdir()
        soname = 'libpython%d.%d.so.1.0' % sys.version_info[:2]
        real_dependencies = subprocess.run(['readelf', '-d', sys.executable], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        if soname in real_dependencies:
            self.skipTest('Delegate fixture needs a real interpreter without the same DT_NEEDED libpython')
        dependency = lib / soname
        source = prefix / 'dependency.c'
        source.write_text('int flux_runtime_delegate(void){return 1;}\n')
        subprocess.run(['gcc', '-shared', '-fPIC', str(source), '-o', str(dependency),
                        '-Wl,-soname,' + soname], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        source.write_text('''#include <unistd.h>
extern int flux_runtime_delegate(void);
int main(int argc,char **argv){
 if(flux_runtime_delegate()!=1)return 89;
 argv[0]=REAL_PYTHON;
 execv(REAL_PYTHON,argv);
 return 90;
}'''.replace('REAL_PYTHON', json.dumps(sys.executable)))
        subprocess.run(['gcc', str(source), str(dependency), '-o', str(binary)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        binary.chmod(0o755)
        environment = dict(self.environment, TFGEN_PYTHON=str(binary))
        before = subprocess.run([str(binary), '-c', 'print("unexpected")'], env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(before.returncode, 0)
        self.assertIn(soname + ': cannot open shared object file', before.stderr)
        report = self.folder / 'real-parent-overlay.json'
        probe = ('import json,os,sys,tkinter; from pathlib import Path; '
                 'tcl=tkinter.Tcl(); Path(sys.argv[1]).write_text(json.dumps('
                 '{"version":list(sys.version_info[:3]),"tcl":tcl.eval("info patchlevel"),'
                 '"library_path":os.environ.get("LD_LIBRARY_PATH")}))')
        command = ['/bin/bash', '-c',
            'set -eEuo pipefail; source "$1"; tfgen_install_exit_feedback; '
            'tfgen_open_launch_log; tfgen_prepare_python "$2"; '
            '[[ "$TFGEN_PYTHON_BIN" == "$3" ]]; '
            '"$TFGEN_PYTHON_BIN" -c "$4" "$5"',
            'real-parent-overlay', str(self.project / 'scripts/startup.sh'),
            str(self.project), str(binary), probe, str(report)]
        result = subprocess.run(command, cwd=self.folder, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Flux launch result: exit=0', result.stderr)
        applied = json.loads(report.read_text())
        self.assertEqual(applied['version'], list(sys.version_info[:3]))
        self.assertEqual(applied['library_path'], str(lib))
        self.assertTrue(applied['tcl'].startswith('8.6'), applied)
        self.assertIn(soname, self.log_for(result).read_text())
        self.assertFalse(self.app_report.exists())


class PrivateContextReplayTests(unittest.TestCase):
    def test_reuse_requires_a_private_matching_snapshot_and_never_replaces_it(self):
        project = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('flux_context_replay_test', project / 'cadence/plugin_context.py')
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        with tempfile.TemporaryDirectory(prefix="flux context's tests ") as directory:
            workspace = Path(directory)
            (workspace / 'cds.lib').write_text('DEFINE TestLibrary design\n')
            private = workspace / 'private'
            private.mkdir(mode=0o700)
            source = private / 'context.tsv'
            fields = dict(library='TestLibrary', cell='SourceCell', view='layout', technology='AttachedTech',
                          library_path=str(workspace / 'design'), view_path=str(workspace / 'design/SourceCell/layout'),
                          workspace=str(workspace), cds_lib=str(workspace / 'cds.lib'))
            source.write_text(plugin.HEADER + '\n' + ''.join('C\t%s\t%s\n' % (json.dumps(k), json.dumps(v))
                              for k, v in fields.items()) + 'E\t"PATH"\t"/captured/path"\n')
            source.chmod(0o600)
            with patch.object(plugin, 'discover_source_process', return_value=None):
                target = plugin.convert_context(source)
            original = target.read_bytes()
            self.assertEqual(plugin.convert_context(source, reuse_existing=True), target)
            self.assertEqual(target.read_bytes(), original)
            changed = json.loads(original)
            changed['cell'] = 'DifferentSource'
            target.write_text(json.dumps(changed)); target.chmod(0o600)
            changed_bytes = target.read_bytes()
            with self.assertRaisesRegex(ValueError, 'does not match'):
                plugin.convert_context(source, reuse_existing=True)
            self.assertEqual(target.read_bytes(), changed_bytes)
            target.unlink()
            victim = private / 'victim.json'
            victim.write_bytes(original); victim.chmod(0o600)
            target.symlink_to(victim)
            with self.assertRaises(OSError):
                plugin.convert_context(source, reuse_existing=True)
            self.assertEqual(victim.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
