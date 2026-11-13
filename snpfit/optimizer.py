"""Independent NumPy optimizer with analytic passive-network sensitivities.

Only the selected circuit and measured SNP enter initialization. No external
optimizer, generated model, subprocess, or model-library dependency is used.
"""
from __future__ import annotations
import copy
import time
import numpy as np
from .circuit import validate, admittance, s_from_y


class SensitivityNetwork:
    """Compile a fixed RLC(K) graph and differentiate its Kron reduction."""
    def __init__(self, circuit, frequencies):
        validate(circuit)
        self.template = copy.deepcopy(circuit)
        self.elements = self.template['elements']
        self.freq = np.asarray(frequencies, float)
        self.w = 2*np.pi*self.freq
        self.ports = list(circuit['ports'])
        reference = circuit.get('reference', 'REF')
        nodes = set(self.ports)
        for e in self.elements:
            if e['kind'] != 'mind':
                nodes.update((e['n1'], e['n2']))
        nodes.discard(reference)
        self.nodes = self.ports + sorted(nodes-set(self.ports))
        self.n, self.p = len(self.nodes),len(self.ports)
        ix = {n:i for i,n in enumerate(self.nodes)}
        self.incidence = np.zeros((len(self.elements), self.n))
        for j,e in enumerate(self.elements):
            if e['kind'] != 'mind':
                if e['n1'] != reference: self.incidence[j,ix[e['n1']]] += 1
                if e['n2'] != reference: self.incidence[j,ix[e['n2']]] -= 1
        self.kind = np.asarray([e['kind'] for e in self.elements])
        self.bases = np.asarray([e['value'] for e in self.elements], float)
        self.keys=[]
        self.element_groups=[]
        for e in self.elements:
            key = (e['kind']=='mind', e.get('fit_group', e['name']))
            if key not in self.keys: self.keys.append(key)
            self.element_groups.append(self.keys.index(key))
        self.element_groups=np.asarray(self.element_groups)
        self.ng=len(self.keys)
        self.lidx=np.flatnonzero(self.kind=='ind')
        self.kidx=np.flatnonzero(self.kind=='mind')
        self.ridx=np.flatnonzero(self.kind=='res')
        self.cidx=np.flatnonzero(self.kind=='cap')
        self.b=self.incidence[self.lidx].T
        lix={self.elements[j]['name']:i for i,j in enumerate(self.lidx)}
        self.kpair=[(lix[self.elements[j]['ind1']],lix[self.elements[j]['ind2']]) for j in self.kidx]
        self.base_k=np.arctanh(np.clip(self.bases[self.kidx],-.999999,.999999))

    def values(self,x):
        delta=np.asarray(x)[self.element_groups]
        vals=self.bases*np.exp(np.clip(delta,-16,16))
        if len(self.kidx):
            vals[self.kidx]=np.tanh(self.base_k+delta[self.kidx])
            corr=np.eye(len(self.lidx))
            for j,(a,b) in zip(self.kidx,self.kpair): corr[a,b]=corr[b,a]=vals[j]
            minimum=float(np.linalg.eigvalsh(corr)[0])
            if minimum<1e-5:
                factor=(1-1e-5)/(1-minimum)
                vals[self.kidx]*=factor
        if not np.all(np.isfinite(vals)): raise ValueError('Nonfinite optimizer parameter')
        return vals

    def lmat(self,vals):
        mat=np.diag(vals[self.lidx])
        for j,(a,b) in zip(self.kidx,self.kpair):
            mat[a,b]=mat[b,a]=vals[j]*np.sqrt(mat[a,a]*mat[b,b])
        return mat

    def circuit(self,x):
        out=copy.deepcopy(self.template)
        for e,v in zip(out['elements'],self.values(x)): e['value']=float(v)
        validate(out)
        return out

    def y(self,x,jacobian=False):
        vals=self.values(x)
        outer=self.incidence[:,:,None]*self.incidence[:,None,:]
        g=np.einsum('e,eij->ij',1/vals[self.ridx],outer[self.ridx])
        c=np.einsum('e,eij->ij',vals[self.cidx],outer[self.cidx])
        lm=self.lmat(vals)
        li=np.linalg.solve(lm,np.eye(len(lm)))
        q=self.b@li
        yl=q@self.b.T
        yn=g[None]+1j*self.w[:,None,None]*c[None]+yl[None]/(1j*self.w[:,None,None])
        nf=len(self.freq)
        t=np.broadcast_to(np.eye(self.n,dtype=complex)[:,:self.p],(nf,self.n,self.p)).copy()
        if self.n>self.p:
            t[:,self.p:,:]=-np.linalg.solve(yn[:,self.p:,self.p:],yn[:,self.p:,:self.p])
        y=yn[:,:self.p,:]@t
        if not jacobian: return y
        # Differentiation of T cancels through the eliminated internal-node KCL.
        uq=np.einsum('fnp,nl->fpl',t,q)
        aj=np.einsum('en,fnp->fep',self.incidence,t)
        out=np.empty((self.ng,nf,self.p,self.p),complex)
        eps=1e-5
        for j in range(self.ng):
            xx=np.asarray(x).copy(); xx[j]+=eps
            vv=self.values(xx)
            dvals=(vv-vals)/eps
            dr=-dvals[self.ridx]/vals[self.ridx]**2
            dc=dvals[self.cidx]
            dy=np.einsum('e,fep,feq->fpq',dr,aj[:,self.ridx],aj[:,self.ridx]).astype(complex)
            dy+=1j*self.w[:,None,None]*np.einsum('e,fep,feq->fpq',dc,aj[:,self.cidx],aj[:,self.cidx])
            dl=(self.lmat(vv)-lm)/eps
            dy-=np.einsum('fpl,lm,fqm->fpq',uq,dl,uq)/(1j*self.w[:,None,None])
            out[j]=dy
        return y,out

    def s(self,x,z0,jacobian=False):
        if not jacobian: return s_from_y(self.y(x),z0)
        y,dy=self.y(x,True)
        z=np.broadcast_to(np.asarray(z0,float),(self.p,))
        scale=np.sqrt(z[:,None]*z[None,:])
        a=y*scale
        inv=np.linalg.inv(np.eye(self.p)+a)
        s=2*inv-np.eye(self.p)
        ds=-2*np.einsum('fij,gfjk,fkl->gfil',inv,dy*scale,inv)
        return s,ds


