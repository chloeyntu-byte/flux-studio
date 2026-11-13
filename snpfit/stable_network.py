"""Independent loaded-port branch-current MNA for the bounded SRF study.

All original R/L/C/K values are preserved. This formulation neither inverts the
inductance matrix nor subtracts large Kron-reduction terms. Symmetric numerical
equilibration changes equation scales only; it adds no physical conductance.
"""
from __future__ import annotations

import numpy as np
from .circuit import validate, component_counts
from .optimizer import SensitivityNetwork


class _CompiledMNA:
    def __init__(self,circuit,frequencies):
        validate(circuit)
        self.elements=circuit['elements']
        self.values=np.asarray([e['value'] for e in self.elements],float)
        self.freq=np.asarray(frequencies,float)
        if self.freq.ndim!=1 or not len(self.freq) or np.any(self.freq<=0) or not np.all(np.isfinite(self.freq)):
            raise ValueError('Stable MNA requires positive finite frequencies')
        self.w=2*np.pi*self.freq
        self.ports=list(circuit['ports']);self.p=len(self.ports)
        ref=circuit.get('reference','REF')
        self.branch_indices=np.asarray([j for j,e in enumerate(self.elements) if e['kind']!='mind'],int)
        self.branches=[self.elements[j] for j in self.branch_indices]
        nodes=set(self.ports)
        for e in self.branches:nodes.update((e['n1'],e['n2']))
        nodes.discard(ref)
        self.nodes=self.ports+sorted(nodes-set(self.ports))
        self.n,self.m=len(self.nodes),len(self.branches)
        ix={name:j for j,name in enumerate(self.nodes)}
        self.incidence=np.zeros((self.n,self.m))
        for j,e in enumerate(self.branches):
            if e['n1']!=ref:self.incidence[ix[e['n1']],j]+=1
            if e['n2']!=ref:self.incidence[ix[e['n2']],j]-=1
        self.ridx=np.asarray([j for j,e in enumerate(self.branches) if e['kind']=='res'],int)
        self.cidx=np.asarray([j for j,e in enumerate(self.branches) if e['kind']=='cap'],int)
        self.lidx=np.asarray([j for j,e in enumerate(self.branches) if e['kind']=='ind'],int)
        names={e['name']:j for j,e in enumerate(self.branches)}
        self.couplings=[(j,names[e['ind1']],names[e['ind2']]) for j,e in enumerate(self.elements) if e['kind']=='mind']
        self.last_diagnostics={}

    def impedance(self,values):
        values=np.asarray(values,float)
        branch_values=values[self.branch_indices]
        z=np.zeros((len(self.freq),self.m,self.m),complex)
        if len(self.ridx):z[:,self.ridx,self.ridx]=branch_values[self.ridx][None]
        if len(self.cidx):z[:,self.cidx,self.cidx]=1/(1j*self.w[:,None]*branch_values[self.cidx][None])
        if len(self.lidx):z[:,self.lidx,self.lidx]=1j*self.w[:,None]*branch_values[self.lidx][None]
        for original,a,b in self.couplings:
            mutual=values[original]*np.sqrt(branch_values[a]*branch_values[b])
            z[:,a,b]=z[:,b,a]=1j*self.w*mutual
        if not np.all(np.isfinite(z)):
            raise ValueError('Nonfinite stable-MNA impedance matrix')
        return z

    def solve(self,values,z0,diagnostics=False):
        z0=np.broadcast_to(np.asarray(z0,float),(self.p,))
        if not np.all(np.isfinite(z0)) or np.any(z0<=0):
            raise ValueError('Stable MNA requires positive real port impedances')
        n,m,p=self.n,self.m,self.p
        matrix=np.zeros((len(self.freq),n+m,n+m),complex)
        matrix[:,:n,n:]=self.incidence
        matrix[:,n:,:n]=self.incidence.T
        matrix[:,n:,n:]=-self.impedance(values)
        matrix[:,np.arange(p),np.arange(p)]=1/z0
        rhs=np.zeros((len(self.freq),n+m,p),complex)
        rhs[:,np.arange(p),np.arange(p)]=2/np.sqrt(z0)
        balanced=matrix.copy();rr=rhs.copy();total=np.ones((len(self.freq),n+m))
        for _ in range(10):
            norm=np.max(np.abs(balanced),axis=2)
            if np.any(norm==0) or not np.all(np.isfinite(norm)):
                raise ValueError('Disconnected or nonfinite stable-MNA equation')
            scale=1/np.sqrt(norm)
            balanced*=scale[:,:,None]*scale[:,None,:]
            rr*=scale[:,:,None]
            total*=scale
        solution=np.linalg.solve(balanced,rr)*total[:,:,None]
        sf=solution[:,:p]/np.sqrt(z0)[None,:,None]-np.eye(p)[None]
        if not np.all(np.isfinite(solution)) or not np.all(np.isfinite(sf)):
            raise ValueError('Nonfinite stable-MNA solution')
        if diagnostics:
            residual=np.linalg.norm(matrix@solution-rhs,axis=(-2,-1))/np.maximum(np.linalg.norm(rhs,axis=(-2,-1)),np.finfo(float).tiny)
            self.last_diagnostics={
                'solver':'complete_RLC_branch_current_loaded_port_MNA',
                'symmetric_equilibration_iterations':10,
                'maximum_relative_equation_residual':float(np.max(residual)),
                'relative_equation_residuals':residual.tolist(),
                'equilibrated_conditions':np.linalg.cond(balanced).tolist(),
                'original_values_preserved':True,'added_gmin':0.,
                'inductance_inverse_used':False,'kron_subtraction_used':False}
        return sf,solution[:,n:]


