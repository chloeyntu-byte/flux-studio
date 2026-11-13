"""Behavioral tests using independently specified coupled passive networks."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from snpfit.network import Network, write_touchstone
from snpfit.stable_network import simulate_stable
from snpfit.models import get_model
from snpfit.quickstart import topology_signature
from snpfit.studio_engine import (run_action, inspect_source, _parameter_fit, _prepare,
                                 _snapshot, _assess, _k_error, normalize_config, native_frequencies)
from snpfit.resonance import build_reference
from snpfit.coupling import k_reference


def circuit():
    return {'name':'handwritten_transformer','ports':['P','S'],'reference':'REF',
            'metadata':{'component_limit':20,'candidate':'coupled_rl'},'elements':[
        {'kind':'res','name':'r1','n1':'P','n2':'a','value':2.},
        {'kind':'ind','name':'l1','n1':'a','n2':'REF','value':1e-9},
        {'kind':'res','name':'r2','n1':'S','n2':'b','value':4.},
        {'kind':'ind','name':'l2','n1':'b','n2':'REF','value':2e-9},
        {'kind':'mind','name':'k','ind1':'l1','ind2':'l2','value':.6},
        {'kind':'cap','name':'c1','n1':'P','n2':'REF','value':8e-14},
        {'kind':'cap','name':'c2','n1':'S','n2':'REF','value':4e-14}]}


class StudioEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.true=circuit();freq=np.geomspace(1e9,80e9,801)
        self.full=Network(freq,simulate_stable(self.true,freq,50),50)
        self.path=self.root/'source.s2p';write_touchstone(self.full,self.path)
        self.cfg=dict(source_path=str(self.path),model_id='transformer_se_gnd',port_order='1,2',ports_confirmed=True,
                      short_unused=False,fmin_ghz=1,fmax_ghz=5,l_pct=5,q_pct=20,k_pct=5,srf_pct=2,network_pct=5,
                      component_limit=20,quick_seconds=5,tune_seconds=5,upgrade_seconds=2,topology_id='auto')
        self.band=self.full.band(1e9,5e9);self.spec=get_model('transformer_se_gnd')
    def tearDown(self):self.tmp.cleanup()
    def snapshot(self,c=None):
        cfg,source,band,full=_prepare(self.cfg)
        reference=build_reference(full,self.spec);kref=k_reference(band,self.spec,reference)
        value=copy.deepcopy(c or self.true)
        metrics=_assess(value,band,full,self.spec,cfg,reference,kref)
        out=self.root/('snapshot'+str(len(list(self.root.glob('snapshot*')))));out.mkdir()
        return _snapshot('tune',cfg,source,band,full,value,metrics,out)
    def test_preview_does_not_require_precision_targets(self):
        cfg={k:v for k,v in self.cfg.items() if k not in ('l_pct','q_pct','k_pct','srf_pct','network_pct')}
        result=run_action('preview',cfg,runtime=self.root)
        self.assertEqual(result['status'],'SOURCE_LOADED');self.assertIsNone(result['circuit'])
        self.assertIsNone(result['metrics']);self.assertIn('primary',result['curves']['source'])
    def test_quick_reads_snp_and_never_claims_final_acceptance(self):
        with patch('subprocess.Popen',side_effect=AssertionError('No external fitter allowed')):
            result=run_action('quick',self.cfg,runtime=self.root)
        self.assertEqual(result['status'],'INITIAL_UNVERIFIED');self.assertFalse(result['passed'])
        self.assertTrue(Path(result['run_dir'],'model.sp').is_file())
        self.assertEqual(len(result['curves']['freq_hz']),len(self.band.freq))
        self.assertEqual(result['source']['sha256'],inspect_source(self.path)['sha256'])
    def test_slow_quick_is_successful_initial_output_without_timing_warning(self):
        # Simulate slow preparation/serialization without sleeping or changing
        # the real numerical initializer's monotonic clock. Its useful short
        # fitting allowance remains4.1s, independently of overall wall time.
        ticks=iter([0.,0.,6.5,7.])
        clock=SimpleNamespace(monotonic=lambda:next(ticks),strftime=time.strftime)
        from snpfit.studio_engine import quick_initialize as actual_initialize
        budgets=[]
        def initialize(*args,**kwargs):
            budgets.append(kwargs['timeout_s'])
            return actual_initialize(*args,**kwargs)
        with patch('snpfit.studio_engine.time',clock), patch('snpfit.studio_engine.quick_initialize',side_effect=initialize):
            result=run_action('quick',self.cfg,runtime=self.root)
        self.assertEqual(budgets,[4.1])
        self.assertGreater(result['elapsed_s'],5.)
        self.assertEqual(result['status'],'INITIAL_UNVERIFIED')
        self.assertFalse(result['passed'])  # Initial accuracy qualification stays independent.
        self.assertFalse(result['performance_target_met'])
        self.assertFalse(result['budget_met'])  # Legacy alias is informational.
        self.assertTrue(Path(result['run_dir'],'model.sp').is_file())
        self.assertTrue(result['circuit']['elements'])
        self.assertFalse(any(any(term in warning.lower() for term in ('exceeded','time budget','wall-time','seconds'))
                             for warning in result['warnings']))
        saved=json.loads(Path(result['run_dir'],'result.json').read_text())
        self.assertEqual(saved['status'],'INITIAL_UNVERIFIED')
        self.assertEqual(saved['performance_target_met'],False)

    def test_signed_k_is_a_real_objective_and_graph_stays_fixed(self):
        initial=copy.deepcopy(self.true);initial['elements'][4]['value']=.45
        cc,mm,receipt=_parameter_fit(initial,self.band,self.full,self.spec,self.cfg,5)
        self.assertTrue(receipt['k_in_objective']);self.assertGreater(receipt['accepted_steps'],0)
        self.assertTrue(mm['joint']['passed']);self.assertLess(mm['k']['max_relative_pct'],5)
        self.assertEqual(topology_signature(initial),topology_signature(cc))
        self.assertEqual(initial['elements'][4]['value'],.45)
    def test_tune_does_not_upgrade_and_preserves_input_snapshot(self):
        initial=copy.deepcopy(self.true);initial['elements'][4]['value']=.45
        baseline=self.snapshot(initial);frozen=json.dumps(baseline,sort_keys=True)
        with patch('snpfit.studio_engine._upgrade_candidates',side_effect=AssertionError('No automatic upgrade')):
            result=run_action('tune',self.cfg,baseline,runtime=self.root)
        self.assertTrue(result['passed']);self.assertFalse(result['topology_changed'])
        self.assertEqual(json.dumps(baseline,sort_keys=True),frozen)
        self.assertEqual(baseline['topology_signature'],result['topology_signature'])
    def test_manual_upgrade_keeps_previous_result_and_reports_actual_graph(self):
        baseline=self.snapshot();frozen=json.dumps(baseline,sort_keys=True)
        result=run_action('upgrade',self.cfg,baseline,runtime=self.root)
        self.assertEqual(result['stage'],'upgrade')
        self.assertEqual(result['topology_changed'],result['topology_signature']!=baseline['topology_signature'])
        self.assertEqual(json.dumps(baseline,sort_keys=True),frozen)
        self.assertIn('accepted_new',result);self.assertIn('no_improvement',result)
        self.assertGreater(len(result['upgrade_attempts']),0)
    def test_native_refuses_stale_targets_before_starting_any_tool(self):
        baseline=self.snapshot();self.assertTrue(baseline['passed'])
        changed={**self.cfg,'k_pct':6}
        with patch('snpfit.native.build_native',side_effect=AssertionError('Must not start native')):
            with self.assertRaisesRegex(ValueError,'targets changed'):
                run_action('native',changed,baseline,runtime=self.root)
    def test_source_change_requires_new_initialization(self):
        baseline=self.snapshot()
        with self.path.open('a') as f:f.write('! Source changed\n')
        with self.assertRaisesRegex(ValueError,'Source, model'):
            run_action('tune',self.cfg,baseline,runtime=self.root)
    def test_cancellation_is_explicit_and_cannot_be_accepted(self):
        baseline=self.snapshot();event=threading.Event();event.set()
        result=run_action('tune',self.cfg,baseline,runtime=self.root,cancel=event)
        self.assertEqual(result['status'],'CANCELLED');self.assertFalse(result['passed'])
        self.assertFalse(result['accepted_new'])
    def test_sparse_resonance_is_not_invented(self):
        freq=np.geomspace(1e9,80e9,25);path=self.root/'sparse.s2p'
        write_touchstone(Network(freq,simulate_stable(self.true,freq,50),50),path)
        cfg={**self.cfg,'source_path':str(path)}
        result=run_action('preview',cfg,runtime=self.root)
        self.assertTrue(result['warnings']);self.assertFalse(result['curves']['source_srf']['resolved'])
    def test_native_generated_grid_never_shadows_a_measured_frequency(self):
        # A geometrical scan contains sqrt(50)GHz. The actual input contains
        # the instrument's rounded7.071068GHz. Keep exactly the measured one.
        measured=np.asarray([1e9,7.071068e9,50e9])
        full=Network(measured,simulate_stable(self.true,measured,50),50)
        numeric={'srf':{'branches':{'primary':{'model_hz':7.071067811865e9}}}}
        grid=native_frequencies(full,full,numeric)
        self.assertTrue(np.all(np.isin(measured,grid)))
        self.assertFalse(np.any(grid==7.071067811865e9))
        for value in measured:
            close=grid[np.abs(grid-value)<=1e-6*np.maximum(grid,value)]
            self.assertEqual(close.tolist(),[value])
        # Two original close checkpoints remain authoritative, even though the
        # backend may then need to report the native tool's data limitation.
        original=np.asarray([1e9,7.071068e9,7.0710681e9,50e9])
        dense=Network(original,simulate_stable(self.true,original,50),50)
        grid=native_frequencies(dense,dense,numeric)
        self.assertTrue(np.all(np.isin(original,grid)))

    def test_native_pre_cancel_does_not_start_tools(self):
        baseline=self.snapshot();event=threading.Event();event.set()
        with patch('snpfit.native.build_native',side_effect=AssertionError('Cancelled native must not start')):
            result=run_action('native',self.cfg,baseline,runtime=self.root,cancel=event)
        self.assertEqual(result['status'],'CANCELLED')
        self.assertFalse(result['native_verified'])
        self.assertTrue(Path(result['run_dir'],'error.json').is_file())

    def test_missing_srf_reference_keeps_real_partial_improvement(self):
        initial=copy.deepcopy(self.true);initial['elements'][4]['value']=.45
        cc,mm,receipt=_parameter_fit(initial,self.band,self.band,self.spec,self.cfg,5)
        self.assertFalse(mm['joint']['passed'])
        self.assertIn('srf',mm['joint']['unresolved_targets'])
        self.assertTrue(mm['lq']['passed'])
        self.assertNotEqual(cc['elements'][4]['value'],.45)
        self.assertEqual(topology_signature(initial),topology_signature(cc))

    def test_wrong_terminal_order_and_unconfirmed_mapping_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Confirm the port'):
            normalize_config({**self.cfg,'ports_confirmed':False})
        baseline=self.snapshot();baseline['circuit']['ports'].reverse()
        with self.assertRaisesRegex(ValueError,'terminal order'):
            run_action('tune',self.cfg,baseline,runtime=self.root)

if __name__=='__main__':unittest.main()
