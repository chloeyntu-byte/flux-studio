#!/bin/bash
# Shared launch logging and UI preflight. No persistent environment changes.
# The supervisor reports its own result separately from Cadence IPC status.
# Initialize state here, rather than trusting an inherited environment flag.
tfgen_install_exit_feedback() {
    TFGEN_STARTUP_LOG_OPENED=no
    trap 'tfgen_report_exit "$?"' EXIT
}

tfgen_report_exit() {
    local tfgen_status="$1"
    if (( BASH_SUBSHELL == 0 )); then
        if [[ "${TFGEN_STARTUP_LOG_OPENED-no}" == yes ]]; then
            printf 'Flux launch result: exit=%s\n' "$tfgen_status" >&4
        else
            printf 'Flux launch result: exit=%s\n' "$tfgen_status" >&2
        fi
    fi
}

tfgen_private_directory() {
    local tfgen_dir="$1" tfgen_owner tfgen_mode
    [[ -d "$tfgen_dir" && ! -L "$tfgen_dir" ]] || return 1
    read -r tfgen_owner tfgen_mode < <(LC_ALL=C stat -c '%u %a' -- "$tfgen_dir")
    [[ "$tfgen_owner" == "$UID" && "$tfgen_mode" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$tfgen_mode & 077) == 0 ))
}

tfgen_private_file() {
    local tfgen_file="$1" tfgen_owner tfgen_mode tfgen_links tfgen_size
    [[ -f "$tfgen_file" && ! -L "$tfgen_file" ]] || return 1
    read -r tfgen_owner tfgen_mode tfgen_links tfgen_size < <(LC_ALL=C stat -c '%u %a %h %s' -- "$tfgen_file")
    [[ "$tfgen_owner" == "$UID" && "$tfgen_mode" =~ ^[0-7]{3,4}$ && "$tfgen_links" == 1 ]] || return 1
    (( (8#$tfgen_mode & 077) == 0 && tfgen_size <= 524288 ))
}

tfgen_launch_failed() {
    local tfgen_status="$1" tfgen_line tfgen_reason='See the private launch log for the detailed error.'
    # Command-substitution failures are reported once by the parent shell.
    if (( BASH_SUBSHELL == 0 )); then
        # Extract only our stage markers and Python exception summary. Do not
        # echo arbitrary config output, complete environments or license data.
        while IFS= read -r tfgen_line; do
            case "$tfgen_line" in
                'Flux runtime failed:'*|'Flux probe stage='*'status=failed'|'Flux launch stage=python-path '*|'Flux launch stage=config-format '*) tfgen_reason="${tfgen_line:0:1024}" ;;
                *'error while loading shared libraries:'*|*': symbol lookup error:'*) tfgen_reason="${tfgen_line:0:1024}" ;;
                *'not a valid identifier'*) tfgen_reason='Invalid Bash assignment/export in flux_config.sh; see the logged file and line.' ;;
            esac
            if [[ "$tfgen_line" =~ ^[[:alnum:]_.]*(Error|Exception): ]]; then tfgen_reason="${tfgen_line:0:1024}"; fi
        done < "$TFGEN_LAUNCH_LOG"
        printf 'Flux launch failed: stage=%s exit=%s. Log: %s\n' "${TFGEN_LAUNCH_STAGE-startup}" "$tfgen_status" "${TFGEN_LAUNCH_LOG-unavailable}" >&2
        printf 'Flux launch failed: stage=%s exit=%s.\nInterpreter: %s\nReason: %s\nLog: %s\n' "${TFGEN_LAUNCH_STAGE-startup}" "$tfgen_status" "${TFGEN_PYTHON-automatic discovery}" "$tfgen_reason" "${TFGEN_LAUNCH_LOG-unavailable}" >&4
    fi
}

tfgen_open_launch_log() {
    local tfgen_context="${1-}" tfgen_dir tfgen_log tfgen_fd_inode tfgen_path_inode
    umask 077
    if [[ -n "$tfgen_context" ]]; then
        tfgen_dir="$(dirname -- "$tfgen_context")"
        if ! tfgen_private_directory "$tfgen_dir"; then
            printf '%s\n' 'Flux launch failed: context directory must be private, owned by this user, and not a symlink; no file was opened.' >&2
            return 1
        fi
        tfgen_dir="$(cd -- "$tfgen_dir" && pwd -P)"
    else
        tfgen_dir="$(mktemp -d -- "${TMPDIR-/tmp}/flux_startup_XXXXXXXX_private")" || return 1
    fi
    tfgen_log="$tfgen_dir/launch.log"
    if [[ -e "$tfgen_log" || -L "$tfgen_log" ]]; then
        if ! tfgen_private_file "$tfgen_log"; then
            printf '%s\n' 'Flux launch failed: existing launch.log is not a safe private regular file; no log was replaced.' >&2
            return 1
        fi
        # Preserve an earlier attempt, including its early diagnostic.
        tfgen_log="$tfgen_dir/launch.$$.${RANDOM}.log"
    fi
    set -o noclobber
    if { exec 5>"$tfgen_log"; }; then
        set +o noclobber
    else
        set +o noclobber
        printf 'Flux launch failed: cannot exclusively create log %s\n' "$tfgen_log" >&2
        return 1
    fi
    tfgen_fd_inode="$(LC_ALL=C stat -Lc '%d:%i' -- "/proc/$$/fd/5")"
    tfgen_path_inode="$(LC_ALL=C stat -c '%d:%i' -- "$tfgen_log")"
    if [[ "$tfgen_fd_inode" != "$tfgen_path_inode" ]] || ! tfgen_private_file "$tfgen_log"; then
        exec 5>&-
        printf '%s\n' 'Flux launch failed: log safety check failed.' >&2
        return 1
    fi
    export TFGEN_LAUNCH_LOG="$tfgen_log" TFGEN_LOG_DIR="$tfgen_dir" TFGEN_DIAGNOSTIC_FD=4
    # The IPC/terminal receives the actual path before subsequent redirection.
    printf 'Flux launch log: %s\n' "$TFGEN_LAUNCH_LOG" >&2
    # Cadence can close its IPC transport after a delayed stdout EOF. Keep
    # both original channels alive until the supervisor has reported exit.
    exec 3>&1 4>&2
    exec 1>&5 2>&1
    exec 5>&-
    TFGEN_STARTUP_LOG_OPENED=yes
    export TFGEN_LAUNCH_STAGE=startup
    trap 'tfgen_launch_failed "$?"' ERR
    printf 'Flux startup: uid=%s stage=startup\n' "$UID"
}

