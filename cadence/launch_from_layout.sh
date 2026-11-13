#!/bin/bash
# One click creates one independent UI with the invoking layout's snapshot.
set -Eeuo pipefail
FLUX_PLUGIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FLUX_ROOT="$(cd -- "$FLUX_PLUGIN_DIR/.." && pwd)"
export TFGEN_LAUNCH_CWD="$(pwd -P)"
source "$FLUX_ROOT/scripts/startup.sh"
tfgen_install_exit_feedback
if [[ "$#" != 1 ]]; then
    tfgen_open_launch_log
    export TFGEN_LAUNCH_STAGE=arguments
    printf '%s\n' 'Flux launcher requires one private context TSV.' >&2
    false
fi
tfgen_open_launch_log "$1"
export TFGEN_LAUNCH_STAGE=context_permissions
tfgen_private_file "$1"
tfgen_prepare_python "$FLUX_ROOT"
export TFGEN_LAUNCH_STAGE=context_conversion
FLUX_CONTEXT_JSON="$("$TFGEN_PYTHON_BIN" "$FLUX_PLUGIN_DIR/plugin_context.py" --reuse-existing --no-dialog "$1")"
export TFGEN_LAUNCH_STAGE=context_permissions
tfgen_private_file "$FLUX_CONTEXT_JSON"
tfgen_start_app "$FLUX_ROOT" --context "$FLUX_CONTEXT_JSON"
