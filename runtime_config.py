"""Portable runtime discovery and exact design-library authorization.

Only existing installations are used. Runtime values are private process data;
public diagnostics never include license values or loader environment contents.
"""
import os
from pathlib import Path
import pwd
import shutil

ROOT = Path(__file__).resolve().parent
LAST_RUNTIME = None
NATIVE_ISOLATION_KEYS = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONHOME',
    'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY', 'QT_PLUGIN_PATH')
ENVIRONMENT_KEYS = frozenset(('PATH', 'CDS_LIC_FILE', 'LM_LICENSE_FILE',
    'CDSHOME', 'IC_HOME', 'CDS_ROOT', 'CDS_INST_DIR', 'OA_HOME',
    'CDS_AUTO_64BIT') + NATIVE_ISOLATION_KEYS)


def _captured_environment(values):
    """Retain private whitelisted states; None differs from a present empty value."""
    if not isinstance(values, dict):
        raise ValueError('The captured native environment must be a dictionary.')
    result = {}
    for key, value in values.items():
        if key not in ENVIRONMENT_KEYS:
            continue
        if value is not None and (not isinstance(value, str) or '\x00' in value
                                  or len(value) > 65536):
            raise ValueError('Invalid captured native environment field: %s.' % key)
        result[key] = value
    return result


def original_python_environment(environment):
    """Decode the launcher's first-clean snapshot without inspecting GUI overrides."""
    if environment.get('TFGEN_ORIGINAL_PYTHON_ENV_READY') != '1':
        return None
    result = {}
    for key in NATIVE_ISOLATION_KEYS:
        marker = 'TFGEN_ORIGINAL_' + key
        state = environment.get(marker + '_SET')
        if state == '0':
            result[key] = None
        elif state == '1' and marker in environment:
            result[key] = environment[marker]
        else:
            raise ValueError('Incomplete original native environment state: %s.' % key)
    return _captured_environment(result)


def native_environment(base_environment, captured_environment):
    """Build one OA child's environment, removing GUI values before restoration."""
    result = dict(base_environment)
    for key in NATIVE_ISOLATION_KEYS:
        result.pop(key, None)
    for key, value in _captured_environment(captured_environment).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _process(pid):
    try:
        folder = Path('/proc') / str(int(pid))
        status = folder.joinpath('status').read_text()
        uid = int(next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1])
        if os.geteuid() != 0 and uid != os.getuid():
            return None
        executable = folder.joinpath('exe').resolve(strict=True)
        if executable.name.lower() != 'virtuoso':
            return None
        args = folder.joinpath('cmdline').read_bytes().split(b'\0')
        if b'-nograph' in args or b'-ocean' in args:
            return None
        # /proc is a complete editor snapshot: absent keys are explicitly unset.
        environment = {key: None for key in NATIVE_ISOLATION_KEYS}
        for item in folder.joinpath('environ').read_bytes().split(b'\0'):
            if b'=' not in item:
                continue
            key, value = item.split(b'=', 1)
            key = key.decode('ascii', 'ignore')
            if key in ENVIRONMENT_KEYS:
                value = value.decode('utf-8', 'surrogateescape')
                if len(value) > 65536:
                    return None
                environment[key] = value
        return dict(pid=int(pid), uid=uid, executable=str(executable),
                    workspace=str(folder.joinpath('cwd').resolve(strict=True)),
                    environment=environment)
    except (OSError, ValueError, StopIteration):
        return None


