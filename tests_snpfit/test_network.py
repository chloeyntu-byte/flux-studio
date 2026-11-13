import tempfile
from pathlib import Path
import unittest
import numpy as np
from snpfit.network import Network, read_touchstone, write_touchstone, z_to_s, s_to_z, s_to_y, y_to_s


class NetworkPhysics(unittest.TestCase):
    def test_unequal_reference_and_short_termination(self):
        freq=np.array([1e9,2e9,3e9,4e9,5e9])
        z=np.broadcast_to(np.array([[20+30j,2+3j],[2+3j,10+40j]]),(5,2,2)).copy()
        s=z_to_s(z,[50,75])
        self.assertTrue(np.allclose(s_to_z(s,[50,75]),z))
        self.assertTrue(np.allclose(y_to_s(s_to_y(s,[50,75]),[50,75]),s))
        net=Network(freq,s,[50,75])
        one=net.mapped([0],short_unused=True)
        # Independent two-port short-circuit Schur impedance.
        expected=z[:,0,0]-z[:,0,1]*z[:,1,0]/z[:,1,1]
        self.assertTrue(np.allclose(s_to_z(one.s,one.z0)[:,0,0],expected))
        with self.assertRaises(ValueError):net.mapped([0])

    def test_known_touchstone_ordering_and_encoding(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'asymmetric.s2p')
            p.write_text('# MHz S RI R 50\n1000 .1 .2 .3 .4 .5 .6 .7 .8\n2000 .1 .2 .3 .4 .5 .6 .7 .8\n')
            net=read_touchstone(p)
            self.assertEqual(net.s[0,1,0],.3+.4j)
            self.assertEqual(net.s[0,0,1],.5+.6j)
            p=Path(d,'four.s4p')
            m=np.arange(16).reshape(4,4)/100+1j*.01
            net=Network(np.array([1e9,2e9]),np.stack([m,m]),50)
            write_touchstone(net,p)
            self.assertTrue(np.allclose(read_touchstone(p).s,net.s))
            p=Path(d,'db.s1p');p.write_text('# GHz S DB R 50\n1 -20 90\n2 -20 90\n')
            self.assertAlmostEqual(read_touchstone(p).s[0,0,0].imag,.1)

    def test_band_no_extrapolation_or_silent_sparse_acceptance(self):
        net=Network(np.arange(0,7)*1e9,np.zeros((7,2,2)),50)
        band=net.band(0,6e9)
        self.assertEqual(band.freq[0],1e9)
        self.assertTrue(band.metadata['dc_excluded_from_lq'])
        with self.assertRaises(ValueError):net.band(1e9,50e9)
        with self.assertRaises(ValueError):net.band(1e9,2e9)

    def test_mixed_mode_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'m.ts');p.write_text('[Version] 2.1\n# Hz S RI R 50\n[Number of Ports] 2\n[Mixed-Mode Order] D1,2 C1,2\n')
            with self.assertRaisesRegex(ValueError,'mixed-mode'):read_touchstone(p)


if __name__=='__main__':unittest.main()