def simulate_stable(circuit,frequencies,z0,return_diagnostics=False):
    """Replay exact supplied values; optional diagnostics never change physics."""
    graph=_CompiledMNA(circuit,frequencies)
    sf,_=graph.solve(graph.values,z0,diagnostics=return_diagnostics)
    return (sf,graph.last_diagnostics) if return_diagnostics else sf


def evaluate_stable(circuit,network,spec,limits):
    """Retain the original independent L/Q/S/mutual error definitions exactly."""
    from .fitting import _evaluate_s
    sf=simulate_stable(circuit,network.freq,network.z0)
    metrics=_evaluate_s(sf,network,spec,limits,component_counts(circuit))
    metrics['independent_replay']='complete_RLC_branch_current_loaded_port_MNA'
    return metrics


class StableSensitivityNetwork(SensitivityNetwork):
    """Existing parameterization with complete-MNA S and analytic network tangents.

    Element parameter derivatives retain the existing finite 1e-5 values step.
    Reciprocity gives dS=(1/2) Ibranch.T dZ Ibranch for unit incident power waves,
    equivalent to -A^-1 dA X without separately solving each parameter RHS.
    """
    def __init__(self,circuit,frequencies):
        super().__init__(circuit,frequencies)
        self.stable=_CompiledMNA(self.template,frequencies)

    def s(self,x,z0,jacobian=False):
        vals=self.values(x)
        sf,branch_current=self.stable.solve(vals,z0)
        if not jacobian:
            return sf
        impedance=self.stable.impedance(vals)
        derivatives=np.empty((self.ng,len(self.freq),self.p,self.p),complex)
        eps=1e-5
        for j in range(self.ng):
            xx=np.asarray(x,float).copy();xx[j]+=eps
            dz=(self.stable.impedance(self.values(xx))-impedance)/eps
            derivatives[j]=.5*np.einsum('fmi,fmn,fnj->fij',branch_current,dz,branch_current,optimize=True)
        if not np.all(np.isfinite(derivatives)):
            raise ValueError('Nonfinite stable-MNA sensitivity')
        return sf,derivatives


def selfcheck():
    """Three-frequency closed-form inductor/transformer and tangent checks."""
    from .network import y_to_s
    from .models import get_model
    freq=np.asarray([1e9,10e9,40e9]);w=2*np.pi*freq
    inductor={'name':'handwritten_inductor','ports':['P','N'],'reference':'REF','elements':[
        {'kind':'res','name':'r','n1':'P','n2':'mid','value':2.},
        {'kind':'ind','name':'l','n1':'mid','n2':'N','value':1e-9},
        {'kind':'cap','name':'c','n1':'P','n2':'N','value':1e-13},
        {'kind':'res','name':'rg','n1':'N','n2':'REF','value':1e8}]}
    conductance=1/(2+1j*w*1e-9)+1j*w*1e-13
    yy=np.zeros((len(freq),2,2),complex)
    yy[:,0,0]=yy[:,1,1]=conductance;yy[:,0,1]=yy[:,1,0]=-conductance
    yy[:,1,1]+=1e-8
    si,di=simulate_stable(inductor,freq,[50,75],True)
    ind_error=float(np.max(np.abs(si-y_to_s(yy,[50,75]))))
    assert ind_error<1e-12
    transformer={'name':'handwritten_transformer','ports':['P','S'],'reference':'REF','elements':[
        {'kind':'res','name':'r1','n1':'P','n2':'mid1','value':2.},
        {'kind':'ind','name':'l1','n1':'mid1','n2':'REF','value':1e-9},
        {'kind':'res','name':'r2','n1':'S','n2':'mid2','value':4.},
        {'kind':'ind','name':'l2','n1':'mid2','n2':'REF','value':2e-9},
        {'kind':'mind','name':'k','ind1':'l1','ind2':'l2','value':.6},
        {'kind':'cap','name':'c1','n1':'P','n2':'REF','value':8e-14},
        {'kind':'cap','name':'c2','n1':'S','n2':'REF','value':4e-14}]}
    zz=np.zeros((len(freq),2,2),complex)
    zz[:,0,0]=2+1j*w*1e-9;zz[:,1,1]=4+1j*w*2e-9
    zz[:,0,1]=zz[:,1,0]=1j*w*.6*np.sqrt(2)*1e-9
    yy=np.linalg.inv(zz);yy[:,0,0]+=1j*w*8e-14;yy[:,1,1]+=1j*w*4e-14
    st,dt=simulate_stable(transformer,freq,50,True)
    tf_error=float(np.max(np.abs(st-y_to_s(yy,[50,50]))))
    assert tf_error<1e-12
    graph=StableSensitivityNetwork(transformer,freq);x=np.zeros(graph.ng)
    _,jac=graph.s(x,50,True);errors=[]
    for j in range(graph.ng):
        plus=x.copy();minus=x.copy();plus[j]+=1e-5;minus[j]-=1e-5
        independent=(simulate_stable(graph.circuit(plus),freq,50)-simulate_stable(graph.circuit(minus),freq,50))/(2e-5)
        err=float(np.max(np.abs(jac[j]-independent))/max(float(np.max(np.abs(independent))),1e-12))
        errors.append(err)
    assert max(errors)<3e-5
    return {'passed':True,'inductor_three_frequency_max_s_error':ind_error,
            'transformer_three_frequency_max_s_error':tf_error,
            'maximum_tangent_relative_error':max(errors),
            'maximum_equation_residual':max(di['maximum_relative_equation_residual'],dt['maximum_relative_equation_residual']),
            'checks':['handwritten_inductor_closed_form_mixed_z0','handwritten_coupled_transformer_closed_form',
                      'all_group_sensitivities_independent_central_difference','no_value_changes_or_added_gmin']}
