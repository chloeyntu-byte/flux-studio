"""Independent, bounded two-stage engine for the English multi-page studio.

Only an explicit ``upgrade`` action may change a graph. A quick result is never
qualified; native qualification is obtained from actual OA/Spectre S data. The
5-second quick-generation goal is informational: source parsing, necessary
construction, evaluation and output always finish, even when total time is longer.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid
import numpy as np

from .network import Network, read_touchstone, write_touchstone
from .models import get_model, list_models, add_loss_candidate
from .models.base import Candidate
from .topology import build_candidate
from .quickstart import quick_initialize, topology_signature
from .stable_network import StableSensitivityNetwork, simulate_stable, evaluate_stable
from .fitting import _measure_s, _evaluate_s
from .resonance import (build_reference, evaluate_srf, evaluate_srf_network,
                        local_reference_indices, local_network_guard)
from .coupling import k_reference, coupling_values
from .circuit import component_counts, validate, write_spice

ENGINE_VERSION = 'studio_joint_parameters_v1'
_TARGET_KEYS = ('l_pct', 'q_pct', 'k_pct', 'srf_pct', 'network_pct', 'component_limit')
_SOURCE_KEYS = ('model_id', 'port_order', 'short_unused', 'fmin_ghz', 'fmax_ghz')


def _clean(value):
    if isinstance(value, np.ndarray): return _clean(value.tolist())
    if isinstance(value, np.generic): return _clean(value.item())
    if isinstance(value, dict): return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    return value


def _json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(_clean(value), indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def _digest(value):
    return hashlib.sha256(json.dumps(_clean(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def inspect_source(path):
    source = read_touchstone(path)
    positive = source.freq[source.freq > 0]
    return {'path': str(Path(path).expanduser().resolve()), 'sha256': source.metadata['sha256'],
            'nports': source.nports, 'point_count': len(source.freq),
            'positive_point_count': len(positive), 'fmin_hz': float(positive[0]) if len(positive) else None,
            'fmax_hz': float(positive[-1]) if len(positive) else None,
            'fmin_ghz': float(positive[0]/1e9) if len(positive) else None,
            'fmax_ghz': float(positive[-1]/1e9) if len(positive) else None,
            'port_names': list(source.port_names), 'reference_ohm': source.z0.tolist(),
            'metadata': copy.deepcopy(source.metadata)}


def model_catalog():
    result = []
    for spec in list_models():
        if isinstance(spec, str): spec = get_model(spec)
        label = ('Single-ended' if spec.mode == 'se' else 'Differential') + ' ' + spec.device
        taps = ' + '.join(spec.tap_roles)
        label += ' / ' + (taps if taps else 'No tap') + ' / ' + ('GND pin' if spec.explicit_reference else 'Internal REF')
        result.append({'id': spec.id, 'label': label, 'device': spec.device, 'mode': spec.mode,
                       'port_roles': list(spec.port_roles), 'explicit_reference': spec.explicit_reference,
                       'topologies': [{'id': c.id, 'label': c.id.replace('_', ' ').title()} for c in spec.candidates]})
    return result


def normalize_config(config, *, targets=True):
    cfg = copy.deepcopy(config)
    path = str(cfg.get('source_path', cfg.get('file', ''))).strip()
    if not path: raise ValueError('Load a Touchstone SNP file first.')
    cfg['source_path'] = str(Path(path).expanduser().resolve())
    if not cfg.get('model_id'): raise ValueError('Choose one explicit device model.')
    get_model(cfg['model_id'])
    if not cfg.get('ports_confirmed'): raise ValueError('Confirm the port mapping and unused-port termination.')
    order = cfg.get('port_order', cfg.get('ports', ''))
    if isinstance(order, str):
        try: order = [int(x.strip()) for x in order.replace(';', ',').split(',') if x.strip()]
        except ValueError as exc: raise ValueError('Port mapping must be comma-separated 1-based integers.') from exc
    if not isinstance(order, (list, tuple)) or not all(type(x) is int and x > 0 for x in order):
        raise ValueError('Port mapping must contain positive 1-based integers.')
    cfg['port_order'] = list(order)
    if len(order) != get_model(cfg['model_id']).nports or len(set(order)) != len(order):
        raise ValueError('Port mapping must match the selected model and contain no duplicate ports.')
    if type(cfg.get('short_unused', False)) is not bool:
        raise ValueError('Unused-port shorting must be an explicit Boolean selection.')
    cfg['short_unused'] = cfg.get('short_unused', False)
    for key, default in [('fmin_ghz', 1), ('fmax_ghz', 50)]:
        try: cfg[key] = float(cfg.get(key, default))
        except (ValueError, TypeError) as exc: raise ValueError('Enter a valid frequency range in GHz.') from exc
    if not (0 < cfg['fmin_ghz'] < cfg['fmax_ghz']) or not np.all(np.isfinite([cfg['fmin_ghz'], cfg['fmax_ghz']])):
        raise ValueError('Frequency limits must be finite, positive and increasing.')
    if targets:
        for key in ('l_pct', 'q_pct'):
            try: cfg[key] = float(cfg[key])
            except (KeyError, ValueError, TypeError) as exc: raise ValueError('Enter fresh L and Q error limits for this run.') from exc
            if not np.isfinite(cfg[key]) or not 5 <= cfg[key] <= 20:
                raise ValueError('L and Q error limits must each be between 5 and 20 percent.')
        for key, default in [('k_pct', 5), ('srf_pct', 2), ('network_pct', min(cfg['l_pct'], cfg['q_pct']))]:
            try: cfg[key] = float(cfg.get(key, default))
            except (ValueError, TypeError) as exc: raise ValueError('Enter positive finite k, SRF and network limits.') from exc
            if not np.isfinite(cfg[key]) or not 0 < cfg[key] <= 100:
                raise ValueError('k, SRF and network limits must be positive and no greater than 100 percent.')
    cfg['component_limit'] = int(cfg.get('component_limit', 20))
    if cfg['component_limit'] not in (20, 30): raise ValueError('Choose a per-kind component limit of 20 or 30.')
    for key, default, maximum in [('quick_seconds', 5., 5.), ('tune_seconds', 60., 600.), ('upgrade_seconds', 90., 600.)]:
        try: cfg[key] = float(cfg.get(key, default))
        except (ValueError, TypeError) as exc: raise ValueError('Enter a valid time budget in seconds.') from exc
        if not np.isfinite(cfg[key]) or not 0 < cfg[key] <= maximum:
            raise ValueError('%s must be greater than zero and at most %g seconds.' % (key.replace('_', ' ').title(), maximum))
    cfg['topology_id'] = str(cfg.get('topology_id', 'auto'))
    if cfg['topology_id'] not in ('auto', '') and cfg['topology_id'] not in [c.id for c in get_model(cfg['model_id']).candidates]:
        raise ValueError('The selected initial topology does not belong to this model.')
    return cfg


def _prepare(config, *, targets=True):
    cfg = normalize_config(config, targets=targets)
    full = read_touchstone(cfg['source_path'])
    source = inspect_source(cfg['source_path'])
    if max(cfg['port_order']) > full.nports: raise ValueError('A mapped port number exceeds the SNP port count.')
    full = full.mapped([i-1 for i in cfg['port_order']], short_unused=cfg['short_unused'])
    positive = full.freq > 0
    full = Network(full.freq[positive], full.s[positive], full.z0, list(full.port_names), dict(full.metadata))
    selected = full.band(cfg['fmin_ghz']*1e9, cfg['fmax_ghz']*1e9)
    source.update(mapped_port_roles=list(get_model(cfg['model_id']).port_roles),
                  port_order=list(cfg['port_order']), short_unused=cfg['short_unused'],
                  selected_point_count=len(selected.freq), selected_band_hz=[float(selected.freq[0]), float(selected.freq[-1])])
    return cfg, source, selected, full


def compatibility_fingerprint(config, source):
    return _digest({'source_sha256': source['sha256'], **{k: config[k] for k in _SOURCE_KEYS}})


def acceptance_fingerprint(config, source):
    return _digest({'compatibility': compatibility_fingerprint(config, source), **{k: config[k] for k in _TARGET_KEYS}})


def _check_baseline(baseline, cfg, source, *, accepted=False):
    if not baseline or not baseline.get('circuit'): raise ValueError('Generate initial values before tuning or upgrading.')
    expected = compatibility_fingerprint(cfg, source)
    if baseline.get('compatibility_fingerprint') != expected:
        raise ValueError('Source, model, port mapping or frequency band changed. Generate a new initial circuit.')
    validate(baseline['circuit'], max_each=cfg['component_limit'])
    if list(baseline['circuit']['ports']) != list(get_model(cfg['model_id']).port_roles):
        raise ValueError('The circuit terminal order does not match the selected device model.')
    if accepted:
        if not baseline.get('passed') or baseline.get('status') == 'INITIAL_UNVERIFIED':
            raise ValueError('Schematic generation requires a jointly accepted tuned circuit. Export SPICE to inspect an unverified initial circuit.')
        if baseline.get('acceptance_fingerprint') != acceptance_fingerprint(cfg, source):
            raise ValueError('Precision targets changed. Run Tune again before generating the schematic.')


def _limits(cfg): return {k: cfg[k] for k in ('l_pct', 'q_pct', 'network_pct')}

def _coupling_reference(band, spec, reference):
    brackets = [item.get('bracket_hz') for item in reference.get('branches', {}).values()]
    complete = bool(brackets and all(b and len(b) == 2 for b in brackets))
    result = k_reference(band, spec, reference if complete else None)
    result['domain_status'] = 'below_first_SRF_margin' if complete else 'positive_reference_reactances_SRF_not_observed'
    return result


def _k_error(sf, net, spec, reference, pct):
    result = {'applicable': reference['applicable'], 'reference_resolved': reference['resolved'],
              'passed': False, 'score': 1e99, 'status': 'reference_unresolved', 'k_pct': pct,
              'max_relative_pct': None, 'invalid_candidate_count': 0,
              'reference': reference, 'valid_indices': reference['valid_indices'],
              'absolute_limit': reference['absolute_reference_floor']*pct/100}
    if not reference['applicable']:
        result.update(passed=True, score=0., status='not_applicable'); return result
    if not reference['resolved']: return result
    data = coupling_values(sf, net.z0, net.freq, spec)
    ix = np.asarray(reference['valid_indices'], int)
    values = data['k'][ix]
    invalid = ~np.isfinite(values) | (data['x1'][ix] <= 0) | (data['x2'][ix] <= 0)
    result['invalid_candidate_count'] = int(np.count_nonzero(invalid))
    if np.any(invalid): result['status'] = 'candidate_noninductive'; return result
    source = np.asarray([reference['source_k'][i] for i in ix], float)
    floor = reference['absolute_reference_floor']; delta = np.abs(values-source)
    relative = np.abs(source) > floor
    ratios = delta / (np.maximum(np.abs(source), floor)*pct/100)
    result.update(passed=bool(np.all(ratios <= 1)), score=float(np.max(ratios)), status='evaluated',
                  max_relative_pct=float(np.max(delta[relative]/np.abs(source[relative]))*100) if np.any(relative) else None,
                  max_absolute_error=float(np.max(delta)), worst_frequency_hz=float(net.freq[ix[int(np.argmax(ratios))]]))
    return result


def _observable_scores(lq, kval, srf, local):
    available = {'lq': float(lq['score'])}
    if not kval.get('applicable', True) or kval.get('reference_resolved', False):
        available['k'] = float(kval['score'])
    if srf.get('reference_resolved', False):
        available['srf'] = float(srf['score'])
    if local.get('sample_count', 0) > 0:
        available['local_s'] = float(local['score'])
    # An unlocated candidate root remains a hard failure, but its sentinel must
    # not erase real improvements in other observable objectives when ranking.
    return {key: min(max(value, 0.), 1e6) for key, value in available.items()}


def _joint(lq, kval, srf, local):
    checks = {'lq': lq['passed'], 'k': kval['passed'], 'srf': srf['passed'], 'local_s': local['passed']}
    observable = _observable_scores(lq, kval, srf, local)
    return {'passed': all(checks.values()), 'score': max(observable.values()),
            'observable_scores': observable,
            'unresolved_targets': [key for key in ('k','srf','local_s') if key not in observable],
            'checks': checks, 'status': 'ACCEPTED' if all(checks.values()) else 'TARGETS_NOT_MET'}


def _rank(metrics):
    scores = metrics['joint'].get('observable_scores', {'lq': metrics['lq']['score']})
    return (not metrics['joint']['passed'], max(scores.values()), sum(scores.values()),
            scores.get('srf', 0.))


def _assess(circuit, band, full, spec, cfg, reference, kref, *, roots=True):
    sf = simulate_stable(circuit, band.freq, band.z0)
    lq = _evaluate_s(sf, band, spec, _limits(cfg), component_counts(circuit))
    kval = _k_error(sf, band, spec, kref, cfg['k_pct'])
    if roots:
        srf = evaluate_srf(circuit, full, spec, reference, cfg['srf_pct'])
        local = local_network_guard(circuit, full, spec, reference, cfg['network_pct'])
    else:
        srf = {'passed': False, 'score': 1e99, 'status': 'not_evaluated_for_initial_values',
               'max_error_upper_pct': None, 'branches': {}, 'limit_pct': cfg['srf_pct'],
               'reference_resolved': reference['resolved']}
        local = {'passed': False, 'score': 1e99, 'status': 'not_evaluated_for_initial_values', 'max_s_absolute_error': None}
    return {'lq': lq, 'k': kval, 'srf': srf, 'local_s': local, 'joint': _joint(lq, kval, srf, local)}


def _curves(circuit, band, spec, reference, metrics=None):
    source, source_z = _measure_s(band.s, band.z0, band.freq, spec)
    fitted = {}
    fit_z = None
    if circuit is not None:
        fitted, fit_z = _measure_s(simulate_stable(circuit, band.freq, band.z0), band.z0, band.freq, spec)
    branches = {}
    for name, values in source.items():
        branches[name] = {'source_l_h': values['l_h'].tolist(), 'source_q': values['q'].tolist(),
                          'source_r_ohm': values['r_ohm'].tolist()}
        if name in fitted:
            branches[name].update(fit_l_h=fitted[name]['l_h'].tolist(), fit_q=fitted[name]['q'].tolist(),
                                  fit_r_ohm=fitted[name]['r_ohm'].tolist())
    source_k = fit_k = None
    k_domain = None
    if spec.device == 'transformer':
        k_domain = _coupling_reference(band,spec,reference)
        source_k = list(k_domain['source_k'])
        if circuit is not None:
            raw_k = coupling_values(simulate_stable(circuit, band.freq, band.z0), band.z0, band.freq, spec)['k']
            valid = set(k_domain['valid_indices'])
            fit_k = [float(v) if i in valid and np.isfinite(v) else None for i,v in enumerate(raw_k)]
    out = {'freq_hz': band.freq.tolist(), 'source': _clean(source), 'fitted': _clean(fitted),
           'branches': branches, 'source_k': source_k, 'fit_k': fit_k, 'fitted_k': fit_k,
           'source_srf': reference, 'fitted_srf': (metrics or {}).get('srf'),
           'k_domain': k_domain,
           'k_note': 'Signed effective k is shown only on the source-defined inductive domain; unavailable points remain gaps.',
           'sample_basis': 'All original positive SNP samples in the selected frequency band; no interpolation.'}
    if source_z.shape[-1] == 2:
        out.update(source_mutual_z_real=source_z[:,0,1].real.tolist(), source_mutual_z_imag=source_z[:,0,1].imag.tolist())
        if fit_z is not None: out.update(fit_mutual_z_real=fit_z[:,0,1].real.tolist(), fit_mutual_z_imag=fit_z[:,0,1].imag.tolist())
    return _clean(out)


def _new_run(runtime, action):
    if runtime is None:
        from .workflow import runtime_root
        runtime = runtime_root()
    root = Path(runtime).expanduser().resolve() / 'studio-runs'
    root.mkdir(parents=True, exist_ok=True)
    out = root / (time.strftime('%Y%m%d-%H%M%S') + '-' + action + '-' + uuid.uuid4().hex[:8])
    out.mkdir()
    return out


def _snapshot(action, cfg, source, band, full, circuit, metrics, out, *, message='', warnings=None, extra=None):
    spec = get_model(cfg['model_id'])
    reference = build_reference(full, spec)
    result = {'schema': 1, 'engine': ENGINE_VERSION, 'id': uuid.uuid4().hex, 'stage': action,
              'status': 'INITIAL_UNVERIFIED' if action == 'quick' else 'SOURCE_LOADED' if action == 'preview' else metrics['joint']['status'],
              'passed': bool(action not in ('quick','preview') and metrics and metrics['joint']['passed']),
              'native_verified': False, 'source': source, 'request': cfg,
              'run_dir': str(out), 'circuit': circuit, 'metrics': metrics,
              'compatibility_fingerprint': compatibility_fingerprint(cfg, source),
              'acceptance_fingerprint': acceptance_fingerprint(cfg, source) if action != 'preview' else None,
              'message': message, 'warnings': list(warnings or []),
              'curves': _curves(circuit, band, spec, reference, metrics)}
    if circuit is not None:
        result['component_counts'] = validate(circuit, max_each=cfg['component_limit'])
        result['topology_signature'] = topology_signature(circuit)
        _json(out/'circuit.json', circuit); write_spice(circuit, out/'model.sp')
    write_touchstone(band, out/('selected.s%dp' % band.nports))
    result['selected_snp'] = str(out/('selected.s%dp' % band.nports))
    if extra: result.update(extra)
    _json(out/'result.json', result)
    return _clean(result)


def _parameter_fit(circuit, band, full, spec, cfg, seconds, cancel=None, progress=None):
    """Fixed-graph LM with L/Q/S/mutual/k/SRF in residuals and independent gates."""
    started = time.monotonic(); deadline = started + max(0., seconds)
    reference = build_reference(full, spec)
    kref = _coupling_reference(band, spec, reference)
    current = copy.deepcopy(circuit)
    signature = topology_signature(current)
    metrics = _assess(current, band, full, spec, cfg, reference, kref)
    receipt = {'algorithm': 'bounded_fixed_graph_joint_L_Q_signed_k_SRF_MNA_LM',
               'budget_seconds': seconds, 'graph_changes': 0, 'added_elements': 0,
               'k_in_objective': spec.device == 'transformer', 'iterations': 0, 'accepted_steps': 0,
               'initial_joint_score': metrics['joint']['score'], 'external_tools': [],
               'source_srf_reference': reference, 'coupling_reference': kref}
    protected = {key: bool(metrics[key]['passed']) for key in ('lq','k','srf','local_s')}
    def stopped(): return (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline
    def finish(state):
        receipt.update(state=state, elapsed_s=time.monotonic()-started,
                       final_joint_score=metrics['joint']['score'], joint_passed=metrics['joint']['passed'])
        assert topology_signature(current) == signature
        return current, metrics, receipt
    if metrics['joint']['passed']: return finish('already_accepted')
    if stopped(): return finish('cancelled' if cancel is not None and cancel.is_set() else 'budget_exhausted')
    # A missing SRF reference is never invented; other measured objectives may
    # still improve, but the result remains explicitly unqualified.
    guard_idx = np.unique(np.linspace(0, len(band.freq)-1, min(96, len(band.freq))).astype(int))
    local_idx = local_reference_indices(full, reference)
    if len(local_idx) > 64: local_idx = local_idx[np.unique(np.linspace(0,len(local_idx)-1,64).astype(int))]
    points = []
    if reference['resolved']:
        tolerance = cfg['srf_pct']/100
        for name, item in reference['branches'].items():
            a,b = item['bracket_hz']; lo,hi = (1-tolerance)*b,(1+tolerance)*a
            if lo > hi: continue
            points.append((name, item['estimate_hz'], lo, hi, abs(item['slope_q_per_log_hz'])))
    freq = np.unique(np.r_[band.freq[guard_idx], full.freq[local_idx], [f for _,center,lo,hi,_ in points for f in (center,lo,hi)]])
    bix = np.searchsorted(freq, band.freq[guard_idx]); lix = np.searchsorted(freq, full.freq[local_idx])
    source, source_z = _measure_s(band.s[guard_idx], band.z0, band.freq[guard_idx], spec)
    source_kref = _coupling_reference(band, spec, reference)
    valid_original = set(source_kref['valid_indices'])
    k_slots = [j for j,i in enumerate(guard_idx) if int(i) in valid_original]
    k_source = np.asarray([source_kref['source_k'][int(guard_idx[j])] for j in k_slots], float)
    lscale,qscale,sscale,kscale = cfg['l_pct']/100,cfg['q_pct']/100,cfg['network_pct']/100,cfg['k_pct']/100
    norms = {}
    for name, curves in source.items():
        norms[name] = {key: np.maximum(np.abs(curves[key]), max(float(np.max(np.abs(curves[key])))*1e-6,1e-30)) for key in ('l_h','q')}
    center_indices = {name:np.searchsorted(freq,[center,lo,hi]) for name,center,lo,hi,_ in points}
    graph = StableSensitivityNetwork(current, freq)
    x = np.zeros(graph.ng)
    damping = 1e-2
    def residual(batch):
        count = len(batch); nf = len(freq)
        curves, z = _measure_s(batch.reshape(count*nf, band.nports, band.nports), band.z0, np.tile(freq,count), spec)
        terms = []
        for name in source:
            for key,scale in [('l_h',lscale),('q',qscale)]:
                actual = curves[name][key].reshape(count,nf)[:,bix]
                terms.append((actual-source[name][key][None])/norms[name][key][None]/scale)
        delta = (batch[:,bix]-band.s[guard_idx][None]).reshape(count,-1)/sscale
        terms.extend((delta.real,delta.imag))
        if len(local_idx):
            local = (batch[:,lix]-full.s[local_idx][None]).reshape(count,-1)/sscale
            terms.extend((local.real,local.imag))
        if spec.device == 'transformer':
            zz = z.reshape(count,nf,2,2)[:,bix]
            for a,b in ((0,1),(1,0)):
                value = source_z[:,a,b]
                den = np.maximum(np.abs(value), max(float(np.max(np.abs(value)))*1e-6,1e-30))*min(lscale,qscale)
                md = (zz[:,:,a,b]-value[None])/den[None]
                terms.extend((md.real,md.imag))
            if k_slots:
                x1,x2 = zz[:,k_slots,0,0].imag,zz[:,k_slots,1,1].imag
                floor = source_kref['absolute_reference_floor']
                # Invalid fitted reactances cannot shrink the reference domain.
                safe1 = np.maximum(x1,1e-20); safe2 = np.maximum(x2,1e-20)
                kval = zz[:,k_slots,0,1].imag / np.sqrt(safe1) / np.sqrt(safe2)
                kr = (kval-k_source[None]) / (np.maximum(np.abs(k_source),floor)[None]*kscale)
                terms.append(np.clip(kr,-1e12,1e12))
                refx1 = source_z[k_slots,0,0].imag;refx2=source_z[k_slots,1,1].imag
                terms.append(np.maximum(-x1,0)/np.maximum(np.abs(refx1),1e-20)[None]*100)
                terms.append(np.maximum(-x2,0)/np.maximum(np.abs(refx2),1e-20)[None]*100)
        for name,center,lo,hi,slope in points:
            values = curves[name]['q'].reshape(count,nf)[:,center_indices[name]]
            den = max(slope*cfg['srf_pct']/100,1e-12)
            terms.extend((values[:,0:1]/den*3,
                          np.maximum(-values[:,1:2],0)/den*3,
                          np.maximum(values[:,2:3],0)/den*3))
        raw = np.concatenate(terms,axis=1)
        if not np.all(np.isfinite(raw)): raise ValueError('The selected circuit produced a non-finite joint residual.')
        penalty = np.sign(raw)*np.maximum(np.abs(raw)-.98,0)*3
        return np.concatenate((raw,penalty),axis=1)
    def independent(candidate):
        nonlocal current, metrics
        assessed = _assess(candidate,band,full,spec,cfg,reference,kref)
        if any(protected[key] and not assessed[key]['passed'] for key in protected): return
        key = _rank(assessed)
        old = _rank(metrics)
        if key < old:
            current,metrics = candidate,assessed
    if progress: progress('Tuning device values with a fixed topology; L, Q, k and SRF targets remain active.')
    pending = None
    try:
        for iteration in range(240):
            if stopped() or metrics['joint']['passed']: break
            receipt['iterations'] += 1
            sf, ds = graph.s(x, band.z0, True)
            r = residual(sf[None])[0]
            eps=1e-5
            jac = ((residual(sf[None]+eps*ds)-r[None])/eps).T
            normal,gradient = jac.T@jac,jac.T@r
            diagonal=np.maximum(np.diag(normal),1e-12)
            cost=float(r@r); accepted=False
            for _ in range(7):
                if stopped(): break
                try: step=-np.linalg.solve(normal+damping*np.diag(diagonal),gradient)
                except np.linalg.LinAlgError: damping*=5;continue
                step=np.clip(step,-.35,.35)
                for back in range(5):
                    if stopped(): break
                    trial=x+step*(.5**back)
                    try:
                        trial_s=graph.s(trial,band.z0)
                        rr=residual(trial_s[None])[0]
                        if float(rr@rr) < cost:
                            x=trial;pending=graph.circuit(x);accepted=True
                            receipt['accepted_steps'] += 1;damping=max(1e-9,damping/3)
                            if receipt['accepted_steps']%4==0: independent(pending);pending=None
                            break
                    except (ValueError,np.linalg.LinAlgError,FloatingPointError,OverflowError): pass
                if accepted: break
                damping*=5
            if not accepted: break
        if pending is not None and not (cancel is not None and cancel.is_set()): independent(pending)
    except (ValueError,np.linalg.LinAlgError,FloatingPointError,OverflowError) as error:
        receipt['numerical_warning']=str(error)
    return finish('cancelled' if cancel is not None and cancel.is_set() else 'accepted' if metrics['joint']['passed'] else 'budget_exhausted' if stopped() else 'stationary')


def _upgrade_candidates(baseline, full, spec, cfg, metrics):
    """Finite explicit graph changes, never invoked by quick or tune."""
    old = baseline.get('metadata',{}).get('candidate')
    old_index = next((i for i,c in enumerate(spec.candidates) if c.id == old), -1)
    candidates = list(spec.candidates[max(0,old_index+1):])
    if not candidates:
        sections = int(baseline.get('metadata',{}).get('sections_per_winding',3))
        if sections < 4: candidates=[Candidate('extended_4','Extended four-section winding',4,2,True,bridges=spec.device=='transformer')]
    for candidate in candidates[:3]:
        try:
            circuit=build_candidate(full,spec,candidate,max_each=cfg['component_limit'])
            # Retain values only where both name and all actual connections agree.
            old_elements={e['name']:e for e in baseline['elements']}
            for e in circuit['elements']:
                previous=old_elements.get(e['name'])
                if previous and all(e.get(k)==previous.get(k) for k in ('kind','n1','n2','ind1','ind2')):
                    e['value']=previous['value']
            validate(circuit,max_each=cfg['component_limit'])
            if topology_signature(circuit)!=topology_signature(baseline): yield candidate.id,circuit
        except (ValueError,np.linalg.LinAlgError,FloatingPointError): continue
    try:
        loss_index=len(baseline.get('metadata',{}).get('loss_cells',[]))+1
        loss=add_loss_candidate(copy.deepcopy(baseline),full,spec,metrics['lq'],loss_index)
        if loss is not None:
            loss.setdefault('metadata',{})['component_limit']=cfg['component_limit']
            validate(loss,max_each=cfg['component_limit'])
            if topology_signature(loss)!=topology_signature(baseline): yield 'additional_winding_loss',loss
    except (ValueError,np.linalg.LinAlgError,FloatingPointError): pass


def native_frequencies(full, band, numeric, relative_separation=1e-6):
    """Preserve every measured point; reject near-duplicate generated points.

    Spectre may collapse nearly equal frequencies. Measured checkpoints have
    priority over discovery/seed points, and are never replaced or rounded.
    """
    authoritative = np.unique(np.r_[full.freq, band.freq])
    chosen = list(map(float,authoritative))
    generated = list(map(float,np.geomspace(full.freq[0],full.freq[-1],65)))
    roots = [b['model_hz'] for b in numeric['srf']['branches'].values() if b.get('model_hz')]
    for root in roots:
        generated.extend(float(root*factor) for factor in (.998,.999,.9995,1.,1.0005,1.001,1.002)
                         if full.freq[0] <= root*factor <= full.freq[-1])
    for value in generated:
        existing = np.asarray(chosen,float)
        if np.any(np.abs(existing-value) <= relative_separation*np.maximum(existing,value)):
            continue
        chosen.append(value)
    result = np.asarray(sorted(chosen),float)
    # Exact source values are an invariant, not a nearest-frequency substitute.
    if not np.all(np.isin(authoritative,result)):
        raise AssertionError('Native frequency construction lost a measured checkpoint.')
    return result


def _native(cfg, source, band, full, baseline, out, cancel, progress):
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Schematic generation cancelled before starting native tools.')
    from .native import build_native, replay_native
    from .delivery import library_mappings
    spec=get_model(cfg['model_id']);reference=build_reference(full,spec)
    kref=_coupling_reference(band,spec,reference)
    current=copy.deepcopy(baseline['circuit'])
    numeric=_assess(current,band,full,spec,cfg,reference,kref)
    if not numeric['joint']['passed']: raise ValueError('The selected snapshot no longer passes the current numerical targets.')
    workspace=str(cfg.get('workspace','')).strip()
    if not Path(workspace,'cds.lib').is_file(): raise ValueError('Choose a Virtuoso workspace containing cds.lib.')
    library=str(cfg.get('library','SNPFit_models')).strip();cell=str(cfg.get('cell','passive_fit')).strip()
    if not library or not cell: raise ValueError('Enter a target library and a new cell name.')
    existing=cfg.get('existing_library_path') or library_mappings(workspace).get(library)
    options={k:cfg[k] for k in ('env_script','virtuoso','ocean') if cfg.get(k)}
    if cfg.get('native_user'): options['user']=cfg['native_user']
    plan = _visual_plan(current, cfg, source)
    if progress: progress('Creating the winding-shaped analogLib schematic, then checking saved connectivity.')
    native=build_native(current,workspace,out/'delivery',library,cell,existing_library_path=existing,
                        visual_plan=plan,make_symbol=False,
                        cancel=cancel.is_set if cancel else None,**options)
    frequencies=native_frequencies(full,band,numeric)
    replay_options={k:cfg[k] for k in ('env_script','spectre') if cfg.get(k)}
    if cfg.get('native_user'):replay_options['user']=cfg['native_user']
    if progress:progress('Replaying the exact OA netlist; checking L, Q, signed k, first SRF and local S.')
    replay=replay_native(native,frequencies.tolist(),full.z0.tolist(),cancel=cancel,**replay_options)
    candidate=read_touchstone(replay['response'])
    if len(candidate.freq)!=len(frequencies) or not np.allclose(candidate.freq,frequencies,rtol=5e-7,atol=0):
        raise ValueError('Native replay did not return the complete requested frequency set.')
    candidate.freq=frequencies.copy()
    idx=np.searchsorted(frequencies,band.freq)
    lq_sf=candidate.s[idx]
    lq=_evaluate_s(lq_sf,band,spec,_limits(cfg),component_counts(current))
    kval=_k_error(lq_sf,band,spec,kref,cfg['k_pct'])
    srf=evaluate_srf_network(candidate,full,spec,reference,cfg['srf_pct'])
    local_idx=local_reference_indices(full,reference);locations=np.searchsorted(frequencies,full.freq[local_idx])
    delta=np.abs(candidate.s[locations]-full.s[local_idx]);error=float(np.max(delta)) if len(delta) else None
    local={'passed':error is not None and error<=cfg['network_pct']/100,'score':error/(cfg['network_pct']/100) if error is not None else 1e99,
           'max_s_absolute_error':error,'sample_count':len(local_idx),'status':'native_original_points'}
    metrics={'lq':lq,'k':kval,'srf':srf,'local_s':local,'joint':_joint(lq,kval,srf,local)}
    native.update(replay=replay,metrics=metrics)
    result=_snapshot('native',cfg,source,band,full,current,metrics,out,
                     message='Schematic created and native replay accepted.' if metrics['joint']['passed'] else 'Schematic created, but native replay did not meet every target.',
                     warnings=[] if metrics['joint']['passed'] else ['This cellview is not verified. Inspect the native validation details.'],
                     extra={'native':native,'native_verified':bool(metrics['joint']['passed']), 'visual_plan':plan,
                            'numeric_metrics':numeric,'parent_snapshot_id':baseline['id']})
    result['status']='VERIFIED' if result['native_verified'] else 'NATIVE_NOT_ACCEPTED'
    result['passed']=bool(result['native_verified'])
    # Display actual native samples at the selected original points.
    actual=Network(band.freq,lq_sf,band.z0,list(band.port_names))
    actual_curves,actual_z=_measure_s(actual.s,actual.z0,actual.freq,spec)
    result['curves']['fitted']=_clean(actual_curves)
    for name,values in actual_curves.items():
        result['curves']['branches'][name].update(fit_l_h=values['l_h'].tolist(),fit_q=values['q'].tolist(),fit_r_ohm=values['r_ohm'].tolist())
    if spec.device=='transformer':
        raw_k=coupling_values(actual.s,actual.z0,actual.freq,spec)['k']
        valid_k=set(kref['valid_indices'])
        kk=[float(v) if i in valid_k and np.isfinite(v) else None for i,v in enumerate(raw_k)]
        result['curves']['fit_k']=result['curves']['fitted_k']=kk
        result['curves']['fit_mutual_z_real']=actual_z[:,0,1].real.tolist()
        result['curves']['fit_mutual_z_imag']=actual_z[:,0,1].imag.tolist()
    _json(out/'result.json',result)
    return result


def _visual_plan(circuit, cfg, source):
    """Presentation settings are deliberately outside fit acceptance identity."""
    from .winding_visual import build_visual
    transformer_style = cfg.get('native_transformer_style', 'coplanar')
    if not isinstance(transformer_style, str) or transformer_style not in ('coplanar', 'stacked'):
        raise ValueError('Native transformer style must be coplanar or stacked.')
    lo, hi = float(cfg['fmin_ghz']), float(cfg['fmax_ghz'])
    raw = cfg.get('q_annotation_ghz', '')
    try:
        frequency = float(raw) if str(raw).strip() else math.sqrt(lo*hi)
    except (ValueError, TypeError) as exc:
        raise ValueError('Enter an annotation frequency in GHz, or leave it blank for the band center.') from exc
    if not np.isfinite(frequency) or not lo <= frequency <= hi:
        raise ValueError('The segment Q annotation frequency must be within the selected fitting band.')
    plan = build_visual(circuit, cfg['model_id'], reference_id=cfg.get('visual_reference', 'auto'),
                        source_sha256=source.get('sha256'), port_order=cfg['port_order'],
                        short_unused=cfg['short_unused'], q_frequency_hz=frequency*1e9)
    # Native annotations are model predictions, independent of fit acceptance
    # and the unchanged Tk drawing. Keep the actual measurement fixture/Z0.
    requested = cfg.get('native_report_frequencies_ghz', [5, 10, 20])
    if not isinstance(requested, (list, tuple)) or not 1 <= len(requested) <= 6:
        raise ValueError('Native report frequencies must be a list of one to six GHz values.')
    if any(isinstance(value, (bool, np.bool_)) for value in requested):
        raise ValueError('Native report frequencies must be positive finite GHz values.')
    try:
        frequencies = [float(value)*1e9 for value in requested]
    except (ValueError, TypeError) as exc:
        raise ValueError('Native report frequencies must be positive finite GHz values.') from exc
    if any(not math.isfinite(value) or value <= 0 for value in frequencies):
        raise ValueError('Native report frequencies must be positive finite GHz values.')
    if len(set(frequencies)) != len(frequencies):
        raise ValueError('Native report frequencies must not repeat.')
    report_z0 = cfg.get('native_report_z0_ohm')
    if report_z0 is None and source.get('reference_ohm') is not None:
        original_z0 = source['reference_ohm']
        report_z0 = [original_z0[index-1] for index in cfg['port_order']]
    sampled = circuit.get('metadata', {}).get('acceptance_scope') == 'original_sampled_points_only'
    plan['native_transformer_style'] = transformer_style
    plan['native_report_context'] = {
        'frequencies_hz': frequencies,
        'fit_band_hz': source.get('selected_band_hz', source.get('sampled_band_hz', [lo*1e9, hi*1e9])),
        'z0': report_z0,
        'fixture': cfg.get('native_report_fixture', 'sampled_open' if sampled else 'studio'),
    }
    return plan


def _symbol(cfg, source, baseline, out, cancel, progress):
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Symbol generation cancelled before starting native tools.')
    from .native import build_symbol
    if not baseline.get('native_verified') or not baseline.get('native'):
        raise ValueError('Generate and verify the schematic before adding its symbol.')
    native = baseline['native']
    if (str(cfg.get('library', '')).strip() != native.get('library')
            or str(cfg.get('cell', '')).strip() != native.get('cell')
            or Path(cfg.get('workspace', '')).resolve() != Path(native.get('workspace', '')).resolve()):
        raise ValueError('Generate Symbol uses the verified schematic cell. Restore its workspace, library and cell fields.')
    plan = _visual_plan(baseline['circuit'], cfg, source)
    options = {k: cfg[k] for k in ('env_script', 'virtuoso') if cfg.get(k)}
    if cfg.get('native_user'):
        options['user'] = cfg['native_user']
    if progress:
        progress('Adding the winding-shaped symbol to the verified schematic cell; checking every pin position.')
    symbol = build_symbol(native, baseline['circuit'], plan, out_dir=out/'delivery',
                          cancel=cancel.is_set if cancel else None, **options)
    result = copy.deepcopy(baseline)
    result.update(id=uuid.uuid4().hex, stage='symbol', request=cfg, run_dir=str(out),
                  parent_snapshot_id=baseline['id'], symbol_created=True,
                  message='Symbol created. The verified schematic and fitted response are unchanged.', warnings=[])
    result['native']['symbol'] = symbol
    result['native']['symbol_ports'] = symbol.get('symbol_ports', native.get('schematic_ports', []))
    _json(out/'result.json', result)
    return result


def _run_action(action, config, baseline=None, runtime=None, cancel=None, progress=None, _out=None):
    """Worker-safe entry point; all Tk state is owned by the caller's main thread."""
    if action not in ('preview','quick','tune','upgrade','native','symbol'): raise ValueError('Unknown studio action.')
    started=time.monotonic()
    cfg,source,band,full=_prepare(config,targets=action!='preview')
    spec=get_model(cfg['model_id']);out=_out if _out is not None else _new_run(runtime,action)
    if progress:progress('Loaded %d original samples in the selected band.' % len(band.freq))
    reference=build_reference(full,spec)
    warning=[]
    if not reference['resolved']:
        warning.append('The SNP does not resolve every first SRF with a narrow original bracket. SRF cannot be qualified; load better sampled data around resonance.')
    if action=='preview':
        result=_snapshot(action,cfg,source,band,full,None,None,out,message='Source curves are ready. Generate initial values to begin.',warnings=warning)
    elif action=='quick':
        # Bound optional numerical fitting independently of source I/O. A slow
        # file load must not consume the complete useful initialization budget.
        optimization_target=min(4.1,cfg['quick_seconds'])
        initial=quick_initialize(band,spec.id,_limits(cfg),out/'initial',timeout_s=optimization_target,
                                 candidate_id=None if cfg['topology_id'] in ('','auto') else cfg['topology_id'],
                                 component_limit=cfg['component_limit'],cancel=cancel,progress=lambda _:progress('Estimating passive device values from the selected SNP.') if progress else None)
        metrics=_assess(initial['circuit'],band,full,spec,cfg,reference,_coupling_reference(band,spec,reference),roots=False)
        result=_snapshot(action,cfg,source,band,full,initial['circuit'],metrics,out,
                         message='Initial circuit generated. Tune this topology to check every requested target.',
                         warnings=warning+['Initial values are unverified; quick generation does not guarantee the final L/Q/k/SRF targets.'],
                         extra={'initialization':initial,'accepted_new':True,'topology_changed':False})
    else:
        _check_baseline(baseline,cfg,source,accepted=action in ('native','symbol'))
        if action=='native':return _native(cfg,source,band,full,baseline,out,cancel,progress)
        if action=='symbol':return _symbol(cfg,source,baseline,out,cancel,progress)
        budget=cfg['tune_seconds'] if action=='tune' else cfg['upgrade_seconds']
        if action=='tune':
            circuit,metrics,receipt=_parameter_fit(baseline['circuit'],band,full,spec,cfg,budget,cancel,progress)
            result=_snapshot(action,cfg,source,band,full,circuit,metrics,out,
                             message='All numerical targets are met.' if metrics['joint']['passed'] else 'Fixed-topology tuning stopped before every target was met. Upgrade Structure is available as a manual next step.',
                             warnings=warning,extra={'optimizer':receipt,'parent_snapshot_id':baseline['id'],'accepted_new':True,'topology_changed':False})
        else:
            deadline=time.monotonic()+budget
            old_metrics=_assess(baseline['circuit'],band,full,spec,cfg,reference,_coupling_reference(band,spec,reference))
            best_attempt=None;attempts=[]
            candidates=list(_upgrade_candidates(baseline['circuit'],band,spec,cfg,old_metrics))
            for index,(label,candidate) in enumerate(candidates):
                if cancel is not None and cancel.is_set() or time.monotonic()>=deadline:break
                if progress:progress('Manually requested structure upgrade: %s (%d of %d).' % (label.replace('_',' '),index+1,len(candidates)))
                seconds=max(0.,deadline-time.monotonic())/max(1,len(candidates)-index)
                cc,mm,receipt=_parameter_fit(candidate,band,full,spec,cfg,seconds,cancel,progress)
                attempt={'label':label,'metrics':mm,'optimizer':receipt,'component_counts':component_counts(cc)};attempts.append(attempt)
                key=_rank(mm)
                if best_attempt is None or key<best_attempt[0]:best_attempt=(key,cc,mm)
                if mm['joint']['passed']:break
            if best_attempt is None:
                circuit,metrics=copy.deepcopy(baseline['circuit']),old_metrics
                accepted=False;changed=False
            else:
                _,circuit,metrics=best_attempt
                changed=topology_signature(circuit)!=topology_signature(baseline['circuit'])
                accepted=_rank(metrics) < _rank(old_metrics)
            result=_snapshot(action,cfg,source,band,full,circuit,metrics,out,
                             message='The upgraded circuit improves the measured result.' if accepted else ('No richer legal structure is available within the current component limit. The previous circuit is retained.' if not candidates else 'No improved structure was accepted. The previous circuit is retained; the attempted circuit is shown for comparison.'),
                             warnings=warning,extra={'accepted_new':bool(accepted),'no_improvement':not accepted,'topology_changed':changed,
                                                    'parent_snapshot_id':baseline['id'],'before_snapshot_id':baseline['id'],'upgrade_attempts':attempts,
                                                    'previous_metrics':old_metrics})
    result['elapsed_s']=time.monotonic()-started
    result['cancelled']=bool(cancel is not None and cancel.is_set())
    if result['cancelled']:
        result.update(passed=False,native_verified=False,status='CANCELLED',message='Operation cancelled. The previous page result remains available.',accepted_new=False)
    if action=='quick':
        result['performance_target_seconds']=cfg['quick_seconds']
        result['performance_target_met']=result['elapsed_s']<=cfg['quick_seconds']
        result['budget_met']=result['performance_target_met']  # Legacy informational alias.
    _json(out/'result.json',result)
    return result


