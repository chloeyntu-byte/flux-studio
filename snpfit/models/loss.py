"""Finite, passive LC bypass refinement shared by explicit winding plugins."""
import copy
import numpy as np

from ..circuit import validate


def add_winding_loss_cell(circuit, network, spec, metrics, cell_index):
    if cell_index not in (1, 2):
        return None
    freq = np.asarray(network.freq)
    choices = []
    previous = circuit.get("metadata", {}).get("loss_cells", [])
    for branch_index, (name, curve) in enumerate(metrics["curve"]["branches"].items()):
        original = np.asarray(curve["source_r_ohm"])
        fitted = np.asarray(curve["fit_r_ohm"])
        excess = fitted - original
        for old in previous:
            if old["branch"] == name:
                excess[np.abs(np.log(freq / old["frequency_hz"])) < .15] = -np.inf
        i = int(np.argmax(excess))
        if not np.isfinite(excess[i]) or excess[i] <= .01 * max(abs(fitted[i]), 1e-9):
            continue
        endpoint = spec.winding_pairs[branch_index][0]
        series_resistors = [e for e in circuit["elements"] if e["kind"] == "res" and endpoint in (e["n1"], e["n2"]) and e["value"] < 100 * max(abs(fitted[i]), 1e-9)]
        if not series_resistors:
            continue
        resistor = min(series_resistors, key=lambda e: e["value"])
        choices.append((float(excess[i]), name, i, resistor))
    if not choices:
        return None
    delta, branch, i, original_resistor = max(choices, key=lambda item: item[0])
    out = copy.deepcopy(circuit)
    resistor = next(e for e in out["elements"] if e["name"] == original_resistor["name"])
    fraction = min(.7, max(.05, delta / resistor["value"]))
    bypass_r = resistor["value"] * fraction
    old_b = resistor["n2"]
    mid, junction = "fit_loss_%d_mid" % cell_index, "fit_loss_%d_lc" % cell_index
    resistor["n2"] = mid
    resistor["value"] *= 1 - fraction
    # Width starts at neighboring sample spacing; optimization refines both L/C.
    left = freq[max(0, i - 1)]
    right = freq[min(len(freq) - 1, i + 1)]
    width_hz = max((right - left) / 2, freq[i] * .03)
    resonant_l = bypass_r / (2 * np.pi * width_hz)
    resonant_c = 1 / ((2 * np.pi * freq[i]) ** 2 * resonant_l)
    out["elements"] += [
        {"kind": "res", "name": "rfit_loss_%d" % cell_index, "n1": mid, "n2": old_b, "value": bypass_r},
        {"kind": "ind", "name": "lfit_loss_%d" % cell_index, "n1": mid, "n2": junction, "value": resonant_l},
        {"kind": "cap", "name": "cfit_loss_%d" % cell_index, "n1": junction, "n2": old_b, "value": resonant_c},
    ]
    out["metadata"].setdefault("loss_cells", []).append({"branch": branch, "frequency_hz": float(freq[i]), "initial_width_hz": float(width_hz)})
    validate(out)
    return out