def _optimize_path(circuit,network,spec,limits,timeout_s,cancel=None,progress=None,network_weight=1.0,network_iterations=45,
                   network_class=None,evaluation_function=None,measurement_function=None):
    """Fit tied groups with finite LM phases and independent acceptance.

    The first phase fits the entire complex network. The second includes the
    selected winding L/Q, complex mutual impedance and a smooth worst-point
    objective. Every saved result is re-evaluated by the independent evaluator.
    """
    from .fitting import evaluate,_measure_s
    if measurement_function is not None: _measure_s=measurement_function
    network_class=SensitivityNetwork if network_class is None else network_class
    evaluation_function=evaluate if evaluation_function is None else evaluation_function
    started=time.monotonic()
    graph=network_class(circuit,network.freq)
    x=np.zeros(graph.ng)
    source,source_z=_measure_s(network.s,network.z0,network.freq,spec)
    lscale=float(limits['l_pct'])/100
    qscale=float(limits['q_pct'])/100
    sscale=min(lscale,qscale)
    scales={}
    for name,curve in source.items():
        scales[name]={}
        for key,scale in [('l_h',lscale),('q',qscale)]:
            v=curve[key]
            scales[name][key]=np.maximum(np.abs(v),max(np.max(np.abs(v))*1e-6,1e-30))*scale
    best_circuit=copy.deepcopy(circuit)
    best_metrics=evaluation_function(best_circuit,network,spec,limits)
    initial_score=float(best_metrics['score'])
    history=[]
    evaluations=0

    def residual_many(s_batch,phase):
        nonlocal evaluations
        sf=np.asarray(s_batch)
        ng,nf=sf.shape[:2]
        evaluations+=ng
        err=(sf-network.s[None]).reshape(ng,-1)/sscale
        if phase=='network': return np.concatenate([err.real,err.imag],axis=1)
        curves,zz=_measure_s(sf.reshape(ng*nf,network.nports,network.nports),
                            network.z0,np.tile(network.freq,ng),spec)
        blocks=[]
        for name in source:
            for key in ('l_h','q'):
                blocks.append((curves[name][key].reshape(ng,nf)-source[name][key][None])/scales[name][key][None])
        zz=zz.reshape(ng,nf,zz.shape[-2],zz.shape[-1])
        if source_z.shape[-1]==2:
            for a,b in [(0,1),(1,0)]:
                v=source_z[:,a,b]
                denom=np.maximum(np.abs(v),max(np.max(np.abs(v))*1e-6,1e-20))*sscale
                er=(zz[:,:,a,b]-v[None])/denom[None]
                blocks += [er.real,er.imag]
        sw=network_weight
        blocks += [err.real*sw,err.imag*sw]
        result=np.concatenate(blocks,axis=1)
        if not np.all(np.isfinite(result)): raise ValueError('Nonfinite optimization residual')
        return result

    def residual(sf,phase):
        return residual_many(sf[None],phase)[0]

    def stopped():
        return (cancel is not None and cancel.is_set()) or time.monotonic()-started>=timeout_s

    # A short network phase establishes parasitic values before sensitive Q fit.
    phases=[('network',network_iterations,.2),('rms',180,.55),('worst',110,.8)]
    if graph.ng>=20:
        phases += [('rms',180,.92),('worst',130,1.)]
    else:
        phases[-1]=('worst',110,1.)
    for phase,maxiter,fraction in phases:
        if stopped() or best_metrics['passed']: break
        damping=1e-2
        local_start=time.monotonic()
        r=residual(graph.s(x,network.z0),phase)
        iterations=0
        for it in range(maxiter):
            if stopped() or (time.monotonic()-started)>timeout_s*fraction: break
            iterations=it+1
            sf,ds=graph.s(x,network.z0,True)
            r=residual(sf,phase)
            eps=1e-5
            jac=((residual_many(sf[None]+eps*ds,phase)-r[None])/eps).T
            amplitude=np.abs(r).copy()
            if phase!='network':
                offset=len(source)*2*len(network.freq)
                if source_z.shape[-1]==2:
                    for _ in range(2):
                        width=len(network.freq)
                        mag=np.hypot(r[offset:offset+width],r[offset+width:offset+2*width])
                        amplitude[offset:offset+width]=mag
                        amplitude[offset+width:offset+2*width]=mag
                        offset+=2*width
                width=len(network.freq)*network.nports**2
                mag=np.hypot(r[offset:offset+width],r[offset+width:offset+2*width])
                amplitude[offset:offset+width]=mag
                amplitude[offset+width:offset+2*width]=mag
            if phase=='worst':
                peak=max(np.max(amplitude),1e-12)
                weights=np.maximum(.12,amplitude/peak)**3
            else:
                weights=np.ones(len(r))
                if phase=='rms': weights=np.minimum(np.maximum(1,amplitude),20)
            normal=jac.T@(weights[:,None]*jac)
            gradient=jac.T@(weights*r)
            diagonal=np.maximum(np.diag(normal),1e-12)
            cost=float(np.dot(r*weights,r))
            accepted=False
            for attempt in range(7):
                if stopped(): break
                try: step=-np.linalg.solve(normal+damping*np.diag(diagonal),gradient)
                except np.linalg.LinAlgError:
                    damping*=8;continue
                step=np.clip(step,-.7,.7)
                for back in range(4):
                    trial=x+step*(.5**back)
                    try:
                        nr=residual(graph.s(trial,network.z0),phase)
                        nc=float(np.dot(nr*weights,nr))
                    except (ValueError,np.linalg.LinAlgError,FloatingPointError): continue
                    if nc<cost:
                        x=trial;r=nr;accepted=True
                        damping=max(damping/3,1e-9)
                        cc=graph.circuit(x)
                        mm=evaluation_function(cc,network,spec,limits)
                        if mm['score']<best_metrics['score']:
                            best_circuit,best_metrics=cc,mm
                        break
                if accepted: break
                damping*=6
            if best_metrics['passed'] or not accepted: break
            if np.max(np.abs(step))<1e-6: break
        history.append({'phase':phase,'iterations':iterations,'elapsed_s':time.monotonic()-local_start,'best_score':float(best_metrics['score'])})
        # The next target starts from the best acceptance result, not an RMS
        # improvement whose worst measured sample became worse.
        if phase!='network':
            newgraph=network_class(best_circuit,network.freq)
            graph=newgraph;x=np.zeros(graph.ng)
    record={'algorithm':'independent_numpy_analytic_network_lm','parameters':graph.ng,'phases':history,'residual_evaluations':evaluations,'elapsed_s':time.monotonic()-started,'initial_score':initial_score,'final_score':float(best_metrics['score']),'external_tools':[],
            'network_class':getattr(network_class,'__name__',type(network_class).__name__),
            'evaluation_function':getattr(evaluation_function,'__name__',type(evaluation_function).__name__)}
    best_circuit.setdefault('metadata',{})['refinement']='independent_numpy_analytic_network_lm'
    return best_circuit,best_metrics,record


