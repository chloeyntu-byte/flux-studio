"""Shared, non-electrical winding illustrations for Tk and native OA output.

Source geometry references are retained for audit only. Every model uses a
standard single-turn illustration and fixed logical pin positions. Sections
are associated by arclength without changing the fitted circuit.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from .models import get_model

REFERENCE_DIR = Path(__file__).with_name('visual_references')
GRID = .0625
SCHEMATIC_SCALE = 38.
SYMBOL_SCALE = 6.
WINDING_NAMES = {'L': 'winding', 'P': 'primary', 'S': 'secondary'}


def _profiles():
    return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(REFERENCE_DIR.glob('*.json'))]


def _spec(circuit, model_id=None):
    identity = circuit.get('metadata', {}).get('model_id') or model_id
    if not identity:
        raise ValueError('A selected model is required for a winding illustration.')
    return get_model(identity)


def _family(spec):
    return 'transformer' if spec.device == 'transformer' else ('tapped_inductor' if spec.tap_roles else 'inductor')


def reference_catalog(model_id=None):
    """Portable readonly choices; Automatic and Model template are UI choices."""
    family = _family(get_model(model_id)) if model_id else None
    return [{'id': p['id'], 'label': p['label'], 'device': 'transformer' if p['family']=='transformer' else 'inductor',
             'family': p['family'], 'tapped': p['family']=='tapped_inductor', 'pdk': p['pdk'],
             'source_snp_sha256': p['source_snp_sha256']}
            for p in _profiles() if not family or p['family'] == family]


def _round(point):
    return [round(float(v)/GRID)*GRID for v in point]


def _scale(points, factor):
    return [_round([p[0]*factor, p[1]*factor]) for p in points]


def _distance(a,b):
    return math.hypot(b[0]-a[0], b[1]-a[1])


def _at(path, fraction):
    """Position and forward tangent at a continuous polyline fraction."""
    fraction = max(0., min(1., float(fraction)))
    lengths = [_distance(a,b) for a,b in zip(path,path[1:])]
    total = sum(lengths)
    if not total:
        return list(path[0]), [1.,0.]
    target = fraction*total
    offset=0.
    for (a,b),length in zip(zip(path,path[1:]),lengths):
        if offset+length >= target or offset+length >= total-1e-12:
            t = (target-offset)/length if length else 0.
            return [a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])], [(b[0]-a[0])/length,(b[1]-a[1])/length]
        offset+=length
    return list(path[-1]), [1.,0.]


def _orientation(tangent):
    return 'R%d' % (int(round(math.degrees(math.atan2(tangent[1],tangent[0]))/90))*90 % 360)


def _slice(path,first,last):
    """Keep original corners while cutting a display arclength interval."""
    lengths=[_distance(a,b) for a,b in zip(path,path[1:])]
    total=sum(lengths);start,_=_at(path,first);stop,_=_at(path,last)
    points=[start];offset=0.
    for index,length in enumerate(lengths):
        offset+=length
        if total*first+1e-10<offset<total*last-1e-10:
            points.append(path[index+1])
    points.append(stop)
    return points


def _bounds(points, margin=2.):
    if not points:
        return [-10.,-10.,10.,10.]
    return [min(p[0] for p in points)-margin, min(p[1] for p in points)-margin,
            max(p[0] for p in points)+margin,max(p[1] for p in points)+margin]


def _select(spec, reference_id, source_sha256):
    profiles = [p for p in _profiles() if p['family']==_family(spec)]
    if reference_id not in ('auto','template','unified','unified_single_turn',None,''):
        reference = next((p for p in _profiles() if p['id']==reference_id),None)
        if not reference:
            raise ValueError('Unknown layout reference: '+str(reference_id))
        exact = bool(source_sha256 and source_sha256 in reference['source_snp_sha256'])
        return reference, 'source_layout' if exact else 'selected_reference_shape'
    exact = next((p for p in profiles if source_sha256 and source_sha256 in p['source_snp_sha256']),None)
    if exact and reference_id != 'template':
        return exact,'source_layout'
    reference = next(p for p in profiles if p['pdk']=='generic_a')
    return reference,'model_template'


def _unified_template(spec):
    """Canonical single-turn schematic style; source layouts never set its XY."""
    if spec.device=='inductor':
        windings=[{'id':'L','path':[[-.11,-.5],[-.11,-.34],[-.22,-.34],[-.34,-.22],[-.34,.22],[-.22,.34],
                                  [.22,.34],[.34,.22],[.34,-.22],[.22,-.34],[.11,-.34],[.11,-.5]]}]
    else:
        windings=[{'id':'P','path':[[-.14,.5],[-.14,.35],[-.25,.35],[-.37,.23],[-.37,-.23],[-.25,-.35],
                                  [.25,-.35],[.37,-.23],[.37,.23],[.25,.35],[.14,.35],[.14,.5]]},
                  {'id':'S','path':[[-.10,-.5],[-.10,-.25],[-.17,-.25],[-.27,-.15],[-.27,.15],[-.17,.25],
                                  [.17,.25],[.27,.15],[.27,-.15],[.17,-.25],[.10,-.25],[.10,-.5]]}]
    taps=[]
    for index,winding in enumerate(windings):
        role='TAP' if spec.device=='inductor' else ('PTAP' if index==0 else 'STAP')
        if role not in spec.tap_roles:
            continue
        attach,_=_at(winding['path'],.5)
        if role=='TAP':
            path=[attach,[0.,.5]]
        elif role=='PTAP':
            path=[attach,[-.035,attach[1]],[-.035,.46],[0.,.5]]
        else:
            path=[attach,[.035,attach[1]],[.035,-.46],[0.,-.5]]
        taps.append({'name':role,'winding':winding['id'],'attachment_fraction':.5,'attachment':attach,
                     'path':path,'illustrative':True})
    return {'windings':windings,'tap_paths':taps}


def _style_pins(circuit,spec,template,port_order):
    ports=list(circuit['ports'])
    if isinstance(port_order,str):
        port_order=[int(p.strip()) for p in port_order.split(',') if p.strip()]
    ordered=list(port_order or [])
    if ordered and len(ordered)!=len(ports):
        raise ValueError('The visual port order must match the circuit port count.')
    slots={};internal=[]
    for index,(a,b) in enumerate(spec.winding_pairs):
        winding=template['windings'][index]
        direction='bottom' if winding['id'] in ('L','S') else 'top'
        slots[a]=(winding['path'][0],direction)
        if b=='REF':
            internal.append({'name':circuit.get('reference','REF'),'point':winding['path'][-1],
                             'direction':direction,'model_return':winding['id']})
        else:
            slots[b]=(winding['path'][-1],direction)
    for tap in template['tap_paths']:
        slots[tap['name']]=(tap['path'][-1],'bottom' if tap['name']=='STAP' else 'top')
    result=[]
    for index,role in enumerate(ports):
        point,direction=slots[role]
        result.append({'name':role,'point':point,'direction':direction,'physical_names':[],
                       'file_port':ordered[index] if ordered else None,'position_kind':'unified_style'})
    export=circuit.get('metadata',{}).get('export_reference',circuit.get('metadata',{}).get('explicit_reference',True))
    if export:
        result.append({'name':circuit.get('reference','REF'),'point':[.48,0.],'direction':'right',
                       'physical_names':[],'position_kind':'unified_style'})
        result.extend({**pin,'physical_names':[],'position_kind':'unified_return'} for pin in internal)
    return result,internal if not export else []


def _q_text(row):
    value=row.get('q')
    if value is None:
        return 'inf' if row.get('status')=='lossless' else 'N/A'
    if not math.isfinite(value):
        return 'ideal' if value>0 else 'N/A'
    return '%.4g' % value


def ordered_source_order(port_order):
    if isinstance(port_order,str):
        return [int(p.strip()) for p in port_order.split(',') if p.strip()]
    return list(port_order or [])


def build_visual(circuit, model_id=None, reference_id='auto', source_sha256=None,
                 port_order=None, short_unused=False, q_frequency_hz=None):
    """Return one immutable plain-JSON visual plan without modifying the model."""
    from .segment_q import evaluate_segments, rank_segments
    spec=_spec(circuit,model_id)
    if len({e['name'] for e in circuit['elements']}) != len(circuit['elements']):
        raise ValueError('Duplicate circuit element names prevent a reliable visual plan.')
    source_reference,source_mapping=_select(spec,reference_id,source_sha256)
    reference=_unified_template(spec)
    mapping_kind='unified_single_turn'
    q_frequency_hz=float(1e10 if q_frequency_hz is None else q_frequency_hz)
    if not math.isfinite(q_frequency_hz) or q_frequency_hz<=0:
        raise ValueError('The annotation frequency must be positive and finite.')
    rows=evaluate_segments(circuit,q_frequency_hz,spec.id)
    top10=rank_segments(rows,10)
    path_by_winding={WINDING_NAMES[w['id']]:w for w in reference['windings']}
    paths=[{'id':'main_'+w['id'],'winding':WINDING_NAMES[w['id']],'points':_scale(w['path'],SCHEMATIC_SCALE),
            'role':'winding','illustrative':mapping_kind!='source_layout'} for w in reference['windings']]
    paths.extend({'id':'tap_'+t['name'],'winding':WINDING_NAMES[t['winding']],
                  'points':_scale(t['path'],SCHEMATIC_SCALE),'role':'tap','illustrative':bool(t.get('illustrative'))}
                 for t in reference['tap_paths'])
    logical,internal_returns=_style_pins(circuit,spec,reference,port_order)
    pins=[{'name':p['name'],'position':_round([v*SCHEMATIC_SCALE for v in p['point']]),
           **{k:v for k,v in p.items() if k not in ('name','point')}} for p in logical]
    symbol_paths=[{**p,'points':_scale(next(w['path'] for w in reference['windings'] if 'main_'+w['id']==p['id']),SYMBOL_SCALE)}
                  for p in paths if p['role']=='winding']
    symbol_paths.extend({'id':'tap_'+t['name'],'winding':WINDING_NAMES[t['winding']],
                         'points':_scale(t['path'],SYMBOL_SCALE),'role':'tap','illustrative':bool(t.get('illustrative'))}
                        for t in reference['tap_paths'])
    symbol_pins=[{'name':p['name'],'position':_round([v*SYMBOL_SCALE for v in p['point']]),
                  **{k:v for k,v in p.items() if k not in ('name','point')}} for p in logical]
    elements={e['name']:e for e in circuit['elements']};placements=[];labels=[];assigned=set();segment_ranges={};associations=[]
    for index,pin in enumerate(internal_returns):
        sign=1 if pin['direction']=='top' else -1
        for factor,target in ((SCHEMATIC_SCALE,paths),(SYMBOL_SCALE,symbol_paths)):
            x,y=[value*factor for value in pin['point']]
            unit=1. if factor==SCHEMATIC_SCALE else .25
            target.append({'id':'internal_stem_'+str(index),'role':'internal_reference','winding':None,
                           'points':[_round([x,y]),_round([x,y+sign*unit*.55])]})
            for number,width in enumerate((.5,.34,.18)):
                gy=y+sign*unit*(.55+.18*number)
                target.append({'id':'internal_ground_'+str(index)+'_'+str(number),'role':'internal_reference','winding':None,
                               'points':[_round([x-width*unit,gy]),_round([x+width*unit,gy])]})
        labels.append({'position':_round([pin['point'][0]*SCHEMATIC_SCALE+1.,pin['point'][1]*SCHEMATIC_SCALE]),
                       'text':'0 (internal)','role':'internal_return'})
    branch_ranges={};branch_index=0
    for wind_index,pair in enumerate(spec.winding_pairs):
        wind='winding' if spec.device=='inductor' else ('primary' if wind_index==0 else 'secondary')
        tap_role='TAP' if spec.device=='inductor' else ('PTAP' if wind_index==0 else 'STAP')
        tap=next((t for t in reference['tap_paths'] if WINDING_NAMES[t['winding']]==wind),None)
        split=tap.get('attachment_fraction',.5) if tap else .5
        ranges=[(0.,split),(split,1.)] if tap_role in spec.tap_roles else [(0.,1.)]
        for start,stop in ranges:
            branch_ranges[branch_index]=(wind,start,stop);branch_index+=1
    def place(name,point,tangent,wind=None,segment=None,group='peripheral'):
        if name in assigned or name not in elements:
            return
        assigned.add(name);placements.append({'name':name,'kind':elements[name]['kind'],'center':_round(point),
                                               'orientation':_orientation(tangent),'orientation_basis':'path_axis','symbol_length':1.,
                                               'winding':wind,'segment':segment,'group':group})
    for branch,(wind,first,last) in branch_ranges.items():
        group=[r for r in rows if r.get('branch_index')==branch and r.get('main_l') and r.get('is_main')]
        group.sort(key=lambda r:r.get('ordinal',0))
        path=path_by_winding[wind]['path']
        for index,row in enumerate(group):
            start=first+(last-first)*index/max(1,len(group));stop=first+(last-first)*(index+1)/max(1,len(group))
            segment_ranges[row['id']]={'winding':wind,'start_fraction':start,'stop_fraction':stop}
            main=row['main_l'];resistor=row.get('resistor')
            for name,frac in ((resistor,.25),(main,.65)):
                if name and name in row['element_names']:
                    point,tangent=_at(path,start+(stop-start)*frac)
                    place(name,[v*SCHEMATIC_SCALE for v in point],tangent,wind,row['id'],'winding')
            side=-1 if wind=='primary' else 1
            candidates=[_at(path,start+(stop-start)*f) for f in (.15,.3,.5,.7,.85)]
            point,tangent=(min(candidates,key=lambda item:item[0][0]) if side<0 else max(candidates,key=lambda item:item[0][0]))
            tag=('L' if wind=='winding' else ('P' if wind=='primary' else 'S'))+'.'+str(row['winding_ordinal'])
            row['display_tag']=tag
            associations.append({'row':row,'point':[p*SCHEMATIC_SCALE for p in point],
                                 'side':side,'tag':tag,'auxiliary':[n for n in row['element_names'] if n not in assigned]})
            paths.append({'id':'segment_'+row['id'],'winding':wind,'segment':row['id'],
                          'role':'segment','points':_scale(_slice(path,start,stop),SCHEMATIC_SCALE)})
            for boundary in (start,stop):
                at,direction=_at(path,boundary);normal=[-direction[1],direction[0]]
                center=[p*SCHEMATIC_SCALE for p in at]
                paths.append({'id':'boundary_'+row['id']+'_'+str(boundary),'winding':wind,'segment':row['id'],
                              'role':'boundary','points':[_round([center[j]-normal[j]*.45 for j in (0,1)]),
                                                        _round([center[j]+normal[j]*.45 for j in (0,1)])]})
    bodypoints=[p for path in paths for p in path['points']]
    body=_bounds(bodypoints,0.)
    card_edges=[]
    for wind in path_by_winding:
        cards=sorted((a for a in associations if a['row']['winding']==wind),key=lambda a:-a['point'][1])
        card_heights=[max(4.5,2.8+2.8*math.ceil(len(a['auxiliary'])/3)) for a in cards]
        total_height=sum(card_heights)+1.5*max(0,len(cards)-1)
        top=min(body[3]+1.,max(total_height/2.,body[3]-.5))
        for card,height in zip(cards,card_heights):
            side=card['side'];row=card['row'];width=14.
            right_gap=8. if any(p['name']==circuit.get('reference','REF') for p in pins) else 5.
            left=body[0]-5.-width if side<0 else body[2]+right_gap
            right=left+width;bottom=top-height;card_edges.append(right)
            center_y=(top+bottom)/2
            edge=[right if side<0 else left,center_y]
            elbow=[body[0]-2.5 if side<0 else body[2]+2.5,card['point'][1]]
            paths.append({'id':'association_'+row['id'],'winding':wind,'segment':row['id'],'role':'association',
                          'style':'dashed','points':[_round(card['point']),_round(elbow),_round(edge)]})
            paths.append({'id':'card_'+row['id'],'winding':wind,'segment':row['id'],'role':'annotation_box',
                          'points':[_round([left,top]),_round([right,top]),_round([right,bottom]),
                                    _round([left,bottom]),_round([left,top])]})
            labels.append({'position':_round([left+.6,top-.8]),
                           'text':card['tag']+'  '+row['id']+'  Q='+_q_text(row),
                           'role':'segment_q','segment':row['id'],'winding':wind})
            labels.append({'position':_round([card['point'][0]+side*.8,card['point'][1]+.5]),
                           'text':card['tag'],'role':'segment_tag','segment':row['id'],'winding':wind})
            for index,name in enumerate(card['auxiliary']):
                place(name,[left+2.+(index%3)*4.4,top-2.7-(index//3)*2.8],[1,0],wind,row['id'],'loss_auxiliary')
            if not card['auxiliary']:
                labels.append({'position':_round([left+.6,top-2.4]),'text':'Main RL on winding arc',
                               'role':'card_note','segment':row['id'],'winding':wind})
            top=bottom-1.5
    peripheral=[e for e in circuit['elements'] if e['name'] not in assigned and e['kind']!='mind']
    for index,e in enumerate(peripheral):
        # A separate shunt/coupling component panel keeps the winding legible.
        col=index%8;row=index//8
        place(e['name'],[body[0]+col*3.0,body[1]-5.-row*3.1],[1,0],group='parasitic')
    for index,e in enumerate(e for e in circuit['elements'] if e['kind']=='mind'):
        place(e['name'],[body[0]+(index%8)*3.0,body[1]-8.-math.ceil(len(peripheral)/8)*3.1-(index//8)*2.3],
              [1,0],group='mutual')
    if assigned!=set(elements):
        raise ValueError('The visual plan did not cover every circuit element.')
    table_x=max([body[2],*card_edges])+5.;table_y=body[3]+2.
    labels.append({'position':_round([table_x,table_y]),'text':'Lowest segment Q @ %.6g GHz'%(q_frequency_hz/1e9),
                   'role':'q_table_heading'})
    for offset,text in ((0.,'Rank'),(3.5,'Segment'),(15.,'Q'),(23.,'L_eff (nH)')):
        labels.append({'position':_round([table_x+offset,table_y-1.8]),'text':text,'role':'q_table_heading'})
    for index,row in enumerate(top10):
        inductance=row.get('l_effective_h')
        ltext='N/A' if inductance is None or not math.isfinite(inductance) else '%.4g'%(inductance*1e9)
        for offset,text in ((0.,str(index+1)),(3.5,row['id']),(15.,_q_text(row)),(23.,ltext)):
            labels.append({'position':_round([table_x+offset,table_y-3.6-index*1.4]),
                           'text':text,'role':'q_table_row','segment':row['id']})
    note_y=table_y-5.-len(top10)*1.4
    labels.append({'position':_round([table_x,note_y]),'text':'Q = Im(Z_local) / Re(Z_local)', 'role':'q_table_note'})
    labels.append({'position':_round([table_x,note_y-1.3]),'text':'Mutual / shunt loading excluded', 'role':'q_table_note'})
    labels.append({'position':_round([body[0],body[3]+4.5]),
                   'text':'Model segment Q on single-turn winding guide', 'role':'heading'})
    if peripheral:
        labels.append({'position':_round([body[0],body[1]-2.7]),'text':'Shunt / substrate / coupling branches', 'role':'panel_heading'})
    plotpoints=[p for path in paths for p in path['points']]+[p['center'] for p in placements]+[p['position'] for p in pins]+[
        p['position'] for p in labels if not p['role'].startswith('q_table')]
    allpoints=plotpoints+[p['position'] for p in labels if p['role'].startswith('q_table')]
    bounds=_bounds(allpoints,3.);bounds[2]=max(bounds[2],table_x+28.)
    symbol_points=[p for path in symbol_paths for p in path['points']]+[p['position'] for p in symbol_pins]
    return {'schema':1,'model_id':spec.id,'bounds':bounds,'plot_bounds':_bounds(plotpoints,3.),
            'paths':paths,'placements':placements,'pins':pins,'labels':labels,
            'q_frequency_hz':q_frequency_hz,'segments':rows,'top10':top10,'segment_ranges':segment_ranges,
            'reference_id':'unified','reference_label':'Unified single-turn style',
            'mapping_kind':mapping_kind,'source_reference_id':source_reference['id'],
            'provenance':{'style_version':1,'winding_style':'single_turn',
                'source_reference':copy.deepcopy(source_reference['provenance']),
                'source_reference_id':source_reference['id'],'source_reference_match':source_mapping=='source_layout',
                'requested_reference_id':reference_id,
                'source_geometry_usage':'Historical reference only; source geometry and source port order never set drawn coordinates.',
                'coordinate_grid':GRID,'fixed_ground_aliases':[p['name'] for p in source_reference['pins'] if p['kind']=='ground'],
                'pin_positions':'Fixed logical model slots: inductor bottom; transformer primary top, secondary bottom; taps at respective centers; REF at right.',
                'source_port_order':ordered_source_order(port_order), 'short_unused':bool(short_unused),
                'fit_elements_changed':False,
                'segment_association':'Equal arclength allocation within each dedicated fitted branch on the standardized single-turn guide.'},
            'internal_returns':[{'name':p['name'],'position':_round([v*SCHEMATIC_SCALE for v in p['point']]),
                                 'direction':p['direction'],'model_return':p['model_return']} for p in internal_returns],
            'symbol':{'bounds':_bounds(symbol_points,1.),'paths':symbol_paths,'pins':symbol_pins,
                      'internal_returns':[{'name':p['name'],'position':_round([v*SYMBOL_SCALE for v in p['point']]),
                                           'direction':p['direction'],'model_return':p['model_return']} for p in internal_returns]}}
