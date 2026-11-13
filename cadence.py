"""Task-local OA writer. Only creates a previously absent design cell.

The preview geometry is the write contract; native standard vias are created
from the attached technology, never by drawing cut-looking rectangles.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import shutil
import subprocess
import uuid

import runtime_config

ROOT = Path(__file__).resolve().parent
VIRTUOSO = None
ENV_FILE = None
RUNTIME_ENV = {}
RUNTIME_SNAPSHOT = None


def configure_runtime(values=None):
    """Restore the invoking editor's whitelisted environment only to OA children."""
    RUNTIME_ENV.clear()
    for key, value in (values or {}).items():
        if key in runtime_config.ENVIRONMENT_KEYS and ((isinstance(value, str) and '\x00' not in value) or (key in runtime_config.NATIVE_ISOLATION_KEYS and value is None)):
            RUNTIME_ENV[key] = value
OWNER = pwd.getpwuid(os.getuid() if os.geteuid() != 0 else ROOT.stat().st_uid).pw_name


def configure_context(context=None, workspace=None, cds_lib=None):
    global VIRTUOSO, ENV_FILE, OWNER, RUNTIME_SNAPSHOT
    if context is None and RUNTIME_SNAPSHOT:
        context = {'source_process': {'uid': pwd.getpwnam(RUNTIME_SNAPSHOT['user']).pw_uid,
            'executable': RUNTIME_SNAPSHOT['virtuoso'], 'workspace': RUNTIME_SNAPSHOT['workspace'],
            'environment': RUNTIME_SNAPSHOT['environment'], 'pid': RUNTIME_SNAPSHOT.get('source_pid')},
            'environment': RUNTIME_SNAPSHOT['environment']}
    snapshot = runtime_config.discover_runtime(context, workspace, cds_lib)
    VIRTUOSO, ENV_FILE, OWNER = snapshot['virtuoso'], snapshot['env_file'], snapshot['user']
    RUNTIME_ENV.clear()
    RUNTIME_ENV.update(snapshot['environment'])
    RUNTIME_SNAPSHOT = snapshot
    return snapshot


def ensure_runtime(workspace=None, cds_lib=None):
    return RUNTIME_SNAPSHOT or configure_context(workspace=workspace, cds_lib=cds_lib)
NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')


def skill(value):
    if value is None:
        return 'nil'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return 't' if value else 'nil'
    if isinstance(value, (int, float)):
        return format(value, '.10g')
    return 'list(' + ' '.join(skill(v) for v in value) + ')'


