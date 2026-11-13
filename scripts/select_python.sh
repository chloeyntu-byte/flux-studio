#!/bin/bash
# Shared by both launchers. No packages are installed or persistent settings changed.
TFGEN_RUNTIME_SCRIPT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/python_runtime.py"
tfgen_anchor_path() {
    local tfgen_value="$1" tfgen_base="$2" tfgen_command="${3-no}"
    if [[ -z "$tfgen_value" || "$tfgen_value" == /* ]]; then
        printf '%s\n' "$tfgen_value"
    elif [[ "$tfgen_value" == '~' ]]; then
        printf '%s\n' "${HOME-}"
    elif [[ "$tfgen_value" == '~/'* ]]; then
        printf '%s/%s\n' "${HOME-}" "${tfgen_value:2}"
    elif [[ "$tfgen_command" == yes && "$tfgen_value" != */* ]]; then
        printf '%s\n' "$tfgen_value"
    else
        printf '%s/%s\n' "$tfgen_base" "$tfgen_value"
    fi
}

tfgen_load_config() {
    local tfgen_root="$1" tfgen_key tfgen_base tfgen_value tfgen_command tfgen_status
    local tfgen_initial="$(pwd -P)"
    local tfgen_config="$tfgen_root/flux_config.sh" tfgen_temp tfgen_child_script tfgen_record
    local tfgen_complete=no tfgen_part tfgen_remaining tfgen_normalized tfgen_origin
    local -A tfgen_inherited_values=() tfgen_inherited_present=()
    local -A tfgen_config_values=() tfgen_config_present=()
    local -a tfgen_config_keys=(TFGEN_PYTHON TFGEN_VIRTUOSO TFGEN_CADENCE_ENV TFGEN_WORKSPACE TFGEN_DESIGN_ROOT TFGEN_CDS_LIB TFGEN_RUN_USER TFGEN_PYTHON_LD_LIBRARY_PATH TFGEN_PYTHONHOME TFGEN_PYTHONPATH TFGEN_TCL_LIBRARY TFGEN_TK_LIBRARY TFGEN_EMX TFGEN_EMX_CONFIG TFGEN_EMX_PROCESS TFGEN_EMX_LAYER_MAP TFGEN_STRMOUT TFGEN_SNPFIT_ROOT)
    tfgen_python_override_source=automatic
    for tfgen_key in "${tfgen_config_keys[@]}"; do
        if [[ -v "$tfgen_key" ]]; then
            tfgen_inherited_present["$tfgen_key"]=1
            tfgen_inherited_values["$tfgen_key"]="${!tfgen_key}"
        fi
    done
    if [[ -e "$tfgen_config" || -L "$tfgen_config" ]]; then
        if [[ ! -f "$tfgen_config" || ! -r "$tfgen_config" ]]; then
            printf 'Flux launch stage=config-file path=%q exit=2: configuration must be a readable regular file.\n' "$tfgen_config" >&2
            return 2
        fi
        if LC_ALL=C grep -q $'\r' -- "$tfgen_config"; then
            printf 'Flux launch stage=config-format path=%q exit=2: CRLF/carriage returns detected; save the file with LF line endings.\n' "$tfgen_config" >&2
            return 2
        fi
        if /bin/bash -n "$tfgen_config"; then
            :
        else
            tfgen_status=$?
            printf 'Flux launch stage=config-syntax path=%q exit=%s.\n' "$tfgen_config" "$tfgen_status" >&2
            return "$tfgen_status"
        fi
        tfgen_temp="$(umask 077; mktemp "${TMPDIR:-/tmp}/tfgen-config.XXXXXXXX")" || return 2
        # A fresh shell retains errexit even when this function is called from
        # a parent if/!/&& condition, which otherwise suppresses source errors.
        # Only whitelisted values return via a private NUL record file. Config
        # stdout and unrelated environment mutations never escape the child.
        tfgen_child_script="$(cat <<'TFGEN_CONFIG_CHILD'
tfgen_config_file="$1"
shift
trap 'tfgen_status=$?; printf "Flux launch stage=config-runtime path=%q line=%s exit=%s.\n" "$tfgen_config_file" "$LINENO" "$tfgen_status" >&2; exit "$tfgen_status"' ERR
source "$tfgen_config_file"
for tfgen_config_key in "$@"; do
    if [[ -v "$tfgen_config_key" ]]; then
        printf '%s=%s\0' "$tfgen_config_key" "${!tfgen_config_key}" >&3
    fi
done
printf '__TFGEN_CONFIG_COMPLETE__\0' >&3
TFGEN_CONFIG_CHILD
)"
        if /bin/bash -eE -o pipefail -c "$tfgen_child_script" flux-config "$tfgen_config" "${tfgen_config_keys[@]}" 3>"$tfgen_temp" >/dev/null; then
            while IFS= read -r -d '' tfgen_record; do
                if [[ "$tfgen_record" == __TFGEN_CONFIG_COMPLETE__ ]]; then
                    tfgen_complete=yes
                    continue
                fi
                tfgen_key="${tfgen_record%%=*}"
                tfgen_config_present["$tfgen_key"]=1
                tfgen_config_values["$tfgen_key"]="${tfgen_record#*=}"
            done <"$tfgen_temp"
            rm -f -- "$tfgen_temp"
            if [[ "$tfgen_complete" != yes ]]; then
                printf 'Flux launch stage=config-runtime path=%q exit=2: configuration exited before completing.\n' "$tfgen_config" >&2
                return 2
            fi
        else
            tfgen_status=$?
            rm -f -- "$tfgen_temp"
            printf 'Flux launch stage=config-runtime path=%q exit=%s: configuration loading failed.\n' "$tfgen_config" "$tfgen_status" >&2
            return "$tfgen_status"
        fi
    fi
    for tfgen_key in "${tfgen_config_keys[@]}"; do
        if [[ "${tfgen_inherited_present[$tfgen_key]-}" == 1 ]]; then
            printf -v "$tfgen_key" '%s' "${tfgen_inherited_values[$tfgen_key]}"
            tfgen_base="$tfgen_initial"
            tfgen_origin=inherited
            printf 'Flux launch stage=config-selection field=%s source=inherited: the inherited setting takes precedence over local configuration.\n' "$tfgen_key" >&2
        else
            tfgen_base="$tfgen_root"
            tfgen_origin=config
            if [[ "${tfgen_config_present[$tfgen_key]-}" == 1 ]]; then
                printf -v "$tfgen_key" '%s' "${tfgen_config_values[$tfgen_key]}"
            fi
        fi
        if [[ "$tfgen_key" == TFGEN_PYTHON && -v "$tfgen_key" ]]; then
            tfgen_python_override_source="$tfgen_origin"
        fi
        if [[ -v "$tfgen_key" && "$tfgen_key" != TFGEN_RUN_USER ]]; then
            case "$tfgen_key" in
                TFGEN_PYTHON_LD_LIBRARY_PATH|TFGEN_PYTHONPATH)
                    tfgen_remaining="${!tfgen_key}"
                    tfgen_normalized=
                    if [[ -n "$tfgen_remaining" ]]; then
                        if [[ "$tfgen_remaining" == :* || "$tfgen_remaining" == *: || "$tfgen_remaining" == *::* ]]; then
                            printf 'Flux launch stage=config-path field=%s source=%s exit=2: empty path-list entries are not allowed.\n' "$tfgen_key" "$tfgen_origin" >&2
                            return 2
                        fi
                        while :; do
                            tfgen_part="${tfgen_remaining%%:*}"
                            tfgen_value="$(tfgen_anchor_path "$tfgen_part" "$tfgen_base")"
                            if [[ ! -d "$tfgen_value" ]]; then
                                printf 'Flux launch stage=config-path field=%s source=%s path=%q exit=2: directory does not exist.\n' "$tfgen_key" "$tfgen_origin" "$tfgen_value" >&2
                                return 2
                            fi
                            tfgen_normalized+="${tfgen_normalized:+:}$tfgen_value"
                            [[ "$tfgen_remaining" == *:* ]] || break
                            tfgen_remaining="${tfgen_remaining#*:}"
                        done
                    fi
                    printf -v "$tfgen_key" '%s' "$tfgen_normalized"
                    export "$tfgen_key"
                    continue
                    ;;
            esac
            tfgen_command=no
            if [[ "$tfgen_key" == TFGEN_PYTHON || "$tfgen_key" == TFGEN_VIRTUOSO || "$tfgen_key" == TFGEN_EMX || "$tfgen_key" == TFGEN_STRMOUT ]]; then
                tfgen_command=yes
            fi
            tfgen_value="$(tfgen_anchor_path "${!tfgen_key}" "$tfgen_base" "$tfgen_command")"
            case "$tfgen_key" in
                TFGEN_PYTHONHOME|TFGEN_TCL_LIBRARY|TFGEN_TK_LIBRARY)
                    if [[ -n "$tfgen_value" && ! -d "$tfgen_value" ]]; then
                        printf 'Flux launch stage=config-path field=%s source=%s path=%q exit=2: directory does not exist.\n' "$tfgen_key" "$tfgen_origin" "$tfgen_value" >&2
                        return 2
                    fi
                    ;;
            esac
            printf -v "$tfgen_key" '%s' "$tfgen_value"
        fi
        if [[ -v "$tfgen_key" ]]; then export "$tfgen_key"; fi
    done
    return 0
}

