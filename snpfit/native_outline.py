"""Native-only conductor outlines and electrically honest winding routes.

The shared Tk illustration is input data and is never modified. Main R/L
devices use straight portions of the single-turn winding; chamfer corners are
empty transitions because IC618 OA instances have orthogonal orientations.
"""
from __future__ import annotations

import copy
import math

GRID = .0625
STYLE = {'schema': 2, 'winding_render': 'double_edge',
         'main_devices': 'cardinal_flats', 'schematic_strip_width': .375,
         'symbol_strip_width': .1875, 'visual_terminal_stub_length': .125}


def snap(point):
    return [round(float(value)/GRID)*GRID for value in point]


def _distance(a, b):
    return math.hypot(b[0]-a[0], b[1]-a[1])


def _segments(path):
    offset=0.
    result=[]
    for a,b in zip(path,path[1:]):
        length=_distance(a,b)
        if length:
            result.append((offset,offset+length,a,b,[(b[i]-a[i])/length for i in (0,1)]))
            offset+=length
    return result,offset


def _at(path, distance):
    segments,total=_segments(path)
    distance=min(total,max(0.,distance))
    for first,last,a,b,tangent in segments:
        if distance<=last+1e-10:
            return [a[i]+(distance-first)*tangent[i] for i in (0,1)],tangent
    return list(path[-1]),segments[-1][-1]


def _slice(path, start, stop):
    if stop<start:
        return list(reversed(_slice(path,stop,start)))
    segments,total=_segments(path)
    first,_=_at(path,start);last,_=_at(path,stop)
    points=[first]
    points.extend(list(b) for aoffset,boffset,a,b,tangent in segments if start+1e-9<boffset<stop-1e-9)
    points.append(last)
    result=[]
    for point in points:
        point=snap(point)
        if not result or point!=result[-1]:result.append(point)
    return result


def offset_polyline(points, distance):
    """Miter an open 0/45/90-degree center path without closing its pin slots."""
    points=[list(point) for point in points]
    normals=[]
    for a,b in zip(points,points[1:]):
        length=_distance(a,b)
        if not length:raise ValueError('A conductor center path contains a zero-length edge.')
        normals.append([-(b[1]-a[1])/length,(b[0]-a[0])/length])
    out=[]
    for index,p in enumerate(points):
        if index==0:delta=normals[0]
        elif index==len(points)-1:delta=normals[-1]
        else:
            a,b=normals[index-1],normals[index]
            denominator=1+a[0]*b[0]+a[1]*b[1]
            if denominator<.1:raise ValueError('A conductor outline cannot double back on itself.')
            delta=[(a[i]+b[i])/denominator for i in (0,1)]
        # Use exactly representable OA coordinates. The two mitered edges
        # remain parallel on these symmetric 45-degree chamfers.
        out.append([round((p[i]+distance*delta[i])/.03125)*.03125 for i in (0,1)])
    return out


def _flat_slots(path, first, last, count, keepouts=()):
    if not count:return []
    segments,total=_segments(path);intervals=[]
    for start,stop,a,b,tangent in segments:
        if abs(tangent[0]*tangent[1])>1e-8:continue
        lo=max(start,first*total)+.375
        hi=min(stop,last*total)-.375
        if hi>lo:
            pieces=[(lo,hi)]
            for blocked_start,blocked_stop in keepouts:
                reduced=[]
                for left,right in pieces:
                    if blocked_stop<=left or blocked_start>=right:reduced.append((left,right))
                    else:
                        if blocked_start>left:reduced.append((left,blocked_start))
                        if blocked_stop<right:reduced.append((blocked_stop,right))
                pieces=reduced
            intervals.extend(pieces)
    available=sum(stop-start for start,stop in intervals)
    if available<count*.75:return None
    result=[]
    for index in range(count):
        remaining=(index+.5)*available/count
        for start,stop in intervals:
            if remaining<=stop-start+1e-10:
                position=start+remaining;point,tangent=_at(path,position)
                result.append((snap(point),tangent,position));break
            remaining-=stop-start
    return result


