import unittest
import copy
import tempfile
from dataclasses import replace
import numpy as np
from snpfit.models import list_models
from snpfit.circuit import validate
from snpfit.workflow import fit_file


class InputContracts(unittest.TestCase):
    def test_each_run_requires_explicit_supported_error(self):
        for value in (None,0,4.99,20.01,float('nan')):
            with self.assertRaisesRegex(ValueError,'5–20'):
                fit_file(snp='/does/not/exist',model_id='inductor_se',port_order=[0,1],
                         fmin=1e9,fmax=50e9,l_pct=value,q_pct=10)

    def test_model_roles_are_distinct_no_general_fallback(self):
        models=list_models()
        self.assertEqual(len({m.id for m in models}),len(models))
        self.assertTrue(all('general' not in m.id for m in models))
        self.assertTrue(all(len(set(m.port_roles))==m.nports for m in models))
        for device in ('inductor','transformer'):
            for mode in ('se','dm'):
                self.assertTrue(any(m.device==device and m.mode==mode for m in models))

    def test_mutual_matrix_must_be_passive_not_only_pairwise(self):
        elements=[{'kind':'ind','name':'L'+str(i),'n1':'P'+str(i),'n2':'REF','value':1e-9} for i in range(3)]
        for i,j in ((0,1),(1,2),(0,2)):
            elements.append({'kind':'mind','name':f'K{i}{j}','ind1':'L'+str(i),'ind2':'L'+str(j),'value':-.8})
        with self.assertRaisesRegex(ValueError,'positive definite'):
            validate({'ports':['P0','P1','P2'],'reference':'REF','elements':elements})

    def test_component_limit_is_per_kind(self):
        elements=[{'kind':'res','name':f'R{i}','n1':'P','n2':'REF','value':100} for i in range(21)]
        with self.assertRaisesRegex(ValueError,'per-kind'):
            validate({'ports':['P'],'reference':'REF','elements':elements})

    def test_custom_model_cannot_claim_pass_without_rechecking(self):
        from snpfit.models import get_model,register_model
        from snpfit.fitting import FitResult,fit
        from snpfit.circuit import simulate
        from snpfit.network import Network
        circuit={'name':'known','ports':['P','N'],'reference':'REF','metadata':{},'elements':[
            {'kind':'res','name':'R','n1':'P','n2':'a','value':2.},
            {'kind':'ind','name':'L','n1':'a','n2':'N','value':1e-9},
            {'kind':'cap','name':'C','n1':'P','n2':'REF','value':1e-14}]}
        freq=np.linspace(1e9,5e9,7)
        net=Network(freq,simulate(circuit,freq,[50,50]),50)
        wrong=copy.deepcopy(circuit);wrong['elements'][0]['value']=100.
        spec=replace(get_model('inductor_se'),id='test_custom_guard')
        from snpfit import models as registry
        self.addCleanup(registry._MODELS.pop, spec.id, None)
        self.addCleanup(registry._FITTERS.pop, spec.id, None)
        register_model(spec,lambda *args,**kwargs:FitResult(spec.id,True,circuit=wrong))
        with tempfile.TemporaryDirectory() as directory:
            result=fit(net,spec.id,{'l_pct':10,'q_pct':10},directory)
        self.assertFalse(result.passed)
        self.assertEqual(result.status,'not_met')


if __name__=='__main__':unittest.main()