tfgen_clean_python_environment() {
    # Capture absence, empty and nonempty values once. Nested GUI launchers
    # retain this snapshot instead of mistaking GUI injection for Cadence state.
    local tfgen_original_key tfgen_original_set tfgen_original_value
    if [[ "${TFGEN_ORIGINAL_PYTHON_ENV_READY-}" != 1 ]]; then
        for tfgen_original_key in LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH TCL_LIBRARY TK_LIBRARY QT_PLUGIN_PATH; do
            tfgen_original_set="TFGEN_ORIGINAL_${tfgen_original_key}_SET"
            tfgen_original_value="TFGEN_ORIGINAL_${tfgen_original_key}"
            if [[ "$tfgen_original_key" == LD_LIBRARY_PATH && -v TFGEN_CAPTURED_LD_LIBRARY_PATH ]]; then
                # Legacy snapshots did not encode absence versus empty.
                printf -v "$tfgen_original_set" '%s' "$([[ -n "$TFGEN_CAPTURED_LD_LIBRARY_PATH" ]] && printf 1 || printf 0)"
                printf -v "$tfgen_original_value" '%s' "$TFGEN_CAPTURED_LD_LIBRARY_PATH"
            elif [[ -v "$tfgen_original_key" ]]; then
                printf -v "$tfgen_original_set" '%s' 1
                printf -v "$tfgen_original_value" '%s' "${!tfgen_original_key}"
            else
                printf -v "$tfgen_original_set" '%s' 0
                printf -v "$tfgen_original_value" '%s' ''
            fi
            export "$tfgen_original_set" "$tfgen_original_value"
        done
        export TFGEN_ORIGINAL_PYTHON_ENV_READY=1
    fi
    export TFGEN_CAPTURED_LD_LIBRARY_PATH="${TFGEN_CAPTURED_LD_LIBRARY_PATH-${TFGEN_ORIGINAL_LD_LIBRARY_PATH-}}"
    unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH TCL_LIBRARY TK_LIBRARY QT_PLUGIN_PATH
    local tfgen_field tfgen_target
    for tfgen_field in TFGEN_PYTHON_LD_LIBRARY_PATH TFGEN_PYTHONHOME TFGEN_PYTHONPATH TFGEN_TCL_LIBRARY TFGEN_TK_LIBRARY; do
        case "$tfgen_field" in
            TFGEN_PYTHON_LD_LIBRARY_PATH) tfgen_target=LD_LIBRARY_PATH ;;
            TFGEN_PYTHONHOME) tfgen_target=PYTHONHOME ;;
            TFGEN_PYTHONPATH) tfgen_target=PYTHONPATH ;;
            TFGEN_TCL_LIBRARY) tfgen_target=TCL_LIBRARY ;;
            TFGEN_TK_LIBRARY) tfgen_target=TK_LIBRARY ;;
        esac
        if [[ -n "${!tfgen_field-}" ]]; then
            printf -v "$tfgen_target" '%s' "${!tfgen_field}"
            export "$tfgen_target"
            printf 'Flux launch stage=python-environment field=%s path=%q: explicitly selected interpreter dependency.\n' "$tfgen_target" "${!tfgen_field}" >&2
        fi
    done
}

