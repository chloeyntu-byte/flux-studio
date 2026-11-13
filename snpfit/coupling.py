"""Signed effective two-winding coupling measured from the selected SNP fixture.

This frequency-dependent response metric is distinct from an individual mind
coefficient. It supplements, and never replaces, complete complex mutual-Z/S
acceptance. Its evaluation domain is frozen from the reference SNP alone.
"""
from __future__ import annotations

import hashlib
import numpy as np


def _spec(model):
    from .models import get_model
    return get_model(model) if isinstance(model, str) else model


def _fingerprint(network, spec):
    digest = hashlib.sha256(spec.id.encode('utf-8'))
    for value, dtype in ((network.freq, '<f8'), (network.z0, '<f8'),
                         (network.s, '<c16')):
        digest.update(np.ascontiguousarray(value, dtype=dtype).tobytes())
    return digest.hexdigest()


def coupling_values(s, z0, freq, model):
    """Return NumPy k/X1/X2 arrays using existing SE/DM/open-tap semantics.

    Invalid candidate self-reactances produce NaN k; callers must test the
    frozen source domain, never construct a candidate-specific valid mask.
    """
    from .fitting import _measure_s
    spec = _spec(model)
    if spec.device != 'transformer':
        raise ValueError('Effective coupling requires a two-winding transformer')
    _, z = _measure_s(np.asarray(s), z0, np.asarray(freq, float), spec)
    if z.shape[-2:] != (2, 2):
        raise ValueError('Selected fixture did not produce a two-winding Z')
    x1, x2, mutual = z[:, 0, 0].imag, z[:, 1, 1].imag, z[:, 0, 1].imag
    good = np.isfinite(x1) & np.isfinite(x2) & np.isfinite(mutual) & (x1 > 0) & (x2 > 0)
    k = np.full(len(x1), np.nan)
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        k[good] = mutual[good] / (np.sqrt(x1[good]) * np.sqrt(x2[good]))
    return {'k': k, 'x1': x1, 'x2': x2, 'mutual_x': mutual}


def k_reference(network, model, srf_reference=None):
    """Freeze the reference-only effective-k domain and zero-error convention.

    Defaults are explicit: both X > 1e-4 of their reference peak magnitudes;
    when first-SRF brackets are available, f < .8 times their earliest lower
    endpoint. Without SRF reference, only source positivity/scale is required.
    Source k near zero remains in the domain and uses a recorded absolute test.
    """
    spec = _spec(model)
    freq = np.asarray(network.freq, float)
    out = {
        'applicable': spec.device == 'transformer', 'resolved': False,
        'status': 'not_applicable' if spec.device != 'transformer' else 'unresolved',
        'model_id': spec.id, 'freq_hz': freq.tolist(), 'valid_indices': [],
        'valid_mask': [False] * len(freq), 'source_k': [None] * len(freq),
        'absolute_reference_floor': 1e-12,
        'source_fingerprint': _fingerprint(network, spec),
        'definition': 'signed Im(Z12)/sqrt(Im(Z11)*Im(Z22)) under selected fixture',
        'metric_is_internal_mind_coefficient': False,
        'source_reactance_peak_fraction': 1e-4,
        'first_srf_margin_factor': .8 if srf_reference is not None else None,
        'first_srf_lower_hz': None,
        'measurement': {'mode': spec.mode,
                        'tap_termination': 'open',
                        'other_winding_termination': 'open',
                        'common_mode': 'matched' if spec.mode == 'dm' else 'specified_single_ended_return'},
        'near_zero_rule': 'source |k| <= absolute_reference_floor uses absolute error <= floor*k_pct/100',
        'candidate_domain_rule': 'all frozen source valid points must remain finite with both candidate X positive',
    }
    if not out['applicable']:
        out['resolved'] = True
        out['reason'] = 'Single winding has no two-winding coupling metric'
        return out
    try:
        values = coupling_values(network.s, network.z0, freq, spec)
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as error:
        out['reason'] = 'Reference fixture undefined: ' + str(error)
        return out
    x1, x2, kvals = values['x1'], values['x2'], values['k']
    finite1, finite2 = x1[np.isfinite(x1)], x2[np.isfinite(x2)]
    peak1 = float(np.max(np.abs(finite1))) if len(finite1) else 0.
    peak2 = float(np.max(np.abs(finite2))) if len(finite2) else 0.
    threshold1, threshold2 = max(peak1 * 1e-4, np.finfo(float).tiny), max(peak2 * 1e-4, np.finfo(float).tiny)
    out['reference_x_threshold_ohm'] = [threshold1, threshold2]
    mask = np.isfinite(kvals) & (x1 > threshold1) & (x2 > threshold2) & (freq > 0)
    if srf_reference is not None:
        branches = srf_reference.get('branches', {})
        brackets = [item.get('bracket_hz') for item in branches.values()]
        if not branches or any(not b or len(b) != 2 or not np.all(np.isfinite(b)) or b[0] <= 0 for b in brackets):
            out['reason'] = 'Given SRF reference does not resolve every first-winding bracket'
            return out
        lower = min(float(b[0]) for b in brackets)
        out['first_srf_lower_hz'] = lower
        mask &= freq < .8 * lower
    indices = np.flatnonzero(mask)
    out['valid_indices'] = indices.tolist()
    out['valid_mask'] = mask.tolist()
    out['valid_count'] = int(len(indices))
    if not len(indices):
        out['reason'] = 'No reference sample has both winding reactances safely inductive in the declared domain'
        return out
    peak = float(np.max(np.abs(kvals[indices])))
    floor = max(peak * 1e-6, 1e-12)
    out['absolute_reference_floor'] = floor
    out['near_zero_reference_count'] = int(np.count_nonzero(np.abs(kvals[indices]) <= floor))
    out['source_k'] = [float(kvals[i]) if mask[i] else None for i in range(len(freq))]
    out.update(resolved=True, status='resolved')
    return out


