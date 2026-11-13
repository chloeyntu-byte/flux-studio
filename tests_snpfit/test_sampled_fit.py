import tempfile
from pathlib import Path
import unittest
import numpy as np
from snpfit.sampled_fit import SampledNetwork,fit_sampled,verify_native_sampled
from snpfit.network import read_touchstone,write_touchstone
from snpfit.stable_network import simulate_stable


class SampledFitTests(unittest.TestCase):
    def source(self,d,frequencies):
        circuit={'name':'fixture','ports':['P','N'],'reference':'REF','metadata':{'model_id':'inductor_dm'},
                 'elements':[{'kind':'res','name':'r','n1':'P','n2':'rl','value':1.},
                             {'kind':'ind','name':'l','n1':'rl','n2':'N','value':.8e-9},
                             {'kind':'cap','name':'cp','n1':'P','n2':'REF','value':1e-16},
                             {'kind':'cap','name':'cn','n1':'N','n2':'REF','value':1e-16}]}
        net=SampledNetwork(np.asarray(frequencies),simulate_stable(circuit,frequencies,[50,50]),[50,50],['P','N'])
        file=Path(d)/'source.s2p';write_touchstone(net,file)
        cfg=dict(source_path=str(file),model_id='inductor_dm',port_order=[1,2],ports_confirmed=True,short_unused=False,l_pct=5,q_pct=20)
        return file,cfg

    def test_single_point_is_viewable_but_not_fittable(self):
        with tempfile.TemporaryDirectory() as d:
            file,cfg=self.source(d,[20e9])
            with self.assertRaises(ValueError):read_touchstone(file)
            self.assertEqual(len(read_touchstone(file,network_class=SampledNetwork).freq),1)
            with self.assertRaisesRegex(ValueError,'at least two actual simulated frequencies'):
                fit_sampled(cfg,Path(d)/'fit')
            self.assertFalse((Path(d)/'fit').exists())

    def test_two_actual_points_are_kept_without_synthetic_samples(self):
        with tempfile.TemporaryDirectory() as d:
            file,cfg=self.source(d,[1e9,20e9])
            result=fit_sampled(cfg,Path(d)/'fit')
            self.assertTrue(result['passed'],result['metrics'])
            self.assertEqual(result['frequencies_hz'],[1e9,20e9])
            self.assertEqual(len(result['comparison']['original']['L']),2)
            self.assertFalse(result['broadband_qualified'])
            self.assertEqual(result['acceptance_scope'],'original_sampled_points_only')
            self.assertTrue(verify_native_sampled(result,Path(d)/'fit/fitted.s2p')['passed'])

    def test_complete_supplied_frequency_list_is_qualified(self):
        with tempfile.TemporaryDirectory() as d:
            freq=[1e9,5e9,10e9,15e9,20e9,25e9,30e9]
            file,cfg=self.source(d,freq);result=fit_sampled(cfg,Path(d)/'fit')
            self.assertTrue(result['passed'])
            self.assertEqual(result['frequencies_hz'],freq)
            self.assertLessEqual(result['metrics']['l_error_pct'],5)
            self.assertLessEqual(result['metrics']['q_error_pct'],20)
            cfg['source_sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'Source SNP changed'):fit_sampled(cfg,Path(d)/'bad')

    def test_band_selecting_one_point_is_not_fittable(self):
        with tempfile.TemporaryDirectory() as d:
            _, cfg = self.source(d, [1e9, 20e9])
            cfg.update(fmin_ghz=20, fmax_ghz=20)
            with self.assertRaisesRegex(ValueError, 'at least two actual simulated frequencies'):
                fit_sampled(cfg, Path(d)/'fit')
            self.assertFalse((Path(d)/'fit').exists())

    def test_synthetic_tap_requires_frequency_dependent_loss(self):
        # Four synthetic points from an explicit RL + skin branch formula.
        # Series RL cannot reproduce frequency-dependent loss.
        file = Path(__file__).parent/'fixtures'/'tap_synthetic_four_point.s3p'
        cfg = dict(source_path=str(file), model_id='inductor_dm_tap', port_order=[1,2,3],
                   ports_confirmed=True, short_unused=False, l_pct=5, q_pct=20)
        with tempfile.TemporaryDirectory() as d:
            result = fit_sampled(cfg, Path(d)/'fit')
        seeds = [a for a in result['attempts'] if a['state']=='seed']
        optimized = [a for a in result['attempts'] if a['state']=='parameter_fit']
        self.assertTrue(seeds)
        self.assertIn('series_rl', {a['candidate'] for a in seeds})
        self.assertTrue(optimized)
        series = next(a for a in seeds if a['candidate'] == 'series_rl')
        self.assertFalse(series['metrics']['passed'])
        self.assertTrue(result['passed'], result['metrics'])
        self.assertLessEqual(result['metrics']['l_error_pct'], 5)
        self.assertLessEqual(result['metrics']['q_error_pct'], 20)
        self.assertEqual(result['frequencies_hz'], [1e9,5e9,10e9,20e9])

    def test_wrong_or_duplicate_mapping_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            file,cfg=self.source(d,[1e9,20e9]);cfg['port_order']=[1,1]
            with self.assertRaises(ValueError):fit_sampled(cfg,Path(d)/'fit')

    def test_replay_must_have_same_original_points(self):
        with tempfile.TemporaryDirectory() as d:
            file,cfg=self.source(d,[1e9,20e9]);result=fit_sampled(cfg,Path(d)/'fit')
            other=read_touchstone(Path(d)/'fit/fitted.s2p',network_class=SampledNetwork)
            other.freq=np.array([1e9,21e9])
            with self.assertRaisesRegex(ValueError,'frequencies or ports differ'):verify_native_sampled(result,other)
