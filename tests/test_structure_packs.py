"""Structure packs: discovery, loading, failure isolation and the packs CLI.

Every test points discovery at its own temporary directory and unloads the
packs afterwards, so the result never depends on what sits in the repository's
``packs/`` folder.  Temporary directories are removed through addCleanup.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend
import structure_packs
from pack_support import EXAMPLE_MODEL, EXAMPLE_PACK, isolate_packs

FIXTURES = ROOT / 'tests' / 'fixtures'

GOOD_PACK = '''
PACK_NAME = 'tmp-good'
PACK_VERSION = '0.1'
import copy

def register(flux):
    base = next(item for item in flux.builtin_definitions if item['id'] == 'differential_inductor')
    definition = copy.deepcopy(base)
    definition.update(id=%(model)r, title='Temporary %(model)s', description='Test pack model.')
    def build(parameters, layers, technology, variant):
        result = flux.build_builtin(parameters, layers, technology, 'differential_inductor')
        result['geometry'].setdefault('metrics', {})['variant_seen'] = variant
        return result
    flux.register_model(definition, build)
'''


def good_pack(model='tmp_good_model'):
    return GOOD_PACK % {'model': model}


def model_ids():
    return [item['id'] for item in backend.models()]


class PackTestCase(unittest.TestCase):
    def setUp(self):
        isolate_packs(self)
        directory = tempfile.TemporaryDirectory(prefix='flux-pack-test-')
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.empty = Path(tempfile.mkdtemp(prefix='flux-pack-empty-', dir=self.tmp))

    def write(self, name, code, folder=None):
        path = (folder or self.tmp) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(code), encoding='utf-8')
        return path

    def point_at(self, *entries):
        os.environ['FLUX_PACKS'] = os.pathsep.join(str(entry) for entry in entries)
        os.environ.pop('FLUX_PACKS_DISABLE', None)
        structure_packs.reset()

    def report(self):
        return {entry['name']: entry for entry in backend.structure_pack_report()}

    def library(self):
        """Register the offline generic_d profile and return its library name."""
        library_patch = mock.patch.dict(backend.LIBRARIES, {}, clear=True)
        library_patch.start()
        self.addCleanup(library_patch.stop)
        return backend.load_file_context(str(FIXTURES / 'generic_d.json'))['selected_library']

    def design(self, library, model, structure='layout', **parameters):
        return {'library': library, 'cell': 'PackCell', 'model': model, 'structure': structure,
                'parameters': parameters, 'layers': {}}


class DiscoveryTests(PackTestCase):
    def test_pack_file_directory_and_package_entries(self):
        single = self.write('single_pack.py', good_pack('single_model'))
        folder = self.tmp / 'folder'
        self.write('b_second.py', good_pack('b_model'), folder)
        self.write('a_first.py', good_pack('a_model'), folder)
        self.write('_private.py', good_pack('private_model'), folder)
        self.write('.hidden.py', good_pack('hidden_model'), folder)
        self.write('notes.txt', 'not a pack', folder)
        (folder / 'no_init_dir').mkdir()
        self.write('pkg_pack/__init__.py', good_pack('pkg_model'), folder)
        package = self.tmp / 'standalone_pkg'
        self.write('__init__.py', good_pack('standalone_model'), package)

        self.assertEqual(structure_packs.discover({'FLUX_PACKS': str(single)}), [single])
        self.assertEqual([p.name for p in structure_packs.discover({'FLUX_PACKS': str(folder)})],
                         ['a_first.py', 'b_second.py', 'pkg_pack'])
        self.assertEqual(structure_packs.discover({'FLUX_PACKS': str(package)}), [package])
        joined = os.pathsep.join([str(single), str(folder), str(single), str(self.tmp / 'missing.py')])
        found = structure_packs.discover({'FLUX_PACKS': joined})
        self.assertEqual([p.name for p in found], ['single_pack.py', 'a_first.py', 'b_second.py', 'pkg_pack'])
        self.assertEqual(structure_packs.discover({'FLUX_PACKS': ''}), [])
        self.assertEqual(structure_packs.discover({}), [])

    def test_default_pack_folder_is_searched_after_flux_packs(self):
        env_pack = self.write('env_pack.py', good_pack('env_model'))
        default_dir = self.tmp / 'default_dir'
        default_pack = self.write('default_pack.py', good_pack('default_model'), default_dir)
        with mock.patch.object(structure_packs, 'PACK_DIR', default_dir):
            self.assertEqual(structure_packs.discover({'FLUX_PACKS': str(env_pack)}), [env_pack, default_pack])
            self.assertEqual(structure_packs.discover({}), [default_pack])

    def test_directory_package_with_relative_import_loads(self):
        package = self.tmp / 'pkg_with_helper'
        self.write('helper.py', "SUFFIX = 'from helper'\n", package)
        self.write('__init__.py', good_pack('helper_model').replace(
            "description='Test pack model.'", "description=SUFFIX").replace(
            "import copy", "import copy\nfrom . import helper\nSUFFIX = helper.SUFFIX"), package)
        self.point_at(package)
        self.assertIn('helper_model', model_ids())
        self.assertEqual(next(m for m in backend.models() if m['id'] == 'helper_model')['description'], 'from helper')
        self.assertEqual(self.report()['tmp-good']['source'], 'pkg_with_helper')

    def test_nothing_is_loaded_before_the_first_model_request_and_reset_unloads(self):
        self.point_at(self.write('late.py', good_pack('late_model')))
        self.assertNotIn('late_model', [item['id'] for item in backend.EXTRA_MODELS])
        self.assertIn('late_model', model_ids())
        structure_packs.reset()
        self.assertEqual(backend.EXTRA_MODELS, [])
        self.assertNotIn('late_model', backend.MODEL_BUILDERS)
        self.assertIn('late_model', model_ids())           # discovered and loaded again
        self.assertEqual(len([m for m in backend.EXTRA_MODELS if m['id'] == 'late_model']), 1)


class ExamplePackTests(PackTestCase):
    def setUp(self):
        super().setUp()
        self.point_at(EXAMPLE_PACK)

    def test_example_pack_adds_its_model_after_the_built_in_one(self):
        self.assertEqual(model_ids(), ['differential_inductor', EXAMPLE_MODEL])
        definition = next(m for m in backend.models() if m['id'] == EXAMPLE_MODEL)
        self.assertEqual([item['id'] for item in definition['structures']], ['compact', 'wide'])
        self.assertEqual(definition['emx_metrics'], ['L', 'Q'])
        self.assertEqual(definition['defaults']['center_tap'], True)
        self.assertEqual(definition['defaults']['turns'], 3)
        self.assertTrue({'pgs_extent', 'ring_extent'} <= set(definition['defaults']))   # shared shield parameters
        self.assertTrue({'pgs', 'ring', 'ground_route'} <= set(definition['layer_roles']))
        report = self.report()['example-tapped-inductor']
        self.assertEqual((report['version'], report['models'], report['error']), ('1.0.0', [EXAMPLE_MODEL], None))
        self.assertEqual(report['source'], 'example_tapped_inductor.py')

    def test_both_variants_preview_and_wide_widens_the_terminal_gap(self):
        library = self.library()
        self.assertEqual(model_ids(), ['differential_inductor', EXAMPLE_MODEL])
        results = {}
        for variant in ('compact', 'wide'):
            result = backend.preview(self.design(library, EXAMPLE_MODEL, variant))
            self.assertTrue(result['ok'], result['errors'])
            metrics = result['geometry']['metrics']
            self.assertEqual((metrics['model'], metrics['variant']), (EXAMPLE_MODEL, variant))
            self.assertEqual(metrics['winding_components'], {'L': 1})
            ports = {port['name']: port for port in result['geometry']['ports']}
            self.assertTrue({'L1', 'L2', 'CTAP'} <= set(ports))
            results[variant] = ports['L2']['bbox'][0][0] - ports['L1']['bbox'][1][0]
        self.assertGreater(results['wide'], results['compact'])
        # The model's own default metals come from default_layers (top-2 / top-3 / top-4).
        names = [rule['name'] for rule in sorted(backend.LIBRARIES[library]['_tech']['layers'], key=lambda r: r['order'])]
        self.assertEqual({k: backend.LIBRARIES[library]['model_default_layers'][EXAMPLE_MODEL][k]
                          for k in ('body', 'crossing', 'tap')},
                         {'body': names[-2], 'crossing': names[-3], 'tap': names[-4]})

    def test_unknown_variant_is_rejected_and_built_in_model_is_unaffected(self):
        library = self.library()
        with self.assertRaisesRegex(ValueError, 'routing variant'):
            backend.preview(self.design(library, EXAMPLE_MODEL, 'layout'))
        builtin = backend.preview(self.design(library, 'differential_inductor', center_tap=True))
        self.assertTrue(builtin['ok'], builtin['errors'])
        self.assertEqual(builtin['geometry']['metrics']['model'], 'differential_inductor')

    def test_context_lists_packs_and_no_longer_exposes_global_structures(self):
        context = backend.load_file_context(str(FIXTURES / 'generic_d.json'))
        self.addCleanup(backend.LIBRARIES.pop, context['selected_library'], None)
        self.assertEqual(context['structure_packs'], backend.structure_pack_report())
        self.assertEqual([m['id'] for m in context['models']], ['differential_inductor', EXAMPLE_MODEL])
        self.assertNotIn('structures', context)
        self.assertNotIn('reference', context)

    def test_pack_api_object_exposes_the_documented_contract(self):
        seen = {}
        path = self.write('probe.py', '''
            PACK_NAME = 'probe'
            def register(flux):
                import copy
                seen = flux.__dict__
                definition = copy.deepcopy(next(d for d in flux.builtin_definitions if d['id'] == 'differential_inductor'))
                definition.update(id='probe_model', title='Probe', description='Probe')
                names = sorted(name for name in dir(flux) if not name.startswith('_'))
                definition['description'] = ','.join(names) + '|%s|%s' % (flux.api_version, flux.pack_path.name)
                flux.register_model(definition, lambda *args: None)
        ''')
        self.point_at(path)
        description = next(m for m in backend.models() if m['id'] == 'probe_model')['description']
        names, _, tail = description.partition('|')
        for attribute in ('api_version', 'build_builtin', 'builtin_definitions', 'geometry', 'pack_path',
                          'register_model', 'screen_parameters', 'technology'):
            self.assertIn(attribute, names.split(','))
        self.assertEqual(tail, '1|probe.py')


class VariantContractTests(PackTestCase):
    def test_builder_gets_variant_only_when_model_declares_structures(self):
        path = self.write('variants.py', '''
            import copy
            def register(flux):
                base = next(d for d in flux.builtin_definitions if d['id'] == 'differential_inductor')
                def build(parameters, layers, technology, variant):
                    result = flux.build_builtin(parameters, layers, technology, 'differential_inductor')
                    result['geometry']['metrics']['variant_seen'] = variant
                    return result
                plain = copy.deepcopy(base)
                plain.update(id='plain_model', title='Plain', description='No variants')
                varied = copy.deepcopy(base)
                varied.update(id='varied_model', title='Varied', description='Variants',
                              structures=[{'id': 'one', 'title': 'One'}, {'id': 'two', 'title': 'Two'}])
                flux.register_model(plain, build)
                flux.register_model(varied, build)
        ''')
        self.point_at(path)
        library = self.library()
        plain = backend.preview(self.design(library, 'plain_model'))
        self.assertEqual(plain['geometry']['metrics']['variant_seen'], 'plain_model')
        for variant in ('one', 'two'):
            result = backend.preview(self.design(library, 'varied_model', variant))
            self.assertEqual(result['geometry']['metrics']['variant_seen'], variant)
        self.assertEqual(backend.normalize_design({'model': 'varied_model'})['structure'], 'one')

    def test_invalid_builder_result_is_a_clear_error(self):
        path = self.write('garbage.py', '''
            import copy
            def register(flux):
                definition = copy.deepcopy(next(d for d in flux.builtin_definitions if d['id'] == 'differential_inductor'))
                definition.update(id='garbage_model', title='Garbage', description='Returns nothing useful')
                flux.register_model(definition, lambda *args: None)
        ''')
        self.point_at(path)
        library = self.library()
        with self.assertRaisesRegex(ValueError, 'invalid result'):
            backend.preview(self.design(library, 'garbage_model'))


class BrokenPackTests(PackTestCase):
    def broken_folder(self):
        folder = self.tmp / 'mixed'
        self.write('a_good.py', good_pack('mixed_good_model'), folder)
        self.write('b_syntax.py', 'def register(flux:\n    pass\n', folder)
        self.write('c_no_register.py', 'PACK_NAME = "no-register"\nVALUE = 1\n', folder)
        self.write('d_api.py', 'PACK_NAME = "wrong-api"\nFLUX_PACK_API = 99\ndef register(flux):\n    raise AssertionError("must not run")\n', folder)
        self.write('e_rollback.py', '''
            PACK_NAME = "rollback"
            import copy
            def register(flux):
                definition = copy.deepcopy(next(d for d in flux.builtin_definitions if d['id'] == 'differential_inductor'))
                definition.update(id='half_registered', title='Half', description='Registered before the failure')
                flux.register_model(definition, lambda *args: None)
                raise RuntimeError('boom after registering')
        ''', folder)
        self.write('f_import_error.py', 'PACK_NAME = "import-error"\nraise ZeroDivisionError("at import time")\n', folder)
        self.write('g_empty.py', 'PACK_NAME = "registers-nothing"\ndef register(flux):\n    pass\n', folder)
        self.write('h_bad_definition.py', '''
            PACK_NAME = "bad-definition"
            def register(flux):
                flux.register_model({'id': 'bad definition!', 'title': 'x'}, lambda *args: None)
        ''', folder)
        self.write('i_good_after.py', good_pack('after_good_model').replace('tmp-good', 'tmp-good-after'), folder)
        return folder

    def test_each_failure_is_reported_and_skipped(self):
        self.point_at(self.broken_folder())
        ids = model_ids()
        report = self.report()
        expected = {
            'b_syntax': 'SyntaxError',
            'no-register': 'does not define register',
            'wrong-api': 'API',
            'rollback': 'RuntimeError: boom after registering',
            'f_import_error': 'ZeroDivisionError',
            'registers-nothing': 'registered no models',
            'bad-definition': 'ValueError',
        }
        for name, text in expected.items():
            self.assertIn(name, report)
            self.assertIn(text, report[name]['error'], name)
            self.assertEqual(report[name]['models'], [], name)
        self.assertNotIn('AssertionError', report['wrong-api']['error'])     # register() never ran
        # The two healthy packs load around the broken ones; the built-in model stays first.
        self.assertEqual(ids, ['differential_inductor', 'mixed_good_model', 'after_good_model'])
        self.assertNotIn('half_registered', ids)
        self.assertNotIn('half_registered', backend.MODEL_BUILDERS)
        self.assertIsNone(report['tmp-good']['error'])
        self.assertIsNone(report['tmp-good-after']['error'])

    def test_built_in_model_still_previews_with_broken_packs_present(self):
        self.point_at(self.broken_folder())
        library = self.library()
        result = backend.preview(self.design(library, 'differential_inductor', center_tap=True))
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(model_ids()[0], 'differential_inductor')

    def test_failed_pack_leaves_no_module_behind_and_load_is_repeatable(self):
        path = self.write('flaky.py', 'raise RuntimeError("always fails")\n')
        before = {name for name in sys.modules if name.startswith('flux_pack_')}
        self.point_at(path)
        model_ids()
        self.assertEqual({name for name in sys.modules if name.startswith('flux_pack_')}, before)
        self.assertEqual(len(backend.structure_pack_report()), 1)
        model_ids()                     # a second request must not load (or report) it again
        self.assertEqual(len(backend.structure_pack_report()), 1)

    def test_public_report_has_no_absolute_paths_or_tracebacks(self):
        folder = self.broken_folder()
        self.point_at(folder)
        model_ids()
        report = backend.structure_pack_report()
        self.assertTrue(report)
        for entry in report:
            self.assertEqual(set(entry), {'name', 'source', 'version', 'models', 'error'})
            self.assertNotIn(os.sep, entry['source'])
        text = json.dumps(report)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(str(folder), text)
        self.assertNotIn('Traceback', text)
        self.assertNotIn('File "', text)
        self.assertNotIn(str(ROOT), text)
        # The private traceback is kept internally for diagnosis only.
        self.assertTrue(any('Traceback' in entry.get('traceback', '') for entry in structure_packs._STATE['packs']))
        context = backend.load_file_context(str(FIXTURES / 'generic_d.json'))
        self.addCleanup(backend.LIBRARIES.pop, context['selected_library'], None)
        self.assertEqual(context['structure_packs'], report)
        self.assertNotIn(str(self.tmp), json.dumps(context['structure_packs']))


class DuplicateAndDisableTests(PackTestCase):
    def test_duplicate_model_ids_are_rejected_and_the_first_registration_wins(self):
        folder = self.tmp / 'dupes'
        self.write('a_first.py', good_pack('shared_id'), folder)
        self.write('b_second.py', good_pack('shared_id').replace('tmp-good', 'tmp-second'), folder)
        self.write('c_builtin_clash.py', good_pack('differential_inductor').replace('tmp-good', 'tmp-clash'), folder)
        self.point_at(folder)
        ids = model_ids()
        self.assertEqual(ids, ['differential_inductor', 'shared_id'])
        report = self.report()
        self.assertIsNone(report['tmp-good']['error'])
        self.assertIn('already registered', report['tmp-second']['error'])
        self.assertIn('already registered', report['tmp-clash']['error'])
        self.assertEqual(report['tmp-second']['models'], [])
        builtin = next(m for m in backend.models() if m['id'] == 'differential_inductor')
        self.assertIn('Differential inductor', builtin['title'])
        self.assertEqual(len(backend.EXTRA_MODELS), 1)

    def test_registering_the_same_id_twice_directly_is_rejected(self):
        definition = copy.deepcopy(backend.models()[0])
        definition['id'] = 'direct_twice'
        backend.register_model(definition, lambda *args: None)
        self.addCleanup(backend.unregister_model, 'direct_twice')
        with self.assertRaisesRegex(ValueError, 'already registered'):
            backend.register_model(definition, lambda *args: None)
        self.assertEqual(model_ids().count('direct_twice'), 1)

    def test_flux_packs_disable_skips_discovery_entirely(self):
        pack = self.write('wanted.py', good_pack('disabled_model'))
        for value in ('1', 'true', 'yes'):
            with self.subTest(value=value):
                os.environ['FLUX_PACKS'] = str(pack)
                os.environ['FLUX_PACKS_DISABLE'] = value
                structure_packs.reset()
                self.assertEqual(model_ids(), ['differential_inductor'])
                self.assertEqual(backend.structure_pack_report(), [])
                self.assertEqual(backend.EXTRA_MODELS, [])
        for value in ('', '0'):
            with self.subTest(value=value):
                os.environ['FLUX_PACKS_DISABLE'] = value
                structure_packs.reset()
                self.assertEqual(model_ids(), ['differential_inductor', 'disabled_model'])


class PacksCommandTests(PackTestCase):
    # The runner replaces the pack folder with an empty one so the repository's
    # own packs/ directory cannot change the outcome.
    RUNNER = ("import pathlib, sys; sys.path.insert(0, sys.argv.pop(1)); "
              "import structure_packs; structure_packs.PACK_DIR = pathlib.Path(sys.argv.pop(1)); "
              "import flux_cli; raise SystemExit(flux_cli.main())")

    def run_cli(self, *arguments, flux_packs='', disable=False, isolated=True):
        env = {key: value for key, value in os.environ.items() if not key.startswith('FLUX_PACKS')}
        env.update(FLUX_PACKS=str(flux_packs), PYTHONDONTWRITEBYTECODE='1')
        if disable:
            env['FLUX_PACKS_DISABLE'] = '1'
        if isolated:
            command = [sys.executable, '-B', '-c', self.RUNNER, str(ROOT), str(self.empty)] + list(arguments)
        else:
            command = [sys.executable, '-B', str(ROOT / 'flux_cli.py')] + list(arguments)
        return subprocess.run(command, cwd=str(self.tmp), env=env, capture_output=True, text=True, timeout=120)

    def test_exit_code_is_zero_for_healthy_packs_and_lists_them(self):
        completed = self.run_cli('packs', flux_packs=EXAMPLE_PACK)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        listed = json.loads(completed.stdout)
        self.assertEqual([(item['name'], item['models'], item['error']) for item in listed],
                         [('example-tapped-inductor', [EXAMPLE_MODEL], None)])
        self.assertNotIn(str(ROOT), completed.stdout)

    def test_exit_code_is_one_when_any_pack_fails_but_healthy_ones_are_still_listed(self):
        folder = self.tmp / 'cli_mixed'
        self.write('a_good.py', good_pack('cli_good_model'), folder)
        self.write('b_bad.py', 'def register(flux:\n', folder)
        completed = self.run_cli('packs', flux_packs=folder)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        listed = {item['name']: item for item in json.loads(completed.stdout)}
        self.assertEqual(listed['tmp-good']['models'], ['cli_good_model'])
        self.assertIn('SyntaxError', listed['b_bad']['error'])
        self.assertNotIn('Traceback', completed.stdout)
        self.assertNotIn(str(self.tmp), completed.stdout)

    def test_models_command_lists_built_in_and_pack_models(self):
        completed = self.run_cli('models', flux_packs=EXAMPLE_PACK)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual([item['id'] for item in json.loads(completed.stdout)],
                         ['differential_inductor', EXAMPLE_MODEL])

    def test_disabled_and_empty_discovery_exit_zero_with_an_empty_list(self):
        disabled = self.run_cli('packs', flux_packs=EXAMPLE_PACK, disable=True)
        self.assertEqual((disabled.returncode, json.loads(disabled.stdout)), (0, []))
        empty = self.run_cli('packs')
        self.assertEqual((empty.returncode, json.loads(empty.stdout)), (0, []))

    def test_the_real_script_runs_with_packs_disabled(self):
        completed = self.run_cli('packs', flux_packs=EXAMPLE_PACK, disable=True, isolated=False)
        self.assertEqual((completed.returncode, json.loads(completed.stdout)), (0, []))
        models = self.run_cli('models', disable=True, isolated=False)
        self.assertEqual([item['id'] for item in json.loads(models.stdout)], ['differential_inductor'])

    def test_preview_command_accepts_a_pack_model_and_rejects_a_missing_one(self):
        design = self.tmp / 'design.json'
        output = self.tmp / 'out.json'
        design.write_text(json.dumps({'model': EXAMPLE_MODEL, 'structure': 'wide', 'cell': 'CliCell',
                                      'parameters': {}, 'layers': {}}), encoding='utf-8')
        arguments = ('preview', '--technology', str(FIXTURES / 'generic_d.json'),
                     '--design', str(design), '--output', str(output))
        completed = self.run_cli(*arguments, flux_packs=EXAMPLE_PACK)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(output.read_text())['geometry']['metrics']['variant'], 'wide')
        missing = self.run_cli(*arguments, disable=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn('structure pack', missing.stderr)


if __name__ == '__main__':
    unittest.main()


class ReviewRegressionTests(PackTestCase):
    """Metadata validation, discovery errors and path redaction."""

    def test_malformed_metadata_is_rejected_and_models_list_still_works(self):
        base = next(item for item in backend.MODEL_DEFINITIONS if item['id'] == 'differential_inductor')
        cases = {'layer_roles': None, 'defaults': [], 'title': 3, 'emx_metrics': 'L',
                 'default_layers': [], 'role_labels': 'x'}
        for key, value in cases.items():
            with self.subTest(key=key):
                definition = copy.deepcopy(base)
                definition.update(id='bad_' + key, **{key: value})
                with self.assertRaises(ValueError):
                    backend.register_model(definition, lambda *args: None)
        numeric = copy.deepcopy(base)
        numeric['id'] = 'bad_range'
        numeric['parameter_schema'][0].pop('max')
        with self.assertRaises(ValueError):
            backend.register_model(numeric, lambda *args: None)
        self.point_at(self.empty)
        self.assertEqual([item['id'] for item in backend.models()], ['differential_inductor'])

    def test_pack_with_bad_layer_roles_is_reported_not_listed(self):
        self.write('bad_roles.py', '''
            PACK_NAME = "bad-roles"
            import copy
            def register(flux):
                definition = copy.deepcopy(flux.builtin_definitions[0])
                definition.update(id='bad_roles_model', layer_roles=None)
                flux.register_model(definition, lambda *args: None)
        ''')
        self.point_at(self.tmp / 'bad_roles.py')
        self.assertEqual([item['id'] for item in backend.models()], ['differential_inductor'])
        self.assertTrue(self.report()['bad-roles']['error'])

    def test_unreadable_search_folder_is_reported_and_other_packs_load(self):
        good = self.write('good.py', good_pack('still_loaded_model'))
        blocked = self.tmp / 'blocked'
        blocked.mkdir()
        self.point_at(blocked, good)
        real_iterdir = Path.iterdir

        def iterdir(path):
            if path == blocked:
                raise PermissionError(13, 'Permission denied', str(blocked))
            return real_iterdir(path)
        with mock.patch.object(Path, 'iterdir', iterdir):
            ids = [item['id'] for item in backend.models()]
        self.assertIn('still_loaded_model', ids)
        entry = self.report()['blocked']
        self.assertIn('PermissionError', entry['error'])
        self.assertNotIn(str(self.tmp), entry['error'])

    def test_exception_text_with_absolute_paths_is_redacted_in_the_report(self):
        secret = self.tmp / 'private' / 'settings.json'
        self.write('reads_config.py', '''
            PACK_NAME = "reads-config"
            def register(flux):
                open(%r).read()
        ''' % str(secret))
        self.point_at(self.tmp / 'reads_config.py')
        backend.models()
        error = self.report()['reads-config']['error']
        self.assertIn('settings.json', error)
        self.assertNotIn(str(self.tmp), error)
        self.assertEqual(structure_packs._public_error('ratio 1/2 stays'), 'ratio 1/2 stays')