tfgen_prepare_python() {
    local tfgen_root="$1"
    export TFGEN_LAUNCH_STAGE=load_helper
    source "$tfgen_root/scripts/select_python.sh"
    export TFGEN_LAUNCH_STAGE=config_load
    tfgen_load_config "$tfgen_root"
    export TFGEN_LAUNCH_STAGE=python_environment
    tfgen_clean_python_environment
    TFGEN_RUNTIME_RESULT_DIR="$(umask 077; mktemp -d "$TFGEN_LOG_DIR/python-runtime.XXXXXXXX")"
    export TFGEN_RUNTIME_ENV_FILE="$TFGEN_RUNTIME_RESULT_DIR/result.env"
    export TFGEN_LAUNCH_STAGE=python_probe
    TFGEN_PYTHON_BIN="$(tfgen_select_python "$tfgen_root")"
    export TFGEN_PYTHON="$TFGEN_PYTHON_BIN"
    if [[ -e "$TFGEN_RUNTIME_ENV_FILE" || -L "$TFGEN_RUNTIME_ENV_FILE" ]]; then
        tfgen_apply_runtime_environment "$TFGEN_RUNTIME_ENV_FILE" "$TFGEN_PYTHON_BIN"
        rm -f -- "$TFGEN_RUNTIME_ENV_FILE"
    fi
    if [[ "${TFGEN_DIAGNOSE_ONLY-no}" == yes ]]; then
        # These comparisons never alter the selected isolated GUI environment.
        ( tfgen_restore_original_python_environment
          printf '%s\n' 'Flux Python comparison: original inherited environment (diagnostic only).'
          if tfgen_python_probe_once "$TFGEN_PYTHON_BIN"; then
              printf '%s\n' 'Flux Python comparison: original exit=0.'
          else
              printf 'Flux Python comparison: original exit=%s.\n' "$?"
          fi
          unset LD_LIBRARY_PATH
          printf '%s\n' 'Flux Python comparison: only LD_LIBRARY_PATH cleared (diagnostic only).'
          if tfgen_python_probe_once "$TFGEN_PYTHON_BIN"; then
              printf '%s\n' 'Flux Python comparison: only-LD-cleared exit=0.'
          else
              printf 'Flux Python comparison: only-LD-cleared exit=%s.\n' "$?"
          fi )
    fi
}

tfgen_tk_probe_once() {
    export TFGEN_LAUNCH_STAGE=tk_init
    "$TFGEN_PYTHON_BIN" - <<'TFGEN_TK_PROBE'
import sys
import tkinter as tk
from tkinter import ttk
print('Flux Tk preflight: interpreter=%r version=%s stage=tk_init' % (sys.executable, sys.version.split()[0]), flush=True)
root = tk.Tk()
try:
    root.withdraw()
    root.update_idletasks()
    print('Flux Tk preflight: Tcl=%s Tk=%s passed' % (root.tk.call('info', 'patchlevel'), root.tk.call('package', 'require', 'Tk')), flush=True)
finally:
    root.destroy()
TFGEN_TK_PROBE
}

tfgen_tk_preflight() {
    local tfgen_probe_log="$TFGEN_RUNTIME_RESULT_DIR/tk.stderr" tfgen_status
    if tfgen_tk_probe_once 2>"$tfgen_probe_log"; then
        cat -- "$tfgen_probe_log" >&2
        return 0
    else
        tfgen_status=$?
    fi
    cat -- "$tfgen_probe_log" >&2
    if LC_ALL=C grep -Eq 'cannot open shared object file|error while loading shared libraries|[Cc]an.t find a usable|init[.]tcl|tk[.]tcl' "$tfgen_probe_log"; then
        export TFGEN_LAUNCH_STAGE=tk_dependencies
        if tfgen_resolve_runtime "$TFGEN_PYTHON_BIN" yes; then
            rm -f -- "$TFGEN_RUNTIME_ENV_FILE"
            tfgen_tk_probe_once
            return $?
        fi
    fi
    return "$tfgen_status"
}

tfgen_start_app() {
    local tfgen_root="$1"
    shift
    tfgen_tk_preflight
    export TFGEN_LAUNCH_STAGE=ui_run
    cd -- "$tfgen_root"
    printf 'Flux UI starting: %s\n' "$TFGEN_PYTHON_BIN"
    # Keep the supervising shell so a real Tk/app failure reaches the CIW.
    # Original diagnostic descriptor is private to that supervisor.
    "$TFGEN_PYTHON_BIN" "$tfgen_root/app.py" "$@" 3>&- 4>&-
    printf '%s\n' 'Flux UI exited successfully.'
}
