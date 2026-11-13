"""Fixture-specific first self-resonance and conservative sampled references.

Only original measured samples bracket a reference root. Interpolation provides
an optimization seed, never additional measured evidence or a narrower bracket.
"""
from __future__ import annotations

import numpy as np


def _spec(spec):
    if isinstance(spec, str):
        from .models import get_model
        return get_model(spec)
    return spec


def _curves(network, spec):
    from .fitting import _measure_s
    return _measure_s(network.s, network.z0, network.freq, _spec(spec))[0]


def _crossings(freq, x):
    return [int(i) for i in np.flatnonzero((x[:-1] > 0) & (x[1:] <= 0))]


def build_reference(network, spec, max_bracket_relative_width=.01):
    """Describe the first observed X>0 -> X<=0 crossing of every winding.

    `resolved` means a finite, passive-looking, locally narrow original bracket.
    It does not claim that a finite sampled source proves no hidden resonance.
    """
    spec = _spec(spec)
    freq = np.asarray(network.freq, float)
    if np.any(freq <= 0) or np.any(np.diff(freq) <= 0):
        raise ValueError('SRF reference requires increasing positive original frequencies')
    if not np.isfinite(max_bracket_relative_width) or max_bracket_relative_width <= 0:
        raise ValueError('SRF bracket width limit must be positive')
    curves = _curves(network, spec)
    result = {
        'model_id': spec.id, 'range_hz': [float(freq[0]), float(freq[-1])],
        'definition': 'first winding Im(Z)>0 to Im(Z)<=0 under the selected L/Q fixture',
        'sample_basis': 'original supplied SNP samples; interpolation only seeds optimization',
        'source_metadata': dict(network.metadata),
        'max_bracket_relative_width': float(max_bracket_relative_width), 'branches': {},
    }
    for name, curve in curves.items():
        x = np.asarray(curve['x_ohm'], float)
        r = np.asarray(curve['r_ohm'], float)
        crosses = _crossings(freq, x)
        item = {'status': 'not_captured', 'crossings_observed': len(crosses),
                'bracket_hz': None, 'estimate_hz': None, 'slope_ohm_per_log_hz': None}
        if crosses:
            i = crosses[0]
            a, b = float(freq[i]), float(freq[i + 1])
            xa, xb = float(x[i]), float(x[i + 1])
            width = (b - a) / ((a + b) / 2)
            slope = (xb - xa) / np.log(b / a)
            seed = np.exp(np.log(a) - xa / slope) if slope < 0 else None
            item.update(bracket_hz=[a, b], bracket_indices=[i, i + 1],
                        estimate_hz=float(seed) if seed is not None else None,
                        slope_ohm_per_log_hz=float(slope), bracket_relative_width=float(width),
                        bracket_x_ohm=[xa, xb], bracket_r_ohm=[float(r[i]), float(r[i + 1])],
                        slope_q_per_log_hz=float((curve['q'][i+1]-curve['q'][i])/np.log(b/a)),
                        earliest_observed_crossing=True)
            if not np.all(np.isfinite([xa, xb, r[i], r[i + 1], slope])) or slope >= 0:
                item['status'] = 'invalid_crossing'
            elif min(float(r[i]), float(r[i + 1])) < -max(np.max(np.abs(r)) * 1e-9, 1e-10):
                item['status'] = 'negative_resistance'
            elif width > max_bracket_relative_width:
                item['status'] = 'bracket_too_wide'
            else:
                item['status'] = 'resolved'
        result['branches'][name] = item
    result['resolved'] = all(v['status'] == 'resolved' for v in result['branches'].values())
    return result


def _model_curves(circuit, freq, z0, spec):
    from .stable_network import simulate_stable as simulate
    from .fitting import _measure_s
    return _measure_s(simulate(circuit, freq, z0), z0, np.asarray(freq, float), spec)[0]