def _axis(tangent, reverse=False):
    angle=int(round(math.degrees(math.atan2(tangent[1],tangent[0]))/90))*90
    return 'R%d'%((angle+(180 if reverse else 0))%360)


def _qtext(row):
    q=row.get('q')
    if q is None:return 'Inf' if row.get('status')=='lossless' else 'N/A'
    return format(q,'.4g') if math.isfinite(q) else 'N/A'


def _value(element):
    value=float(element['value'])
    for scale,prefix in ((1e9,'G'),(1e6,'M'),(1e3,'k'),(1.,''),(1e-3,'m'),(1e-6,'u'),(1e-9,'n'),(1e-12,'p'),(1e-15,'f')):
        if value>=scale:
            return element['name']+'='+format(value/scale,'.3g')+prefix+('H' if element['kind']=='ind' else 'Ohm')
    return element['name']+'='+format(value,'.3g')+('H' if element['kind']=='ind' else 'Ohm')


def _row_members(row, elements):
    result=[]
    resistor=elements.get(row.get('resistor'))
    if resistor:
        reverse=resistor['n2']==row['start_net']
        result.append((resistor,reverse))
    main=elements[row['main_l']]
    result.append((main,False))
    return result


def _center_paths(raw, scale):
    from .native_overlap import overlapping_paths, transformer_style
    result=[{**copy.deepcopy(p),'points':[snap([v*scale for v in point]) for point in p['points']]}
            for p in raw['paths'] if p.get('role') in ('winding','tap')]
    return overlapping_paths(result,transformer_style(raw))


def _role_map(circuit, raw):
    from .models import get_model
    spec=get_model(raw['model_id'])
    return {**dict(zip(spec.port_roles,circuit['ports'])),'REF':circuit.get('reference','REF')}


def _position_pins(circuit, raw, paths, pins):
    from .models import get_model
    spec=get_model(raw['model_id'])
    roles=_role_map(circuit,raw)
    for index,pair in enumerate(spec.winding_pairs):
        wind='winding' if spec.device=='inductor' else ('primary' if index==0 else 'secondary')
        path=next(p for p in paths if p['role']=='winding' and p['winding']==wind)
        for end,name in enumerate(pair):
            if name=='REF':
                return_role='P' if index==0 else 'S'
                pin=next((p for p in pins if p.get('model_return')==roles.get(return_role,return_role)),None)
            else:pin=next((p for p in pins if p['name']==roles[name]),None)
            if pin:pin['position']=list(path['points'][0 if end==0 else -1])
    right=max(p[0] for path in paths if path['role']=='winding' for p in path['points'])
    ref_y=(max(p[1] for path in paths if path['role']=='winding' for p in path['points'])+.25
           if spec.device=='transformer' else 0.)
    for pin in pins:
        if pin.get('position_kind')=='unified_style' and pin['name']==circuit.get('reference','REF'):
            pin['position']=snap([right+1.,ref_y])


def _core_paths(paths):
    # These are planar air-core winding illustrations.  Magnetic-core bars
    # between previously separated coils misrepresented the physical shape.
    return []


def _internal_ground_paths(circuit, raw, paths):
    from .models import get_model
    if circuit.get('metadata',{}).get('export_reference',circuit.get('metadata',{}).get('explicit_reference',True)):
        return []
    spec=get_model(raw['model_id']);result=[]
    for index,pair in enumerate(spec.winding_pairs):
        if pair[1]!='REF':continue
        wind='winding' if spec.device=='inductor' else ('primary' if index==0 else 'secondary')
        path=next(p for p in paths if p['role']=='winding' and p['winding']==wind)
        x,y=path['points'][-1];sign=1 if wind=='primary' else -1
        result.append({'id':'internal_ref_'+str(index),'role':'internal_reference','winding':wind,
                       'points':[[x,y],[x,y+sign*.375]]})
        for number,width in enumerate((.25,.1875,.125)):
            gy=y+sign*(.375+number*.125)
            result.append({'id':'internal_ground_%d_%d'%(index,number),'role':'internal_reference','winding':wind,
                           'points':[[x-width,gy],[x+width,gy]]})
    return result


