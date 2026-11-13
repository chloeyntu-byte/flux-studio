"""Native annotation metrics are complete model responses, with explicit fixtures."""
import copy
import json
import unittest
import numpy as np

from snpfit.native_metrics import build_report
from snpfit.models import get_model, list_models
from snpfit.fitting import _measure_s
from snpfit.sampled_fit import _measure_s as _sampled_measure
from snpfit.coupling import coupling_values
from snpfit.stable_network import simulate_stable
from test_winding_visual import circuit_for


def rl(model='inductor_se_gnd', resistance=2.):
    elements = [{'name':'lw0_0','kind':'ind','n1':'mid' if resistance else 'P','n2':'N','value':1e-9}]
    if resistance:
        elements.insert(0,{'name':'rw0_0','kind':'res','n1':'P','n2':'mid','value':resistance})
    return {'name':'inductor','ports':['P','N'],'reference':'REF','elements':elements,
            'metadata':{'model_id':model,'selected_band_hz':[1e9,15e9]}}


def transformer():
    return {'name':'transformer','ports':['P','S'],'reference':'REF',
            'metadata':{'model_id':'transformer_se_gnd'},'elements':[
                {'name':'rw0_0','kind':'res','n1':'P','n2':'pm','value':2.},
                {'name':'lw0_0','kind':'ind','n1':'pm','n2':'REF','value':1e-9},
                {'name':'rw1_0','kind':'res','n1':'S','n2':'sm','value':3.},
                {'name':'lw1_0','kind':'ind','n1':'sm','n2':'REF','value':2e-9},
                {'name':'k01','kind':'mind','ind1':'lw0_0','ind2':'lw1_0','value':-.6}]}


def rows(report):
    return {item['key']: item['cells'] for item in report['rows']}


