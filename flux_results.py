"""On-demand durable candidate actions; no dependence on retained campaign OA.

SNPFit is a sibling application with its own interpreter and public JSON API.
Native environment snapshots are passed only in private requests, never logs.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _private_json(path,value):
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    with os.fdopen(os.open(str(path),flags,0o600),'w') as stream:
        json.dump(value,stream,indent=2,allow_nan=False);stream.write('\n')


def _directory(source,output_dir=None):
    identity=source.get('identity') or source
    base=Path(output_dir).expanduser() if output_dir else Path(identity['workspace'])/(ROOT.name+'_runtime')/'selected_results'
    if not base.is_absolute() or any(p.is_symlink() for p in (base,)+tuple(base.parents)):
        raise ValueError('Action output must be an absolute directory without symbolic-link parents.')
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    out=base/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:10]);out.mkdir(mode=0o700)
    return out


def _selected(row,source):
    if not isinstance(row,dict) or row.get('status')!='completed':raise ValueError('Select a completed result with a retained SNP.')
    from result_data import hydrate_row
    r=hydrate_row(row)
    if r.get('data_error'):raise ValueError(r['data_error'])
    if not r.get('metrics'):raise ValueError('The retained SNP has no usable original metrics.')
    design=copy.deepcopy(r.get('design') or {})
    identity=copy.deepcopy(r.get('source') or {})
    fallback=source.get('identity') or {}
    for key in ('library','cell','workspace','cds_lib','library_path','technology'):
        if not identity.get(key):identity[key]=fallback.get(key) or design.get(key)
        if not identity.get(key):raise ValueError('Selected result is missing recorded '+key+' identity.')
    import cadence
    for key in ('library','cell'):
        if not cadence.NAME.fullmatch(identity[key]):raise ValueError('Invalid result '+key+' identity.')
    if not design.get('model') or not isinstance(design.get('parameters'),dict):
        raise ValueError('The result has no reproducible layout design snapshot.')
    design.update(library=identity['library'],cell=identity['cell'])
    r['design']=design;r['source']=identity
    network=copy.deepcopy(r.get('network') or {})
    if not network.get('ports'):
        old=json.loads((Path(r['job_dir'])/'job.json').read_text())
        network.update(ports=old['ports'],frequencies_hz=old['frequencies_hz'])
    path=Path(r['snp_path']).expanduser().resolve(strict=True)
    network['snp_sha256']=_sha(path);r['snp_path']=str(path);r['network']=network
    return r


def _runtime(source,identity):
    runtime=copy.deepcopy(source.get('runtime') or {})
    if (runtime and all(runtime.get(k) and Path(runtime[k]).resolve()==Path(identity[k]).resolve() for k in ('workspace','cds_lib'))):
        return runtime
    from runtime_config import discover_runtime
    return discover_runtime(workspace=identity['workspace'],cds_lib=identity['cds_lib'])


def discover_snpfit():
    override=os.environ.get('TFGEN_SNPFIT_ROOT')
    if override:
        candidates=[Path(override).expanduser()]
    else:
        siblings=ROOT.parent
        candidates=[ROOT/'snpfit_launcher'] if (ROOT/'snpfit_launcher'/'launch').is_file() else [siblings/'snpfit_studio']+sorted(siblings.glob('snpfit_studio_*'))
    valid=[]
    for p in candidates:
        if p.is_dir() and (p/'launch').is_file() and ((p/'snpfit'/'integration_api.py').is_file() or (p == ROOT/'snpfit_launcher' and (ROOT/'snpfit'/'integration_api.py').is_file())):
            resolved=p.resolve()
            if resolved not in valid:valid.append(resolved)
    if not valid:raise ValueError('SNPFit Studio integration was not found beside Flux. Set TFGEN_SNPFIT_ROOT to its application directory.')
    if len(valid)>1:
        exact=ROOT.parent/'snpfit_studio'
        if exact.resolve() in valid:return exact.resolve()
        raise ValueError('Several SNPFit Studio copies were found. Set TFGEN_SNPFIT_ROOT to the intended copy.')
    return valid[0]


def _request(row,source,out,require_fit=True):
    r=_selected(row,source);identity=r['source'];network=r['network'];ports=network['ports']
    freq=sorted({float(m.get('reported_frequency_hz',m['frequency_hz'])) for m in r['metrics']})
    if require_fit and len(freq)<2:
        raise ValueError('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.')
    signals=ports['signals'];terminals=ports['terminals'];windings={}
    for t in terminals:
        if t['name'] in signals and t.get('endpoint') in ('positive','negative'):
            windings.setdefault(t.get('winding'),{})[t['endpoint']]=t['name']
    tap=[t['name'] for t in terminals if t['name'] in signals and t.get('kind')=='tap']
    if set(windings)=={'L'}:
        roles=['P','N']+(['TAP'] if tap else [])
        actual=[windings['L']['positive'],windings['L']['negative']]+tap
        model='inductor_dm_tap' if tap else 'inductor_dm'
    elif set(windings)=={'P','S'} and not tap:
        roles=['P1','P2','S1','S2'];actual=[windings[w][e] for w in ('P','S') for e in ('positive','negative')]
        model='transformer_dm'
    else:raise ValueError('No supported SNPFit model matches these actual winding/TAP pins.')
    if len(actual)!=len(signals) or set(actual)!=set(signals):raise ValueError('Every actual signal/TAP must be mapped; no silent termination is allowed.')
    request=dict(source_path=r['snp_path'],source_sha256=network['snp_sha256'],model_id=model,
                 port_order=[signals.index(name)+1 for name in actual],ports_confirmed=True,short_unused=False,
                 terminal_aliases={a:b for a,b in zip(roles,actual) if a!=b},workspace=identity['workspace'],
                 cds_lib=identity['cds_lib'],library=identity['library'],cell=identity['cell'],existing_library_path=identity['library_path'],
                 runtime=str(out/'snpfit_runtime'),flux_native_runtime=_runtime(source,identity),overwrite=True,
                 sampled_points_quick=True,fmin_ghz=freq[0]/1e9,fmax_ghz=freq[-1]/1e9,
                 l_pct=5,q_pct=20,quick_seconds=5,component_limit=20,
                 source_port_contract=copy.deepcopy(ports))
    native_styles={'transformer_implicit':'coplanar','coplanar_transformer':'coplanar','stacked_transformer':'stacked'}
    native_style=native_styles.get(r['design']['model'])
    if native_style is not None:request['native_transformer_style']=native_style
    return r,request


def _child_environment(runtime=None):
    env=dict(os.environ)
    # The other application's launcher selects its own GUI dependencies.
    from runtime_config import NATIVE_ISOLATION_KEYS
    for key in NATIVE_ISOLATION_KEYS:env.pop(key,None)
    if runtime is not None:
        snapshot={}
        original=runtime.get('environment') or {}
        for key in NATIVE_ISOLATION_KEYS+('PATH','CDS_LIC_FILE','LM_LICENSE_FILE','CDSHOME','IC_HOME','CDS_ROOT','CDS_INST_DIR','OA_HOME','CDS_AUTO_64BIT'):
            if key in original:snapshot[key]={'set':original[key] is not None,'value':original[key]}
        env['SNPFIT_NATIVE_ENV_JSON']=json.dumps(snapshot)
    env['PYTHONDONTWRITEBYTECODE']='1'
    return env


def _command(argv,out,env,cancel_event=None,timeout=900):
    stdout=out/'response.json';log=out/'action.log'
    with stdout.open('xb') as response,log.open('xb') as errors:
        process=subprocess.Popen(argv,stdout=response,stderr=errors,env=env,start_new_session=True)
        _private_json(out/'process.json',dict(pid=process.pid,process_group=process.pid,started=time.time(),action=Path(argv[0]).name))
        start=time.monotonic();cancelled=False
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set() or time.monotonic()-start>timeout:
                cancelled=True;os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
                break
            time.sleep(.1)
    if cancelled:raise InterruptedError('Action cancelled. Log: '+str(log))
    try:
        result=json.loads(stdout.read_text())
    except (ValueError,OSError):raise RuntimeError('Action returned no valid response (exit %s). Log: %s'%(process.returncode,log))
    if process.returncode or result.get('status') in ('failed','FAILED','ERROR','CANCELLED','TARGETS_NOT_MET','NATIVE_NOT_ACCEPTED','NUMERIC_NOT_ACCEPTED') or result.get('error'):
        raise RuntimeError(str(result.get('error') or result.get('message') or 'Action failed its acceptance checks: '+str(result.get('metrics',{})))+'; log: '+str(log))
    result.setdefault('log_path',str(log));return result


def fit_schematic(row,source,output_dir=None,cancel_event=None):
    out=_directory(source,output_dir);r,request=_request(row,source,out)
    path=out/'request.json';_private_json(path,request)
    result=_command([str(discover_snpfit()/'launch'),'integration','--request',str(path)],out,
                    _child_environment(request['flux_native_runtime']),cancel_event)
    if not result.get('passed') or set(result.get('generated_views',[]))!={'schematic','symbol'}:
        raise RuntimeError('The fit/native verification did not produce both verified views. Log: '+result['log_path'])
    result['candidate_id']=r['id'];return result


def open_snpfit(row,source,output_dir=None):
    out=_directory(source,output_dir);r,request=_request(row,source,out,require_fit=False)
    path=out/'request.json';_private_json(path,request)
    log=out/'gui.log'
    with log.open('xb') as stream:
        process=subprocess.Popen([str(discover_snpfit()/'launch'),'gui','--request',str(path)],stdout=stream,stderr=subprocess.STDOUT,
                                 env=_child_environment(request['flux_native_runtime']),start_new_session=True)
    _private_json(out/'process.json',dict(pid=process.pid,process_group=process.pid,started=time.time(),action='snpfit_gui'))
    time.sleep(.3)
    if process.poll() is not None:raise RuntimeError('SNPFit Studio exited during startup. Log: '+str(log))
    return dict(status='started',pid=process.pid,log_path=str(log),run_dir=str(out),candidate_id=r['id'],
                target={k:request[k] for k in ('library','cell','workspace','cds_lib')})


def regenerate_layout(row,source,output_dir=None,cancel_event=None):
    r=_selected(row,source);out=_directory(source,output_dir)
    runtime=_runtime(source,r['source'])
    request=dict(row=r,runtime=runtime);path=out/'request.json';_private_json(path,request)
    # Isolate technology loading from the UI's current model/library and workers.
    return _command([sys.executable,str(Path(__file__).resolve()),'layout',str(path)],out,dict(os.environ),cancel_event)


def _layout_child(request):
    import backend,cadence
    r=request['row'];identity=r['source'];runtime=request['runtime']
    context={'source_process':{'uid':pwd.getpwnam(runtime['user']).pw_uid,'executable':runtime['virtuoso'],
                              'workspace':identity['workspace'],'environment':runtime['environment'],'pid':runtime.get('source_pid')},
             'environment':runtime['environment']}
    cadence.configure_context(context=context,workspace=identity['workspace'],cds_lib=identity['cds_lib'])
    entry=backend.register_library(identity['library'],identity['workspace'],identity['cds_lib'])
    if not entry.get('available'):raise ValueError(entry.get('error','Target library unavailable'))
    if entry['technology']!=identity['technology'] or Path(backend.LIBRARIES[identity['library']]['_tech']['library_path']).resolve()!=Path(identity['library_path']).resolve():
        raise ValueError('Selected result library/technology binding changed')
    target=Path(identity['library_path'])/identity['cell']/'layout'
    if target.exists():
        raise ValueError('This cell already has a layout. Existing layouts are preserved; choose another target cell before regeneration.')
    source=backend.capture_generation_context(r['design'])
    receipt=backend.generate_in_context(r['design'],source,allow_existing_cell=True)
    if not receipt.get('ok'):raise ValueError(str(receipt.get('errors','Layout generation failed')))
    receipt.update(generated_views=['layout'],target={k:identity[k] for k in ('library','cell','workspace','cds_lib')},candidate_id=r['id'],
                   retention='user_selected_layout_retained')
    return receipt


if __name__=='__main__':
    try:
        if len(sys.argv)!=3 or sys.argv[1]!='layout':raise ValueError('Expected layout and private request path')
        path=Path(sys.argv[2]);info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)&0o077 or info.st_uid!=os.getuid():
            raise ValueError('Action request must be a private regular file owned by the caller')
        print(json.dumps(_layout_child(json.loads(path.read_text())),allow_nan=False))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}));sys.exit(1)
