"""The public example must never discover or invoke a native tool."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import backend
from technology import load_profile

ROOT = Path(__file__).resolve().parents[1]

class OfflineProfileTests(unittest.TestCase):
    def test_example_previews_without_native_calls_and_cannot_write_oa(self):
        with patch('cadence.run_skill', side_effect=AssertionError('native call forbidden')):
            context = backend.load_file_context(ROOT/'examples/generic_technology.json')
            design = json.loads((ROOT/'examples/layout_design.json').read_text())
            design['library'] = context['selected_library']
            result = backend.preview(design)
            self.assertTrue(result['ok'], result['errors'])
            self.assertTrue(result['geometry']['polygons'])
            with self.assertRaisesRegex(ValueError, 'JSON geometry export only'):
                backend.generate(design)
        backend.LIBRARIES.pop(context['selected_library'], None)

    def test_invalid_via_is_rejected_before_geometry(self):
        data = json.loads((ROOT/'examples/generic_technology.json').read_text())
        data['vias'][0]['upper'] = 'UNKNOWN'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'technology.json'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'configured metals'):
                load_profile(path)

    def test_synthetic_campaign_hydrates_from_retained_relative_snps(self):
        from result_data import load_campaign, hydrate_row
        snapshot = load_campaign(ROOT/'examples/campaign')
        self.assertTrue(snapshot['source']['offline'])
        self.assertEqual(len(snapshot['rows']), 9)
        for row in snapshot['rows']:
            actual = hydrate_row(row)
            self.assertNotIn('data_error', actual)
            self.assertEqual(len(actual['metrics']), 41)
            self.assertGreater(actual['metrics'][0]['Qp'], 0)
