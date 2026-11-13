"""Flux integration: explicit immutable source, ports and native target."""
import copy
import hashlib
import json
import os
import pwd
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from . import studio_engine as engine
from .delivery import library_mappings


def _comparison(curves):
    curves = curves or {}
    branches = curves.get('branches') or {}
    original, fitted = {}, {}
    for index, (name, row) in enumerate(branches.items()):
        suffix = '' if len(branches) == 1 else ('p' if index == 0 else 's')
        for metric, source, fit in [('L', 'source_l_h', 'fit_l_h'), ('Q', 'source_q', 'fit_q')]:
            scale = 1e9 if metric == 'L' else 1.0
            original[metric + suffix] = [None if x is None else x * scale for x in row.get(source, [])]
            fitted[metric + suffix] = [None if x is None else x * scale for x in row.get(fit, [])]
    if curves.get('source_k') is not None:
        original['k'], fitted['k'] = curves['source_k'], curves.get('fit_k')
    return {'frequency_hz': curves.get('freq_hz', []), 'original': original, 'fitted': fitted}


def _validate(request):
    cfg = copy.deepcopy(request)
    for key in ('source_path', 'source_sha256', 'model_id', 'port_order', 'ports_confirmed',
                'short_unused', 'workspace', 'cds_lib', 'library', 'cell', 'runtime'):
        if key not in cfg or cfg[key] in ('', None):
            raise ValueError('Missing required field: '+key)
    for key in ('source_path', 'workspace', 'cds_lib', 'runtime'):
        if not Path(cfg[key]).is_absolute(): raise ValueError(key+' must be an absolute path')
    workspace = Path(cfg['workspace']).resolve()
    if not Path(cfg['cds_lib']).is_file():
        raise ValueError('The specified cds_lib must exist')
    digest = hashlib.sha256(Path(cfg['source_path']).read_bytes()).hexdigest()
    if not re.fullmatch(r'[0-9a-fA-F]{64}', str(cfg['source_sha256'])) or digest != cfg['source_sha256'].lower():
        raise ValueError('Source SHA-256 does not match the request')
    cfg['source_sha256'] = digest
    if cfg['ports_confirmed'] is not True or type(cfg['short_unused']) is not bool:
        raise ValueError('Explicit confirmed port mapping and termination are required')
    for key in ('library', 'cell'):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', cfg[key]): raise ValueError('Invalid '+key)
    mapped = library_mappings(workspace, cfg['cds_lib']).get(cfg['library'])
    if not mapped or not Path(mapped).is_dir(): raise ValueError('Target library is not mapped to an existing directory')
    if cfg.get('existing_library_path') and Path(cfg['existing_library_path']).resolve() != Path(mapped).resolve():
        raise ValueError('Explicit library path conflicts with workspace mapping')
    from .network import read_touchstone
    from .sampled_fit import SampledNetwork
    source = read_touchstone(cfg['source_path'], network_class=SampledNetwork)
    if sum(source.freq > 0) < 2:
        raise ValueError('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.')
    cfg.update(l_pct=5, q_pct=20)
    return cfg, Path(mapped).resolve()


def _copy_owned(source, destination):
    shutil.copytree(str(source), str(destination), copy_function=shutil.copy2)
    if os.geteuid() == 0:
        for src in [source] + list(source.rglob('*')):
            if src.is_symlink(): raise ValueError('Symlink in staged view')
            stat = src.stat()
            os.chown(str(destination / src.relative_to(source)), stat.st_uid, stat.st_gid)
    for src in source.rglob('*'):
        if src.is_symlink(): raise ValueError('Symlink in staged view')
        if src.is_file():
            dest = destination / src.relative_to(source)
            if hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(dest.read_bytes()).digest():
                raise ValueError('Copied native view differs from verified staged view')


