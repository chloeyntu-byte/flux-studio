import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from snpfit.integration_api import _native_context
from snpfit.native import _NATIVE_KEYS, _run_native


class NativeEnvironmentIntegrationTest(unittest.TestCase):
    def test_original_tristate_without_gui_loader_or_env_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); bindir = root/'cadence'/'tools'/'bin'; bindir.mkdir(parents=True)
            exe = bindir/'virtuoso'
            exe.write_text('#!/bin/sh\nprintf "LD=<%s> PY=<%s> TK_SET=<%s> PATH=<%s>\\n" "$LD_LIBRARY_PATH" "$PYTHONPATH" "${TK_LIBRARY+x}" "$PATH"\n')
            exe.chmod(0o755)
            state = {k:{'set':False,'value':None} for k in _NATIVE_KEYS}
            state['LD_LIBRARY_PATH']={'set':True,'value':'native-only'}
            state['PYTHONPATH']={'set':True,'value':''}
            state['PATH']={'set':True,'value':'/usr/bin:/bin'}
            with patch.dict(os.environ, {'LD_LIBRARY_PATH':'gui-only','PYTHONPATH':'gui-only','TK_LIBRARY':'gui-only'}):
                receipt, text = _run_native(str(exe),root/'fixture.il',root,None,'root',5,'verify',native_env_snapshot=state)
            self.assertIn('LD=<native-only> PY=<> TK_SET=<>',text)
            self.assertNotIn('gui-only',text)
            self.assertIn(str(bindir),text)
            self.assertNotIn('/opt/eda/cadence/IC618',json.dumps(receipt))
            self.assertEqual(receipt['argv'][4], '')

    def test_tools_discovered_only_from_supplied_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); bindir=root/'tools'/'bin';bindir.mkdir(parents=True)
            for name in ('virtuoso','ocean','spectre'):
                f=bindir/name;f.write_text('#!/bin/sh\n');f.chmod(0o755)
            env={k:None for k in ('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','PYTHONPATH','TCL_LIBRARY','TK_LIBRARY','QT_PLUGIN_PATH')}
            env['PATH']=str(bindir)
            options,snapshot=_native_context({'workspace':str(root),'cds_lib':str(root/'cds.lib'),
                'flux_native_runtime':{'workspace':str(root),'cds_lib':str(root/'cds.lib'),'environment':env}})
            for name in ('virtuoso','ocean','spectre'): self.assertEqual(options[name],str(bindir/name))
            self.assertNotIn('env_script',options)
            self.assertFalse(snapshot['TK_LIBRARY']['set'])


if __name__ == '__main__':unittest.main()
