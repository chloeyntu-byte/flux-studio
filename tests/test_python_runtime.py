"""Python-runtime discovery tests using real temporary ELF dependency graphs.

Only Python probe results/metadata are mocked in algorithm cases. Shared objects
are compiled by the existing GCC and inspected by the production resolver. They
are test fixtures, not a replacement Python or Tk installation. A separate test
executes an ELF launcher delegating to this test's real Python interpreter.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'python_runtime.py'


def load_runtime():
    spec = importlib.util.spec_from_file_location('flux_test_python_runtime', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PythonRuntimeDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('gcc') or not shutil.which('readelf'):
            raise unittest.SkipTest('ELF fixture tests require existing gcc and readelf; nothing is installed')
        cls.runtime = load_runtime()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flux runtime's ELF fixtures ")
        self.directory = Path(self.temporary.name)
        self.prefix = self.directory / "Python installation's prefix"
        self.candidate = self.prefix / 'bin' / 'python'
        self.candidate.parent.mkdir(parents=True)
        self.compile(self.candidate, 'int main(void){return 0;}')
        self.extension = self.prefix / 'lib/python3.11/lib-dynload/_tkinter.so'
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH',
                                           'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')
                            and not key.startswith('TFGEN_')}
        self.metadata = dict(prefix=str(self.prefix), base_prefix=str(self.prefix),
            exec_prefix=str(self.prefix), version=[3, 11, 15], tkinter_file=str(self.extension),
            tcl_version='8.6', tk_version='8.6', libdirs=[str(self.prefix / 'lib')])
        self.probes = []

    def tearDown(self):
        self.temporary.cleanup()

    def compile(self, target, source, soname=None, links=(), bits=64, rpath=None):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_file = target.with_name(target.name + '.c')
        source_file.write_text(source)
        command = ['gcc', '-x', 'c', str(source_file), '-o', str(target)]
        if soname:
            command += ['-shared', '-fPIC', '-Wl,-soname,' + soname]
        if bits == 32:
            command += ['-m32', '-nostdlib']
        if links:
            command += ['-x', 'none']
        for linked in links:
            command += [str(linked)]
        if rpath is not None:
            command += ['-Wl,-rpath,' + rpath]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        target.chmod(0o755)
        source_file.unlink()
        return target

    def graph(self, tcl_dir=None, tk_dir=None, leaf_dir=None, soname=None, bits=64, rpath=None):
        tcl_dir = tcl_dir or self.prefix / 'lib/tcl-tk/lib'
        tk_dir = tk_dir or self.prefix / 'lib/tcl-tk/lib'
        links = []
        tcl_source = 'int flux_tcl(void){return 1;}'
        if leaf_dir:
            leaf = self.compile(Path(leaf_dir) / 'libfluxleaf.so.1',
                'int flux_leaf(void){return 1;}', 'libfluxleaf.so.1')
            links = [leaf]
            tcl_source = 'extern int flux_leaf(void); int flux_tcl(void){return flux_leaf();}'
        tcl = self.compile(Path(tcl_dir) / 'libtcl8.6.so', tcl_source, 'libtcl8.6.so', links)
        tk = self.compile(Path(tk_dir) / 'libtk8.6.so',
            'extern int flux_tcl(void); int flux_tk(void){return flux_tcl();}',
            soname or 'libtk8.6.so', [tcl], bits=bits)
        self.compile(self.extension,
            'extern int flux_tk(void); int flux_extension(void){return flux_tk();}',
            '_tkinter.so', [tk], rpath=rpath)
        return tcl, tk

    def probe(self, missing=(), tcl_data=None, tk_data=None):
        """Mock only interpreter probes; discovery reads the genuine ELF fixtures."""
        def run(candidate, environment, with_tk=False):
            self.probes.append(dict(environment))
            paths = [Path(part).resolve() for part in environment.get('LD_LIBRARY_PATH', '').split(':')
                     if part and Path(part).is_absolute()]
            stderr, stage = '', 'complete'
            for soname, directory in missing:
                if Path(directory).resolve() not in paths:
                    stderr = 'ImportError: %s: cannot open shared object file: No such file or directory' % soname
                    stage = 'tk_import'
                    break
            if not stderr and tcl_data is not None and environment.get('TCL_LIBRARY') != str(tcl_data):
                stderr = "_tkinter.TclError: Can't find a usable init.tcl in the following directories:"
                stage = 'tcl_init'
            if not stderr and with_tk and tk_data is not None and environment.get('TK_LIBRARY') != str(tk_data):
                stderr = "_tkinter.TclError: Can't find a usable tk.tcl in the following directories:"
                stage = 'tk_init'
            return dict(returncode=1 if stderr else 0, stdout='', stderr=stderr,
                        metadata=dict(self.metadata), stage=stage)
        return run

    def resolve(self, probe, environment=None, candidate=None, with_tk=False):
        with patch.object(self.runtime, '_probe', side_effect=probe):
            return self.runtime.resolve_python(str(candidate or self.candidate),
                environment=dict(self.environment if environment is None else environment), with_tk=with_tk)

    def data(self, directory, kind, version='8.6.13'):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = 'init.tcl' if kind == 'Tcl' else 'tk.tcl'
        (directory / name).write_text('package require -exact %s %s\n' % (kind, version))
        return directory

    def assert_failure(self, probe, keyword, environment=None, with_tk=False):
        with self.assertRaises(self.runtime.RuntimeFailure) as raised:
            self.resolve(probe, environment=environment, with_tk=with_tk)
        failure = raised.exception
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn(keyword.lower(), (failure.message + '\n' + '\n'.join(failure.diagnostics)).lower())
        return failure

    def test_clean_probe_does_not_inject_libraries_or_tcl_tk_data(self):
        for with_tk in (False, True):
            with self.subTest(with_tk=with_tk):
                self.probes = []
                result = self.resolve(self.probe(), with_tk=with_tk)
                self.assertEqual(result['overlays'], {})
                self.assertEqual(result['interpreter'], str(self.candidate))
                self.assertEqual(len(self.probes), 1)

    def test_missing_tk_dependency_is_found_under_prefix_tcl_tk_lib(self):
        tcl, tk = self.graph()
        baseline = dict(self.environment)
        result = self.resolve(self.probe([('libtk8.6.so', tk.parent)]))
        self.assertIn(str(tk.parent), result['overlays']['LD_LIBRARY_PATH'].split(':'))
        self.assertEqual(self.environment, baseline)
        self.assertEqual(self.probes[-1]['LD_LIBRARY_PATH'], result['overlays']['LD_LIBRARY_PATH'])

    def test_split_tk_tcl_and_transitive_dependency_directories_are_discovered(self):
        leaf_dir = self.prefix / 'lib'
        tcl, tk = self.graph(tcl_dir=self.prefix / 'lib/tcl8.6',
                             tk_dir=self.prefix / 'lib/tcl-tk/lib', leaf_dir=leaf_dir)
        result = self.resolve(self.probe([('libtk8.6.so', tk.parent),
                                         ('libtcl8.6.so', tcl.parent),
                                         ('libfluxleaf.so.1', leaf_dir)]))
        paths = result['overlays']['LD_LIBRARY_PATH'].split(':')
        self.assertTrue({str(tk.parent), str(tcl.parent), str(leaf_dir)} <= set(paths))
        self.assertEqual(len(paths), len(set(paths)))

    def test_unrelated_cadence_qt_library_directory_is_not_used(self):
        unrelated = self.directory / 'Cadence/tools/qt/lib'
        _, tk = self.graph(tcl_dir=unrelated, tk_dir=unrelated)
        for field in ('TFGEN_ORIGINAL_LD_LIBRARY_PATH', 'TFGEN_CAPTURED_LD_LIBRARY_PATH'):
            with self.subTest(field=field):
                self.probes = []
                environment = dict(self.environment, CDSHOME=str(self.directory/'Cadence'))
                environment[field] = str(unrelated)
                self.assert_failure(self.probe([('libtk8.6.so', tk.parent)]), 'libtk8.6.so', environment=environment)
                self.assertFalse(any(str(unrelated) in p.get('LD_LIBRARY_PATH', '').split(':') for p in self.probes))

    def test_python_own_sysconfig_library_path_may_contain_qt_name(self):
        own = self.prefix/'lib/Qt companion libraries'
        _, tk = self.graph(tcl_dir=own, tk_dir=own)
        self.metadata['libdirs'].append(str(own))
        result = self.resolve(self.probe([('libtk8.6.so', tk.parent)]))
        self.assertIn(str(own), result['overlays']['LD_LIBRARY_PATH'].split(':'))

    def test_empty_dot_and_relative_rpaths_are_not_search_roots(self):
        unrelated = self.directory / 'unrelated working directory'
        _, tk = self.graph(tcl_dir=unrelated, tk_dir=unrelated, rpath=':.:relative-libs')
        self.metadata['libdirs'] += ['', '.', 'relative-libs']
        previous = os.getcwd()
        try:
            os.chdir(str(unrelated))
            self.assert_failure(self.probe([('libtk8.6.so', unrelated)]), 'libtk8.6.so')
        finally:
            os.chdir(previous)
        self.assertFalse(any(str(unrelated) in p.get('LD_LIBRARY_PATH', '').split(':') for p in self.probes))

    def test_soname_mismatch_is_rejected_with_specific_missing_dependency(self):
        _, tk = self.graph(tk_dir=self.prefix/'lib')
        tk.unlink()
        self.compile(tk, 'int flux_tk(void){return 1;}', 'libtk8.5.so')
        failure = self.assert_failure(self.probe([('libtk8.6.so', tk.parent)]), 'libtk8.6.so')
        self.assertIn('SONAME mismatch', '\n'.join(failure.diagnostics))
        self.assertFalse(any(str(tk.parent) in p.get('LD_LIBRARY_PATH', '').split(':') for p in self.probes))

    def test_elf_class_mismatch_is_rejected(self):
        tcl, tk = self.graph(tk_dir=self.prefix/'lib')
        tk.unlink()
        self.compile(tk, 'int flux_tk(void){return 1;}', 'libtk8.6.so', bits=32)
        failure = self.assert_failure(self.probe([('libtk8.6.so', tk.parent)]), 'libtk8.6.so')
        self.assertIn('architecture mismatch', '\n'.join(failure.diagnostics))
        self.assertFalse(any(str(tk.parent) in p.get('LD_LIBRARY_PATH', '').split(':') for p in self.probes))

    def test_absent_library_keeps_accurate_name_and_failure_stage(self):
        self.graph()
        failure = self.assert_failure(self.probe([('libtk_flux_absent.so.9', self.prefix/'lib/missing')]),
                                      'libtk_flux_absent.so.9')
        self.assertIn(failure.stage, ('tk_import', 'library_discovery', 'dependencies'))

    def test_symlink_interpreter_keeps_lexical_selection_and_real_prefix_dependencies(self):
        _, tk = self.graph()
        entry = self.directory / 'entry point/python'
        entry.parent.mkdir()
        entry.symlink_to(self.candidate)
        for spelling in (str(entry), str(entry.parent) + '/../entry point/python'):
            with self.subTest(spelling=spelling):
                result = self.resolve(self.probe([('libtk8.6.so', tk.parent)]), candidate=spelling)
                self.assertEqual(result['interpreter'], spelling)
                self.assertIn(str(tk.parent), result['overlays']['LD_LIBRARY_PATH'].split(':'))

    def test_moved_prefix_with_spaces_and_apostrophes_uses_only_new_paths(self):
        self.graph()
        old = self.prefix
        moved = self.directory / "moved project's Python prefix"
        shutil.move(str(old), str(moved))
        self.candidate = moved/'bin/python'
        self.extension = moved/'lib/python3.11/lib-dynload/_tkinter.so'
        for key in ('prefix', 'base_prefix', 'exec_prefix'):
            self.metadata[key] = str(moved)
        self.metadata['tkinter_file'] = str(self.extension)
        self.metadata['libdirs'] = [str(moved/'lib')]
        result = self.resolve(self.probe([('libtk8.6.so', moved/'lib/tcl-tk/lib')]))
        self.assertEqual(result['interpreter'], str(self.candidate))
        self.assertNotIn(str(old), json.dumps(result['overlays']))
        self.assertIn(str(moved/'lib/tcl-tk/lib'), result['overlays']['LD_LIBRARY_PATH'].split(':'))

    def test_tcl_and_tk_data_are_selected_independently_from_matching_prefix(self):
        self.graph()
        tcl_data = self.data(self.prefix/'share/tcltk/tcl8.6', 'Tcl')
        tk_data = self.data(self.prefix/'lib/tk8.6', 'Tk')
        result = self.resolve(self.probe(tcl_data=tcl_data, tk_data=tk_data), with_tk=True)
        self.assertEqual(result['overlays']['TCL_LIBRARY'], str(tcl_data))
        self.assertEqual(result['overlays']['TK_LIBRARY'], str(tk_data))

    def test_incompatible_tcl_data_abi_is_not_injected(self):
        self.graph()
        bad = self.data(self.prefix/'lib/tcl8.6', 'Tcl', '8.5.19')
        self.assert_failure(self.probe(tcl_data=bad), 'tcl')
        self.assertFalse(any(p.get('TCL_LIBRARY') == str(bad) for p in self.probes))

    def test_incompatible_tk_data_abi_is_not_injected(self):
        self.graph()
        bad = self.data(self.prefix/'lib/tk8.6', 'Tk', '8.5.19')
        self.assert_failure(self.probe(tk_data=bad), 'tk', with_tk=True)
        self.assertFalse(any(p.get('TK_LIBRARY') == str(bad) for p in self.probes))

    def test_explicit_library_override_is_authoritative_and_not_extended(self):
        _, tk = self.graph()
        chosen = self.directory/'explicit dependencies'
        chosen.mkdir()
        env = dict(self.environment, LD_LIBRARY_PATH=str(chosen), TFGEN_PYTHON_LD_LIBRARY_PATH=str(chosen))
        original = dict(env)
        self.assert_failure(self.probe([('libtk8.6.so', tk.parent)]), 'libtk8.6.so', environment=env)
        self.assertEqual(env, original)
        self.assertFalse(any(str(tk.parent) in p.get('LD_LIBRARY_PATH', '').split(':') for p in self.probes))

    def test_explicit_tcl_directory_is_preserved_while_tk_may_be_discovered(self):
        self.graph()
        tcl_data = self.data(self.directory/'explicit Tcl data', 'Tcl')
        tk_data = self.data(self.prefix/'lib/tk8.6', 'Tk')
        env = dict(self.environment, TCL_LIBRARY=str(tcl_data), TFGEN_TCL_LIBRARY=str(tcl_data))
        result = self.resolve(self.probe(tcl_data=tcl_data, tk_data=tk_data), environment=env, with_tk=True)
        self.assertNotIn('TCL_LIBRARY', result['overlays'])
        self.assertEqual(result['overlays']['TK_LIBRARY'], str(tk_data))

    def test_selected_external_provider_libraries_lead_to_its_matching_data(self):
        provider = self.directory / "selected toolkit's provider"
        _, tk = self.graph(tcl_dir=provider/'lib', tk_dir=provider/'lib')
        tcl_data = self.data(provider/'share/tcltk/tcl8.6', 'Tcl')
        tk_data = self.data(provider/'share/tcltk/tk8.6', 'Tk')
        environment = dict(self.environment, TFGEN_ORIGINAL_LD_LIBRARY_PATH=str(provider/'lib'))
        result = self.resolve(self.probe([('libtk8.6.so', tk.parent)],
                              tcl_data=tcl_data, tk_data=tk_data), environment=environment, with_tk=True)
        self.assertEqual(result['overlays']['TCL_LIBRARY'], str(tcl_data))
        self.assertEqual(result['overlays']['TK_LIBRARY'], str(tk_data))
        self.assertEqual(result['overlays']['LD_LIBRARY_PATH'], str(provider/'lib'))

    def test_symbol_and_display_errors_are_preserved_without_automatic_injection(self):
        for stage, detail in (('tk_import','ImportError: _tkinter.so: undefined symbol: TclFluxMissing'),
                              ('tk_init','_tkinter.TclError: no display name and no $DISPLAY environment variable')):
            with self.subTest(stage=stage):
                self.probes = []
                def probe(candidate, environment, with_tk=False):
                    self.probes.append(dict(environment))
                    return dict(returncode=1,stdout='',stderr=detail,metadata=dict(self.metadata),stage=stage)
                failure = self.assert_failure(probe, detail, with_tk=True)
                self.assertEqual(failure.stage, stage)
                self.assertIn(detail, failure.message)
                self.assertEqual(len(self.probes), 1)

    def test_real_clean_python_tcl_probe_keeps_environment_unmodified(self):
        result = self.runtime.resolve_python(sys.executable, environment=dict(self.environment), with_tk=False)
        self.assertEqual(result['overlays'], {})
        self.assertEqual(result['interpreter'], str(Path(sys.executable).absolute()))

    def test_actual_loader_recovers_the_real_transitive_elf_graph(self):
        # Genuine dlopen errors drive all iterations. Only the selected
        # interpreter metadata is supplied by the fixture; this is not Tk.
        leaf = self.compile(self.prefix/'lib/libflux_loader_leaf.so.1',
                            'int flux_leaf(void){return 1;}', 'libflux_loader_leaf.so.1')
        tcl = self.compile(self.prefix/'lib/tcl8.6/libtcl8.6_flux_fixture.so',
            'extern int flux_leaf(void);int flux_tcl(void){return flux_leaf();}',
            'libtcl8.6_flux_fixture.so', [leaf])
        tk = self.compile(self.prefix/'lib/tcl-tk/lib/libtk8.6_flux_fixture.so',
            'extern int flux_tcl(void);int flux_tk(void){return flux_tcl();}',
            'libtk8.6_flux_fixture.so', [tcl])
        self.compile(self.extension,
            'extern int flux_tk(void);int flux_extension(void){return flux_tk();}',
            '_tkinter.so', [tk])
        outcomes = []
        def actual_loader_probe(candidate, environment, with_tk=False):
            process = subprocess.run([sys.executable, '-c',
                'import ctypes,sys;ctypes.CDLL(sys.argv[1]);print("ELF graph loaded")',str(self.extension)],
                env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
            outcomes.append(process)
            return dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr,
                        metadata=dict(self.metadata),stage='complete' if process.returncode==0 else 'tk_import')
        result = self.resolve(actual_loader_probe)
        self.assertGreaterEqual(len(outcomes), 4)
        self.assertIn('libtk8.6_flux_fixture.so', outcomes[0].stderr)
        self.assertIn('libtcl8.6_flux_fixture.so', outcomes[1].stderr)
        self.assertIn('libflux_loader_leaf.so.1', outcomes[2].stderr)
        self.assertEqual(outcomes[-1].returncode, 0)
        self.assertEqual(outcomes[-1].stdout.strip(), 'ELF graph loaded')
        self.assertEqual(set(result['overlays']['LD_LIBRARY_PATH'].split(':')),
                         {str(tk.parent), str(tcl.parent), str(leaf.parent)})

    @unittest.skipIf(sysconfig.get_config_var('Py_ENABLE_SHARED'),
                     'a copied interpreter cannot start when Python is built as a shared library')
    def test_real_elf_missing_libpython_fallback_reaches_actual_python_probe(self):
        # This ELF fixture delegates to the running real Python; it is not
        # advertised as a newly built Python or Tk installation.
        prefix = self.directory / "real ELF delegate's prefix"
        soname = 'libpython%d.%d.so.1.0' % sys.version_info[:2]
        dependency = self.compile(prefix/'lib'/soname,
            'int flux_runtime_delegate(void){return 1;}', soname)
        source = '''#include <unistd.h>
#include <stdlib.h>
extern int flux_runtime_delegate(void);
int main(int argc,char **argv){
 if(flux_runtime_delegate()!=1)return 89;
 argv[0]=REAL_PYTHON;
 execv(REAL_PYTHON,argv);
 return 90;
}'''.replace('REAL_PYTHON', json.dumps(sys.executable))
        delegate = self.compile(prefix/'bin/python', source, links=[dependency])
        before = subprocess.run([str(delegate), '-c', 'print("ok")'], env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(before.returncode, 0)
        self.assertIn(soname, before.stderr)
        result = self.runtime.resolve_python(str(delegate), environment=dict(self.environment), with_tk=False)
        self.assertIn(str(dependency.parent), result['overlays']['LD_LIBRARY_PATH'].split(':'))
        self.assertEqual(result['interpreter'], str(delegate))
        after = subprocess.run([str(delegate), '-c', 'import sys,tkinter;print(sys.version_info[:2]);tkinter.Tcl()'],
            env=dict(self.environment, **result['overlays']), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_system_cache_exact_soname_fallback_supports_spaces_without_relative_roots(self):
        name = 'libtk8.6_flux_cached.so'
        provider = self.directory / "cached provider's library directory"
        library = self.compile(provider/name, 'int flux_cached(void){return 1;}', name)
        self.compile(self.extension, 'extern int flux_cached(void);int flux_extension(void){return flux_cached();}',
                     '_tkinter.so', [library])
        wrong = self.directory/'wrong cache SONAME'
        relative = self.directory/'relative cached'
        self.compile(wrong/name, 'int flux_cached(void){return 1;}', name)
        self.compile(relative/name, 'int flux_cached(void){return 1;}', name)
        cache = ('  libtk8.60_flux_cached.so (libc6,x86-64) => %s\n'
                 '  %s (libc6,x86-64) => relative cached/%s\n'
                 '  %s (libc6,x86-64) => \n'
                 '  %s (libc6,x86-64) => %s\n') % (wrong/name,name,name,name,name,library)
        def query(command, **kwargs):
            self.assertEqual(command[1:], ['-p'])
            self.assertNotIn('LD_LIBRARY_PATH', kwargs['env'])
            return subprocess.CompletedProcess(command, 0, cache.encode(), b'')
        previous = os.getcwd()
        try:
            os.chdir(str(self.directory))
            with patch.object(self.runtime.subprocess, 'run', side_effect=query) as queried:
                result = self.resolve(self.probe([(name,provider)]))
        finally:
            os.chdir(previous)
        self.assertEqual(queried.call_count, 1)
        self.assertEqual(result['overlays']['LD_LIBRARY_PATH'], str(provider))
        self.assertFalse(any(str(wrong) in p.get('LD_LIBRARY_PATH','').split(':') or
                             str(relative) in p.get('LD_LIBRARY_PATH','').split(':') for p in self.probes))

    def test_system_cache_does_not_bypass_elf_architecture_validation(self):
        name = 'libtk8.6_flux_cached.so'
        provider = self.directory/'wrong architecture cached provider'
        library = self.compile(provider/name, 'int flux_cached(void){return 1;}', name)
        self.compile(self.extension, 'extern int flux_cached(void);int flux_extension(void){return flux_cached();}',
                     '_tkinter.so', [library])
        library.unlink()
        self.compile(library, 'int flux_cached(void){return 1;}', name, bits=32)
        cache = '  %s (libc6,x86-64) => %s\n' % (name,library)
        def query(command, **kwargs):
            self.assertEqual(command[1:], ['-p'])
            return subprocess.CompletedProcess(command, 0, cache.encode(), b'')
        with patch.object(self.runtime.subprocess, 'run', side_effect=query):
            failure = self.assert_failure(self.probe([(name,provider)]),name)
        self.assertIn('architecture mismatch', '\n'.join(failure.diagnostics))
        self.assertFalse(any(str(provider) in p.get('LD_LIBRARY_PATH','').split(':') for p in self.probes))

    def test_cli_nul_record_is_private_and_never_replaces_existing_output(self):
        output = self.directory / "private result's record"
        command = [sys.executable, str(MODULE_PATH), '--python', sys.executable, '--output', str(output)]
        result = subprocess.run(command, env=self.environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        record = output.read_bytes()
        self.assertTrue(record.endswith(b'\0'))
        fields = dict(item.decode().split('=',1) for item in record.split(b'\0') if item)
        self.assertEqual(fields['TFGEN_RUNTIME_INTERPRETER'], sys.executable)
        self.assertIn('TFGEN_RUNTIME_SCHEMA', fields)
        repeated = subprocess.run(command, env=self.environment, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(output.read_bytes(), record)

    def test_cli_missing_executable_reports_failure_without_success_record(self):
        output = self.directory/'failed-record'
        result = subprocess.run([sys.executable,str(MODULE_PATH),'--python',str(self.directory/'absent python'),
            '--output',str(output)], env=self.environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('absent python', result.stderr)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
