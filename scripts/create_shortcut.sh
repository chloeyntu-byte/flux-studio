#!/bin/bash
# Create only the explicitly requested local shortcut; no system desktop changes.
set -eu
TFGEN_SHORTCUT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$#" != 1 ]]; then
    printf '%s\n' 'Usage: bash scripts/create_shortcut.sh /path/to/Flux.desktop' >&2
    exit 2
fi
TFGEN_SHORTCUT_OUTPUT="$1"
if [[ "$TFGEN_SHORTCUT_OUTPUT" != /* ]]; then
    TFGEN_SHORTCUT_OUTPUT="$(pwd -P)/$TFGEN_SHORTCUT_OUTPUT"
fi
source "$TFGEN_SHORTCUT_ROOT/scripts/select_python.sh"
tfgen_load_config "$TFGEN_SHORTCUT_ROOT"
tfgen_clean_python_environment
TFGEN_SHORTCUT_PYTHON="$(tfgen_select_python "$TFGEN_SHORTCUT_ROOT")"
exec "$TFGEN_SHORTCUT_PYTHON" - "$TFGEN_SHORTCUT_ROOT" "$TFGEN_SHORTCUT_OUTPUT" <<'PY'
from pathlib import Path
import os
import sys

root, target = map(Path, sys.argv[1:])
if target.suffix != '.desktop':
    raise SystemExit('The explicit output path must end in .desktop.')
if not target.parent.is_dir():
    raise SystemExit('The explicit output directory does not exist.')
def argument(value):
    changes = {'\\': '\\\\\\\\', '"': '\\\\"', '$': '\\\\$', '`': '\\\\`',
               '\n': '\\n', '\r': '\\r', '\t': '\\t', '%': '%%'}
    return '"' + ''.join(changes.get(char, char) for char in str(value)) + '"'
root_value = str(root).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
entry = ('[Desktop Entry]\nType=Application\nName=Flux Passive Layout Studio\n'
         'Comment=Local shortcut to the selected Flux project\nExec=/bin/bash ' +
         argument(root / 'launch.sh') + '\nPath=' + root_value +
         '\nTerminal=false\nCategories=Development;Engineering;\nStartupNotify=true\n')
descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
    stream.write(entry)
print('Created the requested local shortcut. Recreate it after moving its project.')
PY
