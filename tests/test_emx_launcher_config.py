"""Optional EMX configuration follows the same portable launcher contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


KEYS=('TFGEN_EMX','TFGEN_EMX_CONFIG','TFGEN_EMX_PROCESS','TFGEN_EMX_LAYER_MAP','TFGEN_STRMOUT')
SCRIPT=Path(__file__).resolve().parents[1]/'scripts/select_python.sh'


class EMXLauncherConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="flux EMX config's ")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.project=self.root/'portable application'
        self.project.mkdir()
        self.cwd=self.root/'actual launching directory'
        self.cwd.mkdir()

    def load(self,text,overrides=None):
        (self.project/'flux_config.sh').write_text(text)
        env=dict(PATH=os.defpath,LANG='C',LC_ALL='C',LD_LIBRARY_PATH='/original/native',UNRELATED='retained')
        env.update(overrides or {})
        code='import json,os; print(json.dumps({k:os.environ.get(k) for k in '+repr(KEYS+('LD_LIBRARY_PATH','UNRELATED'))+'}))'
        command=['bash','-c','source "$1"; tfgen_load_config "$2" || exit $?; exec "$3" -c "$4"',
                 'flux-config-test',str(SCRIPT),str(self.project),sys.executable,code]
        result=subprocess.run(command,cwd=str(self.cwd),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_local_paths_commands_and_unrelated_environment_isolation(self):
        values=self.load("TFGEN_EMX=emx\nTFGEN_STRMOUT=strmout\nTFGEN_EMX_CONFIG='./local config/emxconfig.il'\n"
             "TFGEN_EMX_PROCESS='../process files/selected.proc'\nTFGEN_EMX_LAYER_MAP='./chosen map'\n"
             "export LD_LIBRARY_PATH=/do-not-leak\nexport UNRELATED=changed\n")
        self.assertEqual(values['TFGEN_EMX'],'emx')
        self.assertEqual(values['TFGEN_STRMOUT'],'strmout')
        self.assertEqual(Path(values['TFGEN_EMX_CONFIG']).resolve(),self.project/'local config/emxconfig.il')
        self.assertEqual(Path(values['TFGEN_EMX_PROCESS']).resolve(),self.root/'process files/selected.proc')
        self.assertEqual(Path(values['TFGEN_EMX_LAYER_MAP']).resolve(),self.project/'chosen map')
        self.assertEqual(values['LD_LIBRARY_PATH'],'/original/native')
        self.assertEqual(values['UNRELATED'],'retained')

    def test_inherited_overrides_keep_priority_empty_states_and_launch_relative_base(self):
        values=self.load("TFGEN_EMX='./file emx'\nTFGEN_EMX_CONFIG='./configured.il'\n",
                         {'TFGEN_EMX':'./native tools/emx','TFGEN_EMX_CONFIG':''})
        self.assertEqual(Path(values['TFGEN_EMX']).resolve(),self.cwd/'native tools/emx')
        self.assertEqual(values['TFGEN_EMX_CONFIG'],'')

    def test_missing_optional_emx_is_not_a_layout_bootstrap_error(self):
        values=self.load("TFGEN_EMX='./not installed/emx'\nTFGEN_EMX_CONFIG='./not installed/config.il'\n")
        self.assertEqual(Path(values['TFGEN_EMX']).resolve(),self.project/'not installed/emx')
        self.assertEqual(Path(values['TFGEN_EMX_CONFIG']).resolve(),self.project/'not installed/config.il')


if __name__=='__main__':unittest.main()