def _promote(staged, libpath, cell, backup_root, overwrite, verify=None):
    target = libpath/cell
    if target.is_symlink() or (target.exists() and not target.is_dir()): raise ValueError('Unsafe target cell path')
    if target.exists():
        for path in target.rglob('*'):
            if path.is_symlink() or 'lock' in path.name.lower(): raise ValueError('Target contains a symlink or lock; no views changed')
    previous = [v for v in ('schematic','symbol') if (target/v).exists()]
    if previous and not overwrite: raise ValueError('Target views exist; set overwrite=true to replace them')
    for view in ('schematic','symbol'):
        if not (staged/view).is_dir() or (staged/view).is_symlink(): raise ValueError('Validated staged view is missing: '+view)
    backup = backup_root/('backup-'+uuid.uuid4().hex)
    backup.mkdir(parents=True)
    for view in previous: shutil.copytree(str(target/view), str(backup/view), copy_function=shutil.copy2)
    new_cell = not target.exists()
    target.mkdir(exist_ok=True)
    if new_cell and os.geteuid() == 0:
        owner = staged.stat()
        os.chown(str(target), owner.st_uid, owner.st_gid)
    old, installed, pending_paths = [], [], []
    try:
        for view in ('schematic','symbol'):
            pending = target/('.snpfit-new-'+view+'-'+uuid.uuid4().hex)
            pending_paths.append(pending)
            _copy_owned(staged/view, pending)
            if (target/view).exists():
                saved = target/('.snpfit-old-'+view+'-'+uuid.uuid4().hex)
                (target/view).rename(saved); old.append((saved,target/view))
            pending.rename(target/view); installed.append(target/view)
        if verify is not None: verify()
    except BaseException:
        for path in reversed(installed): shutil.rmtree(str(path))
        for saved, destination in reversed(old): saved.rename(destination)
        for pending in pending_paths:
            if pending.exists(): shutil.rmtree(str(pending))
        if new_cell and target.exists() and not any(target.iterdir()): target.rmdir()
        raise
    for saved, _ in old: shutil.rmtree(str(saved))
    return str(backup) if previous else None


def _aliased_native(circuit, plan, aliases):
    aliases = aliases or {}
    if not isinstance(aliases, dict) or any(k not in circuit['ports'] for k in aliases):
        raise ValueError('Terminal aliases must name actual signal roles')
    ports = [aliases.get(p,p) for p in circuit['ports']]
    if len(set(ports)) != len(ports) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', str(p)) for p in ports):
        raise ValueError('Terminal aliases must be unique portable identifiers')
    nodes = {e[key] for e in circuit['elements'] for key in ('n1','n2') if key in e}
    internal = nodes - set(circuit['ports'])
    if internal.intersection(ports) or circuit.get('reference','REF') in ports:
        raise ValueError('Terminal alias conflicts with an internal circuit node or reference')
    cloned = copy.deepcopy(circuit); cloned['ports'] = ports
    for element in cloned['elements']:
        for key in ('n1', 'n2'):
            if key in element: element[key] = aliases.get(element[key], element[key])
    def replace(value):
        if isinstance(value, dict): return {k:replace(v) for k,v in value.items()}
        if isinstance(value, list): return [replace(v) for v in value]
        return aliases.get(value,value) if isinstance(value,str) else value
    return cloned, replace(plan)