def _fix_tap_paths(paths, pins, transformer, roles):
    """Connect each midpoint naturally outward to a centered tap terminal."""
    if not transformer:return
    for path in paths:
        if path.get('role')!='tap':continue
        name=path['id'][4:]
        if name not in ('PTAP','STAP'):continue
        parent=next(p for p in paths if p.get('role')=='winding' and p['winding']==path['winding'])
        midpoint,_=_at(parent['points'],_segments(parent['points'])[1]/2)
        pin=next(p for p in pins if p['name']==roles.get(name,name))
        sign=-1 if name=='PTAP' else 1
        midpoint=snap(midpoint)
        # Coplanar equal-length electrical halves need not meet at x=0.
        # A short 45-degree outward tap puts the public terminal between the
        # two signal legs, rather than almost coincident with a signal pin.
        elbow=snap([0.,midpoint[1]+sign*abs(midpoint[0])])
        y=sign*max(abs(pin['position'][1]),abs(elbow[1])+.5)
        pin['position']=snap([0.,y]);pin['direction']='bottom' if sign<0 else 'top'
        path['points']=([midpoint,elbow] if midpoint!=elbow else [midpoint])+[list(pin['position'])]


def _outline_paths(paths, width):
    result=[]
    for path in paths:
        for side,distance in (('left',width/2),('right',-width/2)):
            result.append({'id':path['id']+'_'+side,'role':'conductor_edge','winding':path['winding'],
                           'center_path':path['id'],'side':side,
                           'points':offset_polyline(path['points'],distance)})
    return result


def _endpoint(node, point, pins, reference):
    found=next((p for p in pins if p['name']==node and _distance(p['position'],point)<1e-6),None)
    if found:return {'pin':node,'position':list(point)}
    return {'net':node,'position':list(point)}


def _term(item, forward):
    element,reverse=item['element'],item['reverse']
    term='PLUS' if forward!=reverse else 'MINUS'
    return {'component':element['name'],'terminal':term},element['n1' if term=='PLUS' else 'n2']


def _route(id, net, start, end, points):
    points=[snap(point) for point in points]
    if len(points)<2:return None
    return {'id':id,'net':net,'start':start,'end':end,'points':points}


def _main_wires(circuit, raw, paths, pins, placed):
    from .models import get_model
    spec=get_model(raw['model_id']);reference=circuit.get('reference','REF');routes=[];roles=_role_map(circuit,raw)
    for index,pair in enumerate(spec.winding_pairs):
        winding='winding' if spec.device=='inductor' else ('primary' if index==0 else 'secondary')
        path=next(p for p in paths if p['role']=='winding' and p['winding']==winding)
        points=path['points'];total=_segments(points)[1]
        items=sorted((p for p in placed if p['winding']==winding),key=lambda p:p['arc'])
        nodes=[roles[n] for n in pair]
        anchors=[{'arc':0.,'before':_endpoint(nodes[0],points[0],pins,reference),'before_net':nodes[0],
                  'after':_endpoint(nodes[0],points[0],pins,reference),'after_net':nodes[0]}]
        for item in items:
            before,bnet=_term(item,True);after,anet=_term(item,False)
            anchors.append({'arc':item['arc'],'before':before,'before_net':bnet,'after':after,'after_net':anet})
        anchors.append({'arc':total,'before':_endpoint(nodes[1],points[-1],pins,reference),'before_net':nodes[1],
                        'after':_endpoint(nodes[1],points[-1],pins,reference),'after_net':nodes[1]})
        tap=next((p for p in paths if p.get('role')=='tap' and p['winding']==winding),None)
        if tap:
            name=roles[tap['id'][4:]];point,_=_at(points,total/2)
            junction={'net':name,'position':snap(point)}
            anchors.append({'arc':total/2,'before':junction,'before_net':name,'after':junction,'after_net':name})
            anchors.sort(key=lambda anchor:anchor['arc'])
        for number,(a,b) in enumerate(zip(anchors,anchors[1:])):
            if a['after_net']!=b['before_net']:continue
            route=_route(winding+'_wire_'+str(number),a['after_net'],a['after'],b['before'],_slice(points,a['arc'],b['arc']))
            if route:routes.append(route)
        if tap:
            start=_endpoint(name,tap['points'][-1],pins,reference)
            route=_route(name+'_wire',name,start,junction,list(reversed(tap['points'])))
            if route:routes.append(route)
    return routes