def _scan_frequencies(source_network, reference, max_log_step=.004):
    lo, hi = map(float, reference['range_hz'])
    count = int(np.ceil(np.log(hi / lo) / max_log_step)) + 1
    # A bounded deterministic model scan plus all original measured samples.
    count = max(257, min(8193, count))
    extras = list(np.asarray(source_network.freq, float))
    for item in reference['branches'].values():
        if item.get('bracket_hz'):
            extras.extend(item['bracket_hz'])
            extras.append(item['estimate_hz'])
    grid = np.unique(np.concatenate((np.geomspace(lo, hi, count), np.asarray(extras))))
    return grid[(grid >= lo) & (grid <= hi)]


def evaluate_srf_network(candidate_network, source_network, spec, reference=None, srf_pct=5.0):
    """Conservative original-grid comparison, useful for native dense sweeps.

    Candidate roots stay bracketed too. Worst endpoint-to-endpoint relative
    error is used; candidate interpolation cannot turn uncertainty into PASS.
    """
    spec = _spec(spec)
    if reference is None:
        reference = build_reference(source_network, spec)
    candidate = build_reference(candidate_network, spec, max_bracket_relative_width=1.)
    output = _empty_metrics(reference, srf_pct)
    coverage=(float(candidate_network.freq[0]) <= float(reference['range_hz'][0])*(1+1e-10) and
              float(candidate_network.freq[-1]) >= float(reference['range_hz'][1])*(1-1e-10))
    output['candidate_covers_reference_range']=bool(coverage)
    for name, ref in reference['branches'].items():
        item = {'reference': ref, 'candidate': candidate['branches'][name], 'passed': False,
                'error_upper_pct': None, 'estimate_error_pct': None}
        fit = item['candidate']
        if coverage and ref['status'] == 'resolved' and fit['status'] == 'resolved':
            a, b = ref['bracket_hz']; c, d = fit['bracket_hz']
            upper = max(abs(c-a)/a, abs(c-b)/b, abs(d-a)/a, abs(d-b)/b) * 100
            item.update(error_upper_pct=float(upper),
                        estimate_error_pct=float(abs(fit['estimate_hz']/ref['estimate_hz']-1)*100),
                        passed=bool(upper <= float(srf_pct)), status='compared')
        else:
            item['status'] = ('candidate_range_incomplete' if not coverage else
                              'reference_unresolved' if ref['status'] != 'resolved' else 'model_root_not_captured')
        output['branches'][name] = item
    return _finish_metrics(output)


def _empty_metrics(reference, srf_pct):
    if not np.isfinite(float(srf_pct)) or float(srf_pct) <= 0:
        raise ValueError('SRF error percentage must be positive')
    return {'passed': False, 'score': 1e300, 'limit_pct': float(srf_pct), 'branches': {},
            'reference_resolved': bool(reference['resolved']),
            'definition': reference['definition'], 'range_hz': list(reference['range_hz']),
            'acceptance_basis': 'maximum relative error against both original EMX reference bracket endpoints',
            'first_root_scope': 'first detected crossing in original source samples and bounded deterministic model scan'}


def _finish_metrics(output):
    errors = [i['error_upper_pct'] for i in output['branches'].values()]
    output['passed'] = bool(errors and all(i['passed'] for i in output['branches'].values()))
    output['max_error_upper_pct'] = max(errors) if errors and all(v is not None for v in errors) else None
    output['score'] = output['max_error_upper_pct'] / output['limit_pct'] if output['max_error_upper_pct'] is not None else 1e300
    return output


def local_reference_indices(source_network, reference, relative_window=.03):
    """Original direct SNP samples within each first root's local window."""
    freq=np.asarray(source_network.freq,float)
    selected=np.zeros(len(freq),bool)
    for item in reference['branches'].values():
        if item.get('estimate_hz') is not None:
            center=float(item['estimate_hz'])
            selected |= (freq >= center*(1-relative_window)) & (freq <= center*(1+relative_window))
        if item.get('bracket_indices'):
            selected[np.asarray(item['bracket_indices'],int)]=True
    return np.flatnonzero(selected)


