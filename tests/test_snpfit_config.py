"""SNPFit override follows Flux's existing config precedence and path rules."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'select_python.sh'


class SNPFitConfigTests(unittest.TestCase):
    def test_config_and_inherited_paths_are_anchored_and_inherited_wins(self):
        with tempfile.TemporaryDirectory(prefix='flux config ') as tmp:
            root = Path(tmp) / 'project directory'; root.mkdir()
            launch = Path(tmp) / 'launch directory'; launch.mkdir()
            (root / 'flux_config.sh').write_text('export TFGEN_SNPFIT_ROOT="../studio configured"\n', encoding='utf-8')
            env = {k: v for k, v in os.environ.items() if not k.startswith('TFGEN_')}
            cmd = ['bash', '-c', 'source "$1"; tfgen_load_config "$2" || exit; printf "%s" "$TFGEN_SNPFIT_ROOT"',
                   'config-test', str(SCRIPT), str(root)]
            local = subprocess.run(cmd, cwd=str(launch), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            self.assertEqual(Path(local.stdout).resolve(), (root / '../studio configured').resolve())
            env['TFGEN_SNPFIT_ROOT'] = '../studio inherited'
            inherited = subprocess.run(cmd, cwd=str(launch), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            self.assertEqual(Path(inherited.stdout).resolve(), (launch / '../studio inherited').resolve())


if __name__ == '__main__':
    unittest.main()
