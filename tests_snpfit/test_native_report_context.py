import copy
import unittest
from unittest.mock import patch

from snpfit.studio_engine import _visual_plan


class NativeReportContextTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(model_id='inductor_dm_gnd', fmin_ghz=1, fmax_ghz=50,
                        port_order=[3, 1], short_unused=True, q_annotation_ghz='')
        self.source = dict(reference_ohm=[30, 40, 50], selected_band_hz=[2e9, 40e9])
        self.circuit = dict(metadata={})

    def build(self):
        with patch('snpfit.winding_visual.build_visual', return_value={'paths': ['unchanged'], 'symbol': {'paths': ['unchanged']}}):
            return _visual_plan(self.circuit, self.cfg, self.source)

    def test_actual_source_mapping_and_band_are_preserved(self):
        before = copy.deepcopy((self.cfg, self.source, self.circuit))
        plan = self.build()
        self.assertEqual(plan['native_report_context'], dict(
            frequencies_hz=[5e9, 10e9, 20e9], fit_band_hz=[2e9, 40e9], z0=[50, 30], fixture='studio'))
        self.assertEqual(plan['paths'], ['unchanged'])
        self.assertEqual(plan['symbol'], {'paths': ['unchanged']})
        self.assertEqual((self.cfg, self.source, self.circuit), before)

    def test_sampled_profile_keeps_open_fixture_and_mapped_z0(self):
        self.circuit['metadata']['acceptance_scope'] = 'original_sampled_points_only'
        self.cfg['native_report_z0_ohm'] = [45, 65]
        self.source = dict(sampled_band_hz=[3e9, 12e9])
        context = self.build()['native_report_context']
        self.assertEqual(context['fixture'], 'sampled_open')
        self.assertEqual(context['z0'], [45, 65])
        self.assertEqual(context['fit_band_hz'], [3e9, 12e9])

    def test_backend_frequency_override_is_presentation_only(self):
        self.cfg['native_report_frequencies_ghz'] = [2, 8, 24]
        self.assertEqual(self.build()['native_report_context']['frequencies_hz'], [2e9, 8e9, 24e9])

    def test_invalid_frequency_requests_are_rejected(self):
        for value in ([], [0, 10], [float('nan')], [float('inf')], [True], [5, 5], '5,10,20', list(range(1, 8))):
            with self.subTest(value=value):
                self.cfg['native_report_frequencies_ghz'] = value
                with self.assertRaisesRegex(ValueError, 'Native report frequencies'):
                    self.build()


if __name__ == '__main__':
    unittest.main()
