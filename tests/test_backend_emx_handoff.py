"""The optimizer must consume verified saved geometry, never an editable draft."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backend
import emx_adapter


class SavedEMXHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flux saved layout ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.geometry = {'ports': [{'name': 'CTAP', 'kind': 'tap', 'access_direction': 'top'}],
                         'polygons': [], 'vias': []}
        (self.root / 'geometry.json').write_text(json.dumps({'geometry': self.geometry}))
        self.generated = dict(ok=True, library='PortableDesign', cell='SavedCell', run_dir=str(self.root),
                              geometry_hash=hashlib.sha256(json.dumps(self.geometry, sort_keys=True).encode()).hexdigest())
        self.entry = dict(available=True, technology='ExactAttachedTech', workspace=str(self.root / 'workspace'),
                          cds_lib=str(self.root / 'workspace' / 'cds.lib'),
                          _tech={'library_path': str(self.root / 'external library')})

    def test_preserves_exact_library_binding_and_saved_tap_contract_without_submitting(self):
        expected = copy.deepcopy(self.generated)
        config = {'fixture': 'native configuration'}
        with patch.dict(backend.LIBRARIES, {'PortableDesign': self.entry}, clear=True), \
                patch.object(emx_adapter, 'discover_config', return_value=config) as discover, \
                patch.object(emx_adapter, 'prepare_job', return_value={'status': 'prepared'}) as prepare, \
                patch.object(emx_adapter, 'run_job') as submit:
            result = backend.prepare_emx_job(self.generated, overrides={'frequencies_hz': [1e8]}, run_dir=self.root / 'future job')
        self.assertEqual(result, {'status': 'prepared'})
        self.assertEqual(self.generated, expected)
        source, geometry, _, directory = prepare.call_args[0]
        self.assertEqual(source['library_path'], self.entry['_tech']['library_path'])
        self.assertEqual(source['cds_lib'], self.entry['cds_lib'])
        self.assertEqual(source['technology'], 'ExactAttachedTech')
        self.assertEqual(source['cell'], 'SavedCell')
        self.assertEqual(geometry, self.geometry)
        self.assertEqual(directory, self.root / 'future job')
        discover.assert_called_once()
        submit.assert_not_called()

    def test_changed_or_symlinked_saved_contract_is_rejected(self):
        with patch.dict(backend.LIBRARIES, {'PortableDesign': self.entry}, clear=True):
            file = self.root / 'geometry.json'
            file.write_text(json.dumps({'geometry': dict(self.geometry, vias=[{'changed': True}])}))
            with self.assertRaisesRegex(ValueError, 'differs'):
                backend.prepare_emx_job(self.generated)
            file.rename(self.root / 'other.json')
            file.symlink_to(self.root / 'other.json')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                backend.prepare_emx_job(self.generated)

    def test_unsaved_failed_or_unloaded_design_cannot_start_an_emx_handoff(self):
        with patch.dict(backend.LIBRARIES, {}, clear=True):
            for value in (None, {}, dict(self.generated, ok=False), self.generated):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    backend.prepare_emx_job(value)


if __name__ == '__main__':
    unittest.main()
