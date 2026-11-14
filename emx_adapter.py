"""Portable, explicit EMX handoff for generated RF passives.

The UI/optimizer may call discover_config -> prepare_job -> export_layout ->
run_job from its worker.  No extraction is submitted by discovery/preparation.
Process/material accuracy is separate from a successful native-pin extraction.
Only Python's standard library is required; native environments stay in children.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import shlex
import stat
import struct
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import runtime_config

_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
_DIRECTIONS = {'bottom': 'B', 'top': 'T', 'left': 'L', 'right': 'R'}
_CONFIG_KEYS = ('EMX_path', 'EMX_program', 'EMX_layer_table', 'EMX_process_path', 'EMX_process_name')


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file(value, field, base=None, executable=False):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('EMX {} must name an existing {}.'.format(field, 'executable' if executable else 'file'))
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base or os.getcwd()) / path
    path = path.resolve()
    if not path.is_file() or not os.access(str(path), os.R_OK | (os.X_OK if executable else 0)):
        raise ValueError('EMX {} is unavailable: {}.'.format(field, path))
    return str(path)


def _literal_assignments(path):
    """Read only literal strings/nil/quoted lists; never evaluate SKILL code."""
    from technology import parse_skill
    text = Path(path).read_text(encoding='utf-8')
    if len(text.encode('utf-8')) > 1024 * 1024:
        raise ValueError('EMX configuration exceeds the bounded literal reader limit.')
    result = {}
    for key in _CONFIG_KEYS:
        matches = list(re.finditer(r'(?m)^\s*' + re.escape(key) + r'\s*=\s*', text))
        for match in matches:
            pos = match.end()
            if pos < len(text) and text[pos] == "'":
                pos += 1
            start = pos
            if pos >= len(text):
                continue
            quote, escaped, depth = False, False, 0
            if text[pos] not in ('"', '(') and not text[pos:].startswith('nil'):
                continue
            while pos < len(text):
                char = text[pos]
                if quote:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == '"':
                        quote = False
                        if depth == 0:
                            pos += 1
                            break
                elif char == '"':
                    quote = True
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
                elif depth == 0 and char in '\r\n;':
                    break
                elif char == ';':
                    # Comments inside complex expressions are unsupported,
                    # rather than guessed into a possibly different process.
                    break
                pos += 1
            try:
                value = parse_skill(text[start:pos].strip())
            except ValueError:
                continue
            if value is None or isinstance(value, (str, list)):
                result[key] = value
    return result


def _technology_value(value, technology):
    if isinstance(value, list):
        entries = [row[1] for row in value if isinstance(row, list) and len(row) == 2 and row[0] == technology]
        if len(entries) > 1:
            raise ValueError('Ambiguous EMX configuration for attached technology {}.'.format(technology))
        return entries[0] if entries else None
    return value


def _native_tool_hints(runtime):
    """Read only native child tool path hints; never publish the environment."""
    if not runtime.get('env_file') or not runtime.get('user'):
        return {}
    script = 'printf "\\nFLUX_TOOL_HINTS\\0%s\\0%s\\0%s\\0" "${EMX_path-${EMX_PATH-}}" "${EMX_interface_path-${EMX_INTERFACE_PATH-}}" "${PATH-}"'
    command, environment = _native_command(['/bin/bash', '-c', script], runtime)
    child = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        try:
            output, _ = child.communicate(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Native EMX tool-path discovery timed out while reading the configured environment script.') from exc
    finally:
        cleanup = _cleanup_group(child)
        if not cleanup['verified_stopped']:
            raise ValueError('Native EMX tool-path discovery could not stop its owned child process group.')
    marker = b'\nFLUX_TOOL_HINTS\0'
    if child.returncode != 0 or marker not in output or len(output) > 1024 * 1024:
        raise ValueError('Native EMX tool-path discovery failed using the configured environment script.')
    parts = output.rsplit(marker, 1)[1].split(b'\0')
    if len(parts) != 4 or parts[-1]:
        raise ValueError('Native EMX tool-path discovery returned malformed hints.')
    try:
        direct, interface, path = [part.decode('utf-8') for part in parts[:3]]
    except UnicodeDecodeError as exc:
        raise ValueError('Native EMX tool-path hints are not valid UTF-8.') from exc
    return {'emx_path': direct, 'interface': interface, 'path': path}


def _related_emx_configs(virtuoso):
    """Bound discovery to EMX* siblings of the actual Cadence install root."""
    if not virtuoso:
        return []
    executable = Path(virtuoso).expanduser().resolve()
    root = next((parent for parent in executable.parents
                 if (parent / 'tools.lnx86').is_dir() or (parent / 'tools').is_dir()), None)
    if root is None:
        return []
    try:
        siblings = list(root.parent.iterdir())
    except OSError:
        return []
    if len(siblings) > 512:
        raise ValueError('Too many sibling products for bounded EMX discovery; set config_file explicitly.')
    result = []
    for sibling in siblings:
        if not sibling.name.upper().startswith('EMX') or not sibling.is_dir():
            continue
        for suffix in ('virtuoso_ui/emxinterface/emxskill/emxconfig.il', 'emxinterface/emxskill/emxconfig.il'):
            candidate = sibling / suffix
            if candidate.is_file():
                result.append(candidate.resolve())
    return result


def discover_config(technology, runtime=None, overrides=None, environment=None):
    """Resolve existing tools and an exact techlib process/map association.

    Explicit fields: emx_executable, strmout_executable, process_file, layer_map,
    config_file, technology, process_qualification.  A config process scalar is
    accepted only when the caller explicitly sets allow_global_process=True.
    Configuration-relative paths are based on its own directory, not the GUI.
    """
    if not isinstance(technology, str) or not technology:
        raise ValueError('An actual attached technology identity is required for EMX.')
    supplied = dict(overrides or {})
    env = dict(os.environ if environment is None else environment)
    runtime = runtime or {}
    base = runtime.get('workspace') or os.getcwd()
    if supplied.get('technology') not in (None, technology):
        raise ValueError('EMX profile technology does not match the actual attached technology.')
    explicit_executable = supplied.get('emx_executable') or env.get('TFGEN_EMX')
    explicit_config = supplied.get('config_file') or env.get('TFGEN_EMX_CONFIG')
    if explicit_config:
        _file(str(explicit_config), 'configuration', base)
    if explicit_executable and '/' in explicit_executable:
        _file(explicit_executable, 'executable', base, executable=True)
    hints = {}
    if runtime.get('env_file') and (not explicit_executable or '/' not in explicit_executable):
        hints = _native_tool_hints(runtime)
    native_path = hints.get('path') if 'path' in hints else runtime.get('environment', {}).get('PATH')
    search_path = env.get('PATH', os.defpath) if native_path is None else native_path
    if explicit_executable and '/' not in explicit_executable and not shutil.which(explicit_executable, path=search_path):
        raise ValueError('EMX executable is unavailable: {} (explicit override; PATH searched).'.format(explicit_executable))
    direct = env.get('EMX_path') or env.get('EMX_PATH') or hints.get('emx_path')
    interface = env.get('EMX_interface_path') or env.get('EMX_INTERFACE_PATH') or hints.get('interface')
    path_program = shutil.which('emx', path=search_path)
    config_path = explicit_config
    config_source = 'explicit configuration' if explicit_config else None
    candidates = []
    if not config_path:
        candidates.append(Path(base) / 'emxconfig.il')
        if interface:
            candidates.append(Path(interface) / 'emxskill' / 'emxconfig.il')
        program_hint = explicit_executable or (str(Path(direct) / 'emx') if direct else path_program)
        if program_hint:
            resolved = Path(program_hint).expanduser()
            if resolved.is_file():
                parent = resolved.resolve().parent
                for installation in (parent, parent.parent):
                    candidates.extend((installation / 'virtuoso_ui' / 'emxinterface' / 'emxskill' / 'emxconfig.il',
                                       installation / 'emxinterface' / 'emxskill' / 'emxconfig.il'))
        # An active workspace/interface config is authoritative. Only search
        # adjacent products when no such direct config exists.
        found = list(dict.fromkeys(str(p.resolve()) for p in candidates if p.is_file()))
        if not found:
            found = list(dict.fromkeys(str(p) for p in _related_emx_configs(runtime.get('virtuoso'))))
            if found:
                config_source = 'adjacent product of the selected Cadence installation'
        elif found:
            config_source = 'workspace, interface or executable location'
        if len(found) > 1:
            raise ValueError('Several EMX configurations were found: {}. Set config_file or TFGEN_EMX_CONFIG.'.format(', '.join(found)))
        config_path = found[0] if found else None
    values = {}
    config_base = base
    if config_path:
        config_path = _file(str(config_path), 'configuration', base)
        values = _literal_assignments(config_path)
        config_base = str(Path(config_path).parent)
    attempts = []
    executable = None
    if explicit_executable:
        attempts.append('explicit executable: ' + str(explicit_executable))
        executable = (shutil.which(explicit_executable, path=search_path) if '/' not in explicit_executable else explicit_executable)
        if not executable:
            raise ValueError('EMX executable is unavailable: {} (explicit override; PATH searched).'.format(explicit_executable))
        executable = _file(executable, 'executable', base, executable=True)
    else:
        program = values.get('EMX_program') or 'emx'
        if not isinstance(program, str) or not program or '\x00' in program:
            raise ValueError('Invalid EMX_program in configuration {}.'.format(config_path))
        installed = values.get('EMX_path') or direct
        if installed:
            installed_path = Path(installed).expanduser()
            if not installed_path.is_absolute():
                installed_path = Path(config_base) / installed_path
            program_path = Path(program).expanduser()
            candidate = program_path if program_path.is_absolute() else installed_path / program_path
            attempts.append('configuration/tool hint: ' + str(candidate))
            # A selected config's explicit path must not silently switch tools.
            if values.get('EMX_path'):
                executable = _file(str(candidate), 'executable (configuration {})'.format(config_path), config_base, executable=True)
            elif candidate.is_file() and os.access(str(candidate), os.R_OK | os.X_OK):
                executable = str(candidate.resolve())
        if not executable:
            if '/' in program:
                candidate = Path(program).expanduser()
                if not candidate.is_absolute(): candidate = Path(config_base) / candidate
                attempts.append('configuration program: ' + str(candidate))
                if candidate.is_file() and os.access(str(candidate), os.R_OK | os.X_OK): executable = str(candidate.resolve())
            else:
                attempts.append('native PATH program: ' + program)
                executable = shutil.which(program, path=search_path)
        if not executable:
            raise ValueError('EMX executable is unavailable; tried {}. Configuration candidates: {}. Set emx_executable or config_file.'.format('; '.join(attempts), ', '.join(str(p) for p in candidates) or 'none'))
        executable = _file(executable, 'executable', config_base, executable=True)
    strmout = supplied.get('strmout_executable') or env.get('TFGEN_STRMOUT')
    if not strmout and runtime.get('virtuoso'):
        sibling = Path(runtime['virtuoso']).resolve().parent / 'strmout'
        if sibling.is_file():
            strmout = str(sibling)
    if not strmout:
        strmout = shutil.which('strmout', path=search_path)
    strmout = _file(strmout, 'strmout executable', base, executable=True)
    process = supplied.get('process_file') or env.get('TFGEN_EMX_PROCESS')
    if process:
        process = _file(process, 'process file', base)
        process_source = 'explicit project override'
    else:
        directory = values.get('EMX_process_path')
        name = values.get('EMX_process_name')
        if not supplied.get('allow_global_process') and (not isinstance(directory, list) or not isinstance(name, list)):
            raise ValueError('No exact EMX process association for attached technology {}. Set an explicit process_file; no generic-PDK fallback is used.'.format(technology))
        directory, name = _technology_value(directory, technology), _technology_value(name, technology)
        if not isinstance(directory, str) or not isinstance(name, str) or not name:
            raise ValueError('No named EMX process association for attached technology {}. A directory-first-file fallback is not permitted.'.format(technology))
        process = _file(str(Path(directory) / name), 'process file', config_base)
        process_source = 'exact attached-technology association'
    layer_map = supplied.get('layer_map') or env.get('TFGEN_EMX_LAYER_MAP')
    if layer_map:
        layer_map = _file(layer_map, 'layer map', base)
    else:
        layer_map = _file(_technology_value(values.get('EMX_layer_table'), technology), 'layer map for ' + technology, config_base)
    qualification = supplied.get('process_qualification', 'unverified process accuracy')
    warnings = ['Process accuracy is not certified by an extraction completing; verify the destination PDK option and material provenance.']
    result = dict(supplied)
    result.update(technology=technology, emx_executable=executable, strmout_executable=strmout,
                  process_file=process, layer_map=layer_map, config_file=config_path,
                  process_source=process_source, process_qualification=qualification, warnings=warnings,
                  config_source=config_source, config_sha256=_hash(config_path) if config_path else None)
    return result


def port_contract(geometry, config=None):
    """Keep all signal/tap pins; grounds are explicitly declared but not driven."""
    config = config or {}
    grouped = {}
    order = []
    for port in geometry.get('ports', []):
        name = port.get('name')
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError('EMX terminal names must be safe explicit identifiers.')
        kind = port.get('kind', 'ground' if name in ('GND_PGS', 'GND_RING') else 'signal')
        if kind not in ('signal', 'tap', 'ground'):
            raise ValueError('Unknown EMX terminal kind for {}.'.format(name))
        if not isinstance(port.get('layer'), str) or not port['layer']:
            raise ValueError('EMX terminal {} has no layer identity.'.format(name))
        box = port.get('bbox')
        if (not isinstance(box, list) or len(box) != 2 or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in box)
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for p in box for v in p)
                or any(box[1][i] <= box[0][i] for i in (0, 1))):
            raise ValueError('EMX terminal {} has an invalid physical rectangle.'.format(name))
        direction = port.get('access_direction')
        if direction not in (None, 'internal') and direction not in _DIRECTIONS:
            raise ValueError('EMX terminal {} has an invalid access direction.'.format(name))
        row = grouped.get(name)
        if row is None:
            row = dict(name=name, kind=kind, winding=port.get('winding'), endpoint=port.get('endpoint'), regions=[])
            grouped[name] = row
            order.append(name)
        elif any(row[key] != value for key, value in (('kind', kind), ('winding', port.get('winding')), ('endpoint', port.get('endpoint')))):
            raise ValueError('Inconsistent repeated EMX terminal {}.'.format(name))
        row['regions'].append(dict(layer=port['layer'], purpose=port.get('purpose', 'drawing'),
                                   bbox=copy.deepcopy(box), access_direction=direction))
    if not grouped:
        raise ValueError('EMX requires actual generated layout pins.')
    grounds = config.get('ground_ports')
    if grounds is None:
        grounds = [name for name in order if grouped[name]['kind'] == 'ground']
    if not isinstance(grounds, (list, tuple)) or len(set(grounds)) != len(grounds) or any(name not in grouped for name in grounds):
        raise ValueError('EMX ground_ports must reference distinct actual pins.')
    signals = config.get('signal_ports')
    expected = [name for name in order if name not in grounds]
    if signals is None:
        signals = expected
    if not isinstance(signals, (list, tuple)) or len(signals) != len(expected) or set(signals) != set(expected):
        raise ValueError('EMX signals must retain every non-ground pin, including TAP; explicitly ground a TAP only when intended.')
    if not signals:
        raise ValueError('EMX requires at least one signal pin.')
    protocol = config.get('ground_protocol', 'fixed_ground_pins')
    if protocol not in ('fixed_ground_pins', 'local_returns'):
        raise ValueError('Unsupported EMX ground protocol.')
    definitions, included = [], []
    for index, name in enumerate(signals):
        solver_name = 'P{:03d}'.format(index)
        label = name
        if protocol == 'local_returns':
            returned = config.get('return_map', {}).get(name)
            if returned not in grounds:
                raise ValueError('Each local-return signal requires an actual ground pin in return_map.')
            label += ':' + returned
        definitions.append(dict(solver_name=solver_name, pin=name, label=label, driven=True))
        included.append(solver_name)
    if protocol == 'fixed_ground_pins':
        for index, name in enumerate(grounds, len(signals)):
            definitions.append(dict(solver_name='P{:03d}'.format(index), pin=name, label=name, driven=False))
    return dict(terminals=[grouped[name] for name in order], signals=list(signals), grounds=list(grounds),
                definitions=definitions, included=included, protocol=protocol,
                reference=('proc stack-bottom reference with selected ground contacts fixed to zero' if grounds else
                           'proc stack-bottom reference; no physical ground contacts selected') if protocol == 'fixed_ground_pins'
                else 'explicit local return contacts; distinct from fixed Ground pins')


def _run_path(value):
    """Do not canonicalize a task-root symlink into an apparently safe path."""
    path = Path(os.path.abspath(str(Path(value).expanduser())))
    if len(path.parents) > 128:
        raise ValueError('EMX run_dir exceeds the bounded directory-depth limit.')
    for candidate in reversed((path,) + tuple(path.parents)):
        if candidate.is_symlink():
            raise ValueError('EMX run_dir must not traverse a symbolic link: {}.'.format(candidate))
    return path


def resolve_settings(config):
    """Resolve one public, replayable settings record without shell execution."""
    config = dict(config or {})
    result = {}
    for field, default in (('edge_um', .5), ('thickness_um', .5), ('via_merge_um', .5)):
        value = config.get(field, default)
        if isinstance(value, bool):
            raise ValueError('EMX {} must be a positive finite number.'.format(field))
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError('EMX {} must be a positive finite number.'.format(field))
        if not math.isfinite(value) or value <= 0:
            raise ValueError('EMX {} must be a positive finite number.'.format(field))
        result[field] = value
    three_d = config.get('three_d', False)
    if not isinstance(three_d, bool):
        raise ValueError('EMX three_d must be true or false.')
    result['three_d'] = three_d
    parallel = config.get('parallel', 2)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ValueError('EMX parallel must be a positive integer.')
    result['parallel'] = parallel
    for field, minimum in (('memory_mb', 64), ('timeout_seconds', 0)):
        value = config.get(field)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value < minimum:
                raise ValueError('EMX {} is invalid.'.format(field))
            if field == 'memory_mb' and (not isinstance(value, int)):
                raise ValueError('EMX memory_mb must be an integer.')
        result[field] = value
    # New options_text supersedes both legacy extra_options and the 3D boolean.
    # If no selector is present, the legacy boolean supplies the default.
    modern = 'options_text' in config
    raw = config.get('options_text') if modern else config.get('extra_options', '')
    if not isinstance(raw, str):
        raise ValueError('EMX options must be text.')
    try:
        extra = shlex.split(raw)
    except ValueError as exc:
        raise ValueError('Invalid EMX extra options: {}'.format(exc)) from exc
    managed = ('--port', '--include', '--exclude', '--cadence-pins', '--format', '--s-file', '--y-file',
               '--z-file', '--output', '--parallel', '--max-memory', '--edge-width', '--thickness',
               '--via-separation', '--3d', '--sweep', '--multi-sweep', '--discrete-frequency',
               '--frequency', '--process', '--proc', '--input', '--define', '--definitions-file',
               '--label-depth', '--label-snap-distance', '--device-label-snap-distance',
               '--signal-ground-separator', '--internal', '--mode', '--collapse',
               '--device-cells-file', '--device-pins-internal', '--touchstone', '--psf')
    normalized = []
    selector = None
    index = 0
    while index < len(extra):
        token = extra[index]
        if not token.startswith('-') or token == '--':
            raise ValueError('Unexpected positional EMX argument: {}.'.format(token))
        key = token.split('=', 1)[0]
        # Reject combined short flags: getopt may interpret a later character
        # as a managed option (for example -CxP0 contains -x).
        if not token.startswith('--') and len(token) > 2:
            raise ValueError('Use individual EMX short options: {}.'.format(token))
        if modern and key == '--3d':
            if selector is not None or '=' not in token:
                raise ValueError('Specify exactly one --3d=<selector> option.')
            selector = token.split('=', 1)[1]
            if not re.fullmatch(r'-?(?:\*|[A-Za-z_][A-Za-z0-9_.-]*(?:,[A-Za-z_][A-Za-z0-9_.-]*)*)', selector):
                raise ValueError('Invalid EMX 3D conductor selector.')
            index += 1
            continue
        plain = '--' + key[5:] if key.startswith('--no-') else key
        if (any(plain == item or plain.startswith(item + '-') for item in managed) or
                (not token.startswith('--') and any(token.startswith(short) for short in ('-e', '-t', '-v', '-p', '-i', '-o', '-x', '-I', '-3', '-f', '-s', '-y', '-l')))):
            raise ValueError('Extra EMX option conflicts with managed inputs/settings: {}.'.format(token))
        if index + 1 < len(extra) and not extra[index + 1].startswith('-'):
            value = extra[index + 1]
            if '=' in token:
                raise ValueError('Unexpected positional EMX argument: {}.'.format(value))
            if token.startswith('--'):
                # Bind the value unambiguously even if this is a boolean/unknown flag.
                normalized.append(token + '=' + value)
            elif token == '-a':
                normalized.extend((token, value))
            else:
                raise ValueError('Use a long --option=value for this option value: {}.'.format(token))
            index += 2
        else:
            normalized.append(token)
            index += 1
    extra = normalized
    selector = selector if selector is not None else ('*' if three_d else '-*')
    result['three_d_selector'] = selector
    result['three_d'] = selector != '-*'
    if modern:
        result['options_text'] = raw
    result['extra_options'] = raw if not modern else ''
    result['extra_argv'] = extra
    return result


def prepare_job(source, geometry, config, run_dir):
    """Freeze a job contract; no process launch, directory creation or mutation."""
    source = copy.deepcopy(source)
    for key in ('library', 'cell', 'view', 'technology'):
        if not isinstance(source.get(key), str) or not _NAME.fullmatch(source[key]):
            raise ValueError('EMX source requires exact safe library/cell/view/technology identities.')
    if source['technology'] != config.get('technology'):
        raise ValueError('EMX source and process profile refer to different attached technologies.')
    for key in ('workspace', 'cds_lib', 'library_path'):
        if not source.get(key):
            raise ValueError('EMX source is missing {} provenance.'.format(key))
        source[key] = str(Path(source[key]).expanduser().resolve())
    if not Path(source['workspace']).is_dir() or not Path(source['cds_lib']).is_file() or not Path(source['library_path']).is_dir():
        raise ValueError('EMX source workspace/mapping/library is unavailable.')
    source['view_path'] = str(Path(source.get('view_path') or Path(source['library_path']) / source['cell'] / source['view']).resolve())
    try:
        Path(source['view_path']).relative_to(Path(source['library_path']))
    except ValueError:
        raise ValueError('EMX source view must belong to the exact mapped library.')
    oa_files = sorted(Path(source['view_path']).glob('*.oa'))
    if not oa_files or len(oa_files) > 16:
        raise ValueError('EMX requires the existing bounded OA source view, not an uncreated layout.')
    directory = _run_path(run_dir)
    if directory == Path('/') or directory == Path(source['workspace']) or directory.exists():
        raise ValueError('EMX run_dir must be a new private task directory.')
    inputs = {}
    for key in ('emx_executable', 'strmout_executable', 'process_file', 'layer_map'):
        value = _file(config.get(key), key, executable=key.endswith('executable'))
        inputs[key] = dict(path=value, sha256=_hash(value))
    inputs['cds_lib'] = dict(path=source['cds_lib'], sha256=_hash(source['cds_lib']))
    for index, path in enumerate(oa_files):
        inputs['source_oa_' + str(index)] = dict(path=str(path), sha256=_hash(path))
    contract = port_contract(geometry, config)
    mapping = {}
    for line in Path(inputs['layer_map']['path']).read_text(encoding='utf-8').splitlines():
        tokens = shlex.split(line, comments=True)
        if len(tokens) >= 4:
            try:
                value = (int(tokens[2]), int(tokens[3]))
            except ValueError:
                continue
            key = tuple(tokens[:2])
            if key in mapping and mapping[key] != value:
                raise ValueError('Ambiguous native stream map for {}.'.format('/'.join(key)))
            mapping[key] = value
    for terminal in contract['terminals']:
        for region in terminal['regions']:
            key = (region['layer'], region['purpose'])
            if key not in mapping:
                raise ValueError('Native stream map does not contain pin LPP {}.'.format('/'.join(key)))
            region['gds_layer'], region['gds_datatype'] = mapping[key]
    frequencies = config.get('frequencies_hz', [1e9])
    if (not isinstance(frequencies, (list, tuple)) or not 1 <= len(frequencies) <= 256 or
            any(isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0 for f in frequencies)):
        raise ValueError('EMX frequencies_hz must be a bounded nonempty list of positive Hz values.')
    frequencies = sorted(set(float(f) for f in frequencies))
    settings = resolve_settings(config)
    settings['frequencies_hz'] = frequencies
    gds = str(directory / 'layout.gds')
    snp = str(directory / ('response.s{}p'.format(len(contract['signals']))))
    stream = [inputs['strmout_executable']['path'], '-library', source['library'], '-topCell', source['cell'], '-view', source['view'],
              '-strmFile', gds, '-layerMap', inputs['layer_map']['path'], '-case', 'preserve', '-labelCase', 'preserve',
              '-convertPin', 'geometry', '-convertPcellPin', 'geometry', '-pinAttNum', '1',
              '-logFile', str(directory / 'stream.log'), '-summaryFile', str(directory / 'stream-summary.txt')]
    argv = [inputs['emx_executable']['path'], gds, source['cell'], inputs['process_file']['path'], '--cadence-pins=1']
    argv.extend('--port={}={}'.format(row['solver_name'], row['label']) for row in contract['definitions'])
    argv.extend('--include=' + name for name in contract['included'])
    argv.extend(['--parallel=' + str(settings['parallel']), '-e', format(settings['edge_um'], '.10g'),
                 '-t', format(settings['thickness_um'], '.10g'), '-v', format(settings['via_merge_um'], '.10g')])
    argv.append('--3d=' + settings['three_d_selector'])
    if settings['memory_mb'] is not None:
        argv.append('--max-memory={}M'.format(settings['memory_mb']))
    argv.extend(settings['extra_argv'])
    argv.extend(['--no-flexnet-queue', '--format=touchstone', '--s-file=' + snp])
    argv.extend(format(f, '.17g') for f in frequencies)
    return dict(schema='tfgen.emx.job.v1', status='prepared', created_utc=_utc(), source=source,
                run_dir=str(directory), inputs=inputs, geometry_sha256=hashlib.sha256(json.dumps(geometry, sort_keys=True).encode('utf-8')).hexdigest(),
                ports=contract, frequencies_hz=frequencies, stream_argv=stream, emx_argv=argv,
                outputs=dict(gds=gds, touchstone=snp), resources=dict(parallel=settings['parallel'], memory_mb_soft=settings['memory_mb'], edge_um=settings['edge_um'], timeout_seconds=settings['timeout_seconds']), effective_settings=settings,
                config_provenance=dict(path=config.get('config_file'), source=config.get('config_source'), sha256=config.get('config_sha256')),
                process_qualification=config.get('process_qualification', 'unverified process accuracy'),
                warnings=list(config.get('warnings', [])), stages=[])


def _real8(data):
    if len(data) != 8:
        raise ValueError('Malformed GDS real8.')
    if data == b'\0' * 8:
        return 0.0
    return (-1 if data[0] & 0x80 else 1) * (int.from_bytes(data[1:], 'big') / float(1 << 56)) * 16 ** ((data[0] & 0x7f) - 64)


def audit_gds(path, ports, top_cell, max_bytes=256 * 1024 * 1024):
    """Verify actual native property1 pins, rectangles and access directions."""
    file = Path(path)
    if not file.is_file() or file.stat().st_size > max_bytes:
        raise ValueError('EMX GDS is missing or exceeds the bounded audit limit.')
    data = file.read_bytes()
    offset, cell, current, attribute, scale, ended = 0, None, None, None, None, False
    records, names, pin_rows, shapes = 0, [], [], 0
    while offset + 4 <= len(data):
        size, kind, datatype = struct.unpack('>HBB', data[offset:offset + 4])
        if size < 4 or size % 2 or offset + size > len(data):
            raise ValueError('Malformed/truncated GDS record.')
        body = data[offset + 4:offset + size]
        offset += size
        records += 1
        if kind == 3:
            if len(body) != 16:
                raise ValueError('Malformed GDS units.')
            scale = _real8(body[8:]) * 1e6
        elif kind == 6:
            cell = body.rstrip(b'\0').decode('ascii')
            names.append(cell)
        elif kind in (8, 9, 10, 11, 12, 45):
            current = dict(kind=kind, cell=cell, properties=[])
        elif kind == 13 and current is not None:
            current['layer'] = struct.unpack('>h', body)[0]
        elif kind in (14, 22) and current is not None:
            current['datatype'] = struct.unpack('>h', body)[0]
        elif kind == 16 and current is not None:
            if len(body) % 8:
                raise ValueError('Malformed GDS coordinates.')
            values = struct.unpack('>' + 'i' * (len(body) // 4), body)
            current['xy'] = [list(values[i:i + 2]) for i in range(0, len(values), 2)]
        elif kind == 43:
            attribute = struct.unpack('>h', body)[0]
        elif kind == 44 and current is not None and attribute == 1:
            parts = body.rstrip(b'\0').decode('ascii', 'replace').split()
            if len(parts) < 4 or any(c not in 'TBLR' for c in parts[0]):
                raise ValueError('Malformed native Cadence pin property1.')
            current['properties'].append(dict(direction=parts[0], name=parts[2]))
        elif kind == 17 and current is not None:
            if current['kind'] in (8, 9, 45):
                shapes += 1
            if current['properties']:
                if current['cell'] != top_cell or current['kind'] not in (8, 45) or not current.get('xy'):
                    raise ValueError('Native pin is not a top-level rectangular boundary.')
                for prop in current['properties']:
                    pin_rows.append(dict(current, **prop))
            current = None
        elif kind == 4:
            ended = True
            break
    if not ended or any(data[offset:]) or scale is None or not math.isfinite(scale) or scale <= 0 or top_cell not in names or shapes == 0:
        raise ValueError('GDS has no complete expected structure, units or conductor geometry.')
    expected = {row['name']: row for row in ports['terminals']}
    actual_names = {row['name'] for row in pin_rows}
    if actual_names != set(expected):
        raise ValueError('Native GDS pin names do not match the complete terminal contract; expected {}, found {}.'.format(sorted(expected), sorted(actual_names)))
    matched = []
    for name, terminal in expected.items():
        candidates = [row for row in pin_rows if row['name'] == name]
        if len(candidates) != len(terminal['regions']):
            raise ValueError('Native GDS omitted or duplicated a physical pin region for {}.'.format(name))
        remaining = list(candidates)
        for region in terminal['regions']:
            found = None
            for row in remaining:
                xy = row['xy'][:-1] if row['xy'][0] == row['xy'][-1] else row['xy']
                xs, ys = {p[0] for p in xy}, {p[1] for p in xy}
                if len(xy) != 4 or len(xs) != 2 or len(ys) != 2:
                    continue
                box = [[min(xs) * scale, min(ys) * scale], [max(xs) * scale, max(ys) * scale]]
                desired = _DIRECTIONS.get(region['access_direction'], 'TBLR' if region['access_direction'] == 'internal' else None)
                if (('gds_layer' not in region or (row['layer'] == region['gds_layer'] and row['datatype'] == region['gds_datatype'])) and
                        all(abs(box[i][j] - region['bbox'][i][j]) <= scale * 1.5 + 1e-8 for i in (0, 1) for j in (0, 1)) and
                        (desired is None or row['direction'] == desired)):
                    found = row
                    matched.append(dict(name=name, direction=row['direction'], bbox_um=box, layer=row['layer'], datatype=row['datatype']))
                    break
            if found is None:
                raise ValueError('Native GDS rectangle/access direction differs for {}.'.format(name))
            remaining.remove(found)
    return dict(sha256=_hash(file), byte_size=len(data), top_cell=top_cell, records=records,
                geometry_elements=shapes, database_unit_um=scale, native_pins=matched, native_pin_count=len(pin_rows))


def _write(job):
    path = Path(job['run_dir']) / 'job.json'
    temporary = path.with_name('job.json.tmp')
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    if os.geteuid() == 0:
        owner = path.parent.stat()
        os.fchown(descriptor, owner.st_uid, owner.st_gid)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(job, handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.replace(str(temporary), str(path))


def _check_inputs(job):
    for name, row in job['inputs'].items():
        if _hash(row['path']) != row['sha256']:
            raise ValueError('Frozen EMX input changed: {}.'.format(name))


def _write_native_input(path, text, runtime):
    """Create a private input readable by the native user, regardless of umask."""
    user = pwd.getpwnam(runtime['user'])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        if os.geteuid() == 0:
            os.fchown(descriptor, user.pw_uid, user.pw_gid)
        elif os.geteuid() != user.pw_uid:
            raise ValueError('Native EMX input must belong to the actual design user.')
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            descriptor = None
            handle.write(text)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _create_run(job, runtime):
    directory = _run_path(job['run_dir'])
    user = pwd.getpwnam(runtime['user'])
    if user.pw_uid == 0 or os.geteuid() not in (0, user.pw_uid):
        raise ValueError('Native EMX task must belong to the actual non-root design user.')
    if directory.exists():
        raise ValueError('EMX run_dir must be a new private task directory.')
    ancestor = directory.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError('EMX parent is not a directory: {}.'.format(ancestor))
    if not _native_can_access(ancestor, user, os.X_OK):
        blocked = ancestor
        for parent in reversed((ancestor,) + tuple(ancestor.parents)):
            if not _native_can_access(parent, user, os.X_OK):
                blocked = parent
                break
        info = blocked.stat()
        raise ValueError('Native user {} cannot traverse existing EMX parent {} (uid={}, mode={:04o}); '
                         'choose an accessible run root. Existing permissions were unchanged.'.format(
                             user.pw_name, blocked, info.st_uid, stat.S_IMODE(info.st_mode)))
    # Every new component belongs to the design user. Directory descriptors and
    # O_NOFOLLOW prevent a symlink from redirecting privileged mkdir/chown.
    flags = os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
    parent_fd = os.open('/', getattr(os, 'O_PATH', os.O_RDONLY) | flags)
    try:
        for index, component in enumerate(directory.parts[1:]):
            try:
                child_fd = os.open(component, getattr(os, 'O_PATH', os.O_RDONLY) | flags, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                child_fd = os.open(component, os.O_RDONLY | flags, dir_fd=parent_fd)
                try:
                    os.fchmod(child_fd, 0o700)
                    if os.geteuid() == 0:
                        os.fchown(child_fd, user.pw_uid, user.pw_gid)
                except Exception:
                    os.close(child_fd)
                    raise
            else:
                if index == len(directory.parts) - 2:
                    os.close(child_fd)
                    raise ValueError('EMX run_dir already exists; it was not modified.')
            os.close(parent_fd)
            parent_fd = child_fd
    finally:
        os.close(parent_fd)
    # strmout has no arbitrary-cds.lib CLI flag on older XStream versions.
    # A task-local include delegates resolution to the exact invoking mapping.
    _write_native_input(directory / 'cds.lib', 'INCLUDE ' + json.dumps(job['source']['cds_lib']) + '\n', runtime)
    _write(job)


def _native_can_access(path, user, mode):
    if os.geteuid() == user.pw_uid:
        return os.access(str(path), mode)
    runuser = shutil.which('runuser')
    test = shutil.which('test', path=os.defpath)
    if not runuser or not test:
        raise ValueError('Native EMX directory validation requires the existing runuser and test utilities.')
    option = '-x' if mode == os.X_OK else '-w'
    checked = subprocess.run([runuser, '-u', user.pw_name, '--', test, option, str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env={'PATH': os.defpath, 'LANG': 'C', 'LC_ALL': 'C'}, timeout=10)
    return checked.returncode == 0


def _native_command(argv, runtime):
    # Restore exact native unset/empty/value states before the child-only setup.
    environment = runtime_config.native_environment(dict(os.environ), runtime.get('environment', {}))
    for key in list(environment):
        if key.startswith('CDS_MPS_') or key.startswith('TFGEN_ORIGINAL_'):
            del environment[key]
    environment.update(LANG='C', LC_ALL='C')
    wrapper = ('TFGEN_CDS_SET="${CDS_LIC_FILE+x}"; TFGEN_CDS_VALUE="${CDS_LIC_FILE-}"; '
               'TFGEN_LM_SET="${LM_LICENSE_FILE+x}"; TFGEN_LM_VALUE="${LM_LICENSE_FILE-}"; '
               'if [ -n "$1" ]; then . "$1" || exit $?; fi; '
               'if [ -n "$2" ]; then export PATH="$2${PATH:+:$PATH}"; fi; shift 2; '
               'if [ "$TFGEN_CDS_SET" = x ]; then export CDS_LIC_FILE'
               '="$TFGEN_CDS_VALUE"; fi; '
               'if [ "$TFGEN_LM_SET" = x ]; then export LM_LICENSE_FILE'
               '="$TFGEN_LM_VALUE"; fi; '
               'export LANG=C LC_ALL=C; exec "$@"')
    command = ['/bin/bash', '-c', wrapper, 'flux-emx-child', runtime.get('env_file') or '', ':'.join(runtime.get('helper_dirs', []))] + list(argv)
    if os.geteuid() == 0:
        runuser = shutil.which('runuser')
        if not runuser:
            raise ValueError('The existing runuser utility is required for root automation.')
        command = [runuser, '-u', runtime['user'], '--'] + command
    return command, environment


def _resource_sample(pgid, directory, include_disk=False):
    """Sample only the owned process group; counters are sampled, not accounting totals."""
    ticks = os.sysconf('SC_CLK_TCK')
    page = os.sysconf('SC_PAGE_SIZE')
    sample = dict(process_count=0, rss_sum_bytes=0, vms_sum_bytes=0,
                  cpu_user_seconds=0.0, cpu_system_seconds=0.0,
                  read_bytes=0, write_bytes=0, io_unavailable_processes=0)
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, 'stat').read_text(encoding='ascii')
            fields = raw[raw.rfind(')') + 2:].split()
            if int(fields[2]) != pgid:
                continue
            sample['process_count'] += 1
            sample['rss_sum_bytes'] += max(0, int(fields[21])) * page
            sample['vms_sum_bytes'] += int(fields[20])
            sample['cpu_user_seconds'] += int(fields[11]) / ticks
            sample['cpu_system_seconds'] += int(fields[12]) / ticks
            try:
                io = dict(line.split(':', 1) for line in Path(entry.path, 'io').read_text(encoding='ascii').splitlines())
                sample['read_bytes'] += int(io['read_bytes'])
                sample['write_bytes'] += int(io['write_bytes'])
            except (OSError, ValueError, KeyError):
                sample['io_unavailable_processes'] += 1
        except (OSError, ValueError, IndexError):
            continue  # Processes can exit while /proc is being inspected.
    try:
        mem = {}
        for line in Path('/proc/meminfo').read_text(encoding='ascii').splitlines():
            key, value = line.split(':', 1)
            if key in ('MemAvailable', 'SwapFree', 'SwapTotal'):
                mem[key] = int(value.split()[0]) * 1024
        sample['host_memory'] = mem
        sample['host_load_average'] = list(os.getloadavg())
        cpu = Path('/proc/stat').read_text(encoding='ascii').splitlines()[0].split()[1:]
        sample['host_cpu_ticks'] = dict(total=sum(int(v) for v in cpu[:8]), iowait=int(cpu[4]))
    except (OSError, ValueError, IndexError):
        sample['host_unavailable'] = True
    if include_disk:
        logical = allocated = count = 0
        truncated = False
        for root, dirs, files in os.walk(directory, followlinks=False):
            count += 1
            if count > 10000:
                truncated = True
                break
            for name in files:
                count += 1
                if count > 10000:
                    truncated = True
                    break
                try:
                    info = os.lstat(os.path.join(root, name))
                    if stat.S_ISREG(info.st_mode):
                        logical += info.st_size
                        allocated += info.st_blocks * 512
                except OSError:
                    continue
            if truncated:
                break
        sample['task_disk'] = dict(logical_bytes=logical, allocated_bytes=allocated,
                                   entries_examined=min(count, 10000), truncated=truncated)
    return sample


class _ResourceObserver:
    """Bounded, best-effort sampling; no command lines, environment or credentials."""
    def __init__(self, pgid, directory, stage, started):
        self.pgid, self.directory, self.started = pgid, directory, started
        self.path = Path(directory) / (stage + '-resources.jsonl')
        self.next_sample = self.next_disk = started
        self.summary = dict(sample_interval_seconds=1, samples=0, errors=0,
                            sampling_note='Sampled group sums and maxima; short-lived processes and between-sample peaks may be missed.',
                            jsonl=str(self.path))
        self.first_cpu = None
        self._created = False

    def sample(self, final=False):
        now = time.monotonic()
        if not final and now < self.next_sample:
            return
        try:
            disk = final or now >= self.next_disk
            value = _resource_sample(self.pgid, self.directory, disk)
            value.update(elapsed_seconds=round(now - self.started, 3), final=final)
            self.next_sample = now + 1
            if disk:
                self.next_disk = now + 10
            for key in ('rss_sum_bytes', 'vms_sum_bytes', 'cpu_user_seconds', 'cpu_system_seconds', 'read_bytes', 'write_bytes', 'process_count'):
                peak = 'max_sampled_' + key
                self.summary[peak] = max(self.summary.get(peak, 0), value[key])
            if 'task_disk' in value:
                self.summary['last_task_disk'] = value['task_disk']
            host = value.get('host_memory', {})
            if 'MemAvailable' in host:
                self.summary['min_host_mem_available_bytes'] = min(self.summary.get('min_host_mem_available_bytes', host['MemAvailable']), host['MemAvailable'])
            if 'host_cpu_ticks' in value:
                if self.first_cpu is None:
                    self.first_cpu = value['host_cpu_ticks']
                value['host_cpu_ticks_delta'] = {k: value['host_cpu_ticks'][k] - self.first_cpu[k] for k in ('total', 'iowait')}
                self.summary['host_cpu_ticks_delta'] = value['host_cpu_ticks_delta']
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
            if not self._created:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(str(self.path), flags, 0o600)
            try:
                if not self._created:
                    os.fchmod(descriptor, 0o600)
                    if os.geteuid() == 0:
                        owner = self.path.parent.stat()
                        os.fchown(descriptor, owner.st_uid, owner.st_gid)
                    self._created = True
                with os.fdopen(descriptor, 'a', encoding='utf-8') as out:
                    descriptor = None
                    out.write(json.dumps(value, sort_keys=True) + '\n')
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            self.summary['samples'] += 1
        except Exception as exc:
            self.next_sample = now + 1
            self.summary['errors'] += 1
            self.summary['last_error_type'] = type(exc).__name__


def _group_members(pgid):
    """Enumerate this task's session/group; zombies are evidence, not live tasks."""
    live, zombies = [], []
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, 'stat').read_text()
        except FileNotFoundError:
            continue
        except ProcessLookupError:
            continue
        fields = raw[raw.rfind(')') + 2:].split()
        if len(fields) < 4:
            raise RuntimeError('Unable to read process group evidence for PID {}.'.format(entry.name))
        if int(fields[2]) != pgid:
            continue
        if int(fields[3]) != pgid:
            raise RuntimeError('Process group session identity differs from the launched task.')
        (zombies if fields[0] == 'Z' else live).append(int(entry.name))
    return sorted(live), sorted(zombies)


