"""An actual fit must work with every external program launch prohibited."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

from snpfit.circuit import simulate
from snpfit.models import list_models
from snpfit.network import Network, write_touchstone
from snpfit.workflow import diagnose, fit_file


class StandaloneRuntime(unittest.TestCase):
    def test_real_fit_without_external_programs_or_model_cache(self):
        circuit = {
            "name": "independent_rl", "ports": ["P", "N"], "reference": "REF",
            "elements": [
                {"kind": "res", "name": "rs", "n1": "P", "n2": "x", "value": 1.7},
                {"kind": "ind", "name": "ls", "n1": "x", "n2": "N", "value": 2.4e-9},
            ],
        }
        freq = np.geomspace(1e9, 5e9, 9)
        source = Network(freq, simulate(circuit, freq, [50, 50]), np.array([50., 50.]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.s2p"
            write_touchstone(source, path)
            with mock.patch.dict(os.environ, {"PATH": "", "SNPFIT_MODELGEN": "/nonexistent/modelgen"}), \
                    mock.patch("subprocess.Popen", side_effect=AssertionError("external program forbidden")), \
                    mock.patch("os.system", side_effect=AssertionError("shell forbidden")):
                diagnosis = diagnose()
                result = fit_file(snp=path, model_id="inductor_se_gnd", port_order=[0, 1],
                                  fmin=1e9, fmax=5e9, l_pct=5, q_pct=5,
                                  runtime=Path(folder)/"fresh-runtime", timeout_s=25)
        self.assertFalse(diagnosis["external_fitting_program_required"])
        self.assertTrue(result["passed"], result.get("metrics"))
        self.assertTrue(result["backend"].startswith("standalone"))
        self.assertEqual(result["request"]["fitting_engine"], "standalone_numpy")
        self.assertNotIn("modelgen", result["request"])

    def test_explicit_candidates_do_not_require_external_model_templates(self):
        self.assertEqual(len([m for m in list_models() if m.id.startswith(("inductor_", "transformer_"))]), 24)
        for model in list_models():
            self.assertTrue(model.candidates)
            for candidate in model.candidates:
                self.assertFalse(getattr(candidate, "modelgen_type", None))
                self.assertFalse(getattr(candidate, "topology", None))


if __name__ == "__main__":
    unittest.main()
