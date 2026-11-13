"""Independent circuit equations and acceptance definitions, without solvers."""
import unittest
from types import SimpleNamespace
import numpy as np

from snpfit.circuit import admittance, simulate
from snpfit.fitting import evaluate, measure_network, _lift_grounded_transformer
from snpfit.models import get_model


class FitPhysics(unittest.TestCase):
    def setUp(self):
        self.freq = np.geomspace(1e9, 5e9, 11)

    def test_series_rl_independent_two_node_equation(self):
        circuit = {"name": "rl", "ports": ["P", "N"], "reference": "REF", "elements": [
            {"name": "rs", "kind": "res", "n1": "P", "n2": "n", "value": 1.5},
            {"name": "ls", "kind": "ind", "n1": "n", "n2": "N", "value": 2.3e-9}]}
        expected = (1 / (1.5 + 2j * np.pi * self.freq * 2.3e-9))[:, None, None] * np.array([[1, -1], [-1, 1]])
        np.testing.assert_allclose(admittance(circuit, self.freq), expected, rtol=1e-11, atol=1e-14)

    def coupled_fixture(self, coupling=.65):
        return {"name": "coupled", "ports": ["P", "S"], "reference": "REF", "metadata": {}, "elements": [
            {"name": "rp", "kind": "res", "n1": "P", "n2": "np", "value": 1.1},
            {"name": "lp", "kind": "ind", "n1": "np", "n2": "REF", "value": 2e-9},
            {"name": "rs", "kind": "res", "n1": "S", "n2": "ns", "value": 1.8},
            {"name": "ls", "kind": "ind", "n1": "ns", "n2": "REF", "value": 3e-9},
            {"name": "kps", "kind": "mind", "ind1": "lp", "ind2": "ls", "value": coupling}]}

    def test_negative_mutual_independent_impedance_equation(self):
        circuit = self.coupled_fixture(-.65)
        inductance = np.array([[2e-9, -.65 * np.sqrt(6) * 1e-9], [-.65 * np.sqrt(6) * 1e-9, 3e-9]])
        expected_z = np.diag([1.1, 1.8])[None, :, :] + 2j * np.pi * self.freq[:, None, None] * inductance
        np.testing.assert_allclose(admittance(circuit, self.freq), np.linalg.inv(expected_z), rtol=1e-11, atol=1e-14)

    def test_weak_transfer_opposite_phase_must_fail(self):
        source_circuit = self.coupled_fixture(.02)
        source = SimpleNamespace(freq=self.freq, s=simulate(source_circuit, self.freq, [50, 50]), z0=np.array([50, 50]))
        wrong = self.coupled_fixture(-.02)
        metrics = evaluate(wrong, source, "transformer_se", {"l_pct": 20, "q_pct": 20})
        self.assertTrue(metrics["network"]["pass"])
        self.assertFalse(metrics["passed"])
        self.assertGreater(metrics["mutual_impedance"]["complex_relative_errors"]["z12"]["max_relative_pct"], 190)

    def test_dm_definition_is_common_mode_matched(self):
        s = np.zeros((len(self.freq), 2, 2), complex)
        s[:, 0, 0], s[:, 1, 1] = .3 + .1j, .2 + .04j
        s[:, 0, 1], s[:, 1, 0] = .6 - .1j, .6 - .1j
        network = SimpleNamespace(freq=self.freq, s=s, z0=np.array([50, 50]))
        measured = measure_network(network, "inductor_dm")
        sdd = (s[:, 0, 0] + s[:, 1, 1] - s[:, 0, 1] - s[:, 1, 0]) / 2
        z = 100 * (1 + sdd) / (1 - sdd)
        np.testing.assert_allclose(measured["branches"]["winding"]["l_h"], z.imag / (2 * np.pi * self.freq))
        np.testing.assert_allclose(measured["branches"]["winding"]["q"], z.imag / z.real)

    def test_grounded_winding_lift_preserves_voltage_current_equations(self):
        y = admittance(self.coupled_fixture(), self.freq)
        lifted, roles = _lift_grounded_transformer(y, get_model("transformer_se"))
        self.assertEqual(roles, ["P", "RET_P", "S", "RET_S"])
        voltage = np.array([1.2 + .2j, -.3j, .5 - .1j, -.2 + .3j])
        winding_voltage = np.array([voltage[0] - voltage[1], voltage[2] - voltage[3]])
        winding_current = y @ winding_voltage
        expected_current = np.stack([winding_current[:, 0], -winding_current[:, 0], winding_current[:, 1], -winding_current[:, 1]], axis=1)
        np.testing.assert_allclose(lifted @ voltage, expected_current, rtol=1e-12, atol=1e-14)


if __name__ == "__main__":
    unittest.main()
