#!/usr/bin/env python3
"""Python 3.6-compatible bootstrap; no global environment changes."""
from __future__ import print_function
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

KEYS=('LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','PYTHONPATH','TCL_LIBRARY','TK_LIBRARY',
      'QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','LC_ALL','LANG')


def main():
    install=Path(__file__).resolve().parents[1]
    env=os.environ.copy()
    if 'SNPFIT_NATIVE_ENV_JSON' not in env:
        env['SNPFIT_NATIVE_ENV_JSON']=json.dumps({k:{'set':k in env,'value':env.get(k)} for k in KEYS})
    for key in KEYS:
        if key not in ('LC_ALL','LANG'):env.pop(key,None)
    env['PYTHONDONTWRITEBYTECODE']='1'
    env['OPENBLAS_NUM_THREADS']='1';env['OMP_NUM_THREADS']='1'
    # Select an installed UTF-8 locale for this GUI child only.
    try:
        locales=subprocess.check_output(['locale','-a'],stderr=subprocess.DEVNULL).decode('ascii','replace').splitlines()
        good=next((x for x in locales if x.lower() in ('en_us.utf8','en_us.utf-8','c.utf8','c.utf-8')),None)
        if good:env['LC_ALL']=good;env['LANG']=good
    except (OSError,subprocess.SubprocessError):pass
    explicit=os.environ.get('SNPFIT_PYTHON')
    candidates=[explicit] if explicit else [sys.executable,shutil.which('python3.11'),shutil.which('python3')]
    if not explicit:
        candidates+=sorted(glob.glob('/opt/*/envs/*/bin/python3.11'))
        candidates+=sorted(glob.glob(str(Path.home()/'.venvs'/'*'/'bin'/'python')))
    probe='import sys; assert sys.version_info >= (3,11); import numpy,tkinter,matplotlib; print("SNPFIT_PYTHON_OK")'
    seen=set();errors=[]
    for value in candidates:
        if not value or value in seen:continue
        seen.add(value)
        # Resolve an explicit command or relative path before changing cwd.
        value = str(Path(value).expanduser().absolute()) if os.path.dirname(value) else (shutil.which(value) or value)
        try:
            result=subprocess.run([value,'-c',probe],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12)
            if result.returncode or b'SNPFIT_PYTHON_OK' not in result.stdout:
                errors.append(value+': '+result.stderr.decode('utf-8','replace')[-300:]);continue
        except (OSError,subprocess.SubprocessError) as exc:
            errors.append(value+': '+str(exc));continue
        os.chdir(str(install))
        os.execve(value,[value,'-m','snpfit']+sys.argv[1:],env)
    print('SNPFit: no usable existing Python >=3.11 with NumPy/Tk/Matplotlib. Set SNPFIT_PYTHON to an existing interpreter.',file=sys.stderr)
    for error in errors:print(error,file=sys.stderr)
    return 2


if __name__=='__main__':sys.exit(main())