def _bounds(paths, pins, placements, labels):
    points=[point for p in paths for point in p['points']]
    points.extend(p['position'] for p in pins);points.extend(p['center'] for p in placements)
    for label in labels:
        x,y=label['position'];role=label.get('role','')
        height=.3125 if role.startswith('metric_') and role!='metric_note' else (.125 if role in ('segment_q','q_table_heading','q_table_row','q_table_note','card_note') else .1875)
        length=len(label['text'])*height*.65
        just=label.get('justification','lowerLeft')
        if just.endswith('Center'):x-=length/2
        if just.endswith('Right'):x-=length
        points.extend(([x,y-.25],[x+length,y+height]))
    return [[min(p[0] for p in points)-.75,min(p[1] for p in points)-.75],
            [max(p[0] for p in points)+.75,max(p[1] for p in points)+.75]]


def _metric_labels(circuit, raw, body):
    from .native_metrics import build_report
    report=build_report(circuit,model_id=raw['model_id'],**raw.get('native_report_context',{}))
    columns=report['columns'];rows=report['rows'];left=body[0][0];bottom=body[1][1]+1.
    top=bottom+1.125+.5*len(rows)
    label_width=2.5;column_width=2.5;right=left+label_width+column_width*len(columns)
    labels=[{'role':'metric_title','position':[left,top+.5],'text':'FITTED RESPONSE','justification':'lowerLeft'}]
    labels.append({'role':'metric_header','position':[left+.1875,top-.4375],'text':'Metric','justification':'lowerLeft'})
    for index,column in enumerate(columns):
        text=column['label']+('*' if column.get('in_fit_band') is False else '')
        labels.append({'role':'metric_header','position':[left+label_width+(index+.5)*column_width,top-.4375],
                       'text':text,'justification':'lowerCenter'})
    for index,row in enumerate(rows):
        y=top-1.-index*.5
        labels.append({'role':'metric_header','position':[left+.1875,y],
                       'text':'k' if row['key']=='k' else row['label'],'justification':'lowerLeft'})
        for number,cell in enumerate(row['cells']):
            labels.append({'role':'metric_value','position':[left+label_width+(number+.5)*column_width,y],
                           'text':cell['text'],'justification':'lowerCenter'})
    for index,note in enumerate(report.get('notes',[])):
        labels.append({'role':'metric_note','position':[left,bottom-.25-.1875*index],'text':note,'justification':'upperLeft'})
    paths=[{'id':'metric_frame','role':'metric_frame','winding':'0','points':[[left,top],[right,top],[right,bottom],[left,bottom],[left,top]]},
           {'id':'metric_header_rule','role':'metric_frame','winding':'0','points':[[left,top-.625],[right,top-.625]]}]
    for index in range(len(columns)):
        x=left+label_width+index*column_width
        paths.append({'id':'metric_column_'+str(index),'role':'metric_frame','winding':'0','points':[[x,top],[x,bottom]]})
    return report,paths,labels


