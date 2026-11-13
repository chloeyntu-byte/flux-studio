"""Bounded SRF-first parameter refinement with unchanged L/Q/S/mutual guards.

No devices, external optimizers or ModelGen models are introduced. A previously
passing L/Q/network circuit is retained unless a replacement still passes.
"""
from __future__ import annotations

import copy
import time
import numpy as np


def _preference(lq, srf):
    return (not bool(lq['passed']),
            float(lq['score']) if not lq['passed'] else float(srf['score']),
            float(srf['score']) if not lq['passed'] else float(lq['score']))


def polish(circuit, lq_network, srf_network, spec, limits, srf_pct, timeout_s,
           cancel=None, progress=None):
    """Return (circuit, original-LQ metrics, conservative SRF metrics, receipt).

    The reference must contain sufficiently narrow ORIGINAL EMX brackets.
    Reference-center signed Q is a differentiable frequency-shift surrogate;
    only independent complete-range first-root replay can accept a candidate.
    """
    from .stable_network import StableSensitivityNetwork as SensitivityNetwork, evaluate_stable as evaluate
    from .fitting import _measure_s
    from .resonance import build_reference, evaluate_srf, local_reference_indices, local_network_guard
    from .models import get_model

    if isinstance(spec, str):
        spec = get_model(spec)
    if not np.isfinite(float(srf_pct)) or float(srf_pct) <= 0:
        raise ValueError('SRF error percentage must be positive')
    started = time.monotonic()
    original = copy.deepcopy(circuit)
    reference = build_reference(srf_network, spec)
    original_lq = evaluate(original, lq_network, spec, limits)
    def assess_srf(cc):
        sr=evaluate_srf(cc,srf_network,spec,reference,srf_pct)
        local=local_network_guard(cc,srf_network,spec,reference,limits.get('network_pct',min(limits['l_pct'],limits['q_pct'])))
        sr['root_passed']=sr['passed']
        sr['local_network']=local
        sr['passed']=bool(sr['passed'] and local['passed'])
        sr['score']=max(float(sr['score']),float(local['score']))
        return sr

    original_srf = assess_srf(original)
    best, best_lq, best_srf = original, original_lq, original_srf
    receipt = {'algorithm':'bounded_srf_first_with_original_lq_network_mutual_guards',
               'network_solver':'complete_RLC_branch_current_loaded_port_MNA',
               'strategy':'srf_priority', 'timeout_s':float(timeout_s),
               'max_iterations':240, 'added_elements':0, 'external_tools':[],
               'root_surrogate':'signed Q at reference center and feasible window bounds; zero equals Im(Z)=0 for positive R',
               'initial_lq_score':float(original_lq['score']),
               'initial_srf_score':float(original_srf['score']),
               'reference':reference, 'independent_candidate_evaluations':1,
               'local_network_guard':original_srf['local_network']}
    iterations = 0
    accepted_steps = 0
    pending = None
    best_proxy = None

    def cancelled():
        return cancel is not None and cancel.is_set()

    def stopped():
        return cancelled() or time.monotonic()-started >= max(0,float(timeout_s))

    def finish(state):
        nonlocal best_lq
        if cancelled():
            state = 'cancelled'
            best_lq = copy.deepcopy(best_lq)
            best_lq['passed'] = False
        receipt.update(state=state, iterations=iterations, accepted_steps=accepted_steps,
                       elapsed_s=time.monotonic()-started,
                       final_lq_score=float(best_lq['score']), final_srf_score=float(best_srf['score']),
                       joint_passed=bool(best_lq['passed'] and best_srf['passed']),
                       final_local_network_guard=best_srf['local_network'])
        return best, best_lq, best_srf, receipt

    if cancelled():
        return finish('cancelled')
    if stopped():
        return finish('budget_exhausted')
    if not reference['resolved']:
        return finish('reference_unresolved')
    # The uncertainty of the raw reference alone may exceed the requested limit.
    tolerance = float(srf_pct)/100
    for item in reference['branches'].values():
        a,b = item['bracket_hz']
        if (1-tolerance)*b > (1+tolerance)*a:
            return finish('reference_uncertainty_exceeds_target')
    if best_lq['passed'] and best_srf['passed']:
        return finish('already_accepted')
    if progress:
        progress('自谐振优先：保留原始 L/Q/S/互阻抗门限，独立检查首个过零')

    source, source_z = _measure_s(lq_network.s,lq_network.z0,lq_network.freq,spec)
    nf = len(lq_network.freq)
    ls = float(limits['l_pct'])/100
    qs = float(limits['q_pct'])/100
    ss = float(limits.get('network_pct',min(limits['l_pct'],limits['q_pct'])))/100
    ms = min(ls,qs)
    local_ix=local_reference_indices(srf_network,reference)
    local_freq=np.asarray(srf_network.freq,float)[local_ix]
    allfreq = list(np.asarray(lq_network.freq,float)) + list(local_freq)
    points = []
    for name,item in reference['branches'].items():
        a,b = item['bracket_hz']
        center = float(item['estimate_hz'])
        lower,upper = (1-tolerance)*b,(1+tolerance)*a
        points.append((name,center,lower,upper,abs(float(item['slope_q_per_log_hz']))))
        allfreq.extend((center,lower,upper))
        allfreq.extend(item['bracket_hz'])
    freq = np.unique(np.asarray(allfreq,float))
    guard_ix = np.searchsorted(freq,lq_network.freq)
    local_graph_ix=np.searchsorted(freq,local_freq)
    center_ix = {name:np.searchsorted(freq,[center,lower,upper]) for name,center,lower,upper,_ in points}
    lden,qden = {},{}
    for name,curves in source.items():
        for key,destination in [('l_h',lden),('q',qden)]:
            v = curves[key]
            destination[name] = np.maximum(np.abs(v),max(float(np.max(np.abs(v)))*1e-6,np.finfo(float).tiny))

    def blocks(s_batch):
        sf = np.asarray(s_batch)
        count = len(sf)
        curves,z = _measure_s(sf.reshape(count*len(freq),lq_network.nports,lq_network.nports),
                             lq_network.z0,np.tile(freq,count),spec)
        guards = []
        srf_terms = []
        for name in source:
            for key,den,scale in [('l_h',lden[name],ls),('q',qden[name],qs)]:
                values = curves[name][key].reshape(count,len(freq))[:,guard_ix]
                guards.append((values-source[name][key][None])/den[None]/scale)
        sdelta = (sf[:,guard_ix]-lq_network.s[None]).reshape(count,-1)/ss
        local_sdelta=(sf[:,local_graph_ix]-srf_network.s[local_ix][None]).reshape(count,-1)/ss
        complex_guards = [sdelta,local_sdelta]
        if source_z.shape[-1]==2:
            zz = z.reshape(count,len(freq),2,2)[:,guard_ix]
            for a,b in ((0,1),(1,0)):
                v=source_z[:,a,b]
                den=np.maximum(np.abs(v),max(float(np.max(np.abs(v)))*1e-6,np.finfo(float).tiny))*ms
                complex_guards.append((zz[:,:,a,b]-v[None])/den[None])
        realguards=np.concatenate(guards,axis=1)
        cg=np.concatenate(complex_guards,axis=1)
        peakguard=np.maximum(np.max(np.abs(realguards),axis=1),np.max(np.abs(cg),axis=1))
        for name,center,lower,upper,slope in points:
            xx=curves[name]['q'].reshape(count,len(freq))[:,center_ix[name]]
            denom=max(slope*tolerance,1e-12)
            # The center residual is approximately log(f_root/f_ref)/tolerance.
            srf_terms.append(xx[:,0:1]/denom)
            # Bound signs help keep the root in the conservative feasible window.
            srf_terms.append(np.maximum(-xx[:,1:2],0)/denom)
            srf_terms.append(np.maximum(xx[:,2:3],0)/denom)
        sr=np.concatenate(srf_terms,axis=1)
        if not all(np.all(np.isfinite(v)) for v in (realguards,cg,peakguard,sr)):
            raise ValueError('Nonfinite SRF-priority residual')
        return sr,realguards,cg,peakguard

    def residual(s_batch,factor):
        sr,rg,cg,_=blocks(s_batch)
        # 3% internal headroom helps retain exact hard guards in native replay.
        realpen=np.sign(rg)*np.maximum(np.abs(rg)-.97,0)*60
        mag=np.abs(cg)
        cpen=cg/np.maximum(mag,1e-30)*np.maximum(mag-.97,0)*60
        return np.concatenate((sr/factor,realpen,cpen.real,cpen.imag),axis=1)

    def proxy_key(sf):
        sr,_,_,guard=blocks(sf[None])
        g=float(guard[0]); peak=float(np.max(np.abs(sr)))
        return (g>1,g if g>1 else peak,peak if g>1 else g),g

    def consider(cc):
        nonlocal best,best_lq,best_srf
        ll=evaluate(cc,lq_network,spec,limits)
        # A feasible input is protected before paying for independent root replay.
        if best_lq['passed'] and not ll['passed']:
            return
        sr=assess_srf(cc)
        receipt['independent_candidate_evaluations']+=1
        if _preference(ll,sr)<_preference(best_lq,best_srf):
            best,best_lq,best_srf=cc,ll,sr

    stages=[]
    try:
        for factor,maxiter in ((1.,100),(.3,80),(.1,60)):
            if stopped() or (best_lq['passed'] and best_srf['passed']):
                break
            graph=SensitivityNetwork(best,freq)
            x=np.zeros(graph.ng)
            damping=1e-2
            stage_count=0
            best_proxy,_=proxy_key(graph.s(x,lq_network.z0))
            pending=None
            for _ in range(maxiter):
                if stopped():
                    break
                iterations+=1;stage_count+=1
                sf,ds=graph.s(x,lq_network.z0,True)
                r=residual(sf[None],factor)[0]
                eps=1e-5
                jac=((residual(sf[None]+eps*ds,factor)-r[None])/eps).T
                normal=jac.T@jac
                gradient=jac.T@r
                diagonal=np.maximum(np.diag(normal),1e-12)
                cost=float(r@r)
                _,old_guard=proxy_key(sf)
                accepted=False
                for _attempt in range(7):
                    if stopped():
                        break
                    try:
                        step=-np.linalg.solve(normal+damping*np.diag(diagonal),gradient)
                    except np.linalg.LinAlgError:
                        damping*=5;continue
                    step=np.clip(step,-.35,.35)
                    for back in range(5):
                        if stopped():
                            break
                        trial=x+step*(.5**back)
                        try:
                            ssf=graph.s(trial,lq_network.z0)
                            key,new_guard=proxy_key(ssf)
                            if old_guard<=1 and new_guard>1:
                                continue
                            nr=residual(ssf[None],factor)[0]
                            if float(nr@nr)>=cost:
                                continue
                            x=trial;accepted=True;accepted_steps+=1
                            damping=max(1e-9,damping/3)
                            if key<best_proxy:
                                pending=graph.circuit(x)
                                best_proxy=key
                            if pending is not None and accepted_steps%5==0:
                                consider(pending);pending=None
                            break
                        except (ValueError,np.linalg.LinAlgError,FloatingPointError):
                            continue
                    if accepted:
                        break
                    damping*=5
                if not accepted or (best_lq['passed'] and best_srf['passed']):
                    break
            if pending is not None and not cancelled():
                consider(pending);pending=None
            stages.append({'target_factor':factor,'iterations':stage_count,
                           'best_lq_score':float(best_lq['score']),'best_srf_score':float(best_srf['score'])})
    except (ValueError,np.linalg.LinAlgError,FloatingPointError) as error:
        receipt['numerical_error']=str(error)
    receipt['stages']=stages
    return finish('budget_exhausted' if stopped() and not cancelled() else 'completed')