def _native_can_traverse(path, account):
    if os.geteuid() == account.pw_uid:
        return os.access(str(path), os.X_OK)
    runuser, test = shutil.which('runuser'), shutil.which('test', path=os.defpath)
    if not runuser or not test:
        raise ValueError('Native directory validation requires runuser and test.')
    return subprocess.run([runuser, '-u', account.pw_name, '--', test, '-x', str(path)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env={'PATH': os.defpath, 'LANG': 'C', 'LC_ALL': 'C'},
                          timeout=10).returncode == 0


def _require_native_traversal(path, account):
    if _native_can_traverse(path, account):
        return
    for component in reversed((path,) + tuple(path.parents)):
        if not _native_can_traverse(component, account):
            info = component.stat()
            raise ValueError('Native user {} cannot traverse existing directory {} (uid={}, mode={:04o}); '
                             'existing permissions were unchanged.'.format(account.pw_name, component,
                                 info.st_uid, stat.S_IMODE(info.st_mode)))
    raise ValueError('Native user {} cannot traverse {}.'.format(account.pw_name, path))


def job_dir(label, runtime=None):
    if runtime is None:
        ensure_runtime()
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_]+', label):
        raise ValueError('Invalid native job label.')
    account = pwd.getpwnam(runtime['user'] if runtime is not None else OWNER)
    if os.geteuid() not in (0, account.pw_uid):
        raise ValueError('Native job must belong to the actual design user.')
    root = ROOT.resolve(strict=True)
    _require_native_traversal(root, account)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
    root_fd = os.open(str(root), flags)
    runtime_fd = None
    runtime = root / 'runtime'
    try:
        try:
            os.mkdir('runtime', 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            created = False
        try:
            runtime_fd = os.open('runtime', flags, dir_fd=root_fd)
        except OSError as exc:
            raise ValueError('Runtime directory must be a real directory, not a symlink: {}.'.format(runtime)) from exc
        if created:
            os.fchmod(runtime_fd, 0o700)
            if os.geteuid() == 0:
                os.fchown(runtime_fd, account.pw_uid, account.pw_gid)
        else:
            _require_native_traversal(runtime, account)
        name = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '_' + label + '_' + uuid.uuid4().hex[:8]
        os.mkdir(name, 0o700, dir_fd=runtime_fd)
        child_fd = os.open(name, flags, dir_fd=runtime_fd)
        try:
            os.fchmod(child_fd, 0o700)
            if os.geteuid() == 0:
                os.fchown(child_fd, account.pw_uid, account.pw_gid)
        finally:
            os.close(child_fd)
        return runtime / name
    finally:
        if runtime_fd is not None:
            os.close(runtime_fd)
        os.close(root_fd)


def run_skill(source, workspace, directory, timeout=120, cds_lib=None, runtime=None):
    snapshot = runtime if runtime is not None else ensure_runtime(workspace, cds_lib)
    native_env = snapshot.get('environment', {}) if runtime is not None else RUNTIME_ENV
    executable = snapshot['virtuoso'] if runtime is not None else VIRTUOSO
    env_file = snapshot.get('env_file') if runtime is not None else ENV_FILE
    owner = snapshot['user'] if runtime is not None else OWNER
    if runtime is not None:
        if Path(workspace).resolve() != Path(snapshot['workspace']).resolve():
            raise ValueError('The frozen native runtime belongs to a different workspace.')
        if cds_lib is not None and runtime_config.resolve_mapping(workspace, cds_lib) != Path(snapshot['cds_lib']).resolve():
            raise ValueError('The frozen native runtime belongs to a different library mapping.')
    if cds_lib is not None:
        mapping = runtime_config.resolve_mapping(workspace, cds_lib)
        if not mapping.is_file():
            raise ValueError('The invoking library mapping file does not exist.')
        source = 'ddSetForcedLib(%s)\nddUpdateLibList()\n' % skill(str(mapping)) + source
    source_path = directory / 'job.il'
    source_path.write_text(source, encoding='utf-8')
    source_path.chmod(0o644)
    # No inherited MPS session and no global environment modifications.
    child_env = runtime_config.native_environment(dict(os.environ), native_env)
    for key in tuple(child_env):
        if key.startswith('CDS_MPS_'):
            del child_env[key]
    child_env['LANG'] = 'en_US.UTF-8'
    child_env['LC_ALL'] = 'en_US.UTF-8'
    wrapper = ('TFGEN_CDS_SET="${CDS_LIC_FILE+x}"; TFGEN_CDS_LICENSE="${CDS_LIC_FILE-}"; '
               'TFGEN_LM_SET="${LM_LICENSE_FILE+x}"; TFGEN_LM_LICENSE="${LM_LICENSE_FILE-}"; '
               'if [ -n "$1" ]; then source "$1" || exit $?; fi; '
               'if [ -n "$6" ]; then export PATH="$6${PATH:+:$PATH}"; fi; '
               'if [ "$TFGEN_CDS_SET" = x ]; then export CDS_LIC_FILE'
               '="$TFGEN_CDS_LICENSE"; fi; '
               'if [ "$TFGEN_LM_SET" = x ]; then export LM_LICENSE_FILE'
               '="$TFGEN_LM_LICENSE"; fi; '
               'cd "$2" || exit 1; exec "$3" -ocean -nograph -nocdsinit -restore "$4" -log "$5"')
    args = ['/bin/bash', '-c', wrapper,
            'transformer-generator', env_file or '', str(workspace), executable, str(source_path), str(directory / 'ocean.log'), ':'.join(snapshot['helper_dirs'])]
    if os.geteuid() == 0:
        runuser = shutil.which('runuser')
        if not runuser:
            raise RuntimeError('Root automation requires the existing runuser system utility. Launch Flux as the design user.')
        args = [runuser, '-u', owner, '--'] + args
    proc = subprocess.Popen(args, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, text=True, encoding='utf-8', errors='replace')
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
        (directory / 'stdout.log').write_text(output, encoding='utf-8')
        raise RuntimeError('Cadence batch timed out. See this task\'s logs.') from exc
    (directory / 'stdout.log').write_text(output, encoding='utf-8')
    known_startup_error = '*Error* The default SKILL generic function has not been defined for the function "asiEnvGetVar". Ensure that this function is called with the correct argument(s) (tool partition name).'
    error_lines = [line for line in output.splitlines() if '*Error*' in line or 'SYNTAX ERROR' in line]
    unexpected_errors = [line for line in error_lines if line.strip() != known_startup_error]
    # A legacy technology libInit emits this single known simulator-registration
    # error before any job script. A completed job must still prove its own marker.
    completed = 'TFGEN_SUCCESS' in output or 'TFGEN_PDK_SUCCESS' in output
    if proc.returncode != 0 or unexpected_errors or (error_lines and not completed):
        errors = unexpected_errors or error_lines
        raise RuntimeError('Cadence operation failed: ' + '; '.join(errors[-6:])[:1200])
    return output


def write_cell(library, cell, workspace, technology, result, design, runtime=None, allow_existing_cell=False):
    if not NAME.fullmatch(library) or not NAME.fullmatch(cell):
        raise ValueError('Library and cell names must start with a letter and contain only letters, digits and underscores (up to 80 characters).')
    expected_library_path = str(technology['library_path'])
    library_path = Path(expected_library_path).resolve()
    runtime_config.permitted_library(library_path, workspace, technology, technology.get('_permission_context'))
    geometry = result['geometry']
    for port in geometry['ports']:
        if port.get('access_direction', 'bottom') not in ('bottom', 'top', 'left', 'right'):
            raise ValueError('A native pin requires one supported physical access direction.')
    directory = job_dir('generate', runtime=runtime) if runtime is not None else job_dir('generate')
    (directory / 'design.json').write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'geometry.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()
    lines = [
        'procedure(tfgenWrite() let((lib tf cv s n group v vd pin term groups out)',
        'lib=ddGetObj(%s)' % skill(library),
        'unless(lib error("TFGEN missing library"))',
        'unless(lib~>readPath==%s error("TFGEN library binding changed; reload the library"))' % skill(expected_library_path),
        ('when(ddGetObj(%s %s "layout") error("TFGEN target layout view already exists"))' % (skill(library), skill(cell)) if allow_existing_cell else
         'when(ddGetObj(%s %s) error("TFGEN target cell already exists; choose a new name"))' % (skill(library), skill(cell))),
        'tf=techGetTechFile(lib)',
        'unless(tf && tf~>libName==%s error("TFGEN attached technology changed"))' % skill(technology['technology']),
        'when(lib~>name==tf~>libName error("TFGEN refuses a technology library"))',
        'unless(tf~>path==%s error("TFGEN technology-file binding changed"))' % skill(technology['technology_path']),
        'cv=dbOpenCellViewByType(%s %s "layout" "maskLayout" "w")' % (skill(library), skill(cell)),
        'unless(cv error("TFGEN cannot create layout"))',
        'groups=makeTable("tfgenGroups" nil)',
    ]
    def group_part(part):
        return 'pgs' if part in ('pgs', 'ground_route') else ('ring' if part == 'ring' else 'body')
    parts = sorted(set(group_part(p['part']) for p in geometry['polygons']) |
                   set(group_part(v.get('part', 'body')) for v in geometry['vias']) |
                   set(group_part(p.get('part', 'body')) for p in geometry['ports']))
    for part in parts:
        lines.append('groups[%s]=dbCreateFigGroup(cv %s t list(0 0) "R0")' % (skill(part), skill('TFGEN_' + part.upper())))
    for net in sorted(set(p.get('net', 'GND') for p in geometry['polygons'])):
        lines.append('dbMakeNet(cv %s)' % skill(net))
    for poly in geometry['polygons']:
        lines += ['s=dbCreatePolygon(cv list(%s "drawing") %s)' % (skill(poly['layer']), skill(poly['points'])),
                  'unless(s error("TFGEN polygon creation failed"))',
                  's~>net=dbFindNetByName(cv %s)' % skill(poly.get('net', 'GND')),
                  'dbAddFigToFigGroup(groups[%s] s)' % skill(group_part(poly['part']))]
    for via in geometry['vias']:
        params = [['cutRows', via['rows']], ['cutColumns', via['columns']], ['cutSpacing', via['cut_spacing']]]
        # Large-array spacing can differ from the single-cut viaDef default.
        lines += ['vd=techFindViaDefByName(tf %s)' % skill(via['via_def']),
                  'unless(vd error("TFGEN missing actual via definition"))',
                  'v=dbCreateVia(cv vd %s "R0" %s)' % (skill(via['origin']), skill(params)),
                  'unless(v error("TFGEN native via creation failed"))',
                  'v~>net=dbFindNetByName(cv %s)' % skill(via.get('net', 'P')),
                  'dbAddFigToFigGroup(groups[%s] v)' % skill(group_part(via.get('part', 'body')))]
    for port in geometry['ports']:
        lines += ['n=dbMakeNet(cv %s)' % skill(port['net']),
                  'term=dbCreateTerm(n %s "inputOutput")' % skill(port['name']),
                  's=dbCreateRect(cv list(%s "drawing") %s)' % (skill(port['layer']), skill(port['bbox'])),
                  's~>net=n',
                  'pin=dbCreatePin(n s %s term)' % skill(port['name']),
                  'unless(pin error("TFGEN pin binding failed"))',
                  'pin~>accessDir=list(%s)' % skill(port.get('access_direction', 'bottom')),
                  'dbAddFigToFigGroup(groups[%s] s)' % skill(group_part(port.get('part', 'body')))]
    lines += [
        'dbReplaceProp(cv "tfgenVersion" "string" "2.4.0")',
        'dbReplaceProp(cv "tfgenGeometryHash" "string" %s)' % skill(digest),
        'dbReplaceProp(cv "tfgenParameters" "string" %s)' % skill(json.dumps(design, ensure_ascii=True, sort_keys=True)),
        'dbReplaceProp(cv "tfgenPortContract" "string" %s)' % skill(json.dumps(geometry['ports'], ensure_ascii=True, sort_keys=True)),
        'unless(dbSave(cv) error("TFGEN save failed"))',
        'dbClose(cv)',
        'cv=dbOpenCellViewByType(%s %s "layout" "" "r")' % (skill(library), skill(cell)),
        'unless(cv error("TFGEN readback failed"))',
        'out=outfile(%s)' % skill(str(directory / 'readback.tsv')),
        'fprintf(out "CELL\\t%s\\t%s\\t%s\\t%L\\n" cv~>libName cv~>cellName tf~>libName cv~>bBox)',
        'fprintf(out "COUNTS\\t%d\\t%d\\t%d\\t%d\\n" length(cv~>shapes) length(cv~>vias) length(cv~>terminals) length(cv~>figGroups))',
        'foreach(s cv~>shapes fprintf(out "POLYGON\\t%L\\t%L\\t%L\\t%L\\t%L\\n" s~>objType s~>lpp s~>points s~>bBox s~>net~>name))',
        'foreach(v cv~>vias fprintf(out "VIA\\t%L\\t%L\\t%L\\t%L\\t%L\\n" v~>viaHeader~>viaDefName v~>origin v~>bBox v~>viaHeader~>overrideParams v~>net~>name) foreach(s v~>viaHeader~>master~>shapes fprintf(out "VIAFIG\\t%L\\t%L\\t%L\\t%L\\n" v~>origin v~>viaHeader~>viaDefName s~>layerName s~>bBox)))',
        'foreach(term cv~>terminals fprintf(out "PORT\\t%s\\t%L\\t%L\\t%L\\n" term~>name term~>net~>name term~>pins~>fig~>bBox term~>pins~>fig~>lpp))',
        'foreach(term cv~>terminals foreach(pin term~>pins fprintf(out "PINACCESS\\t%s\\t%L\\t%L\\n" term~>name pin~>accessDir term~>direction)))',
        'foreach(group cv~>figGroups fprintf(out "GROUP\\t%s\\t%d\\n" group~>name length(group~>figs)))',
        'close(out)',
        'dbClose(cv)',
        'printf("TFGEN_SUCCESS\\n")',
        't))',
        'if(errset(tfgenWrite() t) then exit(0) else exit(1))',
    ]
    native_options = {'runtime': runtime} if runtime is not None else {}
    output = run_skill('\n'.join(lines) + '\n', workspace, directory, cds_lib=technology.get('cds_lib'), **native_options)
    if 'TFGEN_SUCCESS' not in output:
        raise RuntimeError('Cadence did not return the success marker. See the task logs.')
    readback = (directory / 'readback.tsv').read_text(encoding='utf-8')
    counts_line = next(line for line in readback.splitlines() if line.startswith('COUNTS\t'))
    shapes, vias, ports, groups = map(int, counts_line.split('\t')[1:])
    if shapes != len(geometry['polygons']) + len(geometry['ports']) or vias != len(geometry['vias']) or ports != len(geometry['ports']) or groups != len(parts):
        raise RuntimeError('Saved OA shape/via/port/group counts do not match. Results are retained for inspection.')
    from evidence import verify_readback
    from technology import drawing_grid
    actual = verify_readback(readback, geometry, drawing_grid(technology))
    report = {'ok': True, 'library': library, 'cell': cell, 'view': 'layout', 'technology': technology['technology'],
              'counts': {'shapes': shapes, 'vias': vias, 'ports': ports, 'groups': groups},
              'geometry_hash': digest, 'report': str(directory / 'readback.tsv'), 'run_dir': str(directory),
              'warnings': result.get('warnings', []), 'oa_verification': actual,
              'verification': 'OA reopened and geometry/native cuts checked; no foundry DRC signoff'}
    (directory / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def list_libraries(workspace, cds_lib=None):
    """Read the actual native mapping, including includes and relative defines."""
    configure_context(workspace=workspace, cds_lib=cds_lib)
    directory = job_dir('library_catalog')
    records = directory / 'libraries.tsv'
    source = '''
let((f lib tf captured)
 f=outfile(%s)
 unless(f error("TFGEN cannot write the library catalog"))
 foreach(lib ddGetLibList()
  captured=errset(techGetTechFile(lib) nil)
  tf=if(captured then car(captured) else nil)
  fprintf(f "LIB\\t%%L\\t%%L\\t%%L\\t%%L\\n" lib~>name lib~>readPath if(tf then tf~>libName else nil) if(tf then tf~>path else nil))
 )
 close(f)
 printf("TFGEN_PDK_SUCCESS\\n")
)
exit(0)
''' % skill(str(records))
    run_skill(source, workspace, directory, cds_lib=cds_lib)
    from technology import parse_skill
    entries = []
    for line in records.read_text(encoding='utf-8').splitlines():
        parts = line.split('\t')
        if len(parts) != 5 or parts[0] != 'LIB':
            raise ValueError('The native library catalog is malformed.')
        name, path, tech_name, tech_path = [parse_skill(value) for value in parts[1:]]
        entries.append(dict(name=name, library_path=path, technology=tech_name,
            technology_path=tech_path, is_technology=not tech_name or name == tech_name))
    return entries
