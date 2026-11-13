"""Dedicated single-winding plugins with a finite, physical candidate ladder."""
from .base import Candidate, ModelSpec
from .loss import add_winding_loss_cell

def add_loss_candidate(circuit, network, spec, metrics, cell_index):
    return add_winding_loss_cell(circuit, network, spec, metrics, cell_index)

def model_specs():
    result=[]
    for mode in ('se','dm'):
      for tapped in (False,True):
       for ground in (False,True):
        ident='inductor_%s%s%s'%(mode,'_tap' if tapped else '', '_gnd' if ground else '')
        minimum=2 if tapped else 1
        candidates=(Candidate('series_rl','分段耦合 RL' if tapped else '串联 RL',minimum,0,False),
                    Candidate('compact_pi','集中寄生 RLC',minimum,1,False),
                    Candidate('substrate_skin','衬底与趋肤损耗',minimum,2,True),
                    Candidate('distributed_2','分布两节损耗',2,2,True),
                    Candidate('distributed_3','分布三节损耗',3,2,True))
        # The tapped compact winding already contains two magnetic sections.
        if tapped: candidates=tuple(c for c in candidates if c.id!='distributed_2')
        label=('单端' if mode=='se' else '差分')+'电感 / '+('有 tap' if tapped else '无 tap')+' / '+('显式 GND' if ground else '无独立 GND')
        result.append(ModelSpec(ident,label,'inductor',mode,('P','N','TAP') if tapped else ('P','N'),ground,candidates,(('P','N'),),('TAP',) if tapped else (),'inductor'))
    return result
