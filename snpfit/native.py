"""New-cell-only analogLib schematic creation and native Spectre netlisting.

No workspace startup, PDK, tool installation or existing cell is modified.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import Counter

_ALLOWED = {"res", "cap", "ind", "mind"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _q(value):
    return json.dumps(str(value), ensure_ascii=True)


def _validate(circuit):
    ports = list(circuit["ports"])
    reference = circuit.get("reference", "REF")
    names = ports + [reference]
    if not ports or len(names) != len(set(names)):
        raise ValueError("Signal ports and reference must be nonempty and unique")
    metadata = circuit.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Circuit metadata must be an object")
    for key in ("export_reference", "explicit_reference"):
        if key in metadata and type(metadata[key]) is not bool:
            raise ValueError("Reference-export metadata must use a boolean")
    elements = circuit["elements"]
    counts = Counter(e["kind"] for e in elements)
    component_limit = circuit.get("metadata", {}).get("component_limit", 20)
    if component_limit not in (20,30):
        raise ValueError("Per-kind component limit must be 20 or 30")
    if not elements or set(counts) - _ALLOWED or any(n > component_limit for n in counts.values()):
        raise ValueError("Only res/cap/ind/mind within the selected per-kind limit are allowed")
    if len({e["name"] for e in elements}) != len(elements):
        raise ValueError("Duplicate element name")
    inductors = {e["name"] for e in elements if e["kind"] == "ind"}
    for e in elements:
        names.append(e["name"])
        value = float(e["value"])
        if not math.isfinite(value):
            raise ValueError("Nonfinite component value")
        if e["kind"] == "mind":
            if abs(value) >= 1 or e["ind1"] == e["ind2"] or not {e["ind1"], e["ind2"]} <= inductors:
                raise ValueError("Invalid mutual coupling/reference")
        else:
            names += [e["n1"], e["n2"]]
            if value <= 0 or e["n1"] == e["n2"]:
                raise ValueError("R/L/C must be positive and connect two distinct nodes")
    if not all(isinstance(n, str) and _IDENT.fullmatch(n) for n in names):
        raise ValueError("Names must be simple portable identifiers")
    return counts


def _writable_as(path, uid, gid):
    """Check directory traversal for a target local user without changing ancestors."""
    for p in [path] + list(path.parents):
        stat = p.stat()
        bit = 0o100 if stat.st_uid == uid else 0o010 if stat.st_gid == gid else 0o001
        if not stat.st_mode & bit:
            return False
    return True


def _own_new_tree(path, uid, gid):
    if os.geteuid() == 0:
        for p in [path] + list(path.rglob("*")):
            os.chown(p, uid, gid)
            if p.is_dir():
                p.chmod(0o700)
            elif p.is_file():
                p.chmod(0o600)


_NATIVE_EXTRA_KEYS = ("PATH", "CDS_LIC_FILE", "LM_LICENSE_FILE", "CDSHOME", "IC_HOME", "CDS_ROOT", "CDS_INST_DIR", "OA_HOME", "CDS_AUTO_64BIT")
_NATIVE_KEYS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY",
                "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "LC_ALL", "LANG")


def native_environment(snapshot_override=None):
    """Restore launcher-captured unset/empty/value without GUI overlays."""
    env = os.environ.copy()
    snapshot = snapshot_override if snapshot_override is not None else env.get("SNPFIT_NATIVE_ENV_JSON")
    if isinstance(snapshot, dict): snapshot = json.dumps(snapshot)
    if snapshot is not None:
        if len(snapshot) > 65536:
            raise ValueError("Native environment snapshot is too large")
        data = json.loads(snapshot)
        required = set(_NATIVE_KEYS) - {"LD_PRELOAD"}
        if not isinstance(data, dict) or not required <= set(data) or set(data) - set(_NATIVE_KEYS + _NATIVE_EXTRA_KEYS):
            raise ValueError("Invalid native environment snapshot keys")
        for key, item in data.items():
            if not isinstance(item, dict) or set(item) != {"set", "value"} or type(item["set"]) is not bool:
                raise ValueError("Invalid native environment snapshot state")
            if item["set"] and not isinstance(item["value"], str):
                raise ValueError("Native environment value must be text")
            if not item["set"] and item["value"] is not None:
                raise ValueError("Unset native environment value must be null")
            env.pop(key, None)
            if item["set"]:
                env[key] = item["value"]
    for key in list(env):
        if key.startswith("CDS_MPS_"):
            env.pop(key, None)
    return env


def _run_native(executable, script, work, env_script, user, seconds, name, cancel=None, native_env_snapshot=None):
    env = native_environment(native_env_snapshot)
    # Search helpers under the actual selected executable installation.
    executable_path = Path(executable).resolve()
    helper_dirs = []
    for parent in list(executable_path.parents)[:6]:
        candidate = parent / "bin"
        if candidate.is_dir() and str(candidate) not in helper_dirs: helper_dirs.append(str(candidate))
        if parent.name.startswith("tools."):
            sibling = parent.parent / "tools" / "bin"
            if sibling.is_dir() and str(sibling) not in helper_dirs: helper_dirs.append(str(sibling))
    if helper_dirs: env["PATH"] = os.pathsep.join(helper_dirs + [env.get("PATH", "")])
    command = ["/bin/bash", "-c",
               'if [ -n "$1" ]; then source "$1" || exit $?; fi; '
               'export CDS_LOAD_ENV=CWD; unset CDS_LIB_PATH CDS_LOAD_PATH; '
               'exec "$2" -nograph -nocdsinit -restore "$3" -log "$4"',
               "snpfit-native", str(env_script or ""), str(executable), str(script), name + ".log"]
    if os.geteuid() == 0 and user != "root":
        command = [shutil.which("runuser", path=os.defpath + os.pathsep + "/usr/sbin:/sbin") or "runuser", "-u", user, "--"] + command
    start = time.time()
    with open(work / (name + "-console.log"), "w", encoding="utf-8") as output:
        proc = subprocess.Popen(command, cwd=work, env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        status = "running"
        try:
            while proc.poll() is None:
                if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                    status = "cancelled"
                    raise RuntimeError("Native creation cancelled")
                if time.time() - start > seconds:
                    status = "timeout"
                    raise TimeoutError("Native operation timed out")
                time.sleep(.1)
            status = "completed" if proc.returncode == 0 else "failed"
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            receipt = {"argv": command, "cwd": str(work), "pid": proc.pid,
                       "returncode": proc.returncode, "status": status,
                       "started": start, "finished": time.time()}
            (work / (name + "-receipt.json")).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    text = (work / (name + "-console.log")).read_text(encoding="utf-8", errors="replace")
    if proc.returncode or "*Error*" in text or "SNPFIT_NATIVE_FAILED" in text or re.search(r"^Error:|^ERROR \(OSSHNL", text, re.M):
        raise RuntimeError(f"Native {name} failed; see {work / (name + '-console.log')}")
    return receipt, text


def build_native(circuit_dict, workspace, out_dir, library, cell, *,
                 existing_library_path=None, user=None, env_script=None,
                 virtuoso=None, ocean=None, seconds=120, netlist=True, cancel=None,
                 visual_plan=None, make_symbol=True, cds_lib=None, native_env_snapshot=None, portable_runtime=False):
    """Create one new fitted schematic+symbol, verify saved connectivity, netlist.

    Returns exact library path and native netlist. The private cds.lib inherits
    the chosen workspace. If an existing library path is explicitly supplied,
    only a new cell is created there; any existing cell is refused.
    """
    from .native_visual import (circuit_fingerprint, file_sha256, json_hash,
                                read_visual_receipt, validate_visual_plan, visual_skill_header)
    counts = _validate(circuit_dict)
    if type(make_symbol) is not bool:
        raise ValueError('make_symbol must be a boolean')
    plan = validate_visual_plan(circuit_dict, visual_plan) if visual_plan is not None else None
    if not _IDENT.fullmatch(library) or not _IDENT.fullmatch(cell):
        raise ValueError("Library and cell names must be portable identifiers")
    workspace = Path(workspace).resolve()
    out_dir = Path(out_dir).resolve()
    cds_lib = Path(cds_lib).resolve() if cds_lib else workspace / "cds.lib"
    if not cds_lib.is_file():
        raise ValueError("Workspace cds.lib not found")
    out_dir.mkdir(parents=True, exist_ok=True)
    owner = pwd.getpwnam(user) if user else pwd.getpwuid(workspace.stat().st_uid)
    user = owner.pw_name
    # Never change permissions of /root or unrelated parent directories.
    accessible = _writable_as(out_dir, owner.pw_uid, owner.pw_gid)
    work = out_dir / "native" if accessible else Path(tempfile.mkdtemp(prefix="snpfit-native-"))
    work.mkdir(exist_ok=True)
    library_path = Path(existing_library_path).resolve() if existing_library_path else work / "library"
    if existing_library_path:
        if not library_path.is_dir() or (library_path / cell).exists():
            raise ValueError("Existing library must exist and selected new cell must not exist")
    else:
        library_path.mkdir(exist_ok=True)
    if (library_path / cell).exists():
        raise ValueError("Refusing to overwrite an existing cell")
    cds = f"SOFTINCLUDE {cds_lib}\nDEFINE {library} {library_path}\n"
    (work / "cds.lib").write_text(cds, encoding="utf-8")
    skill_source = Path(__file__).resolve().parent.parent / "skill" / "snpfit_native.il"
    shutil.copy2(skill_source, work / "snpfit_native.il")
    metadata = circuit_dict.get("metadata", {})
    export_ref = metadata.get("export_reference", metadata.get("explicit_reference", True))
    header = [f"snpfitLib={_q(library)}", f"snpfitCell={_q(cell)}",
              "snpfitPorts=list(" + " ".join(_q(p) for p in circuit_dict["ports"]) + ")",
              f"snpfitReference={_q(circuit_dict.get('reference','REF'))}",
              "snpfitExportReference=" + ("t" if export_ref else "nil"),
              f"snpfitConnectivityFile={_q(work / 'connectivity.txt')}",
              f"snpfitTitle={_q(circuit_dict.get('name', cell))}",
              f"snpfitCircuitHash={_q(circuit_fingerprint(circuit_dict))}",
              'snpfitMakeSymbolEnabled=' + ('t' if make_symbol else 'nil')]
    if plan is not None:
        header.extend(visual_skill_header(plan, work/'visual-receipt.txt'))
        (work/'visual-plan.json').write_text(json.dumps(plan, indent=2, allow_nan=False), encoding='utf-8')
    else:
        header.append('snpfitHasVisual=nil')
    header.append("snpfitElements=list(")
    for e in circuit_dict["elements"]:
        row = [e["name"], e["kind"], e["ind1"] if e["kind"] == "mind" else e["n1"],
               e["ind2"] if e["kind"] == "mind" else e["n2"], format(float(e["value"]), ".16g")]
        header.append("  list(" + " ".join(_q(v) for v in row) + ")")
    header += [")", 'load("snpfit_native.il")',
               'snpfitResult=errset(snpfitBuild() t)',
               'unless(snpfitResult printf("SNPFIT_NATIVE_FAILED\\n"))', "exit()"]
    (work / "build.il").write_text("\n".join(header) + "\n", encoding="utf-8")
    if not portable_runtime:
        env_script = env_script or os.environ.get("SNPFIT_ENV_SCRIPT")
        virtuoso = virtuoso or shutil.which("virtuoso")
        ocean = ocean or shutil.which("ocean")
    if not virtuoso or not ocean: raise ValueError("Native Virtuoso and Ocean executables are required")
    _own_new_tree(work, owner.pw_uid, owner.pw_gid)
    receipt, text = _run_native(virtuoso, work / "build.il", work, env_script, user, seconds, "build", cancel, native_env_snapshot)
    if "SNPFIT_NATIVE_CREATED " not in text or not (work / "connectivity.txt").is_file():
        raise RuntimeError(f"Native creation did not verify saved connectivity; see {work}")
    result = {"status": "created", "workspace": str(workspace), "library": library,
              "cell": cell, "library_path": str(library_path), "native_work": str(work), "cds_lib": str(cds_lib),
              "component_counts": dict(counts), "connectivity": str(work / "connectivity.txt"),
              "reference_exported": export_ref, "create_receipt": receipt,
              "schematic_ports": list(circuit_dict["ports"]) + ([circuit_dict.get("reference", "REF")] if export_ref else []),
              "mapping_line": f"DEFINE {library} {library_path}",
              "symbol_created": make_symbol,
              "circuit_fingerprint": circuit_fingerprint(circuit_dict)}
    if make_symbol:
        symbol_match = re.search(r"SNPFIT_SYMBOL_PORTS \(([^\n]*)\)", text)
        if not symbol_match:
            raise RuntimeError("Saved symbol terminal verification is missing")
        symbol_ports = re.findall(r'"([^"\n]+)"', symbol_match.group(1))
        if sorted(symbol_ports) != sorted(result["schematic_ports"]):
            raise RuntimeError("Saved symbol and schematic terminal sets differ")
        result["symbol_ports"] = symbol_ports
    if plan is not None:
        result['visual_plan_sha256'] = json_hash(plan)
        result['symbol_plan_sha256'] = json_hash(plan['symbol'])
        result['visual_plan_file'] = str(work/'visual-plan.json')
        result['visual_schematic'] = read_visual_receipt(work/'visual-receipt.txt', plan, 'SCHEMATIC')
        if make_symbol:
            result['visual_symbol'] = read_visual_receipt(work/'visual-receipt.txt', plan, 'SYMBOL')
    if netlist:
        ocn = [f'envSetVal("asimenv.startup" "projectDir" \'string {_q(work / "simulation")})',
               "simulator('spectre)", f"design({_q(library)} {_q(cell)} \"schematic\")",
               f"resultsDir({_q(work / 'simulation' / 'psf')})",
               "snpfitNetlist=errset(createNetlist(?recreateAll t ?display nil) t)",
               'printf("SNPFIT_NETLIST_RESULT %L\\n" snpfitNetlist)',
               "exit()"]
        (work / "netlist.ocn").write_text("\n".join(ocn) + "\n", encoding="utf-8")
        _own_new_tree(work / "netlist.ocn", owner.pw_uid, owner.pw_gid)
        receipt2, text2 = _run_native(ocean, work / "netlist.ocn", work, env_script, user, seconds, "netlist", cancel, native_env_snapshot)
        match = re.search(r'SNPFIT_NETLIST_RESULT \("([^"\n]+)"\)', text2)
        native_input = Path(match.group(1)) if match else None
        circuit_netlist = native_input.parent / "netlist" if native_input else None
        if native_input is None or not native_input.is_file() or not circuit_netlist.is_file():
            raise RuntimeError(f"Native netlist was not produced uniquely; see {work}")
        result.update(status="netlisted", netlist=str(native_input),
                      circuit_netlist=str(circuit_netlist), netlist_receipt=receipt2,
                      ports=list(circuit_dict["ports"]), reference=circuit_dict.get("reference", "REF"))
    schematic_file = library_path/cell/'schematic'/'sch.oa'
    result['schematic_sha256'] = file_sha256(schematic_file)
    provenance = {'schema': 1, 'tool': 'SNPFit', 'library': library, 'cell': cell,
                  'library_path': str(library_path), 'circuit_fingerprint': result['circuit_fingerprint'],
                  'schematic_sha256': result['schematic_sha256'],
                  'visual_plan_sha256': result.get('visual_plan_sha256'),
                  'symbol_plan_sha256': result.get('symbol_plan_sha256')}
    if netlist:
        result['netlist_sha256'] = file_sha256(result['circuit_netlist'])
        provenance['netlist_sha256'] = result['netlist_sha256']
    provenance_file = work/'native-provenance.json'
    provenance_file.write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    _own_new_tree(provenance_file, owner.pw_uid, owner.pw_gid)
    result['provenance_file'] = str(provenance_file)
    (work / "native-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _own_new_tree(work, owner.pw_uid, owner.pw_gid)
    if work.parent != out_dir:
        # Evidence remains usable at native_work; copy to caller-owned run too.
        evidence = out_dir / "native-evidence"
        shutil.copytree(work, evidence, dirs_exist_ok=True)
        result["evidence_copy"] = str(evidence)
    (out_dir / "native-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def build_symbol(native_result, circuit_dict, visual_plan, *, out_dir=None,
                 user=None, env_script=None, virtuoso=None, seconds=120, cancel=None, native_env_snapshot=None, portable_runtime=False):
    """Append a missing visual symbol to this tool's verified schematic only.

    Existing views are never overwritten.  The fitted schematic and its native
    netlist must retain their exact hashes before and after symbol creation.
    No fitting or simulation is performed.
    """
    if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
        raise RuntimeError('Symbol creation cancelled before native tools.')
    from .native_visual import (circuit_fingerprint, file_sha256, json_hash,
                                read_visual_receipt, validate_visual_plan, visual_skill_header)
    _validate(circuit_dict)
    plan = validate_visual_plan(circuit_dict, visual_plan)
    strict_pass = native_result.get('metrics', {}).get('joint', {}).get('passed') is True
    sampled = native_result.get('sampled_metrics') or {}
    sampled_pass = sampled.get('passed') is True and sampled.get('acceptance_scope') == 'original_sampled_points_only' and sampled.get('broadband_qualified') is False
    if not (strict_pass or sampled_pass):
        raise ValueError('Generate and verify the current schematic before creating its symbol.')
    replay_file = native_result.get('replay', {}).get('response')
    if not replay_file or not Path(replay_file).is_file():
        raise ValueError('The verified native replay evidence is missing.')
    circuit_hash = circuit_fingerprint(circuit_dict)
    if native_result.get('circuit_fingerprint') != circuit_hash:
        raise ValueError('The selected circuit differs from the generated schematic.')
    provenance_path = Path(native_result.get('provenance_file', ''))
    if not provenance_path.is_file() or provenance_path.is_symlink():
        raise ValueError('This schematic has no verified SNPFit creation receipt. Generate a new schematic first.')
    provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
    library = native_result['library']; cell = native_result['cell']
    if not _IDENT.fullmatch(library) or not _IDENT.fullmatch(cell):
        raise ValueError('Native library and cell names must be portable identifiers.')
    library_path = Path(native_result['library_path']).resolve()
    expected = {'schema': 1, 'tool': 'SNPFit', 'library': library, 'cell': cell,
                'library_path': str(library_path), 'circuit_fingerprint': circuit_hash}
    if any(provenance.get(key) != value for key,value in expected.items()):
        raise ValueError('Native schematic provenance does not match the selected target.')
    if json_hash(plan['symbol']) != provenance.get('symbol_plan_sha256'):
        raise ValueError('The selected layout reference differs from the generated schematic. Generate a new schematic first.')
    schematic = library_path/cell/'schematic'/'sch.oa'
    netlist = Path(native_result['circuit_netlist'])
    before = {'schematic': file_sha256(schematic), 'netlist': file_sha256(netlist)}
    if (before['schematic'] != provenance.get('schematic_sha256')
            or before['schematic'] != native_result.get('schematic_sha256')
            or before['netlist'] != provenance.get('netlist_sha256')
            or before['netlist'] != native_result.get('netlist_sha256')):
        raise ValueError('The generated schematic or netlist was changed. Symbol creation requires the unchanged verified model.')
    if (library_path/cell/'symbol').exists():
        raise ValueError('A symbol view already exists; it will not be overwritten.')
    workspace = Path(native_result['workspace']).resolve()
    cds_lib = Path(native_result.get('cds_lib') or workspace/'cds.lib').resolve()
    if not cds_lib.is_file():
        raise ValueError('The recorded Virtuoso workspace is unavailable.')
    target = Path(out_dir).resolve() if out_dir else Path(native_result['native_work'])/'symbol-build'
    target.mkdir(parents=True, exist_ok=True)
    owner = pwd.getpwnam(user) if user else pwd.getpwuid(workspace.stat().st_uid)
    user = owner.pw_name
    accessible = _writable_as(target, owner.pw_uid, owner.pw_gid)
    work = target/'native-symbol' if accessible else Path(tempfile.mkdtemp(prefix='snpfit-symbol-'))
    work.mkdir(exist_ok=True)
    if (work/'symbol.il').exists():
        raise ValueError('This symbol creation run already exists. Choose a fresh output directory.')
    (work/'cds.lib').write_text(f"SOFTINCLUDE {cds_lib}\nDEFINE {library} {library_path}\n", encoding='utf-8')
    shutil.copy2(Path(__file__).resolve().parent.parent/'skill'/'snpfit_native.il', work/'snpfit_native.il')
    metadata = circuit_dict.get('metadata', {})
    export_ref = metadata.get('export_reference', metadata.get('explicit_reference', True))
    lines = [f'snpfitLib={_q(library)}', f'snpfitCell={_q(cell)}',
             'snpfitPorts=list('+' '.join(_q(p) for p in circuit_dict['ports'])+')',
             f'snpfitReference={_q(circuit_dict.get("reference", "REF"))}',
             'snpfitExportReference='+('t' if export_ref else 'nil'),
             f'snpfitCircuitHash={_q(circuit_hash)}',
             f'snpfitExpectedVisualHash={_q(provenance.get("visual_plan_sha256", ""))}']
    lines.extend(visual_skill_header(plan, work/'visual-receipt.txt'))
    lines += ['load("snpfit_native.il")',
              'snpfitResult=errset(snpfitAppendSymbol() t)',
              'unless(snpfitResult printf("SNPFIT_NATIVE_FAILED\\n"))', 'exit()']
    (work/'symbol.il').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    (work/'visual-plan.json').write_text(json.dumps(plan,indent=2,allow_nan=False),encoding='utf-8')
    _own_new_tree(work, owner.pw_uid, owner.pw_gid)
    if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
        raise RuntimeError('Symbol creation cancelled before native tools.')
    if not portable_runtime:
        virtuoso = virtuoso or shutil.which('virtuoso')
        env_script = env_script or os.environ.get('SNPFIT_ENV_SCRIPT')
    if not virtuoso: raise ValueError('Native Virtuoso executable is required')
    receipt, text = _run_native(virtuoso,
                                work/'symbol.il', work, env_script,
                                user, seconds, 'symbol', cancel, native_env_snapshot)
    if 'SNPFIT_SYMBOL_CREATED ' not in text:
        raise RuntimeError('Symbol creation did not confirm saved read-back; see '+str(work))
    visual = read_visual_receipt(work/'visual-receipt.txt', plan, 'SYMBOL')
    after = {'schematic': file_sha256(schematic), 'netlist': file_sha256(netlist)}
    if before != after:
        raise RuntimeError('Symbol creation changed schematic or netlist evidence; native verification is invalid.')
    result = {'status': 'SYMBOL_CREATED', 'symbol_created': True, 'symbol_verified': True,
              'library': library, 'cell': cell, 'library_path': str(library_path),
              'native_work': str(work), 'create_receipt': receipt, 'visual_symbol': visual,
              'symbol_ports': list(visual['port_order']),
              'symbol_sha256': file_sha256(library_path/cell/'symbol'/'symbol.oa'),
              'symbol_plan_sha256': json_hash(plan['symbol']), 'circuit_fingerprint': circuit_hash,
              'schematic_sha256_before': before['schematic'], 'schematic_sha256_after': after['schematic'],
              'netlist_sha256_before': before['netlist'], 'netlist_sha256_after': after['netlist'],
              'schematic_and_netlist_unchanged': True}
    (work/'symbol-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    _own_new_tree(work, owner.pw_uid, owner.pw_gid)
    if work.parent != target:
        shutil.copytree(work,target/'native-symbol-evidence',dirs_exist_ok=True)
    (target/'symbol-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def replay_native(native_result, frequencies, z0=50.0, *, out_dir=None,
                  env_script=None, spectre=None, user=None, seconds=120, cancel=None, native_env_snapshot=None, portable_runtime=False):
    """Run RF replay using the exact component netlist emitted by Ocean/OA.

    The zero-volt reference source and RF test ports belong only to the fixture.
    The fitted model remains analogLib R/L/C/mind only.
    """
    frequencies = [float(f) for f in frequencies]
    if not frequencies or any(not math.isfinite(f) or f <= 0 for f in frequencies) or any(a >= b for a, b in zip(frequencies, frequencies[1:])):
        raise ValueError("Replay requires finite strictly increasing positive frequencies")
    ports = list(native_result["ports"])
    impedances = [float(z0)] * len(ports) if isinstance(z0, (int, float)) else list(map(float, z0))
    if len(impedances) != len(ports) or any(not math.isfinite(z) or z <= 0 for z in impedances):
        raise ValueError("One positive real reference impedance per signal port is required")
    native_work = Path(native_result["native_work"])
    work = Path(out_dir) if out_dir else native_work / "replay"
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source_netlist = Path(native_result["circuit_netlist"])
    source_text = source_netlist.read_text(encoding="utf-8")
    if not source_text.strip():
        raise ValueError("Native component netlist is empty")
    lines = ["// Fixture around the exact Ocean-generated cellview component netlist.",
             "simulator lang=spectre", "global 0", source_text]
    if native_result.get("reference_exported", True):
        lines.append(f"SNPFIT_REF ({native_result['reference']} 0) vsource dc=0")
    for index, (port, resistance) in enumerate(zip(ports, impedances), 1):
        lines.append(f"SNPFIT_PORT{index} ({port} 0) port num={index} r={resistance:.16g}")
    filename = f"response.s{len(ports)}p"
    lines += ["simulatorOptions options reltol=1e-6 vabstol=1e-9 iabstol=1e-15 temp=27 tnom=27",
              "SNPFIT_SP sp ports=[" + " ".join(f"SNPFIT_PORT{i}" for i in range(1,len(ports)+1)) + "] values=[" + " ".join(format(f,".16g") for f in frequencies) + f'] file="{filename}" datafmt=touchstone datatype=realimag paramtype=s',
              "saveOptions options save=allpub"]
    (work / "input.scs").write_text("\n".join(lines) + "\n", encoding="utf-8")
    owner = pwd.getpwnam(user) if user else pwd.getpwuid(native_work.stat().st_uid)
    user = owner.pw_name
    if not portable_runtime:
        env_script = env_script or os.environ.get("SNPFIT_ENV_SCRIPT")
        spectre = spectre or shutil.which("spectre")
    if not spectre: raise ValueError("Native Spectre executable is required")
    _own_new_tree(work, owner.pw_uid, owner.pw_gid)
    command = ["/bin/bash", "-c", 'if [ -n "$1" ]; then source "$1" || exit $?; fi; exec "$2" input.scs -format psfascii -raw psf +log spectre.log +mt=2',
               "snpfit-replay", str(env_script or ""), str(spectre)]
    if os.geteuid() == 0 and user != "root":
        command = [shutil.which("runuser", path=os.defpath + os.pathsep + "/usr/sbin:/sbin") or "runuser", "-u", user, "--"] + command
    start = time.time()
    env = native_environment(native_env_snapshot)
    status = "running"
    with open(work / "console.log", "w", encoding="utf-8") as output:
        proc = subprocess.Popen(command, cwd=work, env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while proc.poll() is None:
                if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                    status = "cancelled"
                    raise RuntimeError("Native replay cancelled")
                if time.time() - start > seconds:
                    status = "timeout"
                    raise TimeoutError("Native replay timed out")
                time.sleep(.1)
            status = "completed" if proc.returncode == 0 else "failed"
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            receipt = {"argv":command,"cwd":str(work),"pid":proc.pid,"status":status,
                       "returncode":proc.returncode,"started":start,"finished":time.time(),
                       "source_native_netlist":str(source_netlist)}
            (work / "receipt.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
    log_path = work / "spectre.log"
    log = log_path.read_text(encoding="utf-8",errors="replace") if log_path.is_file() else ""
    summaries = re.findall(r"spectre completes with\s+(\d+)\s+errors?",log,re.I)
    response = work / filename
    if proc.returncode or not summaries or any(int(n) for n in summaries) or not response.is_file() or response.stat().st_size < 100:
        raise RuntimeError(f"Native Spectre replay failed; see {work}")
    receipt.update(status="completed",response=str(response),log=str(log_path),frequency_count=len(frequencies))
    (work / "receipt.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
    return receipt


def verify_published(circuit_dict, visual_plan, workspace, cds_lib, library, cell, library_path, out_dir,
                     *, user=None, env_script=None, virtuoso=None, native_env_snapshot=None, cancel=None):
    """Reopen the published target read-only and check its real OA connectivity."""
    from .native_visual import circuit_fingerprint, json_hash, validate_visual_plan, visual_skill_header
    plan = validate_visual_plan(circuit_dict, visual_plan)
    if not _IDENT.fullmatch(library) or not _IDENT.fullmatch(cell):
        raise ValueError('Invalid published target')
    workspace, cds_lib, library_path = Path(workspace).resolve(), Path(cds_lib).resolve(), Path(library_path).resolve()
    if not cds_lib.is_file() or not library_path.is_dir(): raise ValueError('Published target mapping unavailable')
    owner = pwd.getpwnam(user) if user else pwd.getpwuid(workspace.stat().st_uid)
    user = owner.pw_name
    out = Path(out_dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    work = out/'native-published' if _writable_as(out, owner.pw_uid, owner.pw_gid) else Path(tempfile.mkdtemp(prefix='snpfit-published-'))
    work.mkdir(exist_ok=True)
    (work/'cds.lib').write_text(f'SOFTINCLUDE {cds_lib}\nDEFINE {library} {library_path}\n', encoding='utf-8')
    shutil.copy2(Path(__file__).resolve().parent.parent/'skill'/'snpfit_native.il', work/'snpfit_native.il')
    metadata = circuit_dict.get('metadata', {})
    export_ref = metadata.get('export_reference', metadata.get('explicit_reference', True))
    pins = list(circuit_dict['ports']) + ([circuit_dict.get('reference','REF')] if export_ref else [])
    lines = [f'snpfitLib={_q(library)}',f'snpfitCell={_q(cell)}',
             'snpfitPorts=list('+' '.join(_q(p) for p in circuit_dict['ports'])+')',
             f'snpfitReference={_q(circuit_dict.get("reference","REF"))}',
             'snpfitExportReference='+('t' if export_ref else 'nil'),
             f'snpfitCircuitHash={_q(circuit_fingerprint(circuit_dict))}',
             f'snpfitConnectivityFile={_q(work/"connectivity.txt")}']
    lines.extend(visual_skill_header(plan,work/'visual-receipt.txt'))
    lines.append('snpfitElements=list(')
    for e in circuit_dict['elements']:
        row=[e['name'],e['kind'],e['ind1'] if e['kind']=='mind' else e['n1'],e['ind2'] if e['kind']=='mind' else e['n2'],format(float(e['value']),'.16g')]
        lines.append(' list('+' '.join(_q(v) for v in row)+')')
    lines.extend([')', 'load("snpfit_native.il")',
                  'procedure(snpfitVerifyPublished() let((cv names devices row inst)',
                  ' names=if(snpfitExportReference then append(snpfitPorts list(snpfitReference)) else snpfitPorts)',
                  ' foreach(view list("schematic" "symbol")',
                  '  cv=dbOpenCellViewByType(snpfitLib snpfitCell view "" "r")',
                  '  unless(cv error("Cannot open published %s\\n" view))',
                  '  unless(cv~>snpfitGenerated=="SNPFit" && cv~>snpfitCircuitHash==snpfitCircuitHash && cv~>snpfitVisualPlanHash==snpfitVisualPlanHash error("Published identity differs %s\\n" view))',
                  '  unless(sort(cv~>terminals~>name \'alphalessp)==sort(copy(names) \'alphalessp) error("Published terminals differ %s\\n" view))',
                  '  when(view=="schematic"',
                  '   devices=setof(x cv~>instances x~>libName=="analogLib" && member(x~>cellName list("res" "ind" "cap" "mind")))',
                  '   unless(length(devices)==length(snpfitElements) error("Published component count differs\\n"))',
                  '   foreach(row snpfitElements inst=car(setof(x devices x~>name==car(row))) unless(inst && inst~>cellName==nth(1 row) error("Published component master differs %s\\n" car(row))))',
                  '   snpfitCheckConnectivity(cv)',
                  '   snpfitCheckVisual(cv snpfitVisualPins "SCHEMATIC"))',
                  '  when(view=="symbol" snpfitCheckVisual(cv snpfitSymbolPins "SYMBOL"))',
                  '  dbClose(cv))',
                  ' printf("SNPFIT_PUBLISHED_VERIFIED %s %s\\n" snpfitLib snpfitCell) t))',
                  'unless(errset(snpfitVerifyPublished() t) printf("SNPFIT_NATIVE_FAILED\\n"))','exit()'])
    (work/'verify.il').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    _own_new_tree(work,owner.pw_uid,owner.pw_gid)
    if not virtuoso: raise ValueError('Native Virtuoso executable is required for published verification')
    receipt, text = _run_native(virtuoso, work/'verify.il', work,
                                env_script,user,120,'published',cancel,native_env_snapshot)
    if 'SNPFIT_PUBLISHED_VERIFIED ' not in text or not (work/'connectivity.txt').is_file():
        raise RuntimeError('Published OA read-back did not complete; see '+str(work))
    from .native_visual import read_visual_receipt
    read_visual_receipt(work/'visual-receipt.txt',plan,'SCHEMATIC')
    read_visual_receipt(work/'visual-receipt.txt',plan,'SYMBOL')
    evidence = work
    if work.parent != out:
        evidence = out/'native-published-evidence'
        shutil.copytree(work, evidence)
    return {'published_verified':True,'work':str(evidence),'log':str(evidence/'published-console.log'),
            'connectivity':str(evidence/'connectivity.txt'),'receipt':receipt,'ports':pins,'library_path':str(library_path)}
