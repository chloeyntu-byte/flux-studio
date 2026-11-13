"""Flat, passive RLC(K) circuit interchange and independent nodal evaluation."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import numpy as np

KINDS = ("res", "cap", "ind", "mind")


def component_counts(circuit):
    counts = Counter(e["kind"] for e in circuit["elements"])
    return {kind: int(counts.get(kind, 0)) for kind in KINDS}


def validate(circuit, max_each=None):
    if max_each is None:
        max_each = circuit.get("metadata", {}).get("component_limit", 20)
        if max_each not in (20, 30):
            raise ValueError("This model requires a finite per-kind limit of 20 or 30")
    ports = circuit["ports"]
    reference = circuit.get("reference", "REF")
    if not ports or len(set(ports)) != len(ports) or reference in ports:
        raise ValueError("Invalid circuit ports/reference")
    elements = circuit["elements"]
    names = [e["name"] for e in elements]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate component names")
    if any(count > max_each for count in component_counts(circuit).values()):
        raise ValueError("Component count exceeds per-kind limit %d" % max_each)
    inductors = {e["name"] for e in elements if e["kind"] == "ind"}
    for e in elements:
        if e["kind"] not in KINDS or not np.isfinite(e["value"]):
            raise ValueError("Unsupported/nonfinite circuit element")
        if e["kind"] == "mind":
            if abs(e["value"]) >= 1 or e["ind1"] not in inductors or e["ind2"] not in inductors or e["ind1"] == e["ind2"]:
                raise ValueError("Invalid mutual inductance")
        elif e["value"] <= 0 or e["n1"] == e["n2"]:
            raise ValueError("Nonpositive or shorted RLC component")
    # Pairwise |k| < 1 alone is insufficient for multiple coupled inductors.
    if inductors:
        ls = [e for e in elements if e["kind"] == "ind"]
        ix = {e["name"]: i for i, e in enumerate(ls)}
        correlation = np.eye(len(ls))
        for e in elements:
            if e["kind"] == "mind":
                i, j = ix[e["ind1"]], ix[e["ind2"]]
                correlation[i, j] = correlation[j, i] = e["value"]
        if np.linalg.eigvalsh(correlation)[0] <= 1e-10:
            raise ValueError("Coupled-inductor matrix is not positive definite")
    return component_counts(circuit)


def y_from_s(s, z0):
    s = np.asarray(s, dtype=complex)
    z0 = np.asarray(z0, dtype=float)
    if z0.ndim == 0:
        z0 = np.full(s.shape[-1], float(z0))
    if z0.shape != (s.shape[-1],) or np.any(z0 <= 0):
        raise ValueError("Expected positive real reference impedance per port")
    eye = np.eye(s.shape[-1])
    a = 1 / np.sqrt(z0)
    return np.linalg.solve(eye + s, eye - s) * a[:, None] * a[None, :]


def s_from_y(y, z0):
    y = np.asarray(y, dtype=complex)
    z0 = np.asarray(z0, dtype=float)
    if z0.ndim == 0:
        z0 = np.full(y.shape[-1], float(z0))
    eye = np.eye(y.shape[-1])
    a = np.sqrt(z0)
    normalized = y * a[:, None] * a[None, :]
    return np.linalg.solve(eye + normalized, eye - normalized)


def admittance(circuit, frequencies):
    """Evaluate port Y by eliminating internal nodes; no artificial gmin."""
    validate(circuit)
    frequencies = np.asarray(frequencies, dtype=float)
    if np.any(frequencies <= 0) or not np.all(np.isfinite(frequencies)):
        raise ValueError("RLC(K) evaluation requires positive finite frequencies")
    ports, reference = list(circuit["ports"]), circuit.get("reference", "REF")
    nodes = set(ports)
    for e in circuit["elements"]:
        if e["kind"] != "mind":
            nodes.update((e["n1"], e["n2"]))
    nodes.discard(reference)
    ordered = ports + sorted(nodes - set(ports))
    index = {name: i for i, name in enumerate(ordered)}
    n, p = len(ordered), len(ports)

    def incidence(e):
        a = np.zeros(n)
        if e["n1"] != reference:
            a[index[e["n1"]]] += 1
        if e["n2"] != reference:
            a[index[e["n2"]]] -= 1
        return a

    g, c = np.zeros((n, n)), np.zeros((n, n))
    ls = [e for e in circuit["elements"] if e["kind"] == "ind"]
    for e in circuit["elements"]:
        if e["kind"] in ("res", "cap"):
            a = incidence(e)
            if e["kind"] == "res":
                g += np.outer(a, a) / e["value"]
            else:
                c += np.outer(a, a) * e["value"]
    inductive = np.zeros((n, n))
    if ls:
        b = np.stack([incidence(e) for e in ls], axis=1)
        lmat = np.diag([e["value"] for e in ls])
        lix = {e["name"]: i for i, e in enumerate(ls)}
        for e in circuit["elements"]:
            if e["kind"] == "mind":
                i, j = lix[e["ind1"]], lix[e["ind2"]]
                lmat[i, j] = lmat[j, i] = e["value"] * np.sqrt(ls[i]["value"] * ls[j]["value"])
        inductive = b @ np.linalg.solve(lmat, b.T)
    w = 2 * np.pi * frequencies[:, None, None]
    yn = g[None, :, :] + 1j * w * c[None, :, :] + inductive[None, :, :] / (1j * w)
    if n == p:
        return yn
    return yn[:, :p, :p] - yn[:, :p, p:] @ np.linalg.solve(yn[:, p:, p:], yn[:, p:, :p])


def simulate(circuit, frequencies, z0):
    return s_from_y(admittance(circuit, frequencies), z0)


def spice_number(value):
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?)([a-zA-Z]*)", value.strip())
    if not match:
        raise ValueError("Unsupported SPICE number: %s" % value)
    number, suffix = match.groups()
    multipliers = {"": 1, "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
    suffix = suffix.lower()
    if suffix not in multipliers:
        raise ValueError("Unsupported SPICE scale: %s" % suffix)
    return float(number.replace("D", "E").replace("d", "e")) * multipliers[suffix]


def parse_spice(path, port_names=None, metadata=None, max_each=20):
    """Parse a numeric, flat SPICE RLC/K subcircuit, rejecting other syntax."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    header = None
    elements = []
    done = False
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("*"):
            continue
        fields = raw.split()
        first = fields[0].lower()
        if first == ".subckt":
            if header is not None:
                raise ValueError("Expected one flat subcircuit")
            header = fields[1:]
            continue
        if first == ".ends":
            done = True
            continue
        if header is None or done:
            raise ValueError("Unexpected content outside subcircuit")
        kinds = {"r": "res", "c": "cap", "l": "ind", "k": "mind"}
        kind = kinds.get(first[0])
        if kind is None or len(fields) != 4:
            raise ValueError("Unsupported nonnumeric/non-RLC SPICE line: %s" % raw)
        e = {"name": first, "kind": kind, "value": spice_number(fields[3])}
        if kind == "mind":
            e.update(ind1=fields[1].lower(), ind2=fields[2].lower())
        else:
            e.update(n1=fields[1], n2=fields[2])
        elements.append(e)
    if not header or not done or len(header) < 3:
        raise ValueError("Incomplete SPICE subcircuit")
    actual_ports = header[1:-1]
    port_names = list(actual_ports if port_names is None else port_names)
    if len(port_names) != len(actual_ports):
        raise ValueError("SPICE port count does not match selected model")
    mapping = dict(zip(actual_ports, port_names))
    mapping[header[-1]] = "REF"
    for e in elements:
        if e["kind"] != "mind":
            e["n1"] = mapping.get(e["n1"], e["n1"])
            e["n2"] = mapping.get(e["n2"], e["n2"])
    circuit = {"name": header[0], "ports": port_names, "reference": "REF", "elements": elements, "metadata": dict(metadata or {})}
    validate(circuit, max_each=max_each)
    return circuit


def write_spice(circuit, path):
    validate(circuit)
    lines = ["* SNPFit flat passive model", ".subckt %s %s %s" % (circuit["name"], " ".join(circuit["ports"]), circuit.get("reference", "REF"))]
    for e in circuit["elements"]:
        if e["kind"] == "mind":
            lines.append("%s %s %s %.15e" % (e["name"], e["ind1"], e["ind2"], e["value"]))
        else:
            lines.append("%s %s %s %.15e" % (e["name"], e["n1"], e["n2"], e["value"]))
    lines.append(".ends %s" % circuit["name"])
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