def selfcheck():
    """Exercise real refinement, guard preservation, cancellation and budgets."""
    import threading
    from .models import get_model
    from .network import Network
    from .circuit import simulate, component_counts
    spec=get_model('inductor_se_gnd')
    base={'name':'manual','ports':['P','N'],'reference':'REF','elements':[
        {'kind':'res','name':'r','n1':'P','n2':'rnode','value':2.},
        {'kind':'ind','name':'l','n1':'rnode','n2':'N','value':1e-9},
        {'kind':'cap','name':'c','n1':'P','n2':'N','value':1e-13},
        {'kind':'res','name':'rg','n1':'N','n2':'REF','value':1e8}]}
    exact=np.sqrt(1/(1e-9*1e-13)-(2/1e-9)**2)/(2*np.pi)
    f=np.unique(np.r_[np.geomspace(1e9,5e10,80),exact*np.array([.999,.9999,1.0001,1.001])])
    srf=Network(f,simulate(base,f,50),50)
    lf=np.geomspace(1e9,5e9,10)
    lq=Network(lf,simulate(base,lf,50),50)
    initial=copy.deepcopy(base)
    initial['elements'][2]['value']*=.9
    limits={'l_pct':5,'q_pct':20,'network_pct':5}
    cc,ll,sr,rec=polish(initial,lq,srf,spec,limits,2,5)
    assert ll['passed'] and sr['passed'] and rec['iterations']>0
    assert sr['local_network']['passed'] and sr['local_network']['sample_count']>=2
    assert not sr['local_network']['lq_relative_constraints_added']
    assert rec['initial_lq_score']<1 and rec['initial_srf_score']>1
    assert component_counts(cc)==component_counts(initial)
    assert [(e['kind'],e.get('n1'),e.get('n2')) for e in cc['elements']]==[(e['kind'],e.get('n1'),e.get('n2')) for e in initial['elements']]
    event=threading.Event();event.set()
    cancelled=polish(initial,lq,srf,spec,limits,2,5,cancel=event)
    assert cancelled[3]['state']=='cancelled' and cancelled[3]['iterations']==0
    assert not cancelled[1]['passed']
    tiny=polish(initial,lq,srf,spec,limits,2,1e-6)
    assert tiny[3]['state']=='budget_exhausted' and tiny[3]['iterations']==0
    exact_result=polish(base,lq,srf,spec,limits,2,5)
    assert exact_result[3]['state']=='already_accepted' and exact_result[3]['iterations']==0
    return {'passed':True,'initial_srf_error_upper_pct':rec['initial_srf_score']*2,
            'final_srf_error_upper_pct':sr['max_error_upper_pct'],'final_lq_score':ll['score'],
            'iterations':rec['iterations'], 'checks':['real_handwritten_RLC_refinement',
            'passing_LQ_guards_preserved','original_direct_local_S_guard',
            'no_added_devices_or_connectivity_changes',
            'pre_cancelled','finite_tiny_budget','already_passing_preserved']}
