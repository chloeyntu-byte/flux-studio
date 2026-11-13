"""Generic symmetric serial inductor with optional physical midpoint tap.

The topology is independent of any one foundry. All via stacks come from the
supplied technology and same-net contacts are screened for accidental bypass.
"""
import math
import geometry as common

DEFAULTS = dict(inner_diameter=100, width=8, spacing=8, turns=2,
                leg_length=45, terminal_gap=12, center_tap=False, rule_policy='warn')


def _error(part, code, message, fields=()):
    return dict(part=part, code=code, message=message, fields=list(fields))


def generate_differential(parameters, layers, technology):
    p = dict(DEFAULTS)
    p.update(parameters or {})
    result = dict(ok=False, errors=[], warnings=[], parameters=p,
                  geometry=dict(polygons=[], vias=[], ports=[], bounds=[[0, 0], [0, 0]], metrics={}))
    for key, low, high in [('inner_diameter',30,1000),('width',.1,100),('spacing',.1,100),
                           ('turns',1,8),('leg_length',5,500),('terminal_gap',.1,200)]:
        try:
            if isinstance(p[key], bool): raise ValueError()
            v = float(p[key])
            if not math.isfinite(v) or not low <= v <= high or (key == 'turns' and v != int(v)):
                raise ValueError()
            p[key] = int(v) if key == 'turns' else v
        except (TypeError, ValueError, OverflowError):
            result['errors'].append(_error('body','parameter_range', '%s must be within %s..%s%s.' % (key,low,high,' integer turns' if key=='turns' else ' um'),[key]))
    if not isinstance(p['center_tap'], bool):
        result['errors'].append(_error('body','parameter_type','center_tap must be true or false.',['center_tap']))
    if p.get('rule_policy') not in ('warn', 'strict'):
        result['errors'].append(_error('body','invalid_option','rule_policy must be warn or strict.',['rule_policy']))
    layers = dict(layers or {})
    rules = {r['name']: r for r in technology.get('layers', [])}
    shields=[role for role in ('pgs','ring') if common.shielding_role_active(role,p)]
    for role in ['body','crossing'] + shields + (['tap'] if p.get('center_tap') is True else []):
        if layers.get(role) not in rules:
            result['errors'].append(_error(role,'unknown_layer','Select a verified PDK metal for %s.' % role,[role]))
    if layers.get('body') == layers.get('crossing'):
        result['errors'].append(_error('crossing','same_underpass_layer','Crossing metal must differ from body.',['body','crossing']))
    if p.get('center_tap') is True and layers.get('tap') in (layers.get('body'),layers.get('crossing')):
        result['errors'].append(_error('tap','same_tap_layer','Tap metal must differ from body and crossing.',['tap','body','crossing']))
    if result['errors']: return result
    b = common.GeometryBuilder(p, layers, technology, result)
    p = b.parameters
    g = b.geometry
    grid=b.grid
    w=p['width']; n=p['turns']; pitch=w+p['spacing']; factor=math.sqrt(2)-1
    radii=[p['inner_diameter']/2+w/2+i*pitch for i in range(n)]
    extent=radii[-1]+w/2
    length=2*extent+2*p['leg_length']
    clearance=max(common._spacing(rules[layers[x]],w,length,True) for x in ('body','crossing'))
    if p['spacing'] < clearance+2*grid:
        b.rule_issue('body','turn_spacing','Turn spacing must be at least %.4f um.'%(clearance+2*grid),['spacing'])
    if p['terminal_gap'] < clearance+2*grid:
        b.rule_issue('legs','terminal_spacing','Terminal gap must be at least %.4f um.'%(clearance+2*grid),['terminal_gap'])
    pad=(w,w)
    if n>1:
        pad=b.landing_size(layers['body'],layers['crossing'],w)
        if pad and max(pad)>w+1e-8:
            b.errors.append(_error('crossing','via_width','Winding width is too small for a square native-via landing.',['width']))
    if p['center_tap']:
        tap_pad=b.landing_size(layers['body'],layers['tap'],w)
        if tap_pad and max(tap_pad)>w+1e-8:
            b.errors.append(_error('tap','via_width','Winding width is too small for a square tap-via landing.',['width']))
    if b.errors or pad is None: return result
    drop=pitch
    corner=max((pad[0]+pad[1])/2,w/2)
    def needed(space):
        return math.ceil((max(drop/2+pad[0]/2+clearance+2*grid,
                              corner-drop/2+math.sqrt(2)*(w/2+space+4*grid))-1e-9)/grid)*grid
    safe, preferred=needed(clearance),needed(max(p['spacing'],clearance))
    cap=math.floor((factor*radii[0]-w*factor/2-2*grid)/grid)*grid
    h=min(preferred,cap) if n>1 else min((p['terminal_gap']+w)/2,cap)
    hp=(p['terminal_gap']+w)/2
    if cap < (safe if n>1 else hp)-1e-8 or hp>cap+1e-8:
        b.errors.append(_error('body','crossover_room','Insufficient flat-edge space for the crossover landing or terminals; increase inner diameter.',['inner_diameter','spacing','width','terminal_gap']))
        return result
    if n>1 and h+1e-8<preferred:
        b.warnings.append(_error('crossing','target_gap_clamped','Crossover span was reduced to fit the available flat; PDK clearance is still required.',['inner_diameter','spacing']))
    # Keep same-net sections independent for the additional shortcut screen.
    sections=[]
    route_lengths=[]
    series_graph=[]
    def add(points,layer,part,feature,nodes,**metadata):
        before=len(g['polygons'])
        b.add_path(points,w,layer,part,'L',feature=feature,**metadata)
        created = g['polygons'][before:]
        if created and created[0].get('centerline'):
            line = created[0]['centerline']
            distance = sum(math.hypot(q[0]-a[0],q[1]-a[1]) for a,q in zip(line[:-1],line[1:]))
            route_lengths.append((feature, metadata, distance))
            if len(nodes) == 2:
                series_graph.append(dict(source=nodes[0], target=nodes[1], length=distance, feature=feature, layer=layer))
            elif len(nodes) == 3 and feature == 'center_join':
                for endpoint in nodes[:2]:
                    series_graph.append(dict(source=endpoint, target=nodes[2], length=distance/2, feature=feature, layer=layer))
        for poly in created:
            poly['section_nodes'] = list(nodes)
            sections.append((poly,frozenset(nodes),feature))
    def via(point,lower,upper,nodes,feature):
        before=len(g['polygons'])
        b.add_via_stack(point,lower,upper,w,'crossing','L')
        for poly in g['polygons'][before:]:
            poly.update(section=feature)
            sections.append((poly,frozenset(nodes),feature))
    def node(i,side,end): return '%d:%d:%s'%(i,side,end)
    for i,r in enumerate(radii):
        a=factor*r
        for side in (-1,1):
            points=[[side*h,-r],[side*a,-r],[side*r,-a],[side*r,a],[side*a,r],[side*h,r]]
            add(points,layers['body'],'body','symmetric_half_turn',[node(i,side,'bottom'),node(i,side,'top')],turn_index=i,side='left' if side<0 else 'right')
    for k,i in enumerate(range(n-1,0,-1)):
        top=(k%2==0); sign=1 if top else -1; end='top' if top else 'bottom'; outer=radii[i]; inner=radii[i-1]
        # Body route is left outer to right inner; underpass is the other diagonal.
        for side,layer in [(-1,layers['body']),(1,layers['crossing'])]:
            points=[[side*h,sign*outer],[side*drop/2,sign*outer],[-side*drop/2,sign*inner],[-side*h,sign*inner]]
            ns=[node(i,side,end),node(i-1,-side,end)]
            add(points,layer,'crossing','symmetric_crossover',ns,outer_turn=i,bridge_layer=layer)
            if layer==layers['crossing']:
                via(points[0],layers['body'],layer,[ns[0]],'cross_%d_outer'%i)
                via(points[-1],layers['body'],layer,[ns[1]],'cross_%d_inner'%i)
    end='top' if n%2 else 'bottom'; sign=1 if end=='top' else -1
    mid=[0,sign*radii[0]]
    add([[-h,mid[1]],[h,mid[1]]],layers['body'],'body','center_join',[node(0,-1,end),node(0,1,end),'mid'])
    leg_end=-(extent+p['leg_length']); port_depth=min(w,5.)
    for side,name,endpoint in [(-1,'L1','positive'),(1,'L2','negative')]:
        ns=[node(n-1,side,'bottom'),'port_'+name]
        add([[side*h,-radii[-1]],[side*hp,-radii[-1]],[side*hp,leg_end]],layers['body'],'legs','signal_lead',ns,terminal_route=name)
        g['ports'].append(dict(name=name,layer=layers['body'],bbox=[[b.snap(side*hp-w/2),b.snap(leg_end)],[b.snap(side*hp+w/2),b.snap(leg_end+port_depth)]],net='L',part='legs',kind='signal',winding='L',endpoint=endpoint,access_direction='bottom'))
    if p['center_tap']:
        via(mid,layers['body'],layers['tap'],['mid'],'tap_mid')
        top=extent+p['leg_length']
        exit_x=b.tap_exit_x(mid,w)
        add([mid,[exit_x,mid[1]],[exit_x,top]],layers['tap'],'tap','tap_route',['mid','port_CTAP'])
        g['ports'].append(dict(name='CTAP',layer=layers['tap'],bbox=[[b.snap(exit_x-w/2),b.snap(top-port_depth)],[b.snap(exit_x+w/2),b.snap(top)]],net='L',part='tap',kind='tap',winding='L',endpoint='center',access_direction='top'))
    # No unrelated conductive section may touch, even on the same net. Sections
    # sharing an explicit graph endpoint are the only allowed contacts.
    seen=set()
    for idx,(poly,nodes,feature) in enumerate(sections):
        if not poly.get('points'): continue
        for other,onodes,ofeature in sections[:idx]:
            if poly['layer']!=other['layer'] or nodes & onodes or not other.get('points'): continue
            if common.polygon_distance(poly['points'],other['points'])<=1e-8:
                key=tuple(sorted((feature,ofeature)))
                if key not in seen:
                    seen.add(key)
                    b.errors.append(_error('body','same_net_shortcut','Unintended contact between %s and %s on %s.'%(feature,ofeature,poly['layer']),['spacing','width','inner_diameter']))
    # A shared endpoint does not license a contact elsewhere on the same
    # half-turn. Check its remaining straight segments independently.
    for arc, anodes, af in sections:
        if af != 'symmetric_half_turn' or not arc.get('centerline'): continue
        centerline = arc['centerline']
        for other, onodes, of in sections:
            shared = anodes & onodes
            if not shared or of == af or arc['layer'] != other['layer'] or not other.get('points'): continue
            segments = list(zip(centerline[:-1],centerline[1:]))
            if any(x.endswith(':bottom') for x in shared): segments = segments[2:]
            if any(x.endswith(':top') for x in shared): segments = segments[:-2]
            for start,stop in segments:
                if common.polygon_distance(common._stroke([start,stop],w),other['points']) <= 1e-8:
                    key=('endpoint_bypass',af,of)
                    if key not in seen:
                        seen.add(key)
                        b.errors.append(_error('body','endpoint_shortcut','A connected section also touches a non-endpoint portion of a half-turn.',['spacing','width','inner_diameter']))
                    break
    # Arc and X routes are mirror counterparts: total length on each side is
    # equal by construction, including equal-width endpoint leads.
    adjacency = {}
    for edge in series_graph:
        if edge['feature'] == 'tap_route': continue
        adjacency.setdefault(edge['source'], []).append((edge['target'], edge['length']))
        adjacency.setdefault(edge['target'], []).append((edge['source'], edge['length']))
    def midpoint_distance(start):
        pending=[(start,None,0.)]
        while pending:
            current,previous,distance=pending.pop()
            if current == 'mid': return distance
            pending.extend((nxt,current,distance+span) for nxt,span in adjacency.get(current,[]) if nxt != previous)
        return None
    left_length, right_length = midpoint_distance('port_L1'), midpoint_distance('port_L2')
    equal_lengths = left_length is not None and right_length is not None and abs(left_length-right_length) <= 4*grid
    if not equal_lengths:
        b.errors.append(_error('body','midpoint_balance','The actual serial paths to the differential midpoint are not balanced.',['inner_diameter','terminal_gap']))
    g['metrics'].update(model='differential_inductor',body_extent=extent,octagonal_center=[0,0],
        turn_centerline_radii={'L':radii},tap_midpoint=mid,leg_bottom=leg_end,
        terminal_directions={'L':'bottom'},symmetric_half_count=2*n,
        symmetric_arm_lengths_equal=equal_lengths,half_path_lengths={'L1_to_midpoint': left_length, 'L2_to_midpoint': right_length},
        series_graph=series_graph,crossover_count=max(0,n-1),center_join_side=end)
    if b.errors: return result
    final = common.postprocess_geometry(result,layers,technology,body_extent=extent,expected_windings=['L'])
    for port in final['geometry']['ports']:
        if port.get('net') == 'GND':
            port.setdefault('kind','ground')
    return final