def _native_context(cfg):
    from .native import _NATIVE_KEYS, _NATIVE_EXTRA_KEYS
    runtime = cfg.get('flux_native_runtime') or {}
    if not isinstance(runtime, dict): raise ValueError('flux_native_runtime must be an object')
    for key in ('workspace', 'cds_lib'):
        if runtime.get(key) and Path(runtime[key]).resolve() != Path(cfg[key]).resolve():
            raise ValueError('Native runtime '+key+' conflicts with target')
    env = runtime.get('environment')
    snapshot = None
    if env is not None:
        required = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')
        if not isinstance(env, dict) or any(k not in env for k in required):
            raise ValueError('Native environment snapshot is incomplete')
        snapshot = {}
        for key in _NATIVE_KEYS:
            value = env.get(key)
            if value is not None and not isinstance(value, str): raise ValueError('Invalid native environment state')
            snapshot[key] = {'set':value is not None, 'value':value}
        for key in _NATIVE_EXTRA_KEYS:
            if key in env:
                value = env[key]
                if value is not None and not isinstance(value, str): raise ValueError('Invalid native environment state')
                snapshot[key] = {'set':value is not None, 'value':value}
    options = {k:v for k,v in {
        'user':runtime.get('user') or cfg.get('native_user'),
        'env_script':runtime.get('env_file') or cfg.get('env_script'),
        'virtuoso':runtime.get('virtuoso') or cfg.get('virtuoso'),
        'ocean':runtime.get('ocean') or cfg.get('ocean'),
        'spectre':runtime.get('spectre') or cfg.get('spectre'),
    }.items() if v}
    captured_path = env.get('PATH') if isinstance(env, dict) else os.environ.get('PATH', '')
    paths = list(runtime.get('helper_dirs') or [])
    if options.get('virtuoso'):
        for parent in list(Path(options['virtuoso']).resolve().parents)[:6]:
            candidate = parent/'bin'
            if candidate.is_dir(): paths.append(str(candidate))
    paths.extend((captured_path or '').split(os.pathsep))
    search_path = os.pathsep.join(path for path in paths if path and Path(path).is_absolute())
    for key in ('virtuoso', 'ocean', 'spectre'):
        selected = options.get(key) or shutil.which(key, path=search_path)
        if not selected or not Path(selected).is_file() or not os.access(selected, os.X_OK):
            raise ValueError('Native '+key+' executable could not be discovered; specify it in native runtime.')
        options[key] = str(Path(selected).absolute())
    return options, snapshot


