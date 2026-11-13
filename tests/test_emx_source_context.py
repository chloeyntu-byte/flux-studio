"""A campaign keeps the exact applied source while the layout editor changes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import backend
import cadence
from pack_support import EXAMPLE_MODEL, install_example_pack


class FrozenSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flux frozen source ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'cds.lib').write_text('DEFINE Design ../design\n')
        self.library = self.root / 'design'
        self.library.mkdir()
        self.runtime = dict(user='designer', workspace=str(self.workspace),
                            cds_lib=str(self.workspace / 'cds.lib'), virtuoso='/native/virtuoso',
                            environment={'LD_LIBRARY_PATH': '/native/only', 'PYTHONPATH': None},
                            env_file=None, helper_dirs=[])
        tech = dict(library='Design', library_path=str(self.library), technology='ActualTech',
                    technology_path=str(self.root / 'tech'), layers=[{'name': 'M1', 'order': 1},
                    {'name': 'M2', 'order': 2}, {'name': 'M3', 'order': 3}], vias=[])
        self.entry = dict(name='Design', workspace=str(self.workspace), cds_lib=self.runtime['cds_lib'],
                          technology='ActualTech', available=True, _tech=tech,
                          layers=tech['layers'], model_default_layers={})
        self.design = dict(model='differential_inductor', library='Design', cell='Candidate',
                           parameters={'turns': 2, 'width': 8}, layers={})
        for name, value in [('LIBRARIES', {'Design': self.entry}), ('RUNTIME', self.runtime)]:
            p = patch.object(backend, name, value)
            p.start()
            self.addCleanup(p.stop)

    def capture(self):
        return backend.capture_generation_context(self.design, expected_library=self.entry)

    def test_copy_contains_all_fields_types_layers_and_independent_optional_values(self):
        source = self.capture()
        fields = {f['name']: f for f in source['fields']}
        self.assertEqual(fields['turns']['type'], 'int')
        self.assertEqual(fields['center_tap']['type'], 'bool')
        self.assertIsNone(fields['pgs_spine_width']['current'])
        self.assertIn('layers.tap', fields)
        self.assertEqual(set(source['design']['layers']), set(source['definition']['layer_roles']))
        self.design['parameters']['turns'] = 7
        self.runtime['environment']['LD_LIBRARY_PATH'] = '/changed'
        self.entry['_tech']['layers'][0]['name'] = 'changed'
        self.assertEqual(source['design']['parameters']['turns'], 2)
        self.assertEqual(source['runtime']['environment']['LD_LIBRARY_PATH'], '/native/only')
        self.assertEqual(source['library']['_tech']['layers'][0]['name'], 'M1')

    def test_nonblocking_capture_during_context_read_and_mismatched_binding(self):
        with backend.CONTEXT_LOCK:
            with self.assertRaisesRegex(RuntimeError, 'loading'):
                self.capture()
        with self.assertRaisesRegex(ValueError, 'changed'):
            backend.capture_generation_context(self.design, dict(self.entry, workspace='/other'))
        self.runtime['cds_lib'] = '/other/cds.lib'
        with self.assertRaisesRegex(ValueError, 'no longer match'):
            self.capture()

    def test_preview_and_write_use_frozen_entry_and_runtime_after_same_named_library_switch(self):
        install_example_pack(self)
        source = self.capture()
        backend.LIBRARIES['Design'] = dict(self.entry, technology='OtherPDK', workspace='/other')
        backend.RUNTIME = dict(self.runtime, workspace='/other')
        result = {'ok': True, 'geometry': {'ports': []}, 'errors': []}
        with patch.object(backend, '_preview_entry', return_value=result) as preview, \
             patch('runtime_config.permitted_library', return_value=self.library), \
             patch.object(backend, 'write_cell', return_value={'ok': True}) as writer:
            backend.generate_in_context(self.design, source)
        self.assertEqual(preview.call_args[0][1]['technology'], 'ActualTech')
        self.assertEqual(writer.call_args[0][2], str(self.workspace))
        self.assertEqual(writer.call_args[1]['runtime']['workspace'], str(self.workspace))
        self.assertEqual(backend.RUNTIME['workspace'], '/other')
        with self.assertRaisesRegex(ValueError, 'differs'):
            backend.preview_in_context(dict(self.design, library='Wrong'), source)
        with self.assertRaisesRegex(ValueError, 'captured body model'):
            backend.preview_in_context(dict(self.design, model=EXAMPLE_MODEL), source)

    def test_existing_cell_is_never_passed_to_writer(self):
        source = self.capture()
        (self.library / 'Candidate').mkdir()
        with patch.object(backend, '_preview_entry', return_value={'ok': True}), \
             patch('runtime_config.permitted_library', return_value=self.library), \
             patch.object(backend, 'write_cell') as writer:
            with self.assertRaisesRegex(ValueError, 'never overwritten'):
                backend.generate_in_context(self.design, source)
        writer.assert_not_called()

    def test_ui_draft_cannot_seed_a_campaign(self):
        ui = app.TransformerApp.__new__(app.TransformerApp)
        ui.context_busy = ui.preview_busy = ui.generating = False
        ui.dirty = True
        ui.applied_design = self.design
        ui.preview_ok = True
        ui.preview_revision = ui.revision = 3
        ui.preview_library = self.entry
        ui.library = self.entry
        ui.geometry = {'ports': [
            {'name': 'L1', 'kind': 'signal', 'layer': 'M2', 'bbox': [[0, 0], [2, 2]]},
            {'name': 'GND_RING', 'kind': 'ground', 'layer': 'M1', 'bbox': [[4, 0], [6, 2]]},
        ]}
        with self.assertRaisesRegex(ValueError, 'Apply Parameters'):
            ui.emx_source()
        ui.dirty = False
        source = ui.emx_source()
        self.assertEqual(source['layout_revision'], 3)
        self.assertEqual(source['port_contract']['signals'], ['L1'])
        self.assertEqual(source['port_contract']['grounds'], ['GND_RING'])
        self.assertEqual([entry['driven'] for entry in source['port_contract']['definitions']], [True, False])
        ui.preview_revision = 2
        with self.assertRaisesRegex(ValueError, 'Apply Parameters'):
            ui.emx_source()

    def test_explicit_native_runtime_ignores_changed_global_environment(self):
        proc = Mock(returncode=0)
        proc.communicate.return_value = ('TFGEN_SUCCESS\n', None)
        job = self.root / 'job'
        job.mkdir()
        with patch.object(cadence, 'RUNTIME_ENV', {'LD_LIBRARY_PATH': '/wrong/gui'}), \
             patch.object(cadence, 'VIRTUOSO', '/wrong/virtuoso'), \
             patch.object(cadence, 'ENV_FILE', '/wrong/setup'), \
             patch.object(cadence.os, 'geteuid', return_value=1000), \
             patch.object(cadence.subprocess, 'Popen', return_value=proc) as spawn, \
             patch.object(cadence.runtime_config, 'native_environment', return_value={'PATH': '/native'}) as env:
            cadence.run_skill('printf("TFGEN_SUCCESS\\n")', self.workspace, job,
                              cds_lib=self.runtime['cds_lib'], runtime=self.runtime)
        self.assertEqual(env.call_args[0][1], self.runtime['environment'])
        self.assertIn('/native/virtuoso', spawn.call_args[0][0])
        self.assertNotIn('/wrong/virtuoso', spawn.call_args[0][0])
        self.assertNotIn('/wrong/setup', spawn.call_args[0][0])
        with self.assertRaisesRegex(ValueError, 'different workspace'):
            cadence.run_skill('', '/elsewhere', job, runtime=self.runtime)


if __name__ == '__main__':
    unittest.main()