def _cleanup_group(child, grace_seconds=1.0, kill_seconds=2.0):
    """Signal only the session created for this owned Popen, then verify live members."""
    pgid = child.pid
    evidence = dict(process_group=pgid, term_sent=False, kill_sent=False, live=[], zombies=[])
    def snapshot():
        child.poll()  # Reap the direct child promptly; zombies are not live.
        live, zombies = _group_members(pgid)
        evidence.update(live=live, zombies=zombies)
        return live
    live = snapshot()
    if live:
        try:
            os.killpg(pgid, signal.SIGTERM)
            evidence['term_sent'] = True
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace_seconds
        while snapshot() and time.monotonic() < deadline:
            time.sleep(.05)
        if evidence['live']:
            # A successful snapshot proves this exact session/group still exists.
            try:
                os.killpg(pgid, signal.SIGKILL)
                evidence['kill_sent'] = True
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + kill_seconds
            while snapshot() and time.monotonic() < deadline:
                time.sleep(.05)
    snapshot()
    evidence['verified_stopped'] = not evidence['live']
    return evidence


def _run_stage(job, stage, argv, runtime, timeout, cancel_event=None):
    _check_inputs(job)
    command, environment = _native_command(argv, runtime)
    record = dict(stage=stage, argv=list(argv), cwd=job['run_dir'], started_utc=_utc(), status='running')
    job['stages'].append(record)
    if cancel_event is not None and cancel_event.is_set():
        record.update(status='cancelled', exit_code=None, finished_utc=_utc(), seconds=0)
        _write(job)
        raise RuntimeError('EMX {} cancelled before process launch.'.format(stage))
    log = Path(job['run_dir']) / (stage + '-console.log')
    with log.open('x', encoding='utf-8') as output:
        if os.geteuid() == 0:
            owner = log.parent.stat()
            os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
        try:
            child = subprocess.Popen(command, cwd=job['run_dir'], env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            record.update(status='launch_failed', exit_code=None, finished_utc=_utc(), seconds=0, log=str(log), error=str(exc))
            _write(job)
            raise RuntimeError('EMX {} could not launch {}: {}. See {}.'.format(stage, argv[0], exc, log)) from exc
        started = time.monotonic()
        record.update(pid=child.pid, process_group=child.pid, log=str(log))
        stopped, caught, cleanup_error = None, None, None
        observer = None
        try:
            _write(job)
            observer = _ResourceObserver(child.pid, job['run_dir'], stage, started)
            record['resources'] = observer.summary
            while True:
                observer.sample()
                if child.poll() is not None:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    stopped = 'cancelled'
                    break
                if timeout is not None and time.monotonic() - started >= timeout:
                    stopped = 'timed_out'
                    break
                time.sleep(.1)
        except BaseException as exc:
            caught = exc
            stopped = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
        finally:
            try:
                cleanup = _cleanup_group(child)
                record['cleanup'] = cleanup
                if not cleanup['verified_stopped']:
                    cleanup_error = 'Owned process group still has live members: {}'.format(cleanup['live'])
                elif not stopped and cleanup['term_sent']:
                    stopped = 'orphaned_children'
                    record['error'] = 'Wrapper exited while owned child processes were still running; they were stopped.'
            except BaseException as exc:
                cleanup_error = 'Could not verify owned process group cleanup: {}'.format(exc)
                record['cleanup'] = dict(process_group=child.pid, verified_stopped=False, error=str(exc))
            if observer is not None:
                observer.sample(final=True)
            record.update(exit_code=child.returncode, finished_utc=_utc(), seconds=round(time.monotonic() - started, 3),
                          status='cleanup_failed' if cleanup_error else (stopped or ('completed' if child.returncode == 0 else 'failed')))
            if cleanup_error:
                record['error'] = cleanup_error
            _write(job)
    if cleanup_error:
        raise RuntimeError('EMX {} cleanup_failed: {}. See {}.'.format(stage, cleanup_error, log)) from caught
    if caught is not None:
        raise caught
    if stopped or child.returncode:
        raise RuntimeError('EMX {} {}; exit {}. See {}.'.format(stage, record['status'], child.returncode, log))
    return record


def _probe_source(job, runtime, cancel_event=None):
    """Recheck native identity and every source pin immediately before export."""
    from cadence import skill
    from technology import parse_skill
    source = job['source']
    directory = Path(job['run_dir'])
    records = directory / 'source-pins.tsv'
    progress = directory / 'source-progress.log'
    lines = ['procedure(fluxEmxMark(stage) let((f) f=outfile({} \"a\") when(f fprintf(f \"%s\\n\" stage) close(f)) printf(\"FLUX_EMX_MARK:%s\\n\" stage)))'.format(skill(str(progress))),
             'fluxEmxMark(\"script_started\")',
             'procedure(fluxEmxProbe() let((lib tf cv out term pin fig figs ddview)',
             'ddSetForcedLib({})'.format(skill(source['cds_lib'])), 'ddUpdateLibList()',
             'fluxEmxMark(\"mapping_ready\")',
             'lib=ddGetObj({})'.format(skill(source['library'])),
             'unless(lib && lib~>readPath=={} error("Flux EMX source library binding changed"))'.format(skill(source['library_path'])),
             'fluxEmxMark(\"library_verified\")',
             'tf=techGetTechFile(lib)',
             'unless(tf && tf~>libName=={} error("Flux EMX attached technology changed"))'.format(skill(source['technology'])),
             'fluxEmxMark(\"technology_verified\")',
             'ddview=ddGetObj({} {} {})'.format(skill(source['library']), skill(source['cell']), skill(source['view'])),
             'unless(ddview && ddview~>readPath=={} error("Flux EMX source view binding changed"))'.format(skill(source['view_path'])),
             'fluxEmxMark("view_verified")',
             'cv=dbOpenCellViewByType({} {} {} "" "r")'.format(skill(source['library']), skill(source['cell']), skill(source['view'])),
             'unless(cv error("Flux EMX source view not found"))',
             'fluxEmxMark(\"view_opened\")',
             'out=outfile({})'.format(skill(str(records))),
             'foreach(term cv~>terminals foreach(pin term~>pins figs=pin~>fig when(figs unless(listp(figs) figs=list(figs)) foreach(fig figs fprintf(out "PIN\\t%L\\t%L\\t%L\\t%L\\n" term~>name fig~>layerName fig~>bBox pin~>accessDir)))))',
             'close(out)', 'fluxEmxMark("pins_read")', 'dbClose(cv)', 'printf("FLUX_EMX_SOURCE_SUCCESS\\n")', 't))',
             'if(errset(fluxEmxProbe() t) then exit(0) else exit(1))']
    script = directory / 'probe.il'
    _write_native_input(script, '\n'.join(lines) + '\n', runtime)
    argv = [runtime['virtuoso'], '-ocean', '-nograph', '-nocdsinit', '-restore', str(script), '-log', str(directory / 'source-ocean.log')]
    try:
        _run_stage(job, 'source', argv, runtime, 120, cancel_event)
    except Exception as exc:
        markers = progress.read_text(encoding='utf-8', errors='replace').splitlines() if progress.is_file() else []
        last = markers[-1] if markers else 'not started (native initialization)'
        if job.get('stages'):
            job['stages'][-1]['last_source_marker'] = last
            _write(job)
        raise RuntimeError('{} Last source marker: {}.'.format(exc, last)) from exc
    if not records.is_file() or 'FLUX_EMX_SOURCE_SUCCESS' not in (directory / 'source-console.log').read_text(encoding='utf-8', errors='replace'):
        raise ValueError('Native EMX source pin verification did not create its record.')
    native = []
    for line in records.read_text(encoding='utf-8').splitlines():
        fields = line.split('\t')
        if len(fields) != 5 or fields[0] != 'PIN':
            raise ValueError('Malformed native EMX source pin evidence.')
        name, layer, bbox, directions = [parse_skill(field) for field in fields[1:]]
        native.append(dict(name=name, layer=layer, bbox=bbox, directions=directions))
    expected = [(terminal['name'], region) for terminal in job['ports']['terminals'] for region in terminal['regions']]
    if len(native) != len(expected):
        raise ValueError('Native EMX source pin count changed.')
    remaining = list(native)
    for name, region in expected:
        match = next((row for row in remaining if row['name'] == name and row['layer'] == region['layer'] and
                      all(abs(row['bbox'][i][j] - region['bbox'][i][j]) < 1e-6 for i in (0, 1) for j in (0, 1)) and
                      (region['access_direction'] is None or set(row['directions']) ==
                       (set(_DIRECTIONS) if region['access_direction'] == 'internal' else {region['access_direction']}))), None)
        if match is None:
            raise ValueError('Native source rectangle/layer/access direction differs for {}.'.format(name))
        remaining.remove(match)
    job['native_source_pins'] = native
    _write(job)


def export_layout(job, runtime, cancel_event=None):
    """Export only the frozen source; verify real property1 rather than TEXT."""
    if job.get('status') != 'prepared':
        raise ValueError('Only a prepared EMX job may export a new layout.')
    _check_inputs(job)
    _create_run(job, runtime)
    try:
        _probe_source(job, runtime, cancel_event)
        _run_stage(job, 'stream', job['stream_argv'], runtime, 120, cancel_event)
        job['gds_audit'] = audit_gds(job['outputs']['gds'], job['ports'], job['source']['cell'])
        job['status'] = 'exported'
    except Exception as exc:
        job.update(status='export_failed', error=str(exc))
        _write(job)
        raise
    _write(job)
    return job


def read_touchstone(path, port_count, frequencies=None):
    """Read finite classic Touchstone S RI/MA/DB with exact complete records."""
    option, values, tokens = None, [], []
    for line in Path(path).read_text(encoding='ascii').splitlines():
        line = line.split('!', 1)[0].strip()
        if not line:
            continue
        if line.startswith('#'):
            if option is not None:
                raise ValueError('Touchstone options changed inside the result.')
            option = line[1:].upper().split()
            continue
        if line.startswith('['):
            raise ValueError('This EMX adapter requires classic Touchstone output, not an unparsed v2 matrix.')
        try:
            numbers = [float(value.replace('D', 'E').replace('d', 'e')) for value in line.split()]
        except ValueError as exc:
            raise ValueError('Invalid numeric Touchstone output.') from exc
        if any(not math.isfinite(value) for value in numbers):
            raise ValueError('Non-finite Touchstone output.')
        values.extend(numbers)
        tokens.extend(line.split())
    if not option or len(option) < 5 or option[1] != 'S' or option[2] not in ('RI', 'MA', 'DB') or option[3] != 'R':
        raise ValueError('Touchstone must declare S RI/MA/DB and a real reference impedance.')
    scales = {'HZ': 1, 'KHZ': 1e3, 'MHZ': 1e6, 'GHZ': 1e9}
    if option[0] not in scales:
        raise ValueError('Unsupported Touchstone frequency unit.')
    impedance = float(option[4])
    if not math.isfinite(impedance) or impedance <= 0:
        raise ValueError('Touchstone reference impedance must be positive.')
    count = 1 + 2 * port_count * port_count
    if not values or len(values) % count:
        raise ValueError('Touchstone is empty/header-only or has incomplete network records.')
    rows = []
    decimal_scale = Decimal(str(scales[option[0]]))
    for start in range(0, len(values), count):
        frequency = values[start] * scales[option[0]]
        token = tokens[start]
        try:
            printed = Decimal(token.replace('D', 'E').replace('d', 'e'))
            decimal_frequency = printed * decimal_scale
            resolution = Decimal(1).scaleb(printed.as_tuple().exponent) * decimal_scale
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ValueError('Invalid Touchstone frequency precision.') from exc
        flattened = []
        for index in range(start + 1, start + count, 2):
            a, b = values[index:index + 2]
            if option[2] == 'RI':
                flattened.append(complex(a, b))
            else:
                magnitude = 10 ** (a / 20) if option[2] == 'DB' else a
                flattened.append(magnitude * complex(math.cos(math.radians(b)), math.sin(math.radians(b))))
        # Classic two-port output alone has the historical column-major order.
        matrix = [[flattened[j * port_count + i] if port_count == 2 else flattened[i * port_count + j]
                   for j in range(port_count)] for i in range(port_count)]
        if frequency <= 0:
            raise ValueError('Touchstone frequencies must be positive.')
        rows.append(dict(frequency_hz=frequency, reported_frequency_hz=frequency,
                         frequency_token=token, frequency_resolution_hz=float(resolution),
                         _decimal_frequency=decimal_frequency, _decimal_resolution=resolution, s=matrix))
    if frequencies is not None:
        if len(rows) != len(frequencies):
            raise ValueError('EMX did not return every exact requested frequency: point count differs.')
        requested = [Decimal(format(float(f), '.17g')) for f in frequencies]
        used = set()
        for index, row in enumerate(rows):
            half = row['_decimal_resolution'] / 2
            matches = [i for i, value in enumerate(requested) if abs(value - row['_decimal_frequency']) <= half]
            if len(matches) > 1:
                raise ValueError('Ambiguous Touchstone frequency {}: multiple requested points fall in its printed precision interval.'.format(row['frequency_token']))
            if not matches or matches[0] != index or matches[0] in used:
                raise ValueError('EMX did not return every exact requested frequency in order; unmatched printed frequency {}.'.format(row['frequency_token']))
            used.add(matches[0])
            row['requested_frequency_hz'] = float(frequencies[index])
            row['frequency_hz'] = float(frequencies[index])
    elif any(rows[i]['frequency_hz'] <= rows[i-1]['frequency_hz'] for i in range(1, len(rows))):
        raise ValueError('Touchstone frequencies must be positive and strictly increasing.')
    for row in rows:
        del row['_decimal_frequency'], row['_decimal_resolution']
    return dict(reference_ohms=impedance, port_count=port_count, rows=rows)


def _inverse(matrix):
    n = len(matrix)
    a = [list(row) + [complex(i == j) for j in range(n)] for i, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(a[row][column]))
        if abs(a[pivot][column]) < 1e-18:
            raise ValueError('The extracted network cannot be converted to open-circuit Z at this frequency.')
        a[column], a[pivot] = a[pivot], a[column]
        value = a[column][column]
        a[column] = [v / value for v in a[column]]
        for row in range(n):
            if row != column:
                factor = a[row][column]
                a[row] = [x - factor * y for x, y in zip(a[row], a[column])]
    return [row[n:] for row in a]


def _pin_metric(z, omega, metric, idx):
    def pair(a, b):
        return z[a][a] + z[b][b] - z[a][b] - z[b][a]
    if metric == 'k':
        p, q, r, t = idx
        z1, z2 = pair(p, q), pair(r, t)
        l1, l2 = z1.imag / omega, z2.imag / omega
        zm = z[p][r] - z[p][t] - z[q][r] + z[q][t]
        return (zm.imag / omega) / math.sqrt(l1 * l2) if l1 > 0 and l2 > 0 else None
    zd = pair(*idx)
    if metric in ('L', 'Lp', 'Ls'):
        return zd.imag / omega * 1e9
    return zd.imag / zd.real if zd.real != 0 else None


def summarize_network(network, ports, metric_specs=None):
    """Open-circuit differential winding L/Q and signed mutual coupling.

    ``metric_specs`` (``[{key, metric, pins}]``) adds metrics on explicitly
    chosen pins; they appear per sample under ``pin_metrics`` (L in nH).
    """
    n = network['port_count']
    indices = {name: i for i, name in enumerate(ports['signals'])}
    specs, spec_errors = [], {}
    for spec in metric_specs or []:
        missing = [pin for pin in spec['pins'] if pin not in indices]
        if missing:
            spec_errors[spec['key']] = 'pins %s are not driven signal pins of this network' % ', '.join(missing)
        else:
            specs.append((spec['key'], spec['metric'], [indices[pin] for pin in spec['pins']]))
    pairs = {}
    for terminal in ports['terminals']:
        if terminal['name'] in indices and terminal.get('winding') and terminal.get('endpoint') in ('positive', 'negative'):
            pairs.setdefault(terminal['winding'], {})[terminal['endpoint']] = indices[terminal['name']]
    pairs = {name: (ends['positive'], ends['negative']) for name, ends in pairs.items() if set(ends) == {'positive', 'negative'}}
    rows = []
    for sample in network['rows']:
        s = sample['s']
        inv = _inverse([[complex(i == j) - s[i][j] for j in range(n)] for i in range(n)])
        z = [[network['reference_ohms'] * sum((complex(i == k) + s[i][k]) * inv[k][j] for k in range(n))
              for j in range(n)] for i in range(n)]
        omega = 2 * math.pi * sample['frequency_hz']
        row = dict(frequency_hz=sample['frequency_hz'], windings={})
        for key in ('requested_frequency_hz', 'reported_frequency_hz', 'frequency_token', 'frequency_resolution_hz'):
            if key in sample:
                row[key] = sample[key]
        for name, (p, q) in pairs.items():
            zd = z[p][p] + z[q][q] - z[p][q] - z[q][p]
            row['windings'][name] = dict(resistance_ohms=zd.real, reactance_ohms=zd.imag,
                                          inductance_h=zd.imag / omega, q=zd.imag / zd.real if zd.real != 0 else None)
        if 'P' in pairs and 'S' in pairs:
            p, q = pairs['P']; r, t = pairs['S']
            zm = z[p][r] - z[p][t] - z[q][r] + z[q][t]
            lp, ls = row['windings']['P']['inductance_h'], row['windings']['S']['inductance_h']
            mutual = zm.imag / omega
            row.update(mutual_inductance_h=mutual, coupling_k=mutual / math.sqrt(lp * ls) if lp > 0 and ls > 0 else None)
        if specs:
            row['pin_metrics'] = {key: _pin_metric(z, omega, metric, idx) for key, metric, idx in specs}
        rows.append(row)
    return dict(definition='Z differential winding pairs, every other signal/TAP open; ' +
                (('fixed Ground pins remain zero' if ports['grounds'] else 'no physical Ground pins; implicit proc stack-bottom reference') if ports['protocol'] == 'fixed_ground_pins' else 'the explicit local-return pin protocol is retained'),
                signal_order=ports['signals'], ground_protocol=ports['protocol'], samples=rows,
                **({'pin_metric_errors': spec_errors} if spec_errors else {}))


def run_job(job, runtime, cancel_event=None):
    """Run one prepared/exported job synchronously in a worker, never ModelGen."""
    if job.get('status') == 'prepared':
        export_layout(job, runtime, cancel_event)
    if job.get('status') != 'exported':
        raise ValueError('EMX job is not exported or has already reached a terminal state.')
    if _hash(job['outputs']['gds']) != job['gds_audit']['sha256']:
        raise ValueError('The audited native-pin GDS changed before EMX execution.')
    try:
        job['status'] = 'running'
        _write(job)
        _run_stage(job, 'version', [job['inputs']['emx_executable']['path'], '--version'], runtime, 30, cancel_event)
        job['tool_version'] = (Path(job['run_dir']) / 'version-console.log').read_text(encoding='utf-8', errors='replace').strip()[:4096]
        _run_stage(job, 'emx', job['emx_argv'], runtime, job['resources']['timeout_seconds'], cancel_event)
        network = read_touchstone(job['outputs']['touchstone'], len(job['ports']['signals']), job['frequencies_hz'])
        summary = summarize_network(network, job['ports'], job.get('metric_specs'))
        job.update(status='completed', completed_utc=_utc(), network_summary=summary,
                   output_sha256={key: _hash(path) for key, path in job['outputs'].items()})
        if any(value['inductance_h'] <= 0 or value['resistance_ohms'] <= 0
               for sample in summary['samples'] for value in sample['windings'].values()):
            job['warnings'].append('A sampled winding has non-positive differential L/R; inspect port reference, geometry and resonance before optimization.')
        _check_inputs(job)
    except Exception as exc:
        state = job['stages'][-1].get('status') if job.get('stages') else None
        job.update(status=state if state in ('cancelled', 'timed_out') else 'failed', error=str(exc), completed_utc=_utc())
        _write(job)
        raise
    _write(job)
    return job
