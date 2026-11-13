import unittest

import numpy as np

from snpfit.coupling import coupling_values, evaluate_coupling, k_reference
from snpfit.network import Network, z_to_s
from snpfit.stable_network import simulate_stable


def circuit(k=.6, dm=False):
    ports=['P1','P2','S1','S2'] if dm else ['P','S']
    a,b,c,d = ('P1','P2','S1','S2') if dm else ('P','REF','S','REF')
    elements=[
        {'kind':'res','name':'r1','n1':a,'n2':'a','value':2.},
        {'kind':'ind','name':'l1','n1':'a','n2':b,'value':1e-9},
        {'kind':'res','name':'r2','n1':c,'n2':'b','value':3.},
        {'kind':'ind','name':'l2','n1':'b','n2':d,'value':4e-9},
        {'kind':'mind','name':'m','ind1':'l1','ind2':'l2','value':k}]
    if dm:
        elements.extend([{'kind':'res','name':'rg1','n1':'P2','n2':'REF','value':1e10},
                         {'kind':'res','name':'rg2','n1':'S2','n2':'REF','value':1e10}])
    return {'name':'manual_coupled','ports':ports,'reference':'REF','elements':elements}


def ideal_reference(k=.6):
    f=np.arange(1,6)*1e9
    w=2*np.pi*f
    z=np.zeros((len(f),2,2),complex)
    z[:,0,0]=2+1j*w*1e-9
    z[:,1,1]=3+1j*w*4e-9
    z[:,0,1]=z[:,1,0]=1j*w*k*2e-9
    return Network(f,z_to_s(z,[50,50]),50)


class CouplingPhysics(unittest.TestCase):
    def test_closed_form_effective_k_and_parameter_error(self):
        source=ideal_reference()
        ref=k_reference(source,'transformer_se_gnd')
        self.assertTrue(ref['resolved'])
        self.assertEqual(ref['valid_indices'],list(range(5)))
        np.testing.assert_allclose(ref['source_k'],.6,rtol=1e-12)
        exact=evaluate_coupling(circuit(),source,'transformer_se_gnd',5,ref)
        self.assertTrue(exact['passed'])
        self.assertLess(exact['score'],1e-9)
        changed=evaluate_coupling(circuit(.66),source,'transformer_se_gnd',5,ref)
        self.assertFalse(changed['passed'])
        self.assertAlmostEqual(changed['max_relative_pct'],10.,places=9)

    def test_polarity_is_not_hidden_by_absolute_k(self):
        source=ideal_reference()
        wrong=evaluate_coupling(circuit(-.6),source,'transformer_se_gnd',5)
        self.assertFalse(wrong['passed'])
        self.assertAlmostEqual(wrong['max_relative_pct'],200.,places=9)

    def test_reference_domain_is_frozen_and_candidate_cannot_drop_points(self):
        source=ideal_reference()
        srf={'branches':{'primary':{'bracket_hz':[3.5e9,3.51e9]},
                         'secondary':{'bracket_hz':[4e9,4.01e9]}}}
        ref=k_reference(source,'transformer_se_gnd',srf)
        self.assertEqual(ref['valid_indices'],[0,1])
        bad=circuit()
        bad['elements']=[e for e in bad['elements'] if e['kind']!='mind']
        for e in bad['elements']:
            if e['name']=='l1':e['kind']='cap';e['value']=1e-13
        result=evaluate_coupling(bad,source,'transformer_se_gnd',5,ref)
        self.assertFalse(result['passed'])
        self.assertEqual(result['invalid_candidate_count'],2)
        self.assertEqual(result['valid_indices'],[0,1])

    def test_zero_coupling_stays_in_domain_with_absolute_limit(self):
        source=ideal_reference(0.)
        ref=k_reference(source,'transformer_se_gnd')
        self.assertEqual(len(ref['valid_indices']),5)
        self.assertEqual(ref['near_zero_reference_count'],5)
        exact=evaluate_coupling(circuit(0.),source,'transformer_se_gnd',5,ref)
        wrong=evaluate_coupling(circuit(.01),source,'transformer_se_gnd',5,ref)
        self.assertTrue(exact['passed'])
        self.assertFalse(wrong['passed'])
        self.assertGreater(wrong['absolute_limit'],0)
        self.assertIsNone(wrong['max_relative_pct'])
        self.assertFalse(wrong['near_zero_absolute_pass'])

    def test_source_identity_and_inductor_not_applicable(self):
        source=ideal_reference()
        ref=k_reference(source,'transformer_se_gnd')
        changed=Network(source.freq,source.s*.999,source.z0)
        with self.assertRaisesRegex(ValueError,'identity'):
            evaluate_coupling(circuit(),changed,'transformer_se_gnd',5,ref)
        single=k_reference(source,'inductor_se_gnd')
        self.assertFalse(single['applicable'])
        self.assertTrue(evaluate_coupling(circuit(),source,'inductor_se_gnd',5,single)['passed'])

    def test_differential_fixture_and_internal_mind_distinction(self):
        f=np.arange(1,6)*1e9
        c=circuit(.6,True)
        source=Network(f,simulate_stable(c,f,50),50)
        values=coupling_values(source.s,source.z0,source.freq,'transformer_dm_gnd')
        np.testing.assert_allclose(values['k'],.6,rtol=1e-7)
        ref=k_reference(source,'transformer_dm_gnd')
        self.assertFalse(ref['metric_is_internal_mind_coefficient'])
        self.assertTrue(evaluate_coupling(c,source,'transformer_dm_gnd',5,ref)['passed'])


if __name__=='__main__':unittest.main()
