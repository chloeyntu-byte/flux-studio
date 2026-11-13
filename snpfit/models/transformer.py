"""Dedicated two-winding plugins and bounded physical loss-section ladder."""
from .base import Candidate, ModelSpec
from .loss import add_winding_loss_cell

def add_loss_candidate(circuit,network,spec,metrics,cell_index):
    return add_winding_loss_cell(circuit,network,spec,metrics,cell_index)

def model_specs():
    result=[]
    tap_labels={'none':'无 tap','primary':'初级 tap','secondary':'次级 tap','dual':'双侧 tap'}
    for mode in ('se','dm'):
     for taps in ('none','primary','secondary','dual'):
      for ground in (False,True):
       roles=['P1','P2','S1','S2'] if mode=='dm' else ['P','S']; tap_roles=[]
       if taps in ('primary','dual'): roles.append('PTAP');tap_roles.append('PTAP')
       if taps in ('secondary','dual'): roles.append('STAP');tap_roles.append('STAP')
       ident='transformer_%s%s%s'%(mode,'' if taps=='none' else '_'+taps+'_tap','_gnd' if ground else '')
       minimum=2 if tap_roles else 1
       candidates=(Candidate('coupled_rl','集中耦合 RL',minimum,0,False),
                   Candidate('parasitic_rlck','集中寄生 RLCK',minimum,1,False),
                   Candidate('substrate_skin','衬底与趋肤损耗',minimum,2,True),
                   Candidate('distributed_2','分布两节耦合损耗',2,2,True),
                   Candidate('distributed_3','分布三节耦合损耗',3,2,True),
                   Candidate('distributed_bridge','分布三节交叉寄生',3,2,True,bridges=True))
       if tap_roles: candidates=tuple(c for c in candidates if c.id!='distributed_2')
       label=('单端' if mode=='se' else '差分')+'变压器 / '+tap_labels[taps]+' / '+('显式 GND' if ground else '无独立 GND')
       pairs=(('P1','P2'),('S1','S2')) if mode=='dm' else (('P','REF'),('S','REF'))
       result.append(ModelSpec(ident,label,'transformer',mode,tuple(roles),ground,candidates,pairs,tuple(tap_roles),'transformer'))
    return result
