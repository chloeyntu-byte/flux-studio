#!/usr/bin/env python3
"""Bounded, process-local Python/Tcl/Tk dependency discovery (Python 3.6+).

Only a demonstrated missing library or Tcl/Tk data file permits discovery.
No installation, links, global environment changes, or persistent cache occur.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys


SCHEMA = 'tfgen.python.runtime.v1'
MAX_ATTEMPTS = 12
MAX_DIRECTORIES = 160
MAX_CHILDREN = 96
PROBE_TIMEOUT = 20
OVERLAY_KEYS = ('LD_LIBRARY_PATH', 'TCL_LIBRARY', 'TK_LIBRARY')
_METADATA = 'TFGEN_RUNTIME_METADATA='
_MISSING = re.compile(r'(?P<name>[A-Za-z0-9_.+\-]+\.so(?:\.[A-Za-z0-9_.+\-]+)*)'
                      r': (?:cannot open shared object file|No such file or directory)')
_ABI = re.compile(r'lib(?P<kind>tcl|tk)(?P<version>[0-9]+\.[0-9]+)', re.I)


class RuntimeFailure(Exception):
    def __init__(self, stage, returncode, message, diagnostics=None):
        self.stage = stage
        self.returncode = returncode
        self.message = message
        self.diagnostics = list(diagnostics or [])
        super().__init__(message)


def _note(diagnostics, stage, message):
    diagnostics.append('Flux runtime stage=%s: %s' % (stage, message))


def _canonical_directory(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
        return str(resolved) if resolved.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _elf_info(filename):
    """Read only ELF headers/dynamic strings; avoid external tools or file scans."""
    try:
        with open(str(filename), 'rb') as stream:
            ident = stream.read(16)
            if len(ident) != 16 or ident[:4] != b'\x7fELF' or ident[4] not in (1, 2) or ident[5] not in (1, 2):
                return None
            bits = 32 if ident[4] == 1 else 64
            endian = '<' if ident[5] == 1 else '>'
            header_format = endian + ('HHIIIIIHHHHHH' if bits == 32 else 'HHIQQQIHHHHHH')
            header = struct.unpack(header_format, stream.read(struct.calcsize(header_format)))
            machine, phoff, phsize, phcount = header[1], header[4], header[8], header[9]
            if phcount > 256 or phsize > 256:
                return None
            loads, dynamic = [], None
            program_format = endian + ('IIIIIIII' if bits == 32 else 'IIQQQQQQ')
            program_size = struct.calcsize(program_format)
            if phsize < program_size:
                return None
            for index in range(phcount):
                stream.seek(phoff + index * phsize)
                entry = struct.unpack(program_format, stream.read(program_size))
                if bits == 32:
                    kind, offset, address, size = entry[0], entry[1], entry[2], entry[4]
                else:
                    kind, offset, address, size = entry[0], entry[2], entry[3], entry[5]
                if kind == 1:
                    loads.append((address, offset, size))
                elif kind == 2:
                    dynamic = (offset, size)
            result = dict(bits=bits, machine=machine, endian=endian,
                          needed=[], soname=None, rpaths=[])
            if dynamic is None:
                return result
            dynamic_format = endian + ('iI' if bits == 32 else 'qQ')
            dynamic_size = struct.calcsize(dynamic_format)
            if dynamic[1] > 262144:
                return None
            tags = []
            stream.seek(dynamic[0])
            for _ in range(dynamic[1] // dynamic_size):
                tag, value = struct.unpack(dynamic_format, stream.read(dynamic_size))
                if tag == 0:
                    break
                tags.append((tag, value))
            strings_address = next((value for tag, value in tags if tag == 5), None)
            strings_size = next((value for tag, value in tags if tag == 10), None)
            if strings_address is None or strings_size is None or strings_size > 2097152:
                return result
            strings_offset = next((offset + strings_address - address
                                   for address, offset, size in loads
                                   if address <= strings_address < address + size), None)
            if strings_offset is None:
                return None
            stream.seek(strings_offset)
            strings = stream.read(strings_size)
            def string_at(offset):
                if offset >= len(strings):
                    return ''
                return strings[offset:].split(b'\0', 1)[0].decode('utf-8', 'replace')
            result['needed'] = [string_at(value) for tag, value in tags if tag == 1]
            result['soname'] = next((string_at(value) for tag, value in tags if tag == 14), None)
            result['rpaths'] = [string_at(value) for tag, value in tags if tag in (15, 29)]
            return result
    except (OSError, ValueError, struct.error, OverflowError):
        return None


_PROBE_CODE = r'''
import sys
import json
import traceback
stage = 'version'
metadata = {'prefix': sys.prefix, 'base_prefix': getattr(sys, 'base_prefix', sys.prefix),
            'exec_prefix': sys.exec_prefix, 'version': list(sys.version_info[:3])}
def report_metadata():
    print('TFGEN_RUNTIME_METADATA=' + json.dumps(metadata, ensure_ascii=True), flush=True)
try:
    sys.stderr.write('Flux probe stage=version sys.executable=%r sys.version=%r\n' % (sys.executable, sys.version))
    report_metadata()
    if sys.version_info < (3, 7):
        raise RuntimeError('Python >=3.7 is required for Flux Studio')
    stage = 'tk_import'
    sys.stderr.write('Flux probe stage=tk_import\n')
    import importlib.util
    spec = importlib.util.find_spec('_tkinter')
    metadata['tkinter_file'] = spec.origin if spec else None
    try:
        import sysconfig
        metadata['libdirs'] = [sysconfig.get_config_var(key) for key in ('LIBDIR', 'LIBPL') if sysconfig.get_config_var(key)]
    except Exception:
        pass
    report_metadata()
    import tkinter as tk
    from tkinter import ttk
    import _tkinter
    metadata['tcl_version'] = _tkinter.TCL_VERSION
    metadata['tk_version'] = _tkinter.TK_VERSION
    report_metadata()
    stage = 'tcl_init'
    sys.stderr.write('Flux probe stage=tcl_init\n')
    tcl = tk.Tcl()
    patchlevel = str(tcl.call('info', 'patchlevel'))
    if patchlevel.split('.')[:2] != str(_tkinter.TCL_VERSION).split('.')[:2]:
        raise RuntimeError('Tcl runtime ABI does not match _tkinter')
    metadata['tcl_patchlevel'] = patchlevel
    metadata['tcl_library'] = str(tcl.call('info', 'library'))
    if __WITH_TK__:
        stage = 'tk_init'
        sys.stderr.write('Flux probe stage=tk_init\n')
        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
            tk_version = str(root.tk.call('package', 'require', 'Tk'))
            if tk_version.split('.')[:2] != str(_tkinter.TK_VERSION).split('.')[:2]:
                raise RuntimeError('Tk runtime ABI does not match _tkinter')
            metadata['tk_patchlevel'] = tk_version
        finally:
            root.destroy()
    report_metadata()
except BaseException:
    sys.stderr.write('Flux probe stage=%s status=failed\n' % stage)
    traceback.print_exc()
    sys.exit(1)
sys.stderr.write('Flux probe stage=complete status=ok\n')
'''


def _probe(candidate, environment, with_tk=False):
    code = _PROBE_CODE.replace('__WITH_TK__', 'True' if with_tk else 'False')
    try:
        process = subprocess.run([candidate, '-c', code], env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=PROBE_TIMEOUT)
        stdout = process.stdout.decode('utf-8', 'replace')
        stderr = process.stderr.decode('utf-8', 'replace')
        metadata = {}
        for line in stdout.splitlines():
            if line.startswith(_METADATA):
                try:
                    value = json.loads(line[len(_METADATA):])
                    if isinstance(value, dict):
                        metadata.update(value)
                except ValueError:
                    pass
        stages = re.findall(r'Flux probe stage=([a-z_]+)', stderr)
        return dict(returncode=process.returncode, stdout=stdout, stderr=stderr,
                    metadata=metadata, stage=stages[-1] if stages else 'python_exec')
    except subprocess.TimeoutExpired as exc:
        detail = (exc.stderr or b'').decode('utf-8', 'replace')
        return dict(returncode=124, stdout='', stderr=detail + '\nPython probe timed out.\n',
                    metadata={}, stage='python_exec')
    except OSError as exc:
        return dict(returncode=126, stdout='', stderr=str(exc) + '\n', metadata={}, stage='python_exec')


def _add_directory(directories, value):
    resolved = _canonical_directory(str(value))
    if resolved and resolved not in directories and len(directories) < MAX_DIRECTORIES:
        directories.append(resolved)


def _bounded_children(folder):
    """Inspect a fixed number of entries in one already selected directory."""
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) >= MAX_CHILDREN:
                    break
    except OSError:
        return []
    return sorted(names)


def _rpath_directories(filename, info):
    result = []
    if not info:
        return result
    origin = str(Path(filename).resolve().parent)
    for value in info['rpaths']:
        for part in value.split(':'):
            part = part.replace('${ORIGIN}', origin).replace('$ORIGIN', origin)
            if '$' not in part:
                _add_directory(result, part)
    return result


def _candidate_directories(candidate, metadata, environment, diagnostics):
    directories, prefixes = [], []
    for executable in (Path(candidate), Path(candidate).resolve()):
        parent = executable.parent
        prefixes.append(parent.parent if parent.name in ('bin', 'sbin') else parent)
        _add_directory(directories, parent)
        for value in _rpath_directories(executable, _elf_info(executable)):
            _add_directory(directories, value)
    for key in ('prefix', 'base_prefix', 'exec_prefix'):
        value = metadata.get(key)
        if isinstance(value, str) and Path(value).is_absolute():
            prefixes.append(Path(value))
    for value in metadata.get('libdirs', []):
        if isinstance(value, str):
            _add_directory(directories, value)
    tkinter_file = metadata.get('tkinter_file')
    if isinstance(tkinter_file, str) and Path(tkinter_file).is_absolute():
        _add_directory(directories, Path(tkinter_file).parent)
        for value in _rpath_directories(tkinter_file, _elf_info(tkinter_file)):
            _add_directory(directories, value)
    for prefix in prefixes:
        _add_directory(directories, prefix)
        for suffix in ('lib', 'lib64', 'lib/tcl-tk', 'lib/tcl-tk/lib',
                       'lib64/tcl-tk', 'lib64/tcl-tk/lib', 'share/tcltk', 'share'):
            _add_directory(directories, prefix / suffix)
    # Old loader values are clues, never a loader environment to restore.
    original = environment.get('TFGEN_ORIGINAL_LD_LIBRARY_PATH')
    if original is None:
        original = environment.get('TFGEN_CAPTURED_LD_LIBRARY_PATH', '')
    for part in original.split(':'):
        if part and Path(part).is_absolute():
            resolved = _canonical_directory(part)
            unrelated_qt = resolved and any(re.match(r'^qt(?:[0-9]+(?:\.[0-9]+)*)?$', value, re.I)
                                           for value in Path(resolved).parts)
            own_prefix = False
            if resolved:
                for prefix in prefixes:
                    try:
                        Path(resolved).relative_to(prefix.resolve())
                        own_prefix = True
                        break
                    except (ValueError, OSError):
                        pass
            # Do not restore an unrelated Qt directory from old Cadence LD
            # hints. Actual Python prefixes/sysconfig/RPATH remain authoritative,
            # including a legitimate installation whose name contains "Qt".
            if unrelated_qt and not own_prefix and resolved not in directories:
                _note(diagnostics, 'discovery', 'ignored unrelated Qt loader clue %r' % resolved)
                continue
            _add_directory(directories, part)
    # Enumerate only bounded immediate, named toolkit/Python children.
    parents = list(directories)
    for folder in parents:
        for child in _bounded_children(folder):
            if re.match(r'^(?:python[0-9]+\.[0-9]+[a-z]*|tcl[0-9]+\.[0-9]+|tk[0-9]+\.[0-9]+|[A-Za-z0-9_]+-linux-gnu|tcltk)$', child):
                path = Path(folder) / child
                _add_directory(directories, path)
                for suffix in ('lib', 'lib64', 'lib-dynload', 'config'):
                    _add_directory(directories, path / suffix)
    _note(diagnostics, 'discovery', 'bounded candidate directories=%r' % directories)
    return directories


def _reference_elf(candidate, metadata):
    for value in (metadata.get('tkinter_file'), candidate, str(Path(candidate).resolve())):
        if value:
            info = _elf_info(value)
            if info:
                return info
    return None


def _select_library(name, directories, reference, metadata, diagnostics):
    if reference is None:
        _note(diagnostics, 'library', 'cannot verify the selected executable ELF architecture')
        return None
    expected_abi = _ABI.search(name)
    for directory in directories:
        filename = Path(directory) / name
        if not filename.is_file():
            continue
        info = _elf_info(filename)
        if not info:
            _note(diagnostics, 'library', 'rejected non-ELF candidate %r' % str(filename))
            continue
        if any(info[key] != reference[key] for key in ('bits', 'machine', 'endian')):
            _note(diagnostics, 'library', 'rejected architecture mismatch %r' % str(filename))
            continue
        if info['soname'] not in (None, name):
            _note(diagnostics, 'library', 'rejected SONAME mismatch %r: %r' % (str(filename), info['soname']))
            continue
        actual_abi = _ABI.search(info['soname'] or str(filename.resolve()))
        if expected_abi and actual_abi and expected_abi.groupdict() != actual_abi.groupdict():
            _note(diagnostics, 'library', 'rejected Tcl/Tk SONAME ABI mismatch %r' % str(filename))
            continue
        if actual_abi:
            version = metadata.get(actual_abi.group('kind').lower() + '_version')
            if version and str(version) != actual_abi.group('version'):
                _note(diagnostics, 'library', 'rejected _tkinter Tcl/Tk ABI mismatch %r' % str(filename))
                continue
        _note(diagnostics, 'library', 'selected missing=%r file=%r SONAME=%r ELF=%s/%s' %
              (name, str(filename), info['soname'], info['bits'], info['machine']))
        return str(filename), info
    return None


def _cache_directories(soname, diagnostics=None):
    """Query the existing system cache read-only for one exact missing SONAME."""
    diagnostics = diagnostics if diagnostics is not None else []
    binary = next((value for value in ('/sbin/ldconfig', '/usr/sbin/ldconfig')
                   if os.path.isfile(value) and os.access(value, os.X_OK)), None)
    if binary is None:
        _note(diagnostics, 'library_cache', 'no existing system cache reader; missing=%r' % soname)
        return []
    try:
        process = subprocess.run([binary, '-p'], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=5,
                                 env={'PATH': os.defpath, 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        _note(diagnostics, 'library_cache', 'read-only query failed for %r: %s' % (soname, exc))
        return []
    if process.returncode != 0:
        _note(diagnostics, 'library_cache', 'read-only query exit=%s; missing=%r' %
              (process.returncode, soname))
        return []
    pattern = re.compile(r'^\s*' + re.escape(soname) + r'\s+\([^)]*\)\s+=>\s+(.+?)\s*$')
    directories, matches = [], 0
    # Do not publish cache contents, and cap the exact matches considered.
    for line in process.stdout[:2097152].decode('utf-8', 'replace').splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        path = Path(match.group(1))
        if not path.is_absolute() or path.name != soname or not path.is_file():
            continue
        _add_directory(directories, path.parent)
        matches += 1
        if matches >= 16:
            break
    _note(diagnostics, 'library_cache', 'exact missing=%r candidate directories=%r' % (soname, directories))
    return directories


def _data_candidates(kind, directories, metadata, tried, diagnostics):
    script = 'init.tcl' if kind == 'tcl' else 'tk.tcl'
    version = metadata.get(kind + '_version')
    candidates = []
    for folder in directories:
        _add_directory(candidates, folder)
        if version:
            _add_directory(candidates, Path(folder) / (kind + str(version)))
        for name in _bounded_children(folder):
            if re.match(r'^%s[0-9]+\.[0-9]+(?:\.[0-9]+)?$' % kind, name):
                _add_directory(candidates, Path(folder) / name)
    for directory in candidates:
        if directory in tried or not (Path(directory) / script).is_file():
            continue
        try:
            with open(str(Path(directory) / script), 'r', encoding='utf-8', errors='replace') as stream:
                text = stream.read(32768)
        except OSError:
            continue
        declared = re.search(r'package\s+(?:require(?:\s+-exact)?|provide)\s+%s\s+([0-9]+\.[0-9]+)' % ('Tcl' if kind == 'tcl' else 'Tk'), text)
        named = re.match(r'^%s([0-9]+\.[0-9]+)' % kind, Path(directory).name)
        actual = declared.group(1) if declared else (named.group(1) if named else None)
        if version and actual and actual != str(version):
            _note(diagnostics, 'data', 'rejected %s ABI mismatch %r' % (kind, directory))
            continue
        if version and not actual:
            _note(diagnostics, 'data', 'rejected %s data with unidentifiable ABI %r' % (kind, directory))
            continue
        _note(diagnostics, 'data', 'trying %s=%r for expected ABI=%r' % (script, directory, version))
        return directory
    return None


def _missing_data(stderr):
    lower = stderr.lower()
    if 'init.tcl' in lower and any(value in lower for value in ("can't find", 'cannot find', "couldn't find", "couldn't read", 'no such file', 'version conflict')):
        return 'tcl'
    if 'tk.tcl' in lower and any(value in lower for value in ("can't find", 'cannot find', "couldn't find", "couldn't read", 'no such file', 'version conflict')):
        return 'tk'
    return None


def _failure_summary(probe):
    lines = [line.strip() for line in probe.get('stderr', '').splitlines()
             if line.strip() and not line.startswith('Flux probe stage=')]
    detail = lines[-1][:2048] if lines else 'no diagnostic was returned'
    return 'Python %s failed: %s' % (probe['stage'], detail)


def resolve_python(candidate, environment=None, with_tk=False):
    """Return only GUI overlays; failures preserve probe stage and actual status."""
    baseline = dict(os.environ if environment is None else environment)
    current = dict(baseline)
    diagnostics, metadata = [], {}
    if not isinstance(candidate, str) or not os.path.isabs(candidate) or '\x00' in candidate:
        raise RuntimeFailure('python_path', 126, 'Python candidate must be an absolute executable path.')
    # Preserve the selected absolute spelling, including a symlink or /../.
    # The shell compares this identity exactly; only discovery uses real paths.
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise RuntimeFailure('python_path', 126, 'Python candidate must be an existing executable file: %r.' % candidate)
    _note(diagnostics, 'python_path', 'selected=%r real=%r with_tk=%s' % (candidate, str(Path(candidate).resolve()), bool(with_tk)))
    explicit_loader = bool(baseline.get('TFGEN_PYTHON_LD_LIBRARY_PATH'))
    explicit_data = {'tcl': bool(baseline.get('TFGEN_TCL_LIBRARY') or baseline.get('TCL_LIBRARY')),
                     'tk': bool(baseline.get('TFGEN_TK_LIBRARY') or baseline.get('TK_LIBRARY'))}
    added_libraries, tried_data = [], {'tcl': set(), 'tk': set()}
    extra_directories = []
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _note(diagnostics, 'probe', 'attempt=%s interpreter=%r' % (attempt, candidate))
        last = _probe(candidate, current, with_tk=with_tk)
        metadata.update(last.get('metadata', {}))
        detail = last.get('stderr', '').rstrip()
        if detail:
            diagnostics.append(detail)
        _note(diagnostics, last['stage'], 'actual_exit=%s' % last['returncode'])
        if last['returncode'] == 0:
            overlays = {key: current[key] for key in OVERLAY_KEYS
                        if key in current and current.get(key) != baseline.get(key)}
            return dict(schema=SCHEMA, interpreter=candidate, overlays=overlays,
                        diagnostics=diagnostics, probe=metadata)
        missing = _MISSING.search(last.get('stderr', ''))
        data_kind = _missing_data(last.get('stderr', ''))
        if not missing and not data_kind:
            raise RuntimeFailure(last['stage'], last['returncode'],
                                 _failure_summary(last), diagnostics)
        directories = _candidate_directories(candidate, metadata, baseline, diagnostics)
        for value in extra_directories:
            _add_directory(directories, value)
        if missing:
            name = missing.group('name')
            _note(diagnostics, 'missing_library', 'required SONAME=%r' % name)
            if explicit_loader:
                raise RuntimeFailure(last['stage'], last['returncode'],
                                     'The explicit TFGEN_PYTHON_LD_LIBRARY_PATH is authoritative; missing %s remains unresolved.' % name, diagnostics)
            selected = _select_library(name, directories, _reference_elf(candidate, metadata), metadata, diagnostics)
            if not selected:
                cached = _cache_directories(name, diagnostics)
                if cached:
                    selected = _select_library(name, cached, _reference_elf(candidate, metadata), metadata, diagnostics)
            if not selected:
                raise RuntimeFailure(last['stage'], last['returncode'],
                                     'No compatible library candidate was found for %s.' % name, diagnostics)
            filename, info = selected
            directory = str(Path(filename).parent.resolve())
            if directory in added_libraries or directory in current.get('LD_LIBRARY_PATH', '').split(':'):
                raise RuntimeFailure(last['stage'], last['returncode'],
                                     'Dependency discovery made no progress for %s.' % name, diagnostics)
            added_libraries.append(directory)
            original = baseline.get('LD_LIBRARY_PATH', '')
            current['LD_LIBRARY_PATH'] = ':'.join(([original] if original else []) + added_libraries)
            for value in _rpath_directories(filename, info):
                _add_directory(extra_directories, value)
            if re.match(r'^lib(?:tcl|tk)', name, re.I):
                # A selected external Tcl/Tk provider may keep scripts beside
                # lib, or in the same installation's share/tcltk directory.
                parent = Path(filename).parent.resolve()
                _add_directory(extra_directories, parent)
                _add_directory(extra_directories, parent.parent)
                for suffix in ('lib', 'lib64', 'share', 'share/tcltk', 'lib/tcl-tk', 'lib/tcl-tk/lib'):
                    _add_directory(extra_directories, parent.parent / suffix)
        else:
            variable = 'TCL_LIBRARY' if data_kind == 'tcl' else 'TK_LIBRARY'
            if explicit_data[data_kind]:
                raise RuntimeFailure(last['stage'], last['returncode'],
                                     'The explicitly selected %s is authoritative; its data initialization failed.' % variable, diagnostics)
            selected = _data_candidates(data_kind, directories, metadata, tried_data[data_kind], diagnostics)
            if not selected:
                raise RuntimeFailure(last['stage'], last['returncode'],
                                     'No matching %s data directory was found.' % data_kind, diagnostics)
            tried_data[data_kind].add(selected)
            current[variable] = selected
    raise RuntimeFailure(last['stage'], last['returncode'],
                         'Dependency discovery reached the bounded limit of %s probes.' % MAX_ATTEMPTS, diagnostics)


def write_result(filename, result):
    """Exclusively create one private regular result; never overwrite a target."""
    path = Path(filename)
    if not path.is_absolute():
        raise RuntimeFailure('result_file', 2, 'The runtime result path must be absolute.')
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise RuntimeFailure('result_file', 2, 'The result directory must be private, owned by this user, and not a symlink.')
    records = [('TFGEN_RUNTIME_SCHEMA', result['schema']),
               ('TFGEN_RUNTIME_INTERPRETER', result['interpreter'])]
    overlays = result['overlays']
    if set(overlays) - set(OVERLAY_KEYS):
        raise RuntimeFailure('result_file', 2, 'Unsupported runtime overlay field.')
    records.extend((key, overlays[key]) for key in OVERLAY_KEYS if key in overlays)
    for key, value in records:
        if not isinstance(value, str) or '\x00' in value:
            raise RuntimeFailure('result_file', 2, 'Invalid runtime result value for %s.' % key)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    created = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeFailure('result_file', 2, 'Runtime result safety check failed.')
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(b''.join((key + '=' + value).encode('utf-8') + b'\0' for key, value in records))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            now = path.lstat()
            if (now.st_dev, now.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, dest='candidate')
    parser.add_argument('--output', required=True)
    parser.add_argument('--with-tk', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = resolve_python(args.candidate, with_tk=args.with_tk)
        for line in result['diagnostics']:
            print(line, file=sys.stderr)
        write_result(args.output, result)
        print('Flux runtime stage=result_file: private overlay written; exit=0', file=sys.stderr)
        return 0
    except RuntimeFailure as exc:
        for line in exc.diagnostics:
            print(line, file=sys.stderr)
        print('Flux runtime stage=%s exit=%s: %s' % (exc.stage, exc.returncode, exc.message), file=sys.stderr)
        status = exc.returncode if exc.returncode > 0 else 128 - exc.returncode
        return status if 1 <= status <= 255 else 1
    except (OSError, ValueError) as exc:
        print('Flux runtime stage=result_file exit=2: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