class NativeMetrics(unittest.TestCase):
    def test_default_columns_closed_form_and_no_mutation(self):
        circuit=rl();before=copy.deepcopy(circuit);report=build_report(circuit)
        self.assertEqual(circuit,before)
        self.assertEqual(report['frequencies_hz'],[5e9,10e9,20e9])
        self.assertEqual([c['label'] for c in report['columns']],['5 GHz','10 GHz','20 GHz'])
        self.assertEqual([c['in_fit_band'] for c in report['columns']],[True,True,False])
        result=rows(report)
        np.testing.assert_allclose([c['value'] for c in result['l_winding']],1.,rtol=2e-13)
        np.testing.assert_allclose([c['value'] for c in result['q_winding']],2*np.pi*np.array([5,10,20])/2,rtol=2e-13)
        self.assertTrue(all(c['status']=='not_applicable' and c['text']=='N/A' for c in result['k']))
        self.assertTrue(any('Outside fitted band' in n for n in report['notes']))
        json.dumps(report,allow_nan=False)

    def test_signed_transformer_coupling_is_not_clipped(self):
        report=build_report(transformer());result=rows(report)
        np.testing.assert_allclose([c['value'] for c in result['l_primary']],1.,rtol=2e-13)
        np.testing.assert_allclose([c['value'] for c in result['l_secondary']],2.,rtol=2e-13)
        np.testing.assert_allclose([c['value'] for c in result['k']],-.6,rtol=2e-13)
        self.assertEqual([r['label'] for r in report['rows']],['Lp (nH)','Qp','Ls (nH)','Qs','k (signed)'])

    def test_above_resonance_negative_l_q_and_undefined_k(self):
        circuit=transformer()
        circuit['elements'].append({'name':'cp','kind':'cap','n1':'P','n2':'REF','value':.2e-12})
        report=build_report(circuit);result=rows(report)
        self.assertLess(result['l_primary'][-1]['value'],0)
        self.assertLess(result['q_primary'][-1]['value'],0)
        self.assertEqual(result['k'][-1]['status'],'noninductive')
        self.assertIsNone(result['k'][-1]['value'])
        json.dumps(report,allow_nan=False)

    def test_ideal_lossless_and_finite_extreme_q_are_distinct(self):
        for model in ('inductor_se_gnd','inductor_dm_gnd'):
            report=build_report(rl(model,0))
            self.assertTrue(all(c['status']=='lossless' and c['text']=='Inf' for c in rows(report)['q_winding']))
            json.dumps(report,allow_nan=False)
        report=build_report(rl(resistance=1e-6))
        self.assertTrue(all(c['status']=='finite' and c['value']>1e7 for c in rows(report)['q_winding']))

    def test_all_models_match_existing_studio_and_sampled_fixtures(self):
        frequencies=np.asarray([5e9,10e9,20e9])
        for spec in list_models():
            circuit=circuit_for(spec.id)
            for fixture,measure in (('studio',_measure_s),('sampled_open',_sampled_measure)):
                with self.subTest(model=spec.id,fixture=fixture):
                    z0=np.full(spec.nports,75.)
                    report=build_report(circuit,fixture=fixture,z0=z0)
                    measured,z=measure(simulate_stable(circuit,frequencies,z0),z0,frequencies,spec)
                    actual=rows(report)
                    self.assertEqual(report['status'],'evaluated')
                    for name,curve in measured.items():
                        np.testing.assert_allclose([c['value'] for c in actual['l_'+name]],curve['l_h']*1e9,rtol=2e-12,atol=1e-12)
                        np.testing.assert_allclose([c['value'] for c in actual['q_'+name]],curve['q'],rtol=2e-12,atol=1e-12)
                    if spec.device=='transformer':
                        expected=z[:,0,1].imag/np.sqrt(z[:,0,0].imag*z[:,1,1].imag)
                        np.testing.assert_allclose([c['value'] for c in actual['k']],expected,rtol=2e-12,atol=1e-12)
                        if fixture=='studio':
                            kval=coupling_values(simulate_stable(circuit,frequencies,z0),z0,frequencies,spec)['k']
                            np.testing.assert_allclose([c['value'] for c in actual['k']],kval,rtol=2e-12,atol=1e-12)

    def test_sampled_auto_fixture_aliases_and_unequal_reference_impedance(self):
        circuit=circuit_for('inductor_dm_tap_gnd')
        circuit['metadata']['acceptance_scope']='original_sampled_points_only'
        z0=[35.,80.,60.]
        baseline=build_report(circuit,z0=z0)
        self.assertEqual(baseline['fixture']['id'],'sampled_open')
        self.assertEqual(baseline['fixture']['common_mode'],'open')
        aliases={'P':'PLUS','N':'MINUS','TAP':'CENTER'}
        other=copy.deepcopy(circuit);other['ports']=[aliases[p] for p in other['ports']]
        for element in other['elements']:
            for key in ('n1','n2'):
                if key in element:element[key]=aliases.get(element[key],element[key])
        actual=build_report(other,z0=z0)
        self.assertEqual(actual,baseline)

    def test_custom_columns_and_invalid_context(self):
        report=build_report(rl(),frequencies_hz=[11e9,3e9],fit_band_hz=[2e9,4e9],z0=[75,75])
        self.assertEqual(report['frequencies_hz'],[11e9,3e9])
        self.assertEqual([c['in_fit_band'] for c in report['columns']],[False,True])
        for frequencies in ([],[0],[float('nan')],[float('inf')],[True],[1e9,1e9],list(range(1,8))):
            with self.subTest(frequencies=frequencies),self.assertRaises(ValueError):
                build_report(rl(),frequencies_hz=frequencies)
        for config in ({'fit_band_hz':[3e9,2e9]},{'z0':[-1,50]},{'fixture':'guess'}):
            with self.subTest(config=config),self.assertRaises(ValueError):
                build_report(rl(),**config)
        with self.assertRaises(ValueError):
            build_report(rl('inductor_dm_gnd'),z0=[50,75],fixture='studio')


if __name__=='__main__':
    unittest.main()