def local_network_guard(circuit, source_network, spec, reference=None, network_pct=5.):
    """Independent full complex-S replay on original near-SRF direct samples."""
    from .stable_network import simulate_stable as simulate
    spec=_spec(spec)
    if reference is None:
        reference=build_reference(source_network,spec)
    if not np.isfinite(float(network_pct)) or float(network_pct)<=0:
        raise ValueError('Local network error percentage must be positive')
    ix=local_reference_indices(source_network,reference)
    result={'passed':False,'score':1e300,'max_s_absolute_error':None,
            'limit_s_absolute_error':float(network_pct)/100,'sample_count':len(ix),
            'original_indices':ix.tolist(),'freq_hz':source_network.freq[ix].tolist(),
            'sample_basis':'original direct SNP samples within 3% of first SRF and its original bracket endpoints',
            'lq_relative_constraints_added':False}
    if len(ix)==0:
        result['status']='no_original_local_samples'
        return result
    sf=simulate(circuit,source_network.freq[ix],source_network.z0)
    delta=np.abs(sf-source_network.s[ix])
    worst=np.unravel_index(int(np.argmax(delta)),delta.shape)
    error=float(delta[worst])
    result.update(passed=bool(error<=float(network_pct)/100),
                  score=error/(float(network_pct)/100),max_s_absolute_error=error,
                  status='compared',worst_frequency_hz=float(source_network.freq[ix[worst[0]]]),
                  worst_matrix_entry_1based=[int(worst[1]+1),int(worst[2]+1)])
    return result


def _continuous_zero_evidence(x, resistance, log_radius):
    """Five nested samples distinguish a continuous zero from a pole/jump.

    For a continuous simple zero, reducing distance by four reduces |X| by
    approximately four. A pole grows inward; a discontinuous jump does not
    contract. The center need not be machine-zero in an ill-conditioned solve.
    """
    x=np.asarray(x,float); resistance=np.asarray(resistance,float)
    finite=bool(np.all(np.isfinite(x)) and np.all(np.isfinite(resistance)))
    outer=float(max(abs(x[0]),abs(x[4])))
    inner=float(max(abs(x[1]),abs(x[3])))
    slope=float((x[3]-x[1])/(log_radius/2))
    contraction=inner/max(outer,np.finfo(float).tiny)
    signs=bool(x[0]>0 and x[1]>0 and x[3]<0 and x[4]<0)
    passive=bool(np.min(resistance)>=-max(np.max(np.abs(resistance))*1e-9,1e-10))
    centered=bool(abs(x[2])<=inner*.5)
    valid=bool(finite and signs and passive and slope<0 and contraction<=.7 and centered)
    # A frequency uncertainty far below the user SRF limits is kept explicitly
    # in the model root bracket rather than requiring an unrealistically tiny X.
    guard=max(1e-6,2*abs(float(x[2]))/max(abs(slope),np.finfo(float).tiny))
    valid=bool(valid and guard<=1e-5)
    return {'passed':valid,'log_radius':float(log_radius),
            'probe_x_ohm':x.tolist(),'probe_r_ohm':resistance.tolist(),
            'inner_to_outer_amplitude_ratio':float(contraction),
            'center_to_inner_amplitude_ratio':float(abs(x[2])/max(inner,np.finfo(float).tiny)),
            'slope_ohm_per_log_hz':slope,'numerical_log_frequency_guard':float(guard),
            'test':'nested-distance amplitude contracts toward zero; pole/jump rejected'}


