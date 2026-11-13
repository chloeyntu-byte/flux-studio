"""Proved near-short oxide-capacitor contraction for this program's exporter.

Only the dedicated builder's coxN--substrateN--(rsubN||csubN) structure is
eligible. Values, magnetic coupling and user error limits are never adjusted.
Every accepted contraction removes one capacitor and one private internal node.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import numpy as np

from .circuit import component_counts,validate
from .models import get_model
from .resonance import build_reference,evaluate_srf,local_network_guard
from .stable_network import simulate_stable,evaluate_stable


def _signature(circuit):
    return hashlib.sha256(json.dumps(circuit,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _summary(metrics):
    out={k:metrics.get(k) for k in ('passed','score','max_error_upper_pct','max_s_absolute_error') if k in metrics}
    if 'branches' in metrics:
        out['branches']={}
        for name,branch in metrics['branches'].items():
            out['branches'][name]={k:branch.get(k) for k in ('passed','error_upper_pct','model_hz','status') if k in branch}
            for kind in ('l','q'):
                if kind in branch:
                    out['branches'][name][kind]={k:branch[kind].get(k) for k in ('pass','score','max_relative_pct','near_zero_pass')}
    return out


def _passed_checks(value,path=()):
    checks={}
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ('pass','passed','near_zero_pass') and isinstance(item,(bool,np.bool_)):
                checks[path+(key,)]=bool(item)
            elif isinstance(item,dict) and key not in ('curve','limits'):
                checks.update(_passed_checks(item,path+(key,)))
    return checks


def _contract(circuit,name):
    elements=circuit['elements'];cap=next(e for e in elements if e['name']==name)
    match=re.fullmatch(r'cox(\d+)',name)
    if not match or cap['kind']!='cap':
        return None,'not_a_generated_oxide_capacitor'
    suffix=match.group(1);internal='substrate'+suffix
    reference=circuit.get('reference','REF');ports=set(circuit['ports'])
    if internal in ports or internal==reference:
        return None,'internal_node_is_an_external_terminal'
    if cap.get('n1')==internal and cap.get('n2') in ports:
        external=cap['n2']
    elif cap.get('n2')==internal and cap.get('n1') in ports:
        external=cap['n1']
    else:
        return None,'oxide_capacitor_does_not_join_one_port_to_its_private_substrate'
    attached=[e for e in elements if e['kind']!='mind' and internal in (e.get('n1'),e.get('n2'))]
    expected={name,'rsub'+suffix,'csub'+suffix}
    if {e['name'] for e in attached}!=expected:
        return None,'private_substrate_has_other_connections'
    for element in attached:
        if element['name']==name:continue
        kind='res' if element['name']=='rsub'+suffix else 'cap'
        if element['kind']!=kind or {element['n1'],element['n2']}!={internal,reference}:
            return None,'substrate_return_is_not_the_generated_parallel_RC'
    result=copy.deepcopy(circuit)
    result['elements']=[e for e in result['elements'] if e['name']!=name]
    for element in result['elements']:
        if element['kind']=='mind':continue
        for key in ('n1','n2'):
            if element[key]==internal:element[key]=external
        # Never delete a winding, shorted coupled inductor, or any other device.
        if element['n1']==element['n2']:
            return None,'contraction_would_self_short_another_element'
    validate(result)
    return result,dict(removed_capacitor=name,removed_value_f=float(cap['value']),
                       merged_private_node=internal,retained_external_port=external,
                       removed_elements=1,removed_internal_nodes=1,magnetic_parameters_changed=False)


def condition_for_export(circuit,lq_network,srf_network,spec,limits,srf_pct=2.0,max_delta_s=1e-7,*,reference=None):
    """Return (new circuit, receipt), preserving every previously passing check.

    The proof is finite and specific to the actual supplied L/Q and SRF sample
    union, under both actual port-impedance normalizations. SRF is additionally
    independently scanned and bracketed, using the unchanged original data.
    An unproved or non-negligible candidate leaves the original graph intact.
    """
    if not np.isfinite(max_delta_s) or not 0<max_delta_s<=1e-7:
        raise ValueError('Export contraction requires 0 < max_delta_s <= 1e-7')
    spec=get_model(spec) if isinstance(spec,str) else spec
    original=copy.deepcopy(circuit);validate(original)
    receipt=dict(algorithm='proved_private_series_oxide_capacitor_wire_contraction_v1',
                 status='unchanged',applied=False,original_circuit_sha256=_signature(original),
                 maximum_allowed_complex_s_delta=float(max_delta_s),
                 original_counts=component_counts(original),attempts=[],
                 thresholds_changed=False,parameters_changed=False,added_gmin=0.,external_tools=[],
                 proof_scope='actual original L/Q and SRF frequency union; independent first-root and local-S revalidation')
    metadata=original.get('metadata',{})
    if metadata.get('backend')!='standalone_numpy' or not str(metadata.get('algorithm_version','')).startswith('standalone_physical_ladder'):
        receipt['reason']='not_this_programs_dedicated_physical_builder'
        receipt['final_counts']=component_counts(original)
        receipt['final_circuit_sha256']=receipt['original_circuit_sha256']
        return original,receipt
    frequencies=np.unique(np.r_[lq_network.freq,srf_network.freq])
    if len(frequencies)<3 or np.any(frequencies<=0) or not np.all(np.isfinite(frequencies)):
        raise ValueError('Original positive L/Q and SRF frequency samples are required')
    receipt.update(proof_frequency_count=len(frequencies),proof_frequency_hz=frequencies.tolist())
    normalizations=[]
    for network in (lq_network,srf_network):
        values=np.asarray(network.z0,float)
        if not any(np.array_equal(values,old) for old in normalizations):normalizations.append(values.copy())
    receipt['proof_reference_impedances_ohm']=[z.tolist() for z in normalizations]
    original_s=[simulate_stable(original,frequencies,z) for z in normalizations]
    if reference is None:reference=build_reference(srf_network,spec,max_bracket_relative_width=.006)
    if not reference.get('resolved'):
        receipt.update(reason='original_SRF_reference_unresolved',final_counts=component_counts(original),
                       final_circuit_sha256=receipt['original_circuit_sha256'])
        return original,receipt

    def checked(candidate):
        lq=evaluate_stable(candidate,lq_network,spec,limits)
        root=evaluate_srf(candidate,srf_network,spec,reference=reference,srf_pct=srf_pct)
        local=local_network_guard(candidate,srf_network,spec,reference,
                                  float(limits.get('network_pct',min(limits['l_pct'],limits['q_pct']))))
        return dict(lq=lq,srf=root,local_s=local)

    current=original;baseline=None;last_metrics=None;maximum_delta=0.
    names=[e['name'] for e in original['elements'] if e['kind']=='cap' and re.fullmatch(r'cox\d+',e['name'])]
    for name in names:
        record=dict(capacitor=name,accepted=False)
        receipt['attempts'].append(record)
        try:
            candidate,details=_contract(current,name)
            if candidate is None:
                record['reason']=details;continue
            record['contraction']=details
            errors=[float(np.max(np.abs(simulate_stable(candidate,frequencies,z)-expected)))
                    for z,expected in zip(normalizations,original_s)]
            change=max(errors);record.update(maximum_complex_s_delta_from_original=change,
                                             per_normalization_maximum_complex_s_delta=errors)
            if change>max_delta_s:
                record['reason']='original_sample_response_change_exceeds_bound';continue
            if baseline is None:
                baseline=checked(original)
                receipt['original_metrics']={key:_summary(value) for key,value in baseline.items()}
            metrics=checked(candidate)
            before_checks=_passed_checks(baseline);after_checks=_passed_checks(metrics)
            lost=['/'.join(path) for path,passed in before_checks.items() if passed and not after_checks.get(path,False)]
            record.update(metrics={key:_summary(value) for key,value in metrics.items()},
                          formerly_passing_checks_lost=lost)
            if lost:
                record['reason']='a_previously_passing_acceptance_check_would_fail';continue
            record.update(accepted=True,reason='negligible_original_sample_response_change_and_all_former_passes_preserved')
            current=candidate;last_metrics=metrics;maximum_delta=change
        except (ValueError,KeyError,np.linalg.LinAlgError,FloatingPointError,OverflowError) as error:
            record.update(reason='proof_failed_original_retained',error=str(error))
    receipt.update(applied=any(r['accepted'] for r in receipt['attempts']),
                   status='conditioned' if any(r['accepted'] for r in receipt['attempts']) else 'unchanged',
                   final_counts=component_counts(current),maximum_accepted_complex_s_delta_from_original=maximum_delta)
    if receipt['applied']:
        current.setdefault('metadata',{})['export_conditioning']={
            'algorithm':receipt['algorithm'],'source_circuit_sha256':receipt['original_circuit_sha256'],
            'maximum_complex_s_delta':maximum_delta,
            'removed_capacitors':[r['capacitor'] for r in receipt['attempts'] if r['accepted']],
            'proof_frequency_count':len(frequencies),'thresholds_changed':False}
    receipt['final_circuit_sha256']=_signature(current)
    if last_metrics is not None:receipt['final_metrics']={key:_summary(value) for key,value in last_metrics.items()}
    return current,receipt


def selfcheck():
    """Lossy handwritten RF circuit; short limit, real cap and graph guards."""
    from .network import Network
    base={'name':'manual_export','ports':['P','N'],'reference':'REF',
          'metadata':{'backend':'standalone_numpy','algorithm_version':'standalone_physical_ladder_selfcheck'},
          'elements':[
        {'kind':'res','name':'r','n1':'P','n2':'M','value':2.},
        {'kind':'ind','name':'l','n1':'M','n2':'N','value':1e-9},
        {'kind':'cap','name':'c','n1':'P','n2':'N','value':1e-13},
        {'kind':'res','name':'rg','n1':'N','n2':'REF','value':1e8},
        {'kind':'cap','name':'cox0','n1':'P','n2':'substrate0','value':.1},
        {'kind':'res','name':'rsub0','n1':'substrate0','n2':'REF','value':1e4},
        {'kind':'cap','name':'csub0','n1':'substrate0','n2':'REF','value':1e-14}]}
    spec=get_model('inductor_se_gnd');limits=dict(l_pct=5.,q_pct=20.,network_pct=5.)
    f=np.geomspace(1e9,50e9,801);lf=np.geomspace(1e9,50e9,20)
    source=Network(f,simulate_stable(base,f,50),50);lq=Network(lf,simulate_stable(base,lf,50),50)
    shortened,receipt=condition_for_export(base,lq,source,spec,limits)
    assert receipt['applied'] and len(shortened['elements'])==len(base['elements'])-1
    assert receipt['maximum_accepted_complex_s_delta_from_original']<1e-7
    assert all(e.get('n1')!='substrate0' and e.get('n2')!='substrate0' for e in shortened['elements'])
    assert next(e['value'] for e in shortened['elements'] if e['name']=='l')==1e-9
    real_cap=copy.deepcopy(base);next(e for e in real_cap['elements'] if e['name']=='cox0')['value']=1e-14
    real_source=Network(f,simulate_stable(real_cap,f,50),50)
    real_lq=Network(lf,simulate_stable(real_cap,lf,50),50)
    kept,rejected=condition_for_export(real_cap,real_lq,real_source,spec,limits)
    assert not rejected['applied'] and kept==real_cap
    attached=copy.deepcopy(base);attached['elements'].append(dict(kind='ind',name='internal_inductor',n1='substrate0',n2='REF',value=1e-9))
    candidate,reason=_contract(attached,'cox0');assert candidate is None and reason=='private_substrate_has_other_connections'
    external=copy.deepcopy(base);next(e for e in external['elements'] if e['name']=='cox0')['n2']='N'
    candidate,reason=_contract(external,'cox0');assert candidate is None
    assert base['elements'][4]['value']==.1 and len(base['elements'])==7
    return dict(passed=True,maximum_accepted_complex_s_delta=receipt['maximum_accepted_complex_s_delta_from_original'],
                original_counts=receipt['original_counts'],final_counts=receipt['final_counts'],
                checks=['handwritten_near_short_cap_contracted','all_original_sample_response_proved',
                        'formerly_passing_LQ_SRF_localS_preserved','meaningful_finite_cap_not_contracted',
                        'private_inductor_connection_rejected','external_terminal_merge_rejected',
                        'original_circuit_and_magnetic_parameters_unchanged'])