tfgen_python_probe_once() {
    local tfgen_candidate="$1" tfgen_status
    local tfgen_source="${tfgen_python_probe_source-direct}"
    if [[ ! -f "$tfgen_candidate" || ! -x "$tfgen_candidate" ]]; then
        printf 'Flux launch stage=python-path source=%q path=%q exit=126: interpreter must be an existing executable file.\n' "$tfgen_source" "$tfgen_candidate" >&2
        return 126
    fi
    printf 'Flux launch stage=python-exec source=%q path=%q.\n' "$tfgen_source" "$tfgen_candidate" >&2
    if "$tfgen_candidate" -c '
import sys
import traceback
stage = "version"
try:
    sys.stderr.write("Flux probe stage=version sys.executable=%r sys.version=%r\n" % (sys.executable, sys.version))
    if sys.version_info < (3, 7):
        raise RuntimeError("Python >=3.7 is required")
    stage = "tk_import"
    sys.stderr.write("Flux probe stage=tk_import\n")
    import tkinter
    from tkinter import ttk
    stage = "tcl_init"
    sys.stderr.write("Flux probe stage=tcl_init\n")
    tkinter.Tcl()
except BaseException:
    sys.stderr.write("Flux probe stage=%s status=failed\n" % stage)
    traceback.print_exc()
    sys.exit(1)
sys.stderr.write("Flux probe stage=complete status=ok\n")
' </dev/null >/dev/null; then
        printf 'Flux launch stage=python-probe source=%q path=%q exit=0.\n' "$tfgen_source" "$tfgen_candidate" >&2
        return 0
    else
        tfgen_status=$?
        printf 'Flux launch stage=python-probe source=%q path=%q exit=%s.\n' "$tfgen_source" "$tfgen_candidate" "$tfgen_status" >&2
        return "$tfgen_status"
    fi
}