def run_request(request, progress=None, cancel_event=None):
    """Fit original samples and independently verify native OA replay."""
    try:
        cfg, library_path = _validate(request)
        options, env = _native_context(cfg)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'status':'ERROR', 'passed':False, 'native_verified':False, 'message':str(exc), 'generated_views':[], 'acceptance_scope':'original_sampled_points_only', 'broadband_qualified':False}
    runtime = Path(cfg['runtime']).resolve(); runtime.mkdir(parents=True, exist_ok=True)
    run = runtime/('integration-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]); run.mkdir(mode=0o700)
    log_path = run/'integration.log'
    target = {'workspace':cfg['workspace'], 'cds_lib':cfg['cds_lib'], 'library':cfg['library'], 'cell':cfg['cell'], 'library_path':str(library_path)}
    response = {'status':'ERROR', 'passed':False, 'native_verified':False, 'target':target,
                'acceptance_scope':'original_sampled_points_only', 'broadband_qualified':False,
                'generated_views':[], 'run_dir':str(run), 'log_path':str(log_path), 'comparison':None}
    def log(msg):
        with log_path.open('a', encoding='utf-8') as f: f.write(str(msg)+'\n')
        if progress: progress(str(msg))
    def check_cancel():
        if cancel_event is not None and cancel_event.is_set(): raise InterruptedError('Operation cancelled')
    stage_lib = None
    try:
        from .sampled_fit import fit_sampled, verify_native_sampled
        from .native import build_native, replay_native, build_symbol, verify_published
        log('Target: '+cfg['library']+'/'+cfg['cell']+' in '+str(library_path))
        snapshot = fit_sampled(cfg, run/'fit', cancel_event=cancel_event, progress=log)
        response.update(status=snapshot['status'], metrics=snapshot['metrics'], comparison=snapshot['comparison'], result_path=str(run/'fit'/'sampled-fit.json'))
        if not snapshot['passed']:
            log('Sampled-point numerical targets were not met; target views were not changed.')
            return response
        check_cancel()
        stage_lib = Path(tempfile.mkdtemp(prefix='.snpfit-stage-', dir=str(library_path.parent)))
        owner = pwd.getpwnam(options['user']) if options.get('user') else pwd.getpwuid(Path(cfg['workspace']).stat().st_uid)
        if os.geteuid() == 0: os.chown(str(stage_lib), owner.pw_uid, owner.pw_gid)
        # Visual annotations use the selected actual sampled band where possible.
        plan_cfg = dict(cfg)
        f = snapshot['frequencies_hz']
        plan_cfg.setdefault('fmin_ghz', min(f)/1e9)
        plan_cfg.setdefault('fmax_ghz', max(f)/1e9)
        if float(plan_cfg['fmin_ghz']) == float(plan_cfg['fmax_ghz']):
            plan_cfg['fmax_ghz'] = float(plan_cfg['fmin_ghz']) * (1 + 1e-9)
        plan_cfg['native_report_z0_ohm'] = snapshot['z0']
        plan_cfg['native_report_fixture'] = 'sampled_open'
        plan = engine._visual_plan(snapshot['circuit'], plan_cfg, snapshot['source'])
        circuit, plan = _aliased_native(snapshot['circuit'], plan, cfg.get('terminal_aliases'))
        native_options = {k:options[k] for k in ('user','env_script','virtuoso','ocean') if k in options}
        native = build_native(circuit,cfg['workspace'],run/'native',cfg['library'],cfg['cell'],
                              existing_library_path=str(stage_lib),cds_lib=cfg['cds_lib'],native_env_snapshot=env,
                              visual_plan=plan,make_symbol=False,portable_runtime=True,cancel=cancel_event.is_set if cancel_event else None,**native_options)
        check_cancel()
        replay_options = {k:options[k] for k in ('user','env_script','spectre') if k in options}
        replay = replay_native(native, snapshot['frequencies_hz'], snapshot['z0'], native_env_snapshot=env,
                               cancel=cancel_event,portable_runtime=True,**replay_options)
        verified = verify_native_sampled(snapshot, replay['response'])
        response.update(metrics=verified['metrics'], comparison=verified['comparison'])
        if not verified['passed']:
            response['status']='NATIVE_NOT_ACCEPTED'
            log('Native replay failed sampled-point L/Q acceptance; target views were not changed.')
            return response
        check_cancel()
        native['sampled_metrics'] = verified['metrics']
        native['replay'] = replay
        symbol_options = {k:options[k] for k in ('user','env_script','virtuoso') if k in options}
        symbol = build_symbol(native,circuit,plan,out_dir=run/'symbol',native_env_snapshot=env,
                              cancel=cancel_event.is_set if cancel_event else None,portable_runtime=True,**symbol_options)
        check_cancel()
        if hashlib.sha256(Path(cfg['source_path']).read_bytes()).hexdigest() != cfg['source_sha256'].lower():
            raise ValueError('Source changed before installation; target views were not changed')
        published = {}
        def verify_target():
            check_cancel()
            log('Reopening published schematic and symbol read-only to verify target connectivity.')
            published.update(verify_published(circuit, plan, cfg['workspace'], cfg['cds_lib'], cfg['library'], cfg['cell'], library_path, run/'published', native_env_snapshot=env, cancel=cancel_event.is_set if cancel_event else None, **symbol_options))
            check_cancel()
        backup = _promote(stage_lib/cfg['cell'], library_path, cfg['cell'], run, request.get('overwrite') is True, verify=verify_target)
        response.update(status='VERIFIED_SAMPLED', passed=True, native_verified=True,
                        generated_views=['schematic','symbol'], backup_path=backup, published_verified=True, published=published,
                        native={'replay':replay, 'schematic_sha256':native.get('schematic_sha256'),
                                'symbol_verified':bool(symbol.get('symbol_verified')), 'schematic_ports':native.get('schematic_ports'), 'reference_exported':native.get('reference_exported'), 'reference_semantics':'explicit pin' if native.get('reference_exported') else 'internal global gnd!'})
        log('Validated schematic and symbol installed; backup: '+str(backup))
        return response
    except Exception as exc:
        response.update(status='CANCELLED' if isinstance(exc, InterruptedError) or (cancel_event is not None and cancel_event.is_set()) else 'ERROR', message=str(exc))
        log('ERROR: '+str(exc))
        return response
    finally:
        if stage_lib is not None: shutil.rmtree(str(stage_lib), ignore_errors=True)
        (run/'response.json').write_text(json.dumps(response, indent=2, allow_nan=False)+'\n', encoding='utf-8')