def evaluate_srf(circuit, source_network, spec, reference=None, srf_pct=5.0):
    """Replay a circuit independently; scan the full source range then bisect.

    A local zero at the desired frequency cannot replace an earlier model zero.
    Interpolation of the source never narrows its acceptance bracket.
    """
    spec = _spec(spec)
    if reference is None:
        reference = build_reference(source_network, spec)
    output = _empty_metrics(reference, srf_pct)
    grid = _scan_frequencies(source_network, reference)
    output['model_scan_points'] = len(grid)
    output['maximum_model_scan_log_step'] = float(np.max(np.diff(np.log(grid))))
    curves = _model_curves(circuit, grid, source_network.z0, spec)
    for name, ref in reference['branches'].items():
        item = {'reference': ref, 'passed': False, 'error_upper_pct': None,
                'estimate_error_pct': None, 'model_hz': None, 'model_bracket_hz': None}
        x = np.asarray(curves[name]['x_ohm'])
        crosses = _crossings(grid, x)
        item['model_crossings_observed'] = len(crosses)
        if not crosses:
            item['status'] = 'model_root_not_captured'
        else:
            index = crosses[0]
            a, b = float(grid[index]), float(grid[index + 1])
            initial = [a, b]
            # Independent nodal simulation at each trial; not optimizer sensitivities.
            for _ in range(38):
                mid = np.sqrt(a*b)
                value = float(_model_curves(circuit, [mid], source_network.z0, spec)[name]['x_ohm'][0])
                if not np.isfinite(value):
                    break
                if value > 0:
                    a = mid
                else:
                    b = mid
                if b/a - 1 <= 1e-6:
                    break
            root = np.sqrt(a*b)
            continuity=[]
            zero_ok=False
            for radius in (1e-3,1e-4,1e-5):
                offsets=np.asarray([-radius,-radius/4,0,radius/4,radius])
                local=_model_curves(circuit,root*np.exp(offsets),source_network.z0,spec)[name]
                evidence=_continuous_zero_evidence(local['x_ohm'],local['r_ohm'],radius)
                continuity.append(evidence)
                if evidence['passed']:
                    zero_ok=True
                    break
            slope=evidence['slope_ohm_per_log_hz']
            center_x=float(local['x_ohm'][2])
            numerical_guard=evidence['numerical_log_frequency_guard']
            if zero_ok:
                # Retain the bisection uncertainty and observed numerical
                # centering uncertainty in the conservative acceptance bound.
                a=float(a*np.exp(-numerical_guard))
                b=float(b*np.exp(numerical_guard))
            item.update(model_hz=float(root),model_bracket_hz=[float(a),float(b)],
                        initial_model_bracket_hz=initial,model_slope_ohm_per_log_hz=slope,
                        model_root_residual_ohm=center_x,continuity_checks=continuity,
                        model_root_relative_resolution=float(b/a-1),
                        model_numeric_guard_log=float(numerical_guard))
            if not zero_ok:
                item['status'] = 'model_crossing_not_continuous_zero'
            elif ref['status'] != 'resolved':
                item['status'] = 'reference_unresolved'
            else:
                ra, rb = ref['bracket_hz']
                upper = max(abs(a-ra)/ra, abs(a-rb)/rb, abs(b-ra)/ra, abs(b-rb)/rb)*100
                item.update(status='compared', error_upper_pct=float(upper),
                            estimate_error_pct=float(abs(root/ref['estimate_hz']-1)*100),
                            passed=bool(upper <= float(srf_pct)))
        output['branches'][name] = item
    return _finish_metrics(output)


