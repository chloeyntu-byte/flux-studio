"""Bounded, SNP-only physical initialization for the two-stage prototype.

A quick result is an editable numerical starting circuit, never a verified
model.  OA creation, external solver startup and final SRF/k acceptance are
outside this function.  The returned deadline flag records the measured wall
budget; no deadline or accuracy success is inferred from a generated file.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from .circuit import component_counts, validate, write_spice
from .models import get_model
from .network import Network
from .optimizer import optimize
from .stable_network import StableSensitivityNetwork, evaluate_stable
from .topology import build_candidate

QUICK_ALGORITHM = 'snp_only_physical_seed_and_bounded_short_fit_v1'


def topology_signature(circuit):
    """Signature of terminals, reference and all connections, excluding values."""
    graph = {
        'ports': circuit['ports'],
        'reference': circuit.get('reference', 'REF'),
        'elements': [{k: e[k] for k in ('kind', 'name', 'n1', 'n2', 'ind1', 'ind2', 'fit_group') if k in e}
                     for e in circuit['elements']],
    }
    return hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _sample_network(network, maximum=24):
    """Bound fitting cost using exact input samples; never interpolate data."""
    n = len(network.freq)
    if n <= maximum:
        indices = np.arange(n)
    else:
        fixed = {0, 1, 2, n-3, n-2, n-1}
        # Frequency-nearest observed points approximate a log grid, not new data.
        goals = np.geomspace(network.freq[0], network.freq[-1], maximum-len(fixed))
        for goal in goals:
            j = int(np.searchsorted(network.freq, goal))
            choices = [min(j, n-1), max(0, j-1)]
            fixed.add(min(choices, key=lambda k: abs(math.log(network.freq[k]/goal))))
        indices = np.asarray(sorted(fixed), int)
    sampled = Network(network.freq[indices], network.s[indices], network.z0,
                      list(network.port_names), dict(network.metadata))
    return sampled, indices


def quick_initialize(network, model_id, limits, workdir=None, timeout_s=5.0, *,
                     candidate_id=None, component_limit=20, cancel=None, progress=None):
    """Create a passive starting circuit within a measured maximum 5s budget.

    ``network`` is already port-mapped and band-selected by the caller.  Inputs
    are the requested model, original SNP samples and limits only.  Automatic
    selection keeps substrate/skin/capacitive degrees of freedom for later SRF
    tuning; an explicit ``candidate_id`` fixes a built-in topology immediately.

    Loose20/20 L/Q/S objectives govern only the short initial fit.  Exact final
    metrics are evaluated against ``limits`` on every supplied positive sample.
    ``status`` is always INITIAL_UNVERIFIED and ``passed`` is always False: final
    tuning and independent acceptance remain separate responsibilities.
    """
    started = time.monotonic()
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= 5:
        raise ValueError('Quick initialization requires a wall budget in (0,5] seconds')
    if component_limit not in (20, 30):
        raise ValueError('Per-kind component limit must be 20 or 30')
    spec = get_model(model_id)
    if network.nports != spec.num_ports:
        raise ValueError('Selected model and mapped SNP port counts differ')
    strict_limits = dict(limits)
    # The final evaluator remains the single owner of user-limit validation.
    if not all(k in strict_limits for k in ('l_pct', 'q_pct')):
        raise ValueError('Explicit L and Q precision limits are required')
    lp, qp = float(strict_limits['l_pct']), float(strict_limits['q_pct'])
    if not (5 <= lp <= 20 and 5 <= qp <= 20) or not np.all(np.isfinite([lp, qp])):
        raise ValueError('Each final L/Q requirement must be in [5,20] percent')
    netpct = float(strict_limits.get('network_pct', min(lp, qp)))
    if not math.isfinite(netpct) or netpct <= 0:
        raise ValueError('Network percentage must be positive and finite')
    strict_limits['network_pct'] = netpct
    positive = np.flatnonzero(network.freq > 0)
    excluded_dc_samples = int(len(network.freq)-len(positive))
    if len(positive) < 2:
        raise ValueError('At least two positive original SNP frequencies are required')
    if len(positive) != len(network.freq):
        network = Network(network.freq[positive], network.s[positive], network.z0,
                          list(network.port_names), dict(network.metadata))
    out = Path(workdir) if workdir is not None else None
    output_names = ('initial_circuit.json', 'model.sp', 'quick-result.json')
    if out is not None:
        if any((out/name).exists() for name in output_names):
            raise FileExistsError('Refuse to overwrite an existing quick initialization')
        out.mkdir(parents=True, exist_ok=True)
    if candidate_id is not None:
        candidates = [c for c in spec.candidates if c.id == candidate_id]
        if not candidates:
            raise ValueError('Topology is not registered for this selected model')
    else:
        candidates = [c for c in spec.candidates if c.parasitic == 2 and c.skin]
        if not candidates:
            candidates = list(spec.candidates)
    loose_limits = {'l_pct': 20.0, 'q_pct': 20.0, 'network_pct': 20.0}
    sampled, sample_indices = _sample_network(network)
    deadline = started + timeout_s
    # Reserve normal-sized source replay/output time.  Low-level NumPy calls
    # cannot be preempted; the outer process can enforce a harder service SLA.
    fitting_deadline = deadline - min(.40, timeout_s * .12)
    attempts, seeds = [], []

    def cancelled():
        return bool(cancel is not None and cancel.is_set())

    def rank(metrics, circuit):
        return (float(metrics['score']), sum(component_counts(circuit).values()))

    # Construct the first legal passive fallback before optional optimization.
    for candidate in candidates:
        if seeds and (cancelled() or time.monotonic() >= fitting_deadline):
            break
        begin = time.monotonic()
        try:
            circuit = build_candidate(sampled, spec, candidate, max_each=component_limit)
            counts = validate(circuit, max_each=component_limit)
            metrics = evaluate_stable(circuit, sampled, spec, loose_limits)
            seed = {'candidate': candidate.id, 'circuit': circuit, 'metrics': metrics,
                    'counts': counts}
            seeds.append(seed)
            attempts.append({'candidate': candidate.id, 'state': 'physical_seed',
                             'relaxed_sample_score': float(metrics['score']), 'counts': counts,
                             'elapsed_s': time.monotonic()-begin})
        except (ValueError, np.linalg.LinAlgError, FloatingPointError) as error:
            attempts.append({'candidate': candidate.id, 'state': 'rejected',
                             'reason': str(error), 'elapsed_s': time.monotonic()-begin})
    if not seeds:
        raise ValueError('No legal physical starting circuit could be initialized from this SNP')
    seeds.sort(key=lambda x: rank(x['metrics'], x['circuit']))
    best = seeds[0]
    # Two finite starting topologies at most.  The selected topology is then
    # frozen by signature; subsequent tuning may only alter device values.
    polish_seeds = seeds[:2]
    for index, seed in enumerate(polish_seeds):
        remaining = fitting_deadline-time.monotonic()
        if cancelled() or remaining <= .05:
            break
        budget = remaining/(len(polish_seeds)-index)
        if seed['metrics']['passed']:
            # A loose starting fit needs no expensive final-quality polishing.
            continue
        if progress:
            progress('快速初值：%s；剩余 %.2f 秒' % (seed['candidate'], remaining))
        begin = time.monotonic()
        circuit, metrics, receipt = optimize(
            copy.deepcopy(seed['circuit']), sampled, spec, loose_limits, budget,
            cancel=cancel, progress=progress, network_class=StableSensitivityNetwork,
            evaluation_function=evaluate_stable)
        if topology_signature(circuit) != topology_signature(seed['circuit']):
            raise RuntimeError('Short fitting unexpectedly changed physical topology')
        validate(circuit, max_each=component_limit)
        attempts.append({'candidate': seed['candidate'], 'state': 'short_parameter_fit',
                         'relaxed_sample_score': float(metrics['score']),
                         'elapsed_s': time.monotonic()-begin, 'receipt': receipt})
        if rank(metrics, circuit) < rank(best['metrics'], best['circuit']):
            best = {'candidate': seed['candidate'], 'circuit': circuit, 'metrics': metrics,
                    'counts': component_counts(circuit)}
    circuit = copy.deepcopy(best['circuit'])
    circuit.setdefault('metadata', {}).update(
        initialization_stage='quick_initial', quick_algorithm=QUICK_ALGORITHM,
        status='INITIAL_UNVERIFIED', topology_fixed_for_tuning=True,
        quick_requested_limits=strict_limits)
    # No sampled-score substitution: final preview reports every supplied point.
    strict = evaluate_stable(circuit, network, spec, strict_limits)
    relaxed = evaluate_stable(circuit, network, spec, loose_limits)
    result = {
        'algorithm': QUICK_ALGORITHM, 'model_id': spec.id, 'status': 'INITIAL_UNVERIFIED',
        'passed': False, 'native_verified': False, 'circuit': circuit, 'metrics': strict,
        'numerically_within_requested_lq': bool(strict['passed']),
        'relaxed_metrics': relaxed, 'initial_relaxed_limits': loose_limits,
        'candidate_id': best['candidate'], 'candidate_selection': 'explicit' if candidate_id else 'automatic_finite_family',
        'component_counts': component_counts(circuit), 'component_limit': component_limit,
        'topology_signature': topology_signature(circuit), 'topology_fixed_for_tuning': True,
        'requested_limits': strict_limits, 'attempts': attempts,
        'sampling': {'input_positive_samples': len(network.freq), 'fit_samples': len(sampled.freq),
                     'exact_positive_input_indices': sample_indices.tolist(),
                     'interpolation_used': False, 'final_preview_uses_all_input_positive_points': True,
                     'excluded_DC_samples': excluded_dc_samples},
        'source_policy': 'selected SNP samples only; no previous fit, ModelGen, external tools or network',
        'deadline_seconds': timeout_s,
        'cancelled': cancelled(), 'artifacts_written': out is not None,
        'verification_note': 'Editable initial circuit, not final L/Q/k/SRF or native acceptance',
    }
    if out is not None:
        (out/'initial_circuit.json').write_text(json.dumps(circuit, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        write_spice(circuit, out/'model.sp')
    result['elapsed_s'] = time.monotonic()-started
    result['budget_met'] = result['elapsed_s'] <= timeout_s
    if out is not None:
        (out/'quick-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        result['elapsed_s'] = time.monotonic()-started
        result['budget_met'] = result['elapsed_s'] <= timeout_s
        # Last receipt update is intentionally tiny and never runs optimization.
        (out/'quick-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return result
