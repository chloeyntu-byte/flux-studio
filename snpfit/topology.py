"""Independent passive winding circuits initialized only from selected SNP.

The two explicit family builders share circuit stamping utilities. They never
read fitter binaries, scripts, model templates, cached fitted values or netlists.
"""
from __future__ import annotations
import copy
import numpy as np
from .circuit import y_from_s, validate, admittance

ALGORITHM_VERSION='standalone_physical_ladder_v2'

def _base_branches(spec):
    result=[]
    for winding,(a,b) in enumerate(spec.winding_pairs):
        tap=('TAP' if spec.device=='inductor' else ('PTAP' if winding==0 else 'STAP'))
        seq=[a,tap,b] if tap in spec.tap_roles else [a,b]
        for first,last in zip(seq[:-1],seq[1:]): result.append((first,last,winding))
    return result

def _positive_matrix(matrix,floor):
    matrix=(matrix+matrix.T)/2
    eig,vec=np.linalg.eigh(matrix)
    return (vec*np.maximum(eig,floor))@vec.T

def estimate_winding(network,spec):
    """Low-band winding impedance and terminal capacitance, no cached model."""
    y=y_from_s(network.s,network.z0)
    branches=_base_branches(spec); roles=list(spec.port_roles)
    incidence=np.zeros((len(roles),len(branches)))
    for j,(a,b,_) in enumerate(branches):
        if a!='REF': incidence[roles.index(a),j]=1
        if b!='REF': incidence[roles.index(b),j]=-1
    right=incidence @ np.linalg.inv(incidence.T@incidence)
    branch_y=right.T[None]@y@right[None]
    branch_z=np.linalg.inv(branch_y)
    count=min(3,max(1,len(y)//6))
    resistances=np.maximum(np.median(branch_z[:count].real,axis=0).diagonal(),.01)
    inductance=np.median(branch_z[:count].imag/(2*np.pi*network.freq[:count,None,None]),axis=0)
    inductance=_positive_matrix(inductance,max(float(np.max(np.abs(inductance)))*1e-5,1e-14))
    # Electrostatic seeds use the high-band coefficient of Y=A/(jw)+jw*C.
    omega=2*np.pi*np.asarray(network.freq)
    hi=slice(max(0,len(omega)//2),None); om=omega[hi]
    design=np.stack([omega[-1]/om,om/omega[-1]],axis=1)
    coef=np.linalg.lstsq(design,y[hi].imag.reshape(len(om),-1),rcond=None)[0]
    caps=coef[1].reshape(len(roles),len(roles))/omega[-1]
    rowcap=np.maximum(y[0].imag.sum(axis=1)/omega[0],1e-16)
    conductance=np.maximum(y[0].real.sum(axis=1),1e-7)
    return dict(branches=branches,resistances=resistances,inductance=inductance,
                capacitance=caps,ground_cap=rowcap,ground_conductance=conductance,
                source='only selected SNP low-band branch Z and high-band Y')


def build_candidate(network,spec,candidate,seed=0,max_each=20):
    if max_each not in (20,30): raise ValueError('Finite component limit must be 20 or 30')
    if spec.device=='inductor': return _build_inductor(network,spec,candidate,seed,max_each)
    if spec.device=='transformer': return _build_transformer(network,spec,candidate,seed,max_each)
    raise ValueError('No dedicated physical builder for this model family')

def _build_inductor(network,spec,candidate,seed,max_each=20):
    if spec.device!='inductor': raise ValueError('Inductor plugin mismatch')
    return _winding_ladder(network,spec,candidate,seed,max_each)

def _build_transformer(network,spec,candidate,seed,max_each=20):
    if spec.device!='transformer': raise ValueError('Transformer plugin mismatch')
    return _winding_ladder(network,spec,candidate,seed,max_each)

def _winding_ladder(network,spec,candidate,seed,max_each=20):
    estimate=estimate_winding(network,spec)
    branches=estimate['branches']; base_l=estimate['inductance'];base_r=estimate['resistances']
    circuit={'name':'snpfit_model','ports':list(spec.port_roles),'reference':'REF','elements':[],
             'metadata':{'algorithm_version':ALGORITHM_VERSION,'backend':'standalone_numpy',
                         'model_id':spec.id,'candidate':candidate.id,
                         'export_reference':spec.explicit_reference,'explicit_reference':spec.explicit_reference,
                         'initialization':estimate['source'],'selected_band_hz':[float(network.freq[0]),float(network.freq[-1])],
                         'seed':seed,'sections_per_winding':candidate.sections,
                         'component_limit':max_each}}
    elements=circuit['elements']; magnetic=[]; expanded=[]; middle=[]; winding_middle={}
    def add(kind,name,a,b,value,group=None):
        e={'kind':kind,'name':name,'n1':a,'n2':b,'value':float(value)}
        if group: e['fit_group']=group
        elements.append(e);return e
    # Finite family ladders allow up to four sections in the 30-device study.
    counts=[]
    for i,(a,b,wind) in enumerate(branches):
        peers=[j for j,(_,_,w) in enumerate(branches) if w==wind]
        count=candidate.sections if len(peers)==1 else (candidate.sections//2+(i==peers[-1] and candidate.sections%2))
        counts.append(max(1,count))
    for i,(a,b,wind) in enumerate(branches):
        count=counts[i]
        seq=[a]+['w%d_%d'%(i,j) for j in range(1,count)]+[b];middle.extend(seq[1:-1])
        winding_middle.setdefault(wind,[]).extend(seq[1:-1])
        for j,(first,last) in enumerate(zip(seq[:-1],seq[1:])):
            prefix='w%d_%d'%(i,j); rnode=prefix+'_r'; lnode=prefix+'_l'
            add('res','r'+prefix,first,rnode,base_r[i]/count)
            fraction=.9 if candidate.skin else 1.
            lval=base_l[i,i]*fraction/(count*count)*1.025
            add('ind','l'+prefix,rnode,lnode if candidate.skin else last,lval)
            magnetic.append('l'+prefix);expanded.append((i,count,lval))
            if candidate.skin:
                # Series (L_skin || R_skin) gives finite high-frequency loss.
                add('ind','lskin'+prefix,lnode,last,base_l[i,i]*.1/count)
                corner=np.sqrt(network.freq[0]*network.freq[-1])*(.5 if seed%2 else 1.5)
                add('res','rskin'+prefix,lnode,last,max(base_l[i,i]*.1/count*2*np.pi*corner,.05))
    for i in range(len(magnetic)):
      for j in range(i):
        bi,ni,li=expanded[i];bj,nj,lj=expanded[j]
        value=base_l[bi,bj]*(.9 if candidate.skin else 1)/(ni*nj)/np.sqrt(li*lj)
        elements.append({'kind':'mind','name':'k%d_%d'%(i,j),'ind1':magnetic[i],'ind2':magnetic[j],'value':float(value)})
    if candidate.parasitic:
        roles=list(spec.port_roles);cap=estimate['capacitance'];ground=estimate['ground_cap']
        # Full terminal capacitor graph belongs to this selected winding family;
        # it handles winding self/interwinding electrostatic coupling explicitly.
        edges=[]
        for i,a in enumerate(roles):
            for j in range(i):
                b=roles[j]
                val=max(-float(cap[i,j]),float(np.sqrt(ground[i]*ground[j]))*.03,1e-17)
                edges.append((val,i,j,a,b))
        bridge_pairs=[]
        if candidate.bridges and spec.device=='transformer':
            bridge_pairs=[(a,b) for a in winding_middle.get(0,[]) for b in winding_middle.get(1,[])]
        reserve=len(roles)*(2 if candidate.parasitic==2 else 1)+len(middle)+len(bridge_pairs)
        for val,i,j,a,b in sorted(edges,reverse=True)[:max(0,max_each-reserve)]:
            add('cap','cp%d_%d'%(i,j),a,b,val)
        for j,(a,b) in enumerate(bridge_pairs):
            add('cap','cbridge%d'%j,a,b,float(np.median(ground))*.15)
        for i,port in enumerate(roles):
            c=max(float(ground[i]),1e-16)
            if candidate.parasitic==1:
                add('cap','cg%d'%i,port,'REF',c)
            else:
                sub='substrate%d'%i
                add('cap','cox%d'%i,port,sub,2*c)
                add('cap','csub%d'%i,sub,'REF',2*c)
                omega=2*np.pi*network.freq[0]
                r=float(np.clip(estimate['ground_conductance'][i]/(omega*2*c)**2,10,1e6))
                add('res','rsub%d'%i,sub,'REF',r)
        for j,node in enumerate(middle):
            add('cap','cmid%d'%j,node,'REF',float(np.median(ground))*.25)
    validate(circuit)
    return circuit