tfgen_restore_original_python_environment() {
    local tfgen_key tfgen_state tfgen_value
    for tfgen_key in LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH TCL_LIBRARY TK_LIBRARY QT_PLUGIN_PATH; do
        tfgen_state="TFGEN_ORIGINAL_${tfgen_key}_SET"
        tfgen_value="TFGEN_ORIGINAL_${tfgen_key}"
        unset "$tfgen_key"
        if [[ "${!tfgen_state-}" == 1 ]]; then
            printf -v "$tfgen_key" '%s' "${!tfgen_value-}"
            export "$tfgen_key"
        fi
    done
}

tfgen_apply_runtime_environment() {
    local tfgen_result="$1" tfgen_expected="$2" tfgen_owner tfgen_mode tfgen_links tfgen_size
    local tfgen_record tfgen_key tfgen_value tfgen_part tfgen_remaining
    local -A tfgen_result_values=()
    [[ -f "$tfgen_result" && ! -L "$tfgen_result" ]] || return 1
    read -r tfgen_owner tfgen_mode tfgen_links tfgen_size < <(LC_ALL=C stat -c '%u %a %h %s' -- "$tfgen_result")
    [[ "$tfgen_owner" == "$UID" && "$tfgen_mode" == 600 && "$tfgen_links" == 1 ]] || return 1
    (( tfgen_size <= 65536 )) || return 1
    tfgen_record=''
    while IFS= read -r -d '' tfgen_record; do
        [[ "$tfgen_record" == *=* ]] || return 1
        tfgen_key="${tfgen_record%%=*}"
        case "$tfgen_key" in
            TFGEN_RUNTIME_SCHEMA|TFGEN_RUNTIME_INTERPRETER|LD_LIBRARY_PATH|TCL_LIBRARY|TK_LIBRARY) ;;
            *) return 1 ;;
        esac
        [[ -z "${tfgen_result_values[$tfgen_key]+present}" ]] || return 1
        tfgen_result_values["$tfgen_key"]="${tfgen_record#*=}"
    done < "$tfgen_result"
    [[ -z "$tfgen_record" ]] || return 1
    [[ "${tfgen_result_values[TFGEN_RUNTIME_SCHEMA]-}" == tfgen.python.runtime.v1 &&
       "${tfgen_result_values[TFGEN_RUNTIME_INTERPRETER]-}" == "$tfgen_expected" ]] || return 1
    # Validate every record before mutating any part of the probe/UI environment.
    for tfgen_key in LD_LIBRARY_PATH TCL_LIBRARY TK_LIBRARY; do
        [[ -n "${tfgen_result_values[$tfgen_key]+present}" ]] || continue
        tfgen_remaining="${tfgen_result_values[$tfgen_key]}"
        if [[ "$tfgen_key" != LD_LIBRARY_PATH ]]; then
            [[ "$tfgen_remaining" == /* && -d "$tfgen_remaining" ]] || return 1
            continue
        fi
        [[ -n "$tfgen_remaining" && "$tfgen_remaining" != :* && "$tfgen_remaining" != *: && "$tfgen_remaining" != *::* ]] || return 1
        while :; do
            tfgen_part="${tfgen_remaining%%:*}"
            [[ "$tfgen_part" == /* && -d "$tfgen_part" ]] || return 1
            [[ "$tfgen_remaining" == *:* ]] || break
            tfgen_remaining="${tfgen_remaining#*:}"
        done
    done
    for tfgen_key in LD_LIBRARY_PATH TCL_LIBRARY TK_LIBRARY; do
        if [[ -n "${tfgen_result_values[$tfgen_key]+present}" ]]; then
            printf -v "$tfgen_key" '%s' "${tfgen_result_values[$tfgen_key]}"
            export "$tfgen_key"
        fi
    done
}

tfgen_resolve_runtime() {
    local tfgen_candidate="$1" tfgen_with_tk="${2-no}" tfgen_controller='' tfgen_choice tfgen_location
    local tfgen_result="${TFGEN_RUNTIME_ENV_FILE-}" tfgen_private='' tfgen_status
    local -a tfgen_controls=("$tfgen_candidate") tfgen_arguments=()
    [[ -f "$TFGEN_RUNTIME_SCRIPT" ]] || return 1
    for tfgen_location in python3 python; do
        tfgen_choice="$(command -v -- "$tfgen_location" 2>/dev/null || true)"
        [[ -z "$tfgen_choice" ]] || tfgen_controls+=("$tfgen_choice")
    done
    tfgen_controls+=(/usr/bin/python3 /usr/bin/python)
    for tfgen_choice in "${tfgen_controls[@]}"; do
        [[ -f "$tfgen_choice" && -x "$tfgen_choice" ]] || continue
        if "$tfgen_choice" -c 'import sys,json,struct; sys.exit(0 if sys.version_info >= (3,6) else 1)' </dev/null >/dev/null 2>/dev/null; then
            tfgen_controller="$tfgen_choice"
            break
        fi
    done
    if [[ -z "$tfgen_controller" ]]; then
        printf 'Flux runtime failed: stage=bootstrap interpreter=%q; no executable Python >=3.6 can run the bounded dependency resolver. The original loader exception above identifies the missing runtime.\n' "$tfgen_candidate" >&2
        return 1
    fi
    if [[ -z "$tfgen_result" ]]; then
        tfgen_private="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/flux-python-runtime.XXXXXXXX")" || return 1
        tfgen_result="$tfgen_private/result.env"
    fi
    tfgen_arguments=(--python "$tfgen_candidate" --output "$tfgen_result")
    [[ "$tfgen_with_tk" != yes ]] || tfgen_arguments+=(--with-tk)
    if "$tfgen_controller" "$TFGEN_RUNTIME_SCRIPT" "${tfgen_arguments[@]}" </dev/null >/dev/null; then
        if tfgen_apply_runtime_environment "$tfgen_result" "$tfgen_candidate"; then
            tfgen_status=0
        else
            printf '%s\n' 'Flux runtime failed: unsafe or mismatched dependency result; no environment was applied.' >&2
            tfgen_status=1
        fi
    else
        tfgen_status=$?
    fi
    if [[ -n "$tfgen_private" ]]; then
        [[ ! -f "$tfgen_result" || -L "$tfgen_result" ]] || rm -f -- "$tfgen_result"
        rmdir -- "$tfgen_private" 2>/dev/null || true
    fi
    return "$tfgen_status"
}

tfgen_python_works() {
    local tfgen_candidate="$1" tfgen_private tfgen_status
    if [[ "${TFGEN_NO_AUTORECOVERY-no}" == yes || ! -f "$tfgen_candidate" || ! -x "$tfgen_candidate" ]]; then
        tfgen_python_probe_once "$tfgen_candidate"
        return $?
    fi
    tfgen_private="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/flux-python-probe.XXXXXXXX")" || return 1
    if tfgen_python_probe_once "$tfgen_candidate" 2>"$tfgen_private/probe.stderr"; then
        tfgen_status=0
    else
        tfgen_status=$?
    fi
    cat -- "$tfgen_private/probe.stderr" >&2
    if [[ "$tfgen_status" != 0 ]] &&
       LC_ALL=C grep -Eq 'cannot open shared object file|error while loading shared libraries|[Cc]an.t find.*(init[.]tcl|tk[.]tcl)|[Cc]an.t find a usable|[Cc]ould not find.*(init[.]tcl|tk[.]tcl)' "$tfgen_private/probe.stderr"; then
        if tfgen_resolve_runtime "$tfgen_candidate"; then tfgen_status=0; fi
    fi
    rm -f -- "$tfgen_private/probe.stderr"
    rmdir -- "$tfgen_private"
    return "$tfgen_status"
}

tfgen_absolute_python() {
    local tfgen_value="$1" tfgen_parent
    [[ -n "$tfgen_value" ]] || return 1
    if [[ "$tfgen_value" == /* ]]; then
        printf '%s\n' "$tfgen_value"
    else
        tfgen_parent="$(cd -- "$(dirname -- "$tfgen_value")" && pwd)" || return 1
        printf '%s/%s\n' "$tfgen_parent" "$(basename -- "$tfgen_value")"
    fi
}

tfgen_select_python() {
    local tfgen_root="$1" tfgen_candidate tfgen_location tfgen_prefix
    local -a tfgen_candidates=()
    local -A tfgen_seen=()
    if [[ -n "${TFGEN_PYTHON-}" ]]; then
        tfgen_python_probe_source="${tfgen_python_override_source-explicit override}"
        tfgen_candidate="$TFGEN_PYTHON"
        if [[ "$tfgen_candidate" != */* ]]; then
            tfgen_candidate="$(command -v -- "$tfgen_candidate" 2>/dev/null || true)"
        fi
        tfgen_candidate="$(tfgen_absolute_python "$tfgen_candidate" 2>/dev/null || true)"
        if tfgen_python_works "$tfgen_candidate"; then
            printf '%s\n' "$tfgen_candidate"
            return 0
        fi
        printf '%s\n' "Flux: the explicitly selected interpreter failed startup; see the recorded stage, dependency and exception above." >&2
        printf '%s\n' "Check the explicit override or the optional $tfgen_root/flux_config.sh." >&2
        return 1
    fi
    tfgen_candidates+=("$tfgen_root/.venv/bin/python")
    for tfgen_location in python3 python; do
        tfgen_candidate="$(command -v -- "$tfgen_location" 2>/dev/null || true)"
        [[ -z "$tfgen_candidate" ]] || tfgen_candidates+=("$tfgen_candidate")
    done
    for tfgen_prefix in "${CONDA_PREFIX-}" "${VIRTUAL_ENV-}"; do
        [[ -z "$tfgen_prefix" ]] || tfgen_candidates+=("$tfgen_prefix/bin/python")
    done
    for tfgen_prefix in "${HOME-}/miniforge3" "${HOME-}/miniconda3" "${HOME-}/anaconda3" /opt/miniforge3 /opt/miniconda3 /opt/anaconda3; do
        tfgen_candidates+=("$tfgen_prefix/bin/python")
        for tfgen_candidate in "$tfgen_prefix"/envs/*/bin/python; do
            if [[ -x "$tfgen_candidate" ]]; then
                tfgen_candidates+=("$tfgen_candidate")
            fi
        done
    done
    for tfgen_candidate in "${tfgen_candidates[@]}"; do
        tfgen_python_probe_source=automatic
        tfgen_candidate="$(tfgen_absolute_python "$tfgen_candidate" 2>/dev/null || true)"
        [[ -n "$tfgen_candidate" ]] || continue
        [[ -z "${tfgen_seen[$tfgen_candidate]-}" ]] || continue
        tfgen_seen["$tfgen_candidate"]=1
        if tfgen_python_works "$tfgen_candidate"; then
            printf '%s\n' "$tfgen_candidate"
            return 0
        fi
    done
    printf '%s\n' 'Flux: no candidate passed the recorded Python/Tk runtime checks; see each candidate failure above.' >&2
    printf '%s\n' "If automatic detection cannot find your installation, copy $tfgen_root/flux_config.sh.example to flux_config.sh and fill only the required paths." >&2
    return 1
}