def evaluate_coupling(circuit, network, model, k_pct, reference=None):
    """Evaluate candidate effective k on an immutable source-selected domain."""
    from .stable_network import simulate_stable
    spec = _spec(model)
    tolerance = float(k_pct)
    if not np.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('k percentage error must be finite and positive')
    reference = k_reference(network, spec) if reference is None else reference
    if reference.get('source_fingerprint') != _fingerprint(network, spec):
        raise ValueError('Coupling reference does not match source SNP/model/frequency/impedance identity')
    out = {
        'applicable': bool(reference['applicable']), 'reference_resolved': bool(reference['resolved']),
        'passed': False, 'score': 1e99, 'status': 'reference_unresolved',
        'k_pct': tolerance, 'max_relative_pct': None, 'max_absolute_error': None,
        'invalid_candidate_count': 0,
        'absolute_limit': float(reference['absolute_reference_floor']) * tolerance / 100,
        'absolute_reference_floor': float(reference['absolute_reference_floor']),
        'valid_indices': list(reference['valid_indices']),
        'freq_hz': list(reference['freq_hz']),
        'source_k': list(reference['source_k']),
        'candidate_k': [None] * len(network.freq),
        'reference': reference,
    }
    if not reference['applicable']:
        out.update(passed=True, score=0., status='not_applicable')
        return out
    if not reference['resolved']:
        out['reason'] = reference.get('reason', 'Reference coupling domain unresolved')
        return out
    ix = np.asarray(reference['valid_indices'], dtype=int)
    try:
        response = simulate_stable(circuit, network.freq, network.z0)
        # simulate_stable returns the complete S batch, without Network wrappers.
        values = coupling_values(response, network.z0, network.freq, spec)
        kfit = values['k']
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as error:
        out.update(status='candidate_unresolved', invalid_candidate_count=len(ix), reason=str(error))
        return out
    bad = ~np.isfinite(kfit[ix]) | (values['x1'][ix] <= 0) | (values['x2'][ix] <= 0)
    out['invalid_candidate_count'] = int(np.count_nonzero(bad))
    valid_mask = np.asarray(reference['valid_mask'], bool)
    out['candidate_k'] = [float(kfit[i]) if valid_mask[i] and np.isfinite(kfit[i]) else None for i in range(len(network.freq))]
    if np.any(bad):
        out.update(status='candidate_noninductive_or_nonfinite', invalid_frequency_hz=np.asarray(network.freq)[ix[bad]].tolist())
        return out
    source = np.asarray([reference['source_k'][i] for i in ix], float)
    fitted = kfit[ix]
    with np.errstate(over='ignore', invalid='ignore'):
        error = np.abs(fitted - source)
    if not np.all(np.isfinite(error)):
        out.update(status='nonfinite_error_metric', reason='Absolute effective-k error overflowed')
        return out
    floor = float(reference['absolute_reference_floor'])
    normal = np.abs(source) > floor
    with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
        relative = error[normal] / np.abs(source[normal])
    absolute_pass = bool(np.all(error[~normal] <= out['absolute_limit']))
    with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
        ratio = error / (np.maximum(np.abs(source), floor) * tolerance / 100)
    if not np.all(np.isfinite(ratio)) or not np.all(np.isfinite(relative * 100)):
        out.update(status='nonfinite_error_metric', reason='Normalized effective-k error overflowed')
        return out
    worst = int(np.argmax(ratio))
    out.update(status='evaluated',
               passed=bool(np.all(relative * 100 <= tolerance) and absolute_pass),
               score=float(np.max(ratio)),
               max_relative_pct=float(np.max(relative) * 100) if len(relative) else None,
               max_absolute_error=float(np.max(error)),
               near_zero_reference_count=int(np.count_nonzero(~normal)),
               near_zero_absolute_pass=absolute_pass,
               worst_frequency_hz=float(np.asarray(network.freq)[ix[worst]]))
    return out