def optimize(circuit,network,spec,limits,timeout_s,cancel=None,progress=None,
             network_class=None,evaluation_function=None,measurement_function=None):
    """One primary objective and one finite inductor-only alternate.

    An already accepted circuit returns immediately. Only a failure of the last inductor candidate may
    retry the identical physical topology from this request's original seed
    with a winding-focused network weight. Both paths share the same total
    wall-time budget and the same independent acceptance rules. Transformer
    behavior is exactly the primary objective; no alternate topology is added.
    """
    started=time.monotonic()
    cc,mm,record=_optimize_path(circuit,network,spec,limits,timeout_s,cancel,progress,
                                network_class=network_class,evaluation_function=evaluation_function,measurement_function=measurement_function)
    remaining=timeout_s-(time.monotonic()-started)
    last_candidate=spec.candidates[-1].id
    if spec.device!='inductor' or circuit.get('metadata',{}).get('candidate')!=last_candidate or mm['passed'] or remaining<=.05 or (cancel is not None and cancel.is_set()):
        return cc,mm,record
    if progress: progress('当前电感结构未达标，尝试一次绕组优先目标')
    ac,am,ar=_optimize_path(circuit,network,spec,limits,remaining,cancel,progress,
                            network_weight=1/np.sqrt(network.nports),network_iterations=35,
                            network_class=network_class,evaluation_function=evaluation_function,measurement_function=measurement_function)
    if am['score']<mm['score']: cc,mm=ac,am
    receipt={'algorithm':'independent_numpy_two_finite_inductor_objectives',
             'paths':[record,ar],'elapsed_s':time.monotonic()-started,
             'initial_score':record['initial_score'],'final_score':float(mm['score']),
             'external_tools':[], 'network_class':record['network_class'],
             'evaluation_function':record['evaluation_function']}
    return cc,mm,receipt
