"""Bundle discovery and the real standalone bootstrap use the shipped package."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class BundledSNPFitTests(unittest.TestCase):
    def test_default_discovery_uses_the_bundled_copy(self):
        from flux_results import discover_snpfit
        environment = {k:v for k,v in os.environ.items() if k != 'TFGEN_SNPFIT_ROOT'}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(discover_snpfit(), ROOT/'snpfit_launcher')

    def test_existing_interpreter_command_name_launches_real_cli(self):
        env = dict(os.environ, SNPFIT_PYTHON=Path(sys.executable).name,
                   PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''))
        result = subprocess.run([sys.executable, '-B', str(ROOT/'snpfit_launcher/launch.py'), '--help'],
                                env=env, cwd=str(ROOT.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('studio-action', result.stdout)