def project(circuit, raw):
    from .native_crossings import segment_intersection
    from .native_overlap import crossing_keepouts, overlapping_paths, transformer_style
    """Return the effective native plan; no source/circuit object is mutated."""
    output=copy.deepcopy(raw);elements={e['name']:e for e in circuit['elements']}
    rows=[r for r in raw.get('segments',[]) if r.get('is_main') and r.get('main_l') in elements]
    grouped={}
    for row in rows:grouped.setdefault(row['branch_index'],[]).append(row)
    transformer=raw['model_id'].startswith('transformer_')
    roles=_role_map(circuit,raw)
    style=transformer_style(raw)
    scale=.25
    for attempt in range(24):
        center_paths=_center_paths(raw,scale);slots={};failed=False
        pins=[{**copy.deepcopy(p),'position':snap([v*scale for v in p['position']])} for p in raw['pins']]
        _position_pins(circuit,raw,center_paths,pins)
        _fix_tap_paths(center_paths,pins,transformer,roles)
        for branch,branch_rows in grouped.items():
            branch_rows.sort(key=lambda r:r['ordinal'])
            first=min(raw['segment_ranges'][r['id']]['start_fraction'] for r in branch_rows)
            last=max(raw['segment_ranges'][r['id']]['stop_fraction'] for r in branch_rows)
            path=next(p['points'] for p in center_paths if p['role']=='winding' and p['winding']==branch_rows[0]['winding'])
            members=[(r,e,reverse) for r in branch_rows for e,reverse in _row_members(r,elements)]
            keepouts=crossing_keepouts(center_paths,branch_rows[0]['winding'],segment_intersection) if transformer else ()
            chosen=_flat_slots(path,first,last,len(members),keepouts)
            if chosen is None:failed=True;break
            slots[branch]=list(zip(members,chosen))
        if not failed:break
        scale*=1.125
    else:raise ValueError('The finite native drawing cannot accommodate the main winding devices.')
    body=[[min(p[0] for path in center_paths for p in path['points']),min(p[1] for path in center_paths for p in path['points'])],
          [max(p[0] for path in center_paths for p in path['points']),max(p[1] for path in center_paths for p in path['points'])]]
    placements=[];main_names=set();placed=[];labels=[]
    for branch,entries in slots.items():
        for (row,element,reverse),(point,tangent,arc) in entries:
            main_names.add(element['name'])
            placements.append({'name':element['name'],'kind':element['kind'],'center':point,'orientation':_axis(tangent,reverse),
                               'winding':row['winding'],'segment':row['id'],'group':'winding'})
            placed.append({'element':element,'reverse':reverse,'winding':row['winding'],'arc':arc})
            normal=[-tangent[1],tangent[0]]
            winding_path=next(p for p in center_paths if p['role']=='winding' and p['winding']==row['winding'])
            winding_center=([0.,0.] if transformer else
                            [(min(p[i] for p in winding_path['points'])+max(p[i] for p in winding_path['points']))/2 for i in (0,1)])
            if sum(normal[i]*(point[i]-winding_center[i]) for i in (0,1))>0:normal=[-v for v in normal]
            if transformer:
                path_segments,total_length=_segments(winding_path['points'])
                lead=arc<path_segments[0][1] or arc>path_segments[-1][0]
                outer=(row['winding']=='primary' if style=='stacked' else
                       (row['winding']=='primary' and point[0]<0) or
                       (row['winding']=='secondary' and point[0]>0))
                # Terminal-leg labels face away from the centered tap even
                # when the leg belongs to an inner half of the winding.
                outer=outer or lead
                if outer:normal=[-v for v in normal]
            position=snap([point[i]+normal[i]*.5 for i in (0,1)])
            just='centerRight' if normal[0]<-.5 else ('centerLeft' if normal[0]>.5 else ('upperCenter' if normal[1]<0 else 'lowerCenter'))
            for text,offset in ((element['name'],.125),(_value(element).split('=',1)[1],-.125)):
                labels.append({'text':text,'position':snap([position[0],position[1]+offset]),
                               'role':'device_annotation','justification':just})
            if element['kind']=='ind':
                qposition=([position[0],position[1]-.5625] if abs(normal[0])>.5 else
                           [point[i]+normal[i]*1.125 for i in (0,1)])
                labels.append({'text':row.get('display_tag',row['id'])+'  Q='+_qtext(row),'position':snap(qposition),
                               'role':'segment_q','segment':row['id'],'justification':just})
    # Keep compact local-loss/shunt/mutual panels with their original mapping.
    for placement in raw['placements']:
        if placement['name'] in main_names:continue
        center=snap([v*scale for v in placement['center']])
        placements.append({**copy.deepcopy(placement),'center':center})
    paths=_outline_paths(center_paths,STYLE['schematic_strip_width'])
    paths.extend(_core_paths(center_paths));paths.extend(_internal_ground_paths(circuit,raw,center_paths))
    for path in raw['paths']:
        if path.get('role')=='annotation_box':
            if path.get('role')=='annotation_box' and not any(p.get('segment')==path.get('segment') and p.get('group')=='loss_auxiliary' for p in placements):continue
            points=[snap([v*scale for v in point]) for point in path['points']]
            paths.append({**copy.deepcopy(path),'points':points})
    for row in rows:
        auxiliary=[p for p in placements if p.get('segment')==row['id'] and p.get('group')=='loss_auxiliary']
        if not auxiliary:continue
        top=max(p['center'][1] for p in auxiliary)+.5;left=min(p['center'][0] for p in auxiliary)-.375
        labels.append({'text':'Local loss: '+row.get('display_tag',row['id']),'position':[left,top],
                       'role':'card_note','segment':row['id'],'justification':'lowerLeft'})
    if any(p.get('group') in ('parasitic','mutual') for p in placements):
        labels.append({'text':'Shunt, substrate and mutual elements','position':[body[0][0],body[0][1]-.625],
                       'role':'panel_heading','justification':'lowerLeft'})
    # The existing lowest-ten information stays available with secondary weight.
    table_x=body[1][0]+1.5;table_y=min([body[0][1]-1.,*[p['center'][1]-.75 for p in placements if p.get('group')=='loss_auxiliary']])
    labels.append({'text':'Lowest local Q @ %.4g GHz'%(raw['q_frequency_hz']/1e9),'position':[table_x,table_y],
                   'role':'q_table_note','justification':'lowerLeft'})
    for offset,text in ((0.,'#'),(.5,'Segment'),(2.,'Q'),(3.2,'L_eff (nH)')):
        labels.append({'text':text,'position':[table_x+offset,table_y-.375],'role':'q_table_heading','justification':'lowerLeft'})
    for index,row in enumerate(raw.get('top10',[])):
        inductance=row.get('l_effective_h');ltext='N/A' if inductance is None else format(inductance*1e9,'.4g')
        for offset,text in ((0.,str(index+1)),(.5,row['id']),(2.,_qtext(row)),(3.2,ltext)):
            labels.append({'text':text,'position':[table_x+offset,table_y-.75-index*.3125],'role':'q_table_row','justification':'lowerLeft'})
    notes_y=table_y-1.-len(raw.get('top10',[]))*.3125
    labels.append({'text':'Local Q excludes mutual and shunt loading.','position':[table_x,notes_y],
                   'role':'q_table_note','justification':'upperLeft'})
    report,metric_paths,metric_labels=_metric_labels(circuit,raw,body)
    paths.extend(metric_paths);labels.extend(metric_labels)
    labels.append({'text':'Single-turn outline. Values and annotations are exported snapshots.',
                   'position':[body[0][0],min(p['center'][1] for p in placements)-.75],
                   'role':'device_annotation_note','justification':'lowerLeft'})
    for label in labels:label['position']=snap(label['position'])
    symbol=copy.deepcopy(raw['symbol'])
    symbol_centers=overlapping_paths([copy.deepcopy(p) for p in symbol['paths'] if p.get('role') in ('winding','tap')],style)
    _position_pins(circuit,raw,symbol_centers,symbol['pins'])
    _fix_tap_paths(symbol_centers,symbol['pins'],transformer,roles)
    symbol['paths']=_outline_paths(symbol_centers,STYLE['symbol_strip_width'])
    symbol['paths'].extend(_core_paths(symbol_centers));symbol['paths'].extend(_internal_ground_paths(circuit,raw,symbol_centers))
    symbol['labels']=[]
    symbol['bounds']=_bounds(symbol['paths'],symbol['pins'],[],[])
    wires=_main_wires(circuit,raw,center_paths,pins,placed)
    output.update(paths=paths,pins=pins,placements=placements,labels=labels,symbol=symbol,
                  bounds=_bounds(paths,pins,placements,labels),center_paths=center_paths,
                  winding_wires=wires,native_report=report,native_style=copy.deepcopy(STYLE),
                  native_transform={**STYLE,'schematic_coordinate_scale':scale,'symbol_coordinate_scale':1.0,
                                    'transformer_style':style if transformer else None,'winding_center':[0.,0.]},
                  body_bounds=body,metric_table_bounds=[metric_paths[0]['points'][3],metric_paths[0]['points'][1]])
    return output
