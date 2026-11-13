import unittest

from snpfit.integration_api import _comparison


class IntegrationComparisonTest(unittest.TestCase):
    def test_inductance_is_nanohenries_and_gaps_are_preserved(self):
        result = _comparison({'freq_hz': [1e9, 2e9], 'branches': {
            'primary': {'source_l_h': [3e-10, None], 'fit_l_h': [4e-10, None],
                        'source_q': [4, None], 'fit_q': [5, None]},
            'secondary': {'source_l_h': [8e-10, None], 'fit_l_h': [9e-10, None],
                          'source_q': [6, None], 'fit_q': [7, None]}}})
        self.assertAlmostEqual(result['original']['Lp'][0], .3)
        self.assertAlmostEqual(result['fitted']['Lp'][0], .4)
        self.assertAlmostEqual(result['original']['Ls'][0], .8)
        self.assertEqual(result['original']['Qp'], [4, None])
        self.assertIsNone(result['fitted']['Ls'][1])
        self.assertEqual(result['frequency_hz'], [1e9, 2e9])


if __name__ == '__main__':
    unittest.main()