def run_action(action, config, baseline=None, runtime=None, cancel=None, progress=None):
    """Run an owned action and keep a structured error receipt on failure.

    Quick timing is a soft performance goal, not an error or acceptance gate.
    Only optional numerical fitting has a bounded optimization budget; source
    reading, valid circuit construction, evaluation and output may take longer.
    """
    if action not in ('preview','quick','tune','upgrade','native','symbol'):
        raise ValueError('Unknown studio action.')
    out = _new_run(runtime, action)
    started = time.monotonic()
    try:
        result = _run_action(action, config, baseline, runtime, cancel, progress, _out=out)
        result['elapsed_s'] = time.monotonic()-started
        if action == 'quick':
            result['performance_target_seconds'] = float(result['request']['quick_seconds'])
            result['performance_target_met'] = result['elapsed_s'] <= result['performance_target_seconds']
            result['budget_met'] = result['performance_target_met']  # Compatibility only; never acceptance.
        _json(out/'result.json', result)
        return result
    except Exception as error:
        import traceback
        cancelled = bool(cancel is not None and cancel.is_set())
        _json(out/'error.json', {'stage':action, 'status':'CANCELLED' if cancelled else 'ERROR',
                               'message':str(error), 'traceback':traceback.format_exc(),
                               'elapsed_s':time.monotonic()-started, 'run_dir':str(out),
                               'source_path':str(config.get('source_path',''))})
        if cancelled:
            result = copy.deepcopy(baseline) if baseline else {'circuit':None, 'curves':None, 'metrics':None, 'source':{}}
            result.update(id=uuid.uuid4().hex, stage=action, status='CANCELLED', passed=False,
                          native_verified=False, cancelled=True, accepted_new=False,
                          message='Operation cancelled. The previous page result remains available.',
                          warnings=[], run_dir=str(out), elapsed_s=time.monotonic()-started)
            _json(out/'result.json',result)
            return _clean(result)
        message = str(error)
        if any('\u4e00' <= ch <= '\u9fff' for ch in message):
            message = 'The operation could not complete; inspect the diagnostic log for the underlying tool response.'
        raise ValueError(message + ' Details: ' + str(out/'error.json')) from error
