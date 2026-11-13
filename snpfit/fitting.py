"""Independent threshold-driven physical-family fitting using NumPy only."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import copy
import json
import time
import threading

import numpy as np

from .circuit import admittance, component_counts, s_from_y, validate, write_spice, y_from_s
from .models import add_loss_candidate, get_fitter, get_model
from .topology import build_candidate, ALGORITHM_VERSION
from .lpriority import select_strategy, preference as l_preference, polish as polish_l


def _freq(network):
    return np.asarray(network.freq, dtype=float)


def _json_write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _write_y(path, freq, y):
    flat = y.transpose(0, 2, 1).reshape(len(freq), -1)
    table = np.empty((len(freq), 1 + 2 * y.shape[-1] ** 2))
    table[:, 0] = freq
    table[:, 1::2], table[:, 2::2] = flat.real, flat.imag
    np.savetxt(path, table, fmt="%.15e")


def _lift_grounded_transformer(y, spec):
    """Lift a grounded-return SE network; no missing 4-port data is invented.

    A*Y*A.T expresses exactly the same selected network as winding voltage
    differences. Collapsing RET_P/RET_S back to REF reproduces its original Y.
    This is a mathematical winding-coordinate transformation only.
    """
    if spec.device != "transformer" or spec.mode != "se":
        raise ValueError("Winding lift only belongs to explicit SE transformer plugins")
    full_roles = ["P", "RET_P", "S", "RET_S"] + list(spec.tap_roles)
    transform = np.zeros((len(full_roles), spec.nports))
    for j, role in enumerate(spec.port_roles):
        transform[full_roles.index(role), j] = 1
        return_name = "RET_P" if role in ("P", "PTAP") else "RET_S"
        transform[full_roles.index(return_name), j] = -1
    return transform[None, :, :] @ y @ transform.T[None, :, :], full_roles


def _collapse_transformer_returns(circuit, spec):
    for e in circuit["elements"]:
        if e["kind"] != "mind":
            e["n1"] = "REF" if e["n1"] in ("RET_P", "RET_S") else e["n1"]
            e["n2"] = "REF" if e["n2"] in ("RET_P", "RET_S") else e["n2"]
    circuit["elements"] = [e for e in circuit["elements"] if e["kind"] == "mind" or e["n1"] != e["n2"]]
    circuit["ports"] = list(spec.port_roles)
    circuit["metadata"]["grounded_return_backend_lift"] = True
    validate(circuit, max_each=200)
    return circuit


def _open_modal_impedance(y, weights):
    right=np.broadcast_to(weights,(len(y),)+weights.shape)
    try:
        solution=np.linalg.solve(y,right)
        bad=np.linalg.cond(y)>=1e12
        if np.any(bad):
            solution[bad]=np.linalg.pinv(y[bad],rcond=1e-12)@weights
    except np.linalg.LinAlgError:
        solution=np.linalg.pinv(y,rcond=1e-12)@weights
    errors=np.linalg.norm(y@solution-weights,axis=(-2,-1))
    if np.any(errors>1e-6*max(np.linalg.norm(weights),1)):
        raise ValueError("Selected open-circuit measurement is undefined at a supplied frequency")
    return weights.T[None]@solution


def _measure_y(y, freq, spec):
    roles = list(spec.port_roles)
    n = len(roles)
    if spec.device == "inductor" and spec.mode == "se":
        # N is shorted to REF. An independent TAP is left open, not grounded.
        retained = [roles.index("P"), roles.index("N")]
        opened = [i for i in range(n) if i not in retained]
        reduced = y[:, retained, :][:, :, retained]
        if opened:
            yro = y[:, retained, :][:, :, opened]
            yoo = y[:, opened, :][:, :, opened]
            yor = y[:, opened, :][:, :, retained]
            reduced = reduced - yro @ np.linalg.solve(yoo, yor)
        z = (1 / reduced[:, 0, 0])[:, None, None]
        branch_names = ["winding"]
    else:
        weights = np.zeros((n, len(spec.winding_pairs)))
        for j, (a, b) in enumerate(spec.winding_pairs):
            if a != "REF":
                weights[roles.index(a), j] = 1
            if b != "REF":
                weights[roles.index(b), j] = -1
        z = _open_modal_impedance(y, weights)
        branch_names = ["winding"] if spec.device == "inductor" else ["primary", "secondary"]
    return _curves_from_z(z, freq, branch_names), z


def _curves_from_z(z, freq, branch_names):
    curves = {}
    for j, name in enumerate(branch_names):
        zij = z[:, j, j]
        resistance, reactance = zij.real, zij.imag
        with np.errstate(divide="ignore", invalid="ignore"):
            q = reactance / resistance
        if not np.all(np.isfinite(q)):
            raise ValueError("Q is undefined for selected winding/termination")
        curves[name] = {
            "l_h": reactance / (2 * np.pi * freq),
            "q": q,
            "r_ohm": resistance,
            "x_ohm": reactance,
        }
    return curves


def _measure_s(s, z0, freq, spec):
    if spec.mode != "dm":
        return _measure_y(y_from_s(s, z0), freq, spec)
    roles = list(spec.port_roles)
    retained = [i for i, role in enumerate(roles) if role not in spec.tap_roles]
    opened = [i for i, role in enumerate(roles) if role in spec.tap_roles]
    reduced = s[:, retained, :][:, :, retained]
    if opened:
        sro = s[:, retained, :][:, :, opened]
        soo = s[:, opened, :][:, :, opened]
        sor = s[:, opened, :][:, :, retained]
        reduced = reduced + sro @ np.linalg.solve(np.eye(len(opened)) - soo, sor)
    retained_roles = [roles[i] for i in retained]
    winding_count = len(spec.winding_pairs)
    transform = np.zeros((len(retained), len(retained)))
    differential_z0 = []
    z0 = np.asarray(z0, dtype=float)
    if z0.ndim == 0:
        z0 = np.full(len(roles), float(z0))
    for j, (a, b) in enumerate(spec.winding_pairs):
        ai, bi = retained_roles.index(a), retained_roles.index(b)
        if not np.isclose(z0[roles.index(a)], z0[roles.index(b)], rtol=1e-12, atol=0):
            raise ValueError("Mixed-mode paired ports need equal real Z0; explicitly renormalize first")
        transform[j, ai], transform[j, bi] = 1 / np.sqrt(2), -1 / np.sqrt(2)
        transform[j + winding_count, ai], transform[j + winding_count, bi] = 1 / np.sqrt(2), 1 / np.sqrt(2)
        differential_z0.append(2 * z0[roles.index(a)])
    smm = transform[None, :, :] @ reduced @ transform.T[None, :, :]
    sdd = smm[:, :winding_count, :winding_count]
    eye = np.eye(winding_count)
    scales = np.sqrt(differential_z0)
    z = np.linalg.solve(eye - sdd, eye + sdd) * scales[:, None] * scales[None, :]
    names = ["winding"] if spec.device == "inductor" else ["primary", "secondary"]
    return _curves_from_z(z, freq, names), z


def measure_network(network, model):
    """Return original sampled winding curves under the selected plugin fixture.

    TAPs and unused windings are open; SE-inductor N is shorted. Transformer
    winding impedances use the other winding open. DM common mode is matched.
    """
    spec = get_model(model) if isinstance(model, str) else model
    curves, z = _measure_s(network.s, network.z0, _freq(network), spec)
    return {"freq_hz": _freq(network).tolist(), "branches": {name: {key: value.tolist() for key, value in c.items()} for name, c in curves.items()}, "mutual_z_real": z[:, 0, 1].real.tolist() if z.shape[-1] == 2 else None, "mutual_z_imag": z[:, 0, 1].imag.tolist() if z.shape[-1] == 2 else None}


def _curve_error(source, fitted, tolerance_pct):
    peak = float(np.max(np.abs(source)))
    floor = max(peak * 1e-6, np.finfo(float).tiny)
    normal = np.abs(source) > floor
    absolute_error = np.abs(fitted - source)
    # The near-zero branch is an explicit absolute test with a recorded unit
    # threshold, not a changed relative denominator that silently passes zeros.
    relative = np.zeros_like(source, dtype=float)
    relative[normal] = absolute_error[normal] / np.abs(source[normal])
    absolute_limit = floor * tolerance_pct / 100
    zero_pass = bool(np.all(absolute_error[~normal] <= absolute_limit))
    ratio = np.where(normal, relative / (tolerance_pct / 100), absolute_error / absolute_limit)
    worst = int(np.argmax(ratio))
    return {
        "max_relative_pct": float(np.max(relative[normal]) * 100) if np.any(normal) else None,
        "worst_index": worst,
        "max_absolute_error": float(np.max(absolute_error)),
        "near_zero_points": int(np.count_nonzero(~normal)),
        "near_zero_reference_floor": floor,
        "near_zero_absolute_limit": absolute_limit,
        "near_zero_pass": zero_pass,
        "pass": bool(np.all(relative[normal] * 100 <= tolerance_pct) and zero_pass),
        "score": float(np.max(ratio)),
    }


def evaluate(circuit, network, model, limits):
    """Independent RLC(K) replay and complete discrete-point acceptance."""
    fitted_s = s_from_y(admittance(circuit, _freq(network)), network.z0)
    return _evaluate_s(fitted_s, network, model, limits, component_counts(circuit))


def evaluate_network(candidate_network, source_network, model, limits):
    """Validate an independently simulated cellview against original samples."""
    if not np.allclose(_freq(candidate_network), _freq(source_network), rtol=1e-10, atol=0):
        raise ValueError("Native replay frequencies must match all original acceptance samples")
    if np.asarray(candidate_network.s).shape != np.asarray(source_network.s).shape:
        raise ValueError("Native replay port count does not match source")
    fitted_s = s_from_y(y_from_s(candidate_network.s, candidate_network.z0), source_network.z0)
    return _evaluate_s(fitted_s, source_network, model, limits)


def _evaluate_s(fitted_s, network, model, limits, counts=None):
    spec = get_model(model) if isinstance(model, str) else model
    l_pct, q_pct = float(limits["l_pct"]), float(limits["q_pct"])
    network_pct = float(limits.get("network_pct", min(l_pct, q_pct)))
    if not (5 <= l_pct <= 20 and 5 <= q_pct <= 20) or network_pct <= 0 or not np.all(np.isfinite([l_pct, q_pct, network_pct])):
        raise ValueError("Each run requires explicit L/Q error percentages in [5, 20]")
    freq = _freq(network)
    source, source_z = _measure_s(network.s, network.z0, freq, spec)
    fitted, fitted_z = _measure_s(fitted_s, network.z0, freq, spec)
    delta = np.abs(fitted_s - network.s)
    flat_index = int(np.argmax(delta))
    fi, row, col = np.unravel_index(flat_index, delta.shape)
    network_error = float(delta[fi, row, col])
    branches, curves = {}, {}
    passed = network_error <= network_pct / 100
    score = network_error / (network_pct / 100)
    for name in source:
        le = _curve_error(source[name]["l_h"], fitted[name]["l_h"], l_pct)
        qe = _curve_error(source[name]["q"], fitted[name]["q"], q_pct)
        le["worst_frequency_hz"] = float(freq[le["worst_index"]])
        qe["worst_frequency_hz"] = float(freq[qe["worst_index"]])
        branches[name] = {"l": le, "q": qe}
        passed = passed and le["pass"] and qe["pass"]
        score = max(score, le["score"], qe["score"])
        curves[name] = {"source_l_h": source[name]["l_h"].tolist(), "fit_l_h": fitted[name]["l_h"].tolist(), "source_q": source[name]["q"].tolist(), "fit_q": fitted[name]["q"].tolist(), "source_r_ohm": source[name]["r_ohm"].tolist(), "fit_r_ohm": fitted[name]["r_ohm"].tolist()}
    result = {
        "passed": bool(passed), "score": float(score),
        "limits": {"l_pct": l_pct, "q_pct": q_pct, "network_pct": network_pct},
        "component_counts": counts,
        "branches": branches,
        "network": {"max_s_absolute_error": network_error, "limit_s_absolute_error": network_pct / 100, "pass": network_error <= network_pct / 100, "worst_frequency_hz": float(freq[fi]), "worst_matrix_entry_1based": [int(row + 1), int(col + 1)]},
        "curve": {"freq_hz": freq.tolist(), "branches": curves},
        "measurement": {"mode": spec.mode, "tap_termination": "open", "unused_winding_termination": "open", "common_mode": "matched" if spec.mode == "dm" else "specified_single_ended_return", "sample_basis": "all supplied samples; no interpolation substituted for measurements"},
    }
    if source_z.shape[-1] == 2:
        mutual = {}
        for a, b in ((0, 1), (1, 0)):
            error = _curve_error(source_z[:, a, b], fitted_z[:, a, b], min(l_pct, q_pct))
            error["worst_frequency_hz"] = float(freq[error["worst_index"]])
            mutual["z%d%d" % (a + 1, b + 1)] = error
            result["passed"] = bool(result["passed"] and error["pass"])
            result["score"] = max(result["score"], error["score"])
        result["mutual_impedance"] = {"limit_pct": min(l_pct, q_pct), "complex_relative_errors": mutual, "near_zero_basis": "recorded absolute error threshold in ohms"}
        result["curve"]["source_mutual_z_real"] = source_z[:, 0, 1].real.tolist()
        result["curve"]["source_mutual_z_imag"] = source_z[:, 0, 1].imag.tolist()
        result["curve"]["fit_mutual_z_real"] = fitted_z[:, 0, 1].real.tolist()
        result["curve"]["fit_mutual_z_imag"] = fitted_z[:, 0, 1].imag.tolist()
    return result


@dataclass
class FitResult:
    model_id: str
    passed: bool
    circuit: dict | None = None
    metrics: dict | None = None
    attempts: list = field(default_factory=list)
    status: str = "not_met"
    backend: str = "standalone_numpy_physical_ladder"
    strategy: str = "balanced"

    def as_dict(self):
        return {"model_id": self.model_id, "passed": self.passed, "circuit": self.circuit, "metrics": self.metrics, "attempts": self.attempts, "status": self.status, "backend": self.backend, "strategy": self.strategy}


def fit_dedicated(network, model_id, limits, workdir, timeout_s=180, cancel=None, progress=None):
    """Fit this explicitly selected family's bounded passive candidate ladder.

    Initial values come only from this request's selected SNP samples. No
    executable, third-party script, cached circuit or fitted netlist is read.
    """
    from .optimizer import optimize
    spec = get_model(model_id)
    if np.asarray(network.s).shape[1:] != (spec.nports, spec.nports):
        raise ValueError("Selected model requires %d signal ports in order %s" % (spec.nports, ", ".join(spec.port_roles)))
    for key in ("l_pct", "q_pct"):
        if key not in limits or not np.isfinite(float(limits[key])) or not 5 <= float(limits[key]) <= 20:
            raise ValueError("Every run requires explicit L/Q error percentages in [5, 20]")
    if timeout_s <= 0:
        raise ValueError("Fitting time limit must be positive")
    freq = _freq(network)
    if len(freq) < 3 or not np.all(np.isfinite(freq)) or np.any(freq <= 0) or np.any(np.diff(freq) <= 0):
        raise ValueError("At least three strictly increasing positive frequency samples are required")
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "result.json").exists():
        raise FileExistsError("Use a fresh fitting directory; an existing result will not be overwritten")
    input_path = root / "input.y"
    _write_y(input_path, freq, y_from_s(network.s, network.z0))
    input_sha = hashlib.sha256(input_path.read_bytes()).hexdigest()
    result = FitResult(model_id, False, strategy=select_strategy(limits["l_pct"], limits["q_pct"]))
    started = time.monotonic()
    previous = None
    for candidate_index, candidate in enumerate(spec.candidates):
        if cancel is not None and cancel.is_set():
            result.status = "cancelled"; break
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            result.status = "timeout"; break
        attempt_dir = root / candidate.id
        attempt_dir.mkdir(exist_ok=False)
        record = {"candidate": candidate.id, "label": candidate.label,
                  "input_y_sha256": input_sha, "algorithm_version": ALGORITHM_VERSION,
                  "strategy": result.strategy}
        result.attempts.append(record)
        if progress:
            progress("独立拟合：%s；剩余 %.0f 秒" % (candidate.label, remaining))
        try:
            circuit = build_candidate(network, spec, candidate)
            circuit["metadata"].update(input_y_sha256=input_sha, original_selected_y_sha256=input_sha)
            # Preserve already-fitted electrostatics when adding distributed
            # winding sections; this remains an independent same-request seed.
            if previous is not None:
                old = {e["name"]: e for e in previous["elements"]}
                for e in circuit["elements"]:
                    prior = old.get(e["name"])
                    if prior and e["kind"] in ("cap", "res") and all(e.get(k) == prior.get(k) for k in ("kind", "n1", "n2")):
                        if e["kind"] == "cap" or e["name"].startswith("rsub"):
                            e["value"] = prior["value"]
            counts = validate(circuit)
            initial_metrics = evaluate(circuit, network, spec, limits)
            record["initial_score"] = initial_metrics["score"]
            _json_write(attempt_dir / "initial_circuit.json", circuit)
            # Fixed finite candidate ladder. Wall budget cannot grow with the
            # requested accuracy; a tighter request may exhaust this ladder.
            budget = min(remaining, (8, 18, 36, 50, 60)[min(candidate_index,4)])
            if initial_metrics["passed"]:
                metrics = initial_metrics
                refinement = {"algorithm": "source_physical_initialization", "elapsed_s": 0}
            else:
                circuit, metrics, refinement = optimize(circuit, network, spec, limits, budget, cancel=cancel, progress=progress)
            # Keep the standard path as the warm start for later candidates.
            # A post-refinement must not disturb a formerly feasible ladder.
            next_previous = circuit
            remaining_after_fit = timeout_s - (time.monotonic() - started)
            if result.strategy == "l_priority":
                if remaining_after_fit > 0 and not (cancel is not None and cancel.is_set()):
                    polish_budget = min(remaining_after_fit, 4.0 if candidate_index == len(spec.candidates)-1 else 2.0)
                    polished, polished_metrics, receipt = polish_l(
                        circuit, network, spec, limits, polish_budget, cancel=cancel, progress=progress)
                    record["l_priority_refinement"] = receipt
                    if l_preference(polished_metrics) < l_preference(metrics):
                        circuit, metrics = polished, polished_metrics
                else:
                    record["l_priority_refinement"] = {"state": "cancelled" if cancel is not None and cancel.is_set() else "budget_exhausted", "added_elements": 0}
            circuit.setdefault("metadata", {})["strategy"] = result.strategy
            record.update(state="passed" if metrics["passed"] else "not_met", counts=counts,
                          total_elements=sum(counts.values()),score=metrics["score"],metrics=metrics,
                          refinement=refinement)
            _json_write(attempt_dir / "circuit.json", circuit)
            _json_write(attempt_dir / "metrics.json", metrics)
            write_spice(circuit, attempt_dir / "accepted_parameters.sp")
            better = (result.metrics is None or
                      (l_preference(metrics) < l_preference(result.metrics) if result.strategy == "l_priority"
                       else metrics["score"] < result.metrics["score"]))
            if better:
                result.circuit, result.metrics = circuit, metrics
            previous = next_previous
            if metrics["passed"]:
                result.passed = True; result.circuit, result.metrics = circuit, metrics
                result.status = "passed"; break
        except (ValueError, OSError, np.linalg.LinAlgError) as error:
            record.update(state="invalid_candidate", error=str(error))
        finally:
            _json_write(root / "result.json", result.as_dict())
    if cancel is not None and cancel.is_set():
        result.passed=False; result.status="cancelled"
    _json_write(root / "result.json", result.as_dict())
    return result


_CUSTOM_CONTEXT = threading.local()


def fit(network, model_id, limits, workdir, timeout_s=180, cancel=None, progress=None):
    """Dispatch an explicit model plugin; every result keeps common acceptance.

    A custom fitter has the same call signature as this function and returns a
    FitResult. To reuse the provided backend it calls fit_dedicated, not fit.
    """
    spec = get_model(model_id)
    for key in ("l_pct", "q_pct"):
        if key not in limits or not np.isfinite(float(limits[key])) or not 5 <= float(limits[key]) <= 20:
            raise ValueError("Every run requires explicit L/Q error percentages in [5, 20]")
    if np.asarray(network.s).shape[1:] != (spec.nports, spec.nports):
        raise ValueError("Input port count does not match explicitly selected model")
    fitter = get_fitter(model_id)
    if fitter is None:
        return fit_dedicated(network, model_id, limits, workdir, timeout_s, cancel, progress)
    active = getattr(_CUSTOM_CONTEXT, "active", set())
    if model_id in active:
        raise ValueError("Recursive model fitter; reuse fit_dedicated explicitly")
    _CUSTOM_CONTEXT.active = active | {model_id}
    custom_started = time.monotonic()
    try:
        result = fitter(network, model_id, limits, workdir, timeout_s=timeout_s, cancel=cancel, progress=progress)
    finally:
        _CUSTOM_CONTEXT.active = active
    if not isinstance(result, FitResult):
        raise TypeError("Custom model fitter must return FitResult")
    result.strategy = select_strategy(limits["l_pct"], limits["q_pct"])
    if result.circuit is not None:
        validate(result.circuit, max_each=20)
        result.metrics = evaluate(result.circuit, network, spec, limits)
        if result.strategy == "l_priority":
            remaining = timeout_s - (time.monotonic() - custom_started)
            receipt = {"state": "cancelled" if cancel is not None and cancel.is_set() else "budget_exhausted", "added_elements": 0}
            if remaining > 0 and not (cancel is not None and cancel.is_set()):
                cc, mm, receipt = polish_l(result.circuit, network, spec, limits,
                                           min(remaining, 4.0), cancel=cancel, progress=progress)
                if l_preference(mm) < l_preference(result.metrics):
                    result.circuit, result.metrics = cc, mm
            result.attempts.append({"candidate": "custom_fitter_l_priority", "strategy": result.strategy,
                                    "l_priority_refinement": receipt})
        result.circuit.setdefault("metadata", {})["strategy"] = result.strategy
        result.passed = result.metrics["passed"]
        result.status = "passed" if result.passed else "not_met"
    else:
        result.passed = False
    if cancel is not None and cancel.is_set():
        result.passed = False
        result.status = "cancelled"
    return result
