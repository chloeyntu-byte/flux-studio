"""Quick original-point L/Q qualification for Flux; strict Studio stays unchanged.

Fitting requires at least two actual positive frequencies. Sparse sources establish only behaviour at supplied positive samples. No sample
is duplicated/interpolated and no SRF or broadband acceptance is implied.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import time
import numpy as np
from .network import Network, read_touchstone, s_to_y, y_to_s, write_touchstone
from .models import get_model
from .topology import build_candidate
from .circuit import validate, component_counts, write_spice
from .fitting import _measure_y
from .circuit import y_from_s
from .stable_network import simulate_stable, StableSensitivityNetwork
from .optimizer import optimize
from .coupling import coupling_values

SCOPE = 'original_sampled_points_only'


class SampledNetwork(Network):
    """Network with explicit >=1-point scope; no relaxed global default."""
    def __post_init__(self):
        self.freq=np.asarray(self.freq,float);self.s=np.asarray(self.s,complex)
        if self.s.ndim!=3 or self.s.shape[1]!=self.s.shape[2] or self.s.shape[0]!=len(self.freq) or len(self.freq)<1:
            raise ValueError('At least one complete original frequency sample is required')
        self.z0=np.broadcast_to(np.asarray(self.z0,float),(self.s.shape[1],)).copy()
        if (np.any(~np.isfinite(self.freq)) or np.any(self.freq<0) or np.any(np.diff(self.freq)<=0)
                or np.any(~np.isfinite(self.s)) or np.any(~np.isfinite(self.z0)) or np.any(self.z0<=0)):
            raise ValueError('Invalid sampled network frequencies, S data or references')
        if not self.port_names:self.port_names=['P%d'%(i+1) for i in range(self.nports)]
        if len(self.port_names)!=self.nports:raise ValueError('Port-name count does not match data')

    def mapped(self, order, *, short_unused=False):
        if len(set(order))!=len(order) or any(type(i)!=int or not 0<=i<self.nports for i in order):
            raise ValueError('Distinct valid port indices are required')
        omitted=set(range(self.nports))-set(order)
        if omitted and not short_unused:raise ValueError('Unused port shorting requires explicit confirmation')
        s=y_to_s(s_to_y(self.s,self.z0)[:,order][:,:,order],self.z0[order]) if omitted else self.s[:,order][:,:,order]
        return SampledNetwork(self.freq.copy(),s,self.z0[order], [self.port_names[i] for i in order],
                              dict(self.metadata,port_order_1based=[i+1 for i in order],shorted_ports_1based=[i+1 for i in sorted(omitted)]))

    def band(self, low, high):
        if not (math.isfinite(low) and math.isfinite(high) and 0<low<=high):raise ValueError('Invalid sampled band')
        if low<self.freq[0]*(1-1e-12) or high>self.freq[-1]*(1+1e-12):raise ValueError('Band extends outside actual samples')
        selected=(self.freq>0)&(self.freq>=low*(1-1e-12))&(self.freq<=high*(1+1e-12))
        if not np.any(selected):raise ValueError('No original positive samples in band')
        return SampledNetwork(self.freq[selected],self.s[selected],self.z0,list(self.port_names),dict(self.metadata))


def _clean(x):
    if isinstance(x,np.ndarray):return _clean(x.tolist())
    if isinstance(x,np.generic):return _clean(x.item())
    if isinstance(x,dict):return {str(k):_clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)):return [_clean(v) for v in x]
    if isinstance(x,float) and not math.isfinite(x):return None
    return x


def _source(config):
    path=Path(config['source_path']).expanduser().resolve(strict=True)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if config.get('source_sha256') and config['source_sha256']!=digest:raise ValueError('Source SNP changed')
    spec=get_model(config['model_id']);net=read_touchstone(path,network_class=SampledNetwork)
    order=config.get('port_order')
    if isinstance(order,str):order=[int(v.strip()) for v in order.split(',') if v.strip()]
    if not config.get('ports_confirmed') or not isinstance(order,(list,tuple)) or len(order)!=spec.nports or any(type(i)!=int for i in order):
        raise ValueError('Confirm the full model port mapping')
    net=net.mapped([i-1 for i in order],short_unused=config.get('short_unused',False))
    positive=net.freq>0
    if not np.any(positive):raise ValueError('At least one positive original sample is needed')
    net=SampledNetwork(net.freq[positive],net.s[positive],net.z0,list(net.port_names),dict(net.metadata))
    if len(net.freq)<2:
        raise ValueError('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.')
    # Optional explicit band is clipped nowhere; defaults cover exactly the data.
    lo=float(config.get('fmin_ghz',net.freq[0]/1e9))*1e9
    hi=float(config.get('fmax_ghz',net.freq[-1]/1e9))*1e9
    net=net.band(lo,hi)
    if len(net.freq)<2:
        raise ValueError('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.')
    return spec,net,{'path':str(path),'sha256':digest,'point_count':len(net.freq),'nports':net.nports,
                     'port_order':list(order),'sampled_band_hz':[float(net.freq[0]),float(net.freq[-1])],
                     'mapped_port_roles':list(spec.port_roles),'short_unused':bool(config.get('short_unused',False))}


def _measure_s(s,z0,freq,spec):
    # Flux differential impedances leave all other signals/TAPs open. This
    # uses winding-current incidence and a validated singular-aware Y solve,
    # unlike Studio's original matched-common-mode DM fixture.
    return _measure_y(y_from_s(np.asarray(s),z0),np.asarray(freq),spec)


def _k(s,z0,freq,spec):
    unused,z=_measure_s(s,z0,freq,spec)
    x1,x2=z[:,0,0].imag,z[:,1,1].imag
    with np.errstate(divide='ignore',invalid='ignore'):
        return np.where((x1>0)&(x2>0),z[:,0,1].imag/np.sqrt(x1*x2),np.nan)


def assess_s(sf,network,spec,limits):
    src,_=_measure_s(network.s,network.z0,network.freq,spec)
    fit,_=_measure_s(np.asarray(sf),network.z0,network.freq,spec)
    branches={};scores=[];max_l=max_q=0.
    for name,original in src.items():
        metrics={}
        for key,limit,label in [('l_h',float(limits['l_pct']),'l'),('q',float(limits['q_pct']),'q')]:
            a=np.asarray(original[key]);b=np.asarray(fit[name][key])
            if np.any(~np.isfinite(a)) or np.any(~np.isfinite(b)):raise ValueError('Nonfinite source or candidate L/Q')
            floor=max(float(np.max(np.abs(a)))*1e-8,1e-30)
            error=float(np.max(np.abs(b-a)/np.maximum(np.abs(a),floor))*100)
            metrics[label+'_error_pct']=error;scores.append(error/limit)
            if label=='l':max_l=max(max_l,error)
            else:max_q=max(max_q,error)
        branches[name]=metrics
    return dict(passed=bool(scores and max(scores)<=1),score=max(scores) if scores else 1e99,
                l_error_pct=max_l,q_error_pct=max_q,branches=branches,
                max_s_absolute_error=float(np.max(np.abs(sf-network.s))),acceptance_scope=SCOPE,
                l_limit_pct=float(limits['l_pct']),q_limit_pct=float(limits['q_pct']),
                srf_status='not_qualified_by_sampled_point_profile',broadband_qualified=False)


def _evaluate(circuit,network,spec,limits):
    return assess_s(simulate_stable(circuit,network.freq,network.z0),network,spec,limits)


def _curves(circuit,net,spec,fit_s=None):
    sf=simulate_stable(circuit,net.freq,net.z0) if fit_s is None else np.asarray(fit_s)
    original,_=_measure_s(net.s,net.z0,net.freq,spec);fitted,_=_measure_s(sf,net.z0,net.freq,spec)
    branches={};comparison={'frequency_hz':net.freq.tolist(),'original':{},'fitted':{}}
    for name,a in original.items():
        b=fitted[name];suffix='' if name=='winding' else 'p' if name=='primary' else 's'
        for key,scale,prefix in [('l_h',1e9,'L'),('q',1.,'Q')]:
            comparison['original'][prefix+suffix]=(a[key]*scale).tolist()
            comparison['fitted'][prefix+suffix]=(b[key]*scale).tolist()
        branches[name]={'source_l_h':a['l_h'].tolist(),'source_q':a['q'].tolist(),'source_r_ohm':a['r_ohm'].tolist(),
                        'fit_l_h':b['l_h'].tolist(),'fit_q':b['q'].tolist(),'fit_r_ohm':b['r_ohm'].tolist()}
    source_k=fit_k=None
    if spec.device=='transformer':
        source_k=_k(net.s,net.z0,net.freq,spec).tolist()
        fit_k=_k(sf,net.z0,net.freq,spec).tolist()
        comparison['original']['k']=source_k;comparison['fitted']['k']=fit_k
    curves={'freq_hz':net.freq.tolist(),'source':original,'fitted':fitted,'branches':branches,
            'source_k':source_k,'fit_k':fit_k,'fitted_k':fit_k,'sample_basis':'original positive SNP samples only; no interpolation',
            'source_srf':{'resolved':False,'status':'not_qualified_by_sampled_point_profile'},
            'fitted_srf':{'resolved':False,'status':'not_qualified_by_sampled_point_profile'},
            'k_note':'Signed k is reported; not a separate sampled quick acceptance target.'}
    return _clean(curves),_clean(comparison)


def fit_sampled(config,output_dir,cancel_event=None,progress=None):
    started=time.monotonic();cfg=copy.deepcopy(config)
    spec,net,source=_source(cfg)
    limits={'l_pct':float(cfg.get('l_pct',5)),'q_pct':float(cfg.get('q_pct',20)),'network_pct':5.}
    if not (5<=limits['l_pct']<=20 and 5<=limits['q_pct']<=20):raise ValueError('L/Q limits must be 5..20 percent')
    budget=float(cfg.get('quick_seconds',5));cap=int(cfg.get('component_limit',20))
    if not 0<budget<=5 or cap not in (20,30):raise ValueError('Invalid quick budget/component limit')
    candidates=[x for x in spec.candidates if cfg.get('topology_id','auto') in ('','auto',x.id)]
    seeds=[];attempts=[]
    for candidate in candidates:
        if cancel_event is not None and cancel_event.is_set():raise InterruptedError('Fitting cancelled')
        if seeds and time.monotonic()-started>=budget:break
        try:
            circuit=build_candidate(net,spec,candidate,max_each=cap);validate(circuit,max_each=cap)
            metrics=_evaluate(circuit,net,spec,limits);seeds.append((metrics['score'],circuit,metrics))
            attempts.append({'candidate':candidate.id,'state':'seed','metrics':metrics})
            if metrics['passed']:break
        except (ValueError,np.linalg.LinAlgError,FloatingPointError) as exc:
            attempts.append({'candidate':candidate.id,'state':'rejected','reason':str(exc)})
    if not seeds:raise ValueError('No legal physical-family initial circuit: '+str(attempts))
    seeds.sort(key=lambda x:x[0]);_,circuit,metrics=seeds[0]
    if not metrics['passed']:
        if progress:progress('Quick sampled-point parameter fit; L 5%, Q 20% on actual source points.')
        for index, (_, seed, seed_metrics) in enumerate(seeds):
            remaining=budget-(time.monotonic()-started)
            if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()): break
            share=remaining/max(1,len(seeds)-index)
            candidate,measured,receipt=optimize(seed,net,spec,limits,share,
                                       cancel=cancel_event,progress=progress,network_class=StableSensitivityNetwork,evaluation_function=_evaluate,measurement_function=_measure_s)
            attempts.append({'state':'parameter_fit','candidate':seed.get('metadata',{}).get('candidate'),
                             'metrics':measured,'receipt':receipt})
            if measured['score'] < metrics['score']:
                circuit,metrics=candidate,measured
            if metrics['passed']:break
    if cancel_event is not None and cancel_event.is_set():raise InterruptedError('Fitting cancelled')
    metrics=_evaluate(circuit,net,spec,limits);curves,comparison=_curves(circuit,net,spec)
    circuit.setdefault('metadata',{}).update(acceptance_scope=SCOPE,broadband_qualified=False)
    # Only source and numeric configuration are published; native environment stays outside.
    safe={k:cfg[k] for k in ('model_id','port_order','ports_confirmed','short_unused','l_pct','q_pct','component_limit','quick_seconds','source_path','source_sha256','fmin_ghz','fmax_ghz') if k in cfg}
    safe.update(l_pct=limits['l_pct'],q_pct=limits['q_pct'])
    result=_clean(dict(schema='snpfit.sampled_fit.v1',status='SAMPLED_ACCEPTED' if metrics['passed'] else 'TARGETS_NOT_MET',
                       passed=metrics['passed'],acceptance_scope=SCOPE,broadband_qualified=False,circuit=circuit,config=safe,
                       source=source,model_id=spec.id,metrics=metrics,curves=curves,comparison=comparison,
                       frequencies_hz=net.freq.tolist(),z0=net.z0.tolist(),attempts=attempts,elapsed_s=time.monotonic()-started))
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    if (out/'sampled-fit.json').exists():raise FileExistsError('Sampled fit output already exists')
    (out/'sampled-fit.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    write_spice(circuit,out/'model.sp')
    write_touchstone(SampledNetwork(net.freq,simulate_stable(circuit,net.freq,net.z0),net.z0,list(spec.port_roles)),out/('fitted.s%dp'%net.nports))
    result['run_dir']=str(out);return result


def verify_native_sampled(snapshot,replay):
    """Independently qualify actual OA/Spectre response on original samples."""
    spec,net,source=_source(dict(snapshot['config'],source_sha256=snapshot['source']['sha256']))
    if isinstance(replay,(str,Path)):candidate=read_touchstone(replay,network_class=SampledNetwork)
    elif isinstance(replay,dict):
        value=replay.get('touchstone') or replay.get('snp') or replay.get('snp_path')
        if not value:raise ValueError('Replay receipt has no Touchstone path')
        candidate=read_touchstone(value,network_class=SampledNetwork)
    else:candidate=replay
    if candidate.nports!=net.nports or len(candidate.freq)!=len(net.freq) or not np.allclose(candidate.freq,net.freq,rtol=5e-7,atol=0):
        raise ValueError('Native replay frequencies or ports differ from the original samples')
    report=assess_s(candidate.s,net,spec,{'l_pct':snapshot['metrics']['l_limit_pct'],'q_pct':snapshot['metrics']['q_limit_pct']})
    curves,comparison=_curves(snapshot['circuit'],net,spec,fit_s=candidate.s)
    return dict(passed=report['passed'],metrics=report,curves=curves,comparison=comparison,acceptance_scope=SCOPE,broadband_qualified=False)