def discover_source_process(workspace=None):
    """Prefer the actual invoking ancestor; otherwise one matching live editor."""
    pid, visited = os.getppid(), set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        result = _process(pid)
        if result:
            return result
        try:
            data = (Path('/proc') / str(pid) / 'stat').read_text()
            pid = int(data[data.rfind(')') + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    candidates = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            item = _process(entry.name)
            if item:
                candidates.append(item)
    if workspace:
        requested = Path(workspace).expanduser().resolve()
        matching = [c for c in candidates if Path(c['workspace']).resolve() == requested]
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            signatures = {(c['executable'], c['uid']) for c in matching}
            if len(signatures) == 1:
                return matching[0]
            raise ValueError('Several Virtuoso installations use this workspace. Launch Flux from the desired layout menu or set TFGEN_VIRTUOSO.')
    if len(candidates) == 1:
        return candidates[0]
    return None


def _workspace(start):
    if not start:
        return None
    path = Path(start).expanduser().resolve()
    if path.is_file() and path.name == 'cds.lib':
        return path.parent
    if not path.is_dir():
        return None
    for folder in (path,) + tuple(path.parents):
        if (folder / 'cds.lib').is_file():
            return folder
    return None


def helper_directories(executable):
    """Use helpers belonging to the selected installation, including symlinks."""
    executable = Path(executable).resolve()
    candidates = [executable.parent]
    for parent in executable.parents:
        if parent.name == 'tools' or parent.name.startswith('tools.'):
            candidates = [parent.parent / 'tools' / 'bin', parent / 'bin',
                          parent / 'dfII' / 'bin'] + candidates
            break
    result = []
    for path in candidates:
        if path.is_dir() and str(path) not in result:
            result.append(str(path))
    return result


def _executable(value, search_path):
    if not value:
        return None
    value = os.path.expanduser(str(value))
    candidate = Path(value) if '/' in value else Path(shutil.which(value, path=search_path) or '')
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return str(candidate.resolve())
    return None


def resolve_mapping(workspace, cds_lib=None):
    """Resolve a library mapping relative to its selected workspace."""
    base = Path(workspace).expanduser().resolve()
    mapping = Path(cds_lib).expanduser() if cds_lib else base / 'cds.lib'
    if not mapping.is_absolute():
        mapping = base / mapping
    return mapping.resolve()


def _source_environment(source):
    """Complete an older partial snapshot only from its matching live editor."""
    captured = _captured_environment(source.get('environment', {}))
    if any(key not in captured for key in NATIVE_ISOLATION_KEYS) and source.get('pid'):
        live = _process(source['pid'])
        if (live and live['uid'] == source.get('uid')
                and live['executable'] == source.get('executable')
                and live['workspace'] == source.get('workspace')):
            captured = dict(_captured_environment(live['environment']), **captured)
    return captured


def discover_runtime(context=None, workspace=None, cds_lib=None):
    global LAST_RUNTIME
    context = context or {}
    requested = workspace or context.get('workspace') or os.environ.get('TFGEN_WORKSPACE')
    initial = _workspace(requested or os.environ.get('TFGEN_LAUNCH_CWD') or os.getcwd())
    if initial is None and not requested:
        initial = _workspace(ROOT.parent)
    source = context.get('source_process')
    if not isinstance(source, dict):
        source = discover_source_process(initial)
    if source:
        if not isinstance(source.get('uid'), int) or (os.geteuid() != 0 and source['uid'] != os.getuid()):
            raise ValueError('The invoking Virtuoso belongs to a different user.')
    if requested:
        target_workspace = Path(requested).expanduser().resolve()
        if not target_workspace.is_dir():
            raise ValueError('The selected workspace does not exist.')
    else:
        target_workspace = initial or (Path(source['workspace']) if source else None)
    if target_workspace is None:
        raise ValueError('Cannot locate a workspace with cds.lib. Put the project beside the workspace cds.lib, launch from a layout menu, or set TFGEN_WORKSPACE in flux_config.sh.')
    target_workspace = target_workspace.resolve()
    mapping_value = cds_lib or context.get('cds_lib') or os.environ.get('TFGEN_CDS_LIB')
    mapping = resolve_mapping(target_workspace, mapping_value)
    if not mapping.is_file():
        raise ValueError('No library mapping file was found. Select cds.lib or set TFGEN_CDS_LIB in flux_config.sh.')
    environment = {key: value for key, value in os.environ.items()
                   if key in ENVIRONMENT_KEYS and key not in NATIVE_ISOLATION_KEYS}
    original = original_python_environment(os.environ)
    # Unknown legacy states do not authorize inheriting interpreter-only values.
    environment.update(original or {key: None for key in NATIVE_ISOLATION_KEYS})
    if original is None and 'TFGEN_CAPTURED_LD_LIBRARY_PATH' in os.environ:
        captured_loader = os.environ['TFGEN_CAPTURED_LD_LIBRARY_PATH']
        if captured_loader:
            environment['LD_LIBRARY_PATH'] = captured_loader
        else:
            # An interpreter-only loader path must not reach native children
            # when the invoking Cadence environment had no loader override.
            environment['LD_LIBRARY_PATH'] = None
    if source:
        environment.update(_source_environment(source))
    # A menu's E rows capture the actual current editor session, including nil;
    # unlike /proc they also see session-local environment updates in the CIW.
    environment.update(_captured_environment(context.get('environment', {})))
    explicit = os.environ.get('TFGEN_VIRTUOSO')
    binary = _executable(explicit or (source or {}).get('executable') or 'virtuoso', environment.get('PATH'))
    if not binary:
        raise ValueError('Cannot find a usable Virtuoso executable. Launch Flux from its layout menu, or set TFGEN_VIRTUOSO in flux_config.sh.')
    env_file = os.environ.get('TFGEN_CADENCE_ENV') or None
    if env_file:
        env_file = str(Path(env_file).expanduser().resolve())
        if not Path(env_file).is_file():
            raise ValueError('TFGEN_CADENCE_ENV must name an existing readable environment script.')
    run_user = os.environ.get('TFGEN_RUN_USER') if os.geteuid() == 0 else pwd.getpwuid(os.getuid()).pw_name
    if not run_user and source:
        run_user = pwd.getpwuid(source['uid']).pw_name
    if not run_user:
        owner = ROOT.stat().st_uid
        if owner != 0:
            run_user = pwd.getpwuid(owner).pw_name
    if not run_user or pwd.getpwnam(run_user).pw_uid == 0:
        raise ValueError('Run Flux as the design user. Root automation must provide a layout source or TFGEN_RUN_USER.')
    helpers = helper_directories(binary)
    snapshot = dict(workspace=str(target_workspace), cds_lib=str(mapping), virtuoso=binary,
        user=run_user, environment=environment, env_file=env_file,
        helper_dirs=helpers, source='explicit configuration' if explicit else
        ('running Virtuoso' if source else 'launch PATH'), source_pid=(source or {}).get('pid'))
    LAST_RUNTIME = snapshot
    return snapshot


def public_runtime(snapshot):
    return {key: value for key, value in snapshot.items() if key != 'environment'}


def permitted_library(library_path, workspace, technology, context=None):
    """Authorize one natively resolved mapped design library, never a whole disk."""
    target = Path(library_path).expanduser().resolve()
    binding = Path(technology.get('library_path') or '').expanduser().resolve()
    if target != binding or not target.is_dir():
        raise ValueError('The design-library path changed. Reload its current workspace mapping.')
    mapping = (context or {}).get('cds_lib') or technology.get('cds_lib') or str(Path(workspace) / 'cds.lib')
    if not Path(workspace).is_dir() or not Path(mapping).is_file():
        raise ValueError('The target must come from an existing workspace library mapping.')
    library, tech_name = technology.get('library'), technology.get('technology')
    if not library or not tech_name or library == tech_name:
        raise ValueError('Technology libraries cannot be generation targets. Select an attached design library.')
    if any(part.lower() in ('pdk', 'pdks', 'foundry') for part in target.parts):
        raise ValueError('Foundry installation directories are protected. Select a design library.')
    if LAST_RUNTIME:
        for parent in Path(LAST_RUNTIME['virtuoso']).resolve().parents:
            if parent.name == 'tools' or parent.name.startswith('tools.'):
                if _within(target, parent.parent):
                    raise ValueError('Cadence installation libraries are protected. Select a writable design library.')
                break
    tech_path = technology.get('technology_path')
    if tech_path:
        tech_path = Path(tech_path).expanduser().resolve()
        tech_dir = tech_path if tech_path.is_dir() else tech_path.parent
        if _within(target, tech_dir):
            raise ValueError('The target is inside its technology library. Foundry libraries are protected.')
    if context and context.get('library_path') and Path(context['library_path']).resolve() != target:
        raise ValueError('The authorized target-library binding changed. Reload the library.')
    explicit_root = os.environ.get('TFGEN_DESIGN_ROOT')
    if explicit_root:
        design_root = Path(explicit_root).expanduser().resolve()
        if design_root == Path('/') or not design_root.is_dir() or not _within(target, design_root):
            raise ValueError('The selected library is outside TFGEN_DESIGN_ROOT. Select a library in that configured design directory.')
    return target
