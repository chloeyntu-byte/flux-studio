"""Parameter-file and model-isolation checks without Cadence or OA writes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend
import cadence
from pack_support import EXAMPLE_MODEL, install_example_pack, isolate_packs, register_temporary_model


class ParameterFileTests(unittest.TestCase):
    def test_differential_inductor_is_the_default_and_only_built_in_model(self):
        self.assertEqual(backend.DEFAULT_MODEL, 'differential_inductor')
        isolate_packs(self, disable=True)
        self.assertEqual([m['id'] for m in backend.models()], ['differential_inductor'])
        model = backend.models()[0]
        self.assertNotIn('structures', model)
        normalized = backend.normalize_design({'model': backend.DEFAULT_MODEL})
        self.assertEqual(normalized['model'], 'differential_inductor')
        self.assertEqual(normalized['structure'], 'layout')

    def test_design_without_a_model_id_is_legacy_and_needs_the_pack(self):
        # Pre-model-id files always meant transformer_implicit; never silently
        # reinterpret them as the built-in differential inductor.
        isolate_packs(self, disable=True)
        with self.assertRaisesRegex(ValueError, 'transformer_implicit.*structure pack'):
            backend.normalize_design({})
        register_temporary_model(self, dict(
            id='transformer_implicit', title='Legacy', description='Synthetic stand-in.',
            defaults={'inner_width': 10}, layer_roles=['body', 'crossing'],
            parameter_schema=[{'name': 'inner_width', 'label': 'W', 'part': 'body',
                               'default': 10, 'min': 1, 'max': 50}]),
            lambda *args: {'ok': False, 'errors': [], 'warnings': [], 'parameters': {}, 'geometry': {}})
        self.assertEqual(backend.normalize_design({'parameters': {'inner_width': 12}})['model'], 'transformer_implicit')

    def test_unavailable_pack_model_is_reported_with_a_pack_hint(self):
        isolate_packs(self, disable=True)
        with self.assertRaisesRegex(ValueError, 'structure pack'):
            backend.normalize_design({'model': 'transformer_implicit'})

    def legacy_design(self, model='legacy_pair'):
        return {'library': 'generic_d', 'cell': 'OldParameters', 'model': model,
                'parameters': {'inner_diameter': 110, 'inner_width': 10, 'outer_width': 10,
                               'gap': 8, 'ring_gap': 20},
                'layers': {'body': 'M9', 'crossing': 'AP', 'pgs': 'M1', 'ring': 'AP'}}

    def register_pair_model(self):
        """A two-winding model supplied the way a structure pack would."""
        schema = [{'name': name, 'label': name, 'part': 'body', 'default': default, 'min': 0.1, 'max': 1000, 'step': 0.1}
                  for name, default in (('inner_diameter', 100), ('inner_width', 10), ('outer_width', 10), ('gap', 8))]
        definition = {'id': 'legacy_pair', 'title': 'Legacy pair', 'description': 'Test-only two-winding model.',
                      'defaults': {field['name']: field['default'] for field in schema},
                      'parameter_schema': schema, 'layer_roles': ['body', 'crossing']}
        register_temporary_model(self, definition, lambda *args: {'ok': False, 'errors': [], 'geometry': {}})

    def test_legacy_ring_migrates_once_without_rewriting_saved_body(self):
        isolate_packs(self, disable=True)
        self.register_pair_model()
        legacy = self.legacy_design()
        original = copy.deepcopy(legacy)
        normalized = backend.normalize_design(legacy)
        self.assertEqual(legacy, original)
        self.assertEqual(normalized['parameters']['ring_extent'], 103)
        self.assertEqual(normalized['model'], 'legacy_pair')
        normalized['parameters']['inner_diameter'] = 180
        again = backend.normalize_design(normalized)
        self.assertEqual(again['parameters']['ring_extent'], 103)
        self.assertNotIn('ring_gap', again['parameters'])

    def test_legacy_ring_migration_is_refused_for_the_single_winding_inductor(self):
        legacy = self.legacy_design('differential_inductor')
        legacy['parameters'] = {'turns': 2, 'ring_gap': 20}
        with self.assertRaisesRegex(ValueError, 'Legacy ring-gap migration'):
            backend.normalize_design(legacy)

    def test_file_contains_only_selected_body_and_shared_shields(self):
        design = self.legacy_design('differential_inductor')
        design['parameters'] = {'turns': 3, 'width': 7, 'inner_width': 11, 'primary_turns': 6,
                                'ring_extent': 150, 'ring_shape': 'octagon', 'pgs_extent': 95,
                                'pgs_shape': 'circle'}
        design['layers'].update(secondary='M7', ground_route='M2')
        with tempfile.TemporaryDirectory() as directory, patch.object(backend, 'PRESETS', Path(directory)):
            backend.save_preset('Inductor example', design)
            source = json.loads((Path(directory) / 'Inductor example.json').read_text())
            self.assertEqual(source['schema'], 'tfgen.design.v2')
            selected = source['design']
            self.assertNotIn('inner_width', selected['parameters'])
            self.assertNotIn('primary_turns', selected['parameters'])
            self.assertNotIn('secondary', selected['layers'])
            self.assertEqual(selected['parameters']['ring_extent'], 150)
            self.assertEqual(selected['parameters']['pgs_shape'], 'circle')
            self.assertEqual(backend.load_preset('Inductor example'), source)
            design['parameters']['turns'] = 4
            backend.save_preset('Inductor example', design)
            backups = list(Path(directory).glob('*.bak-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), source)

    def test_invalid_models_and_file_versions_rejected_without_side_effects(self):
        for design in ({'model': 'missing'}, {'parameters': []}, {'layers': []}):
            with self.assertRaises(ValueError):
                backend.normalize_design(design)
        with tempfile.TemporaryDirectory() as directory, patch.object(backend, 'PRESETS', Path(directory)):
            file = Path(directory) / 'Invalid.json'
            file.write_text(json.dumps({'schema': 'tfgen.design.v99', 'design': self.legacy_design('differential_inductor')}))
            with self.assertRaises(ValueError):
                backend.load_preset('Invalid')
            with self.assertRaises(ValueError):
                backend.save_preset('../Escape', self.legacy_design('differential_inductor'))

    def test_model_metadata_cannot_mutate_shared_defaults(self):
        install_example_pack(self)
        definitions = backend.models()
        self.assertEqual([m['id'] for m in definitions], ['differential_inductor', EXAMPLE_MODEL])
        definitions[0]['defaults']['pgs_extent'] = 999
        definitions[1]['parameter_schema'].clear()
        self.assertEqual(backend.models()[0]['defaults']['pgs_extent'], 95)
        self.assertEqual(backend.models()[1]['defaults']['pgs_extent'], 95)
        self.assertTrue(backend.models()[1]['parameter_schema'])
        definitions[1]['structures'][0]['id'] = 'tampered'
        self.assertEqual(backend.models()[1]['structures'][0]['id'], 'compact')

    def test_registered_models_are_validated_and_removable(self):
        isolate_packs(self, disable=True)
        definition = copy.deepcopy(backend.models()[0])
        for broken in (dict(definition, id='differential_inductor'), dict(definition, id='bad id!'),
                       dict(definition, structures=[]), dict(definition, id='x1', parameter_schema=[1])):
            with self.assertRaises(ValueError):
                backend.register_model(broken, lambda *args: None)
        with self.assertRaises(ValueError):
            backend.register_model(dict(definition, id='x2'), None)
        self.assertEqual([m['id'] for m in backend.models()], ['differential_inductor'])
        backend.register_model(dict(definition, id='temp_model'), lambda *args: None)
        self.addCleanup(backend.unregister_model, 'temp_model')
        self.assertIn('temp_model', [m['id'] for m in backend.models()])
        backend.unregister_model('temp_model')
        self.assertNotIn('temp_model', [m['id'] for m in backend.models()])
        backend.unregister_model('differential_inductor')
        self.assertEqual([m['id'] for m in backend.models()], ['differential_inductor'])

    def test_variants_normalize_against_the_selected_models_structures(self):
        install_example_pack(self)
        self.assertEqual(backend.normalize_design({'model': EXAMPLE_MODEL})['structure'], 'compact')
        self.assertEqual(backend.normalize_design({'model': EXAMPLE_MODEL, 'structure': 'wide'})['structure'], 'wide')
        with self.assertRaisesRegex(ValueError, 'routing variant'):
            backend.normalize_design({'model': EXAMPLE_MODEL, 'structure': 'layout'})
        # Models without variants ignore the field.
        self.assertEqual(backend.normalize_design({'model': backend.DEFAULT_MODEL, 'structure': 'wide'})['structure'], 'layout')


class WorkspaceDiscoveryTests(unittest.TestCase):
    """Portable binding and authorization with mocked native reads; no OA writes."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='flux-workspace-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'workspace with spaces'
        self.workspace.mkdir()
        self.mapping = self.workspace / 'alternate.lib'
        self.mapping.write_text('DEFINE PortableDesign ../external_design\n')
        self.library = self.root / 'external_design'
        self.library.mkdir()
        self.tech_directory = self.root / 'technology'
        self.tech_directory.mkdir()
        self.technology = {'library': 'PortableDesign', 'library_path': str(self.library),
                           'technology': 'PortableTech', 'technology_path': str(self.tech_directory),
                           'cds_lib': str(self.mapping), 'grid': 0.005,
                           'layers': [{'name': 'M1', 'order': 1}, {'name': 'M2', 'order': 2}, {'name': 'M3', 'order': 3}],
                           'vias': []}
        for name, value in [('LIBRARIES', {}), ('LIBRARY_SCOPE', None), ('RUNTIME', None), ('LAUNCH_CONTEXT', None)]:
            patcher = patch.object(backend, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = {'workspace': str(self.workspace), 'cds_lib': str(self.mapping),
                        'virtuoso': '/existing/cadence/virtuoso', 'user': 'designer',
                        'environment': {'CDS_LIC_FILE': 'private-value'}, 'env_file': None,
                        'source': 'running Virtuoso', 'helper_dirs': []}
        for name, replacement in [('configure_context', lambda **kwargs: copy.deepcopy(self.runtime)),
                                  ('job_dir', lambda label: self.root / label)]:
            patcher = patch.object(cadence, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(backend, 'load_technology', return_value=copy.deepcopy(self.technology))
        self.native = patcher.start()
        self.addCleanup(patcher.stop)

    def test_standalone_uses_native_current_catalog_not_historical_sources(self):
        catalog = [{'name': 'PortableDesign', 'library_path': str(self.library),
                    'technology': 'PortableTech', 'technology_path': str(self.tech_directory), 'is_technology': False},
                   {'name': 'PortableTech', 'library_path': str(self.tech_directory), 'technology': 'PortableTech', 'is_technology': True},
                   {'name': 'NoTech', 'library_path': str(self.root), 'technology': None, 'is_technology': False}]
        with patch.object(cadence, 'list_libraries', return_value=catalog) as enumerate_libraries, patch.object(backend, 'SOURCES', [('NeverLoad', '/nonexistent')], create=True):
            result = backend.load_context()
        enumerate_libraries.assert_called_once_with(str(self.workspace), cds_lib=str(self.mapping))
        self.assertEqual([item['name'] for item in result['libraries']], ['PortableDesign'])
        self.assertEqual(self.native.call_args.args[0], 'PortableDesign')
        self.assertNotIn('environment', result['runtime'])
        self.assertNotIn('private-value', json.dumps(result))

    def test_layout_source_loads_only_its_bound_library_and_exact_path(self):
        source = {'schema': 'tfgen.layout.context.v1', 'library': 'PortableDesign', 'cell': 'SourceCell',
                  'view': 'layout', 'workspace': str(self.workspace), 'cds_lib': str(self.mapping),
                  'library_path': str(self.library), 'technology': 'PortableTech'}
        with patch.object(cadence, 'list_libraries', side_effect=AssertionError('Context must not enumerate another workspace')):
            result = backend.load_context(source)
        self.assertTrue(result['libraries'][0]['available'])
        self.assertEqual(result['selected_library'], 'PortableDesign')
        self.assertEqual(backend.LIBRARIES['PortableDesign']['_tech']['_permission_context']['library_path'], str(self.library))
        broken = dict(source, library_path=str(self.root))
        result = backend.load_context(broken)
        self.assertFalse(result['libraries'][0]['available'])
        self.assertIn('different library paths', result['libraries'][0]['error'])

    def test_public_context_retains_provenance_and_keeps_runtime_environment_private(self):
        source = {'schema': 'tfgen.layout.context.v1', 'library': 'PortableDesign', 'cell': 'SourceCell',
                  'view': 'layout', 'workspace': str(self.workspace), 'cds_lib': str(self.mapping),
                  'library_path': str(self.library), 'technology': 'PortableTech',
                  'environment': {'CDS_LIC_FILE': 'private-test-value'},
                  'source_process': {'pid': 123, 'environment': {'PATH': 'private-test-path'}}}
        original = copy.deepcopy(source)
        with patch.object(cadence, 'list_libraries', side_effect=AssertionError('Context must not enumerate')):
            result = backend.load_context(source)
        self.assertEqual(source, original)
        self.assertEqual(backend.LAUNCH_CONTEXT['environment'], source['environment'])
        exported = backend.public_source_context(source)
        self.assertEqual(result['source_context'], exported)
        self.assertEqual(exported['library_path'], str(self.library))
        self.assertNotIn('source_process', exported)
        self.assertNotIn('environment', exported)
        self.assertNotIn('private-test-value', json.dumps(result))
        self.assertNotIn('private-test-path', json.dumps(exported))

    def test_manual_mapping_may_be_outside_home_and_scope_switch_clears_cache(self):
        first = backend.register_library('PortableDesign', str(self.workspace), 'alternate.lib')
        self.assertTrue(first['available'])
        self.assertEqual(first['cds_lib'], str(self.mapping))
        second = self.root / 'other workspace'
        second.mkdir()
        (second / 'cds.lib').write_text('DEFINE PortableDesign ../external_design\n')
        backend.LIBRARIES['StaleInstallation'] = {'name': 'StaleInstallation'}
        self.technology['cds_lib'] = str(second / 'cds.lib')
        self.native.return_value = copy.deepcopy(self.technology)
        self.runtime.update(workspace=str(second), cds_lib=str(second / 'cds.lib'))
        result = backend.register_library('PortableDesign', str(second))
        self.assertEqual([item['name'] for item in result['libraries']], ['PortableDesign'])
        self.assertNotIn('StaleInstallation', backend.LIBRARIES)

    def test_generation_reuses_exact_native_permission_and_preserves_existing_cell(self):
        backend.register_library('PortableDesign', str(self.workspace), str(self.mapping))
        design = {'library': 'PortableDesign', 'cell': 'FreshCell', 'model': backend.DEFAULT_MODEL}
        valid = {'ok': True, 'errors': [], 'geometry': {}}
        with patch.object(backend, 'preview', side_effect=lambda *args: copy.deepcopy(valid)), patch.object(backend, 'write_cell', return_value={'ok': True}) as writer:
            self.assertTrue(backend.generate(design)['ok'])
            writer.assert_called_once()
            (self.library / 'FreshCell').mkdir()
            result = backend.generate(design)
            self.assertIn('cell_exists', {e['code'] for e in result['errors']})
            self.assertEqual(writer.call_count, 1)

    def test_protected_native_technology_is_rejected_before_it_can_be_registered(self):
        self.native.return_value = dict(self.technology, library='PortableDesign', technology='PortableDesign')
        with self.assertRaisesRegex(ValueError, 'Technology libraries'):
            backend.register_library('PortableDesign', str(self.workspace), str(self.mapping))
        self.assertNotIn('PortableDesign', backend.LIBRARIES)

    def test_empty_design_catalog_returns_actionable_workspace_error(self):
        with patch.object(cadence, 'list_libraries', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'flux_config.sh'):
                backend.load_context()

    def test_relative_launch_context_uses_original_invocation_folder(self):
        from app import read_launch_context
        invocation = self.root / "designer's workspace with spaces"
        invocation.mkdir()
        filename = "editor's context.json"
        source = {'schema': 'tfgen.layout.context.v1', 'library': 'PortableDesign', 'cell': 'SourceCell',
                  'view': 'layout', 'workspace': str(invocation)}
        (invocation / filename).write_text(json.dumps(source))
        with patch.dict(os.environ, {'TFGEN_LAUNCH_CWD': str(invocation)}):
            self.assertEqual(read_launch_context(filename), source)
            self.assertEqual(read_launch_context(str(invocation / filename)), source)

    def test_relative_mapping_and_provenance_preserve_quoted_paths(self):
        workspace = self.root / "engineer's workspace"
        workspace.mkdir()
        mapping = workspace / "designer's mapping.lib"
        mapping.write_text('DEFINE PortableDesign ../external_design\n')
        self.runtime.update(workspace=str(workspace), cds_lib=str(mapping))
        self.native.return_value = dict(self.technology, cds_lib=str(mapping))
        result = backend.register_library('PortableDesign', str(workspace), mapping.name)
        self.assertEqual(result['cds_lib'], str(mapping))
        self.native.assert_called_once_with('PortableDesign', str(workspace), self.root / 'technology', cds_lib=str(mapping))
        source = {'workspace': str(workspace), 'cds_lib': str(mapping), 'library_path': str(self.library)}
        self.assertEqual(backend.public_source_context(source), source)


if __name__ == '__main__':
    unittest.main()
