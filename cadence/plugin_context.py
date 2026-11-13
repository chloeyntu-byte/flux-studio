#!/usr/bin/env python3
"""Convert one private, inert SKILL context snapshot into the UI JSON contract."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from technology import parse_skill
from runtime_config import ENVIRONMENT_KEYS, NATIVE_ISOLATION_KEYS, discover_source_process

SCHEMA = 'tfgen.layout.context.v1'
HEADER = 'TFGEN_LAYOUT_CONTEXT_1'
LIMIT = 524288
FIELDS = {'library', 'cell', 'view', 'technology', 'library_path', 'view_path',
          'workspace', 'cds_lib'}
ENVIRONMENT = ENVIRONMENT_KEYS
NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)


def scalar(value):
    """Only quoted strings and nil are legal; nothing is evaluated."""
    if value != 'nil' and QUOTED.fullmatch(value) is None:
        raise ValueError('Context fields must be quoted SKILL strings or nil.')
    parsed = parse_skill(value)
    if parsed is not None and (not isinstance(parsed, str) or '\x00' in parsed):
        raise ValueError('Invalid layout context string.')
    return parsed


def decode_context(source):
    lines = source.splitlines()
    if not lines or lines[0] != HEADER:
        raise ValueError('Unrecognized layout context format.')
    context, environment = {}, {}
    for number, line in enumerate(lines[1:], 2):
        fields = line.split('\t')
        if len(fields) != 3 or fields[0] not in {'C', 'E'}:
            raise ValueError('Malformed context row %d.' % number)
        key, value = map(scalar, fields[1:])
        if fields[0] == 'C':
            if key not in FIELDS or key in context:
                raise ValueError('Unknown or repeated layout context field.')
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError('Missing or oversized layout context field: %s.' % key)
            context[key] = value
        else:
            if key not in ENVIRONMENT or key in environment:
                raise ValueError('Unknown or repeated runtime environment field.')
            if value is not None and len(value) > 65536:
                raise ValueError('Oversized runtime environment field.')
            environment[key] = value
    if set(context) != FIELDS:
        raise ValueError('The invoking layout context is incomplete.')
    if any(NAME.fullmatch(context[key]) is None for key in ('library', 'cell')):
        raise ValueError('The invoking library or cell name is unsupported.')
    for key in ('workspace', 'cds_lib', 'library_path', 'view_path'):
        if not Path(context[key]).is_absolute():
            raise ValueError('Context path must be absolute: %s.' % key)
    if not Path(context['workspace']).is_dir():
        raise ValueError('The invoking workspace no longer exists.')
    if not Path(context['cds_lib']).is_file():
        raise ValueError('The captured library mapping file no longer exists.')
    context['schema'] = SCHEMA
    # Preserve nil, empty and nonempty separately. Missing rows in an older
    # snapshot remain missing, so the native runtime can recover them safely.
    context['environment'] = environment
    return context


def read_private_data(filename):
    path = Path(filename).absolute()
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077):
        raise ValueError('Layout context directory must be private and owned by this user.')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > LIMIT):
            raise ValueError('Layout context file must be private, regular, and owned by this user.')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('Layout context file is too large.')
        return path, data
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_private_context(filename):
    path, data = read_private_data(filename)
    return path, decode_context(data.decode('utf-8'))


def convert_context(filename, reuse_existing=False):
    path, context = read_private_context(filename)
    target = path.parent / 'context.json'
    if reuse_existing and (target.exists() or target.is_symlink()):
        _, data = read_private_data(target)
        existing = json.loads(data.decode('utf-8'))
        if (not isinstance(existing, dict) or existing.get('schema') != SCHEMA
                or any(existing.get(key) != context[key] for key in FIELDS)
                or not isinstance(existing.get('environment'), dict)
                or any((key in NATIVE_ISOLATION_KEYS and key not in existing['environment'])
                       or existing['environment'].get(key) != value
                       for key, value in context['environment'].items())):
            raise ValueError('Existing JSON does not match this private layout snapshot. '
                             'Open Flux again from the layout menu to create a fresh context.')
        return target
    process = discover_source_process(context['workspace'])
    if process:
        captured = context['environment']
        discovered = {key: value for key, value in process.get('environment', {}).items()
                      if key in ENVIRONMENT and (value is None or
                         (isinstance(value, str) and '\x00' not in value))}
        context['source_process'] = process
        context['environment'] = dict(discovered, **captured)
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(context, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')
    return target


def show_error(message):
    print('Flux launch failed: %s' % message, file=sys.stderr)
    if os.environ.get('DISPLAY'):
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('Flux Studio', 'Cannot open the layout generator:\n\n' + message,
                                 parent=root)
            root.destroy()
        except Exception:
            pass  # The same diagnostic remains available to the invoking CIW.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-existing', action='store_true', help='reuse only a secure JSON matching this TSV')
    parser.add_argument('--no-dialog', action='store_true', help='report errors to the supervising launcher')
    parser.add_argument('context_tsv')
    args = parser.parse_args()
    try:
        print(convert_context(args.context_tsv, reuse_existing=args.reuse_existing))
        return 0
    except (OSError, UnicodeError, ValueError) as exc:
        if args.no_dialog:
            print('Flux context conversion failed: %s' % exc, file=sys.stderr)
        else:
            show_error(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
