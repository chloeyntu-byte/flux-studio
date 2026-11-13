#!/bin/bash
set -Eeuo pipefail
TFGEN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export TFGEN_LAUNCH_CWD="$(pwd -P)"
source "$TFGEN_DIR/scripts/startup.sh"
tfgen_install_exit_feedback
TFGEN_CONTEXT_ARGUMENT=''
TFGEN_PREVIOUS_ARGUMENT=''
TFGEN_DIAGNOSE_ONLY=no
for TFGEN_ARGUMENT in "$@"; do
    if [[ "$TFGEN_ARGUMENT" == --diagnose ]]; then TFGEN_DIAGNOSE_ONLY=yes; fi
    if [[ "$TFGEN_PREVIOUS_ARGUMENT" == --context ]]; then TFGEN_CONTEXT_ARGUMENT="$TFGEN_ARGUMENT"; fi
    TFGEN_PREVIOUS_ARGUMENT="$TFGEN_ARGUMENT"
done
tfgen_open_launch_log "$TFGEN_CONTEXT_ARGUMENT"
if [[ -n "$TFGEN_CONTEXT_ARGUMENT" ]]; then
    export TFGEN_LAUNCH_STAGE=context_permissions
    tfgen_private_file "$TFGEN_CONTEXT_ARGUMENT"
fi
tfgen_prepare_python "$TFGEN_DIR"
if [[ "$TFGEN_DIAGNOSE_ONLY" == yes ]]; then
    tfgen_tk_preflight
    export TFGEN_LAUNCH_STAGE=environment_doctor
    TFGEN_DOCTOR_STATUS=0
    TFGEN_DOCTOR_REPORT="$("$TFGEN_PYTHON_BIN" -B "$TFGEN_DIR/flux_doctor.py" --display 3>&- 4>&-)" || TFGEN_DOCTOR_STATUS=$?
    printf '%s\n' "$TFGEN_DOCTOR_REPORT"
    printf '%s\n' "$TFGEN_DOCTOR_REPORT" >&4
    if (( TFGEN_DOCTOR_STATUS != 0 )); then
        printf 'Flux environment check failed. Log: %s\n' "$TFGEN_LAUNCH_LOG" >&4
        exit "$TFGEN_DOCTOR_STATUS"
    fi
    printf 'Flux startup diagnostics passed. Log: %s\n' "$TFGEN_LAUNCH_LOG"
    printf 'Flux startup diagnostics passed. Log: %s\n' "$TFGEN_LAUNCH_LOG" >&4
else
    tfgen_start_app "$TFGEN_DIR" "$@"
fi
