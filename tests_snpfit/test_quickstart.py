"""Independent-source contracts for an unverified bounded starting circuit."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from snpfit.circuit import validate
from snpfit.network import Network
from snpfit.quickstart import quick_initialize, topology_signature


def independent_rlc_source(inductance=1e-9, include_dc=False):
    # Direct two-terminal KCL: winding impedance plus explicit ground/bridge C.
    f = np.geomspace(1e9, 10e9, 13)
    w = 2*np.pi*f
    g = 1/(2+1j*w*inductance)
    y = np.zeros((len(f),2,2),complex)
    y[:,0,0] = y[:,1,1] = g+1j*w*10e-15
    y[:,0,1] = y[:,1,0] = -g
    eye=np.eye(2)
    s = np.linalg.solve(eye+50*y, eye-50*y)
    if include_dc:
        f=np.r_[0.,f];s=np.concatenate((s[:1],s),axis=0)
    return Network(f,s,np.array([50.,50.]))


class QuickStartContract(unittest.TestCase):
    limits={'l_pct':5.,'q_pct':20.,'network_pct':5.}

    def test_source_only_legal_initial_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            start=time.monotonic()
            with patch('subprocess.Popen',side_effect=AssertionError('No external fitter allowed')):
                r=quick_initialize(independent_rlc_source(), 'inductor_dm_gnd', self.limits,
                                   Path(tmp)/'quick', timeout_s=5., candidate_id='compact_pi')
            actual=time.monotonic()-start
            self.assertLess(actual,5.)
            self.assertTrue(r['budget_met'])
            self.assertFalse(r['passed'])
            self.assertFalse(r['native_verified'])
            self.assertEqual(r['status'],'INITIAL_UNVERIFIED')
            validate(r['circuit'])
            self.assertEqual(r['topology_signature'],topology_signature(r['circuit']))
            self.assertTrue((Path(tmp)/'quick/model.sp').is_file())
            self.assertEqual(json.loads((Path(tmp)/'quick/quick-result.json').read_text())['candidate_id'],'compact_pi')
            with self.assertRaises(FileExistsError):
                quick_initialize(independent_rlc_source(), 'inductor_dm_gnd', self.limits,
                                 Path(tmp)/'quick', candidate_id='compact_pi')

    def test_changed_snp_changes_physical_inductance_initialization(self):
        # Cancellation retains a physical seed; it does not import cached values.
        cancelled=threading.Event();cancelled.set()
        a=quick_initialize(independent_rlc_source(1e-9),'inductor_dm_gnd',self.limits,
                           candidate_id='compact_pi',cancel=cancelled)
        b=quick_initialize(independent_rlc_source(2e-9),'inductor_dm_gnd',self.limits,
                           candidate_id='compact_pi',cancel=cancelled)
        la=sum(e['value'] for e in a['circuit']['elements'] if e['kind']=='ind')
        lb=sum(e['value'] for e in b['circuit']['elements'] if e['kind']=='ind')
        self.assertGreater(lb,la*1.8)
        self.assertLess(lb,la*2.2)
        self.assertTrue(a['cancelled'])
        self.assertEqual(a['topology_signature'],b['topology_signature'])

    def test_dc_exclusion_and_exact_sparse_sampling(self):
        n=independent_rlc_source(include_dc=True)
        r=quick_initialize(n,'inductor_dm_gnd',self.limits,candidate_id='series_rl')
        self.assertEqual(r['sampling']['excluded_DC_samples'],1)
        self.assertFalse(r['sampling']['interpolation_used'])
        self.assertEqual(r['sampling']['input_positive_samples'],13)
        self.assertEqual(r['metrics']['curve']['freq_hz'],n.freq[1:].tolist())

    def test_invalid_model_or_budget_rejected(self):
        n=independent_rlc_source()
        with self.assertRaises(ValueError):
            quick_initialize(n,'inductor_dm_gnd',self.limits,timeout_s=5.1)
        with self.assertRaises(ValueError):
            quick_initialize(n,'inductor_dm_gnd',self.limits,candidate_id='unregistered_graph')
        with self.assertRaises(ValueError):
            quick_initialize(n,'transformer_dm_gnd',self.limits)


if __name__=='__main__':unittest.main()