def selfcheck():
    """Handwritten lossy RLC: exact root, no root, width and shifted-root guards."""
    from types import SimpleNamespace
    from .stable_network import simulate_stable as simulate
    from .network import Network
    spec = SimpleNamespace(id='handwritten_se', device='inductor', mode='se',
                           port_roles=('P','N'), tap_roles=(), winding_pairs=(('P','N'),))
    # (R+jwL) || C. Its reactance root is sqrt(1/(LC)-(R/L)^2).
    base = {'name':'manual', 'ports':['P','N'], 'reference':'REF', 'elements':[
        {'kind':'res','name':'r','n1':'P','n2':'rnode','value':2.},
        {'kind':'ind','name':'l','n1':'rnode','n2':'N','value':1e-9},
        {'kind':'cap','name':'c','n1':'P','n2':'N','value':1e-13},
        {'kind':'res','name':'rg','n1':'N','n2':'REF','value':1e8}]}
    exact = np.sqrt(1/(1e-9*1e-13)-(2/1e-9)**2)/(2*np.pi)
    f = np.unique(np.r_[np.geomspace(1e9,5e10,80), exact*np.array([.999,.9999,1.0001,1.001])])
    net = Network(f,simulate(base,f,50),50)
    ref = build_reference(net,spec)
    measured = evaluate_srf(base,net,spec,ref,srf_pct=.1)
    assert measured['passed'] and abs(measured['branches']['winding']['model_hz']/exact-1)<1e-6
    import copy
    shifted = copy.deepcopy(base); shifted['elements'][2]['value'] *= .8
    mismatch = evaluate_srf(shifted,net,spec,ref,srf_pct=2)
    assert not mismatch['passed'] and mismatch['max_error_upper_pct']>10
    coarse_f = np.array([1e9,10e9,40e9,50e9])
    coarse = Network(coarse_f,simulate(base,coarse_f,50),50)
    assert not build_reference(coarse,spec)['resolved']
    low_f = np.array([1e9,2e9,3e9])
    low = Network(low_f,simulate(base,low_f,50),50)
    assert build_reference(low,spec)['branches']['winding']['status']=='not_captured'
    # A later zero cannot replace the first observed crossing.
    assert _crossings(np.arange(6)+1,np.array([1,-1,1,2,-1,-2]))==[0,3]
    # Two series-connected handwritten parallel RL-C resonators have two
    # positive-to-negative crossings; the later resonator must not be selected.
    multi = {'name':'manual_two', 'ports':['P','N'], 'reference':'REF', 'elements':[
        {'kind':'res','name':'r1','n1':'P','n2':'ra','value':1.},
        {'kind':'ind','name':'l1','n1':'ra','n2':'M','value':1e-9},
        {'kind':'cap','name':'c1','n1':'P','n2':'M','value':2.5e-13},
        {'kind':'res','name':'r2','n1':'M','n2':'rb','value':1.},
        {'kind':'ind','name':'l2','n1':'rb','n2':'N','value':1e-9},
        {'kind':'cap','name':'c2','n1':'M','n2':'N','value':2.5e-14},
        {'kind':'res','name':'rg','n1':'N','n2':'REF','value':1e8}]}
    mf = np.geomspace(1e9,60e9,3000)
    mn = Network(mf,simulate(multi,mf,50),50)
    mr = build_reference(mn,spec)
    mm = evaluate_srf(multi,mn,spec,mr,2)
    assert mr['branches']['winding']['crossings_observed']==2
    assert mm['passed'] and mm['branches']['winding']['model_hz']<15e9
    # Independent closed-form series sum, evaluated at the nodal root.
    w = 2*np.pi*mm['branches']['winding']['model_hz']
    z1 = 1/(1/(1+1j*w*1e-9)+1j*w*2.5e-13)
    z2 = 1/(1/(1+1j*w*1e-9)+1j*w*2.5e-14)
    assert abs((z1+z2).imag)<1.
    # Analytic poles and jumps retain opposite signs but must not pass as zeros.
    offsets=np.asarray([-1e-3,-2.5e-4,1e-8,2.5e-4,1e-3])
    pole=-1/offsets
    assert not _continuous_zero_evidence(pole,np.ones(5),1e-3)['passed']
    jump=np.asarray([1.,1.,0.,-1.,-1.])
    assert not _continuous_zero_evidence(jump,np.ones(5),1e-3)['passed']
    noisy=np.asarray([10.,2.5,-.0011,-2.5,-10.])
    assert _continuous_zero_evidence(noisy,np.ones(5)*900,1e-3)['passed']
    return {'passed':True,'multiple_resonance_first_hz':mm['branches']['winding']['model_hz'],
            'analytic_root_hz':float(exact),'model_root_hz':measured['branches']['winding']['model_hz'],
            'shifted_error_upper_pct':mismatch['max_error_upper_pct'],
            'checks':['analytic_lossy_parallel_RL_C','shifted_root_rejected','coarse_reference_rejected',
                      'uncaptured_reference_rejected','first_of_multiple_crossings_selected',
                      'two_handwritten_resonators_independent_complex_impedance',
                      'analytic_pole_and_discontinuous_jump_rejected',
                      'continuous_zero_with_observed_numerical_noise_accepted']}
