"""Strict visual-plan handoff and read-back checks for native cellviews.

Geometry is independent of the fitted electrical graph.  R0/R90/R180/R270 in
the plan denote the desired PLUS-to-MINUS axis, not an assumed master rotation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re

_ORIENTATIONS = {'R0', 'R90', 'R180', 'R270'}
_DIRECTIONS = {'left', 'right', 'top', 'bottom'}
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_NATIVE_TRANSFORM = {'schema': 1, 'schematic_coordinate_scale': 0.25, 'grid': 0.0625,
                     'symbol_coordinate_scale': 1.0, 'visual_terminal_stub_length': 0.125, 'device_value_labels': 'main winding R/L, 3 significant digits'}
_JUSTIFICATIONS = {'lowerLeft', 'lowerCenter', 'lowerRight', 'centerLeft', 'centerCenter',
                   'centerRight', 'upperLeft', 'upperCenter', 'upperRight'}


def file_sha256(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular native evidence file: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def circuit_fingerprint(circuit):
    metadata = circuit.get('metadata', {})
    elements = []
    for e in sorted(circuit['elements'], key=lambda item: item['name']):
        keys = ('kind', 'name', 'ind1', 'ind2') if e['kind'] == 'mind' else ('kind', 'name', 'n1', 'n2')
        elements.append({**{key: e[key] for key in keys}, 'value': float(e['value'])})
    return json_hash({'ports': circuit['ports'], 'reference': circuit.get('reference', 'REF'),
                      'export_reference': metadata.get('export_reference', metadata.get('explicit_reference', True)),
                      'elements': elements})


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Visual coordinates must contain two numbers.')
    point = [float(v) for v in value]
    if any(not math.isfinite(v) or abs(v) > 100000 for v in point):
        raise ValueError('Visual coordinates must be finite and bounded.')
    return point


def _bounds(value):
    if isinstance(value, (list, tuple)) and len(value) == 4:
        low, high = _point(value[:2]), _point(value[2:])
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = _point(value[0]), _point(value[1])
    else:
        raise ValueError('Visual bounds must contain two corner points.')
    if low[0] >= high[0] or low[1] >= high[1]:
        raise ValueError('Visual bounds must have positive width and height.')
    return [low, high]


def _safe_data(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_data(v) for v in value]
    return value


def _geometry(section, logical_pins):
    if not isinstance(section, dict):
        raise ValueError('Visual geometry must be an object.')
    output = {'bounds': _bounds(section['bounds']), 'paths': [], 'pins': [], 'labels': [],
              'port_order': list(logical_pins)}
    paths = section.get('paths', [])
    if len(paths) > 128:
        raise ValueError('Too many visual winding paths.')
    total = 0
    for index, path in enumerate(paths):
        points = [_point(p) for p in path['points']]
        total += len(points)
        if len(points) < 2 or total > 20000:
            raise ValueError('Visual winding paths require a bounded point list.')
        saved={'id': str(path.get('id', index)), 'winding': str(path.get('winding', '0')), 'points': points}
        for key in ('role','side','center_path','segment'):
            if key in path:saved[key]=str(path[key])
        output['paths'].append(saved)
    pins = section.get('pins', [])
    if not 1 <= len(pins) <= 64:
        raise ValueError('Visual geometry needs a bounded set of external pins.')
    seen = set()
    occupied = {}
    for pin in pins:
        name = pin['name']
        if name not in logical_pins:
            raise ValueError('Visual pin is outside the selected electrical port contract: ' + str(name))
        point = _point(pin['position'])
        direction = {'up': 'top', 'down': 'bottom'}.get(pin.get('direction'), pin.get('direction'))
        if direction not in _DIRECTIONS:
            raise ValueError('Visual pin direction must be left, right, top or bottom.')
        key = tuple(round(v, 8) for v in point)
        if (name, key) in seen or key in occupied and occupied[key] != name:
            raise ValueError('Visual pin anchors must be distinct; different nets cannot share an anchor.')
        seen.add((name, key)); occupied[key] = name
        physical = pin.get('physical_names', [])
        if not isinstance(physical, list) or len(physical) > 32 or not all(isinstance(p, str) and len(p) <= 128 for p in physical):
            raise ValueError('Physical pin names must be a bounded list of strings.')
        output['pins'].append({'name': name, 'position': point, 'direction': direction, 'physical_names': list(physical)})
    if {p['name'] for p in output['pins']} != set(logical_pins):
        raise ValueError('Visual pin set must equal the electrical external port set.')
    for item in section.get('labels', []):
        text = item['text']
        if not isinstance(text, str) or len(text) > 1000:
            raise ValueError('Visual labels must be short strings.')
        justification = item.get('justification', 'lowerLeft')
        if justification not in _JUSTIFICATIONS:
            raise ValueError('Unsupported native annotation justification.')
        output['labels'].append({'text': text, 'position': _point(item['position']),
                                 'role': str(item.get('role', 'annotation')), 'justification': justification})
    if len(output['labels']) > 2048:
        raise ValueError('Too many visual labels.')
    return output


def _quarter_point(point):
    return [round(float(v)*0.25/0.0625)*0.0625 for v in point]


def _main_value_text(element):
    value=float(element['value'])
    for scale,prefix in ((1e12,'T'),(1e9,'G'),(1e6,'M'),(1e3,'k'),(1.,''),
                         (1e-3,'m'),(1e-6,'u'),(1e-9,'n'),(1e-12,'p'),(1e-15,'f')):
        if value>=scale:
            return element['name']+'='+format(value/scale,'.3g')+prefix+('H' if element['kind']=='ind' else 'Ohm')
    return element['name']+'='+format(value,'.3g')+('H' if element['kind']=='ind' else 'Ohm')


def _native_schematic_transform(output, circuit, raw_plan):
    """Shrink coordinates, retaining real analogLib masters and readable text.

    The common Tk plan and compact symbol are deliberately unchanged.  Applying
    this handoff to an already normalized plan is idempotent.
    """
    if raw_plan.get('native_transform') == _NATIVE_TRANSFORM:
        output['native_transform'] = dict(_NATIVE_TRANSFORM)
        return
    output['bounds'] = [_quarter_point(p) for p in output['bounds']]
    rendered_paths = []
    for path in output['paths']:
        points = []
        for point in path['points']:
            point = _quarter_point(point)
            if not points or point != points[-1]:
                points.append(point)
        if len(points) >= 2:
            path['points'] = points
            rendered_paths.append(path)
    output['paths'] = rendered_paths
    for pin in output['pins']:
        pin['position'] = _quarter_point(pin['position'])
    for placement in output['placements']:
        placement['center'] = _quarter_point(placement['center'])
    for label in output['labels']:
        label['position'] = _quarter_point(label['position'])
    by_name = {e['name']: e for e in circuit['elements']}
    raw_placements = {p['name']:p for p in raw_plan['placements']}
    for placement in output['placements']:
        raw = raw_placements[placement['name']]
        element = by_name[placement['name']]
        if raw.get('group') != 'winding' or element['kind'] not in ('ind','res'):
            continue
        angle = math.radians(int(placement['orientation'][1:]))
        normal = [-math.sin(angle), math.cos(angle)]
        center = placement['center']
        if normal[0]*center[0]+normal[1]*center[1] < 0:
            normal = [-v for v in normal]
        if raw.get('winding') == 'secondary':
            normal = [-v for v in normal]
        # Vertical devices sit beside external Q cards.  Their compact value
        # text faces the winding interior rather than entering a card header.
        if placement['orientation'] in ('R90', 'R270'):
            normal = [1.0 if center[0] < 0 else -1.0, 0.0]
        position = [round((center[i]+0.4375*normal[i])/0.0625)*0.0625 for i in (0,1)]
        if abs(normal[0]) > 0.5:
            justification = 'centerRight' if normal[0]<0 else 'centerLeft'
        else:
            justification = 'upperCenter' if normal[1]<0 else 'lowerCenter'
        output['labels'].append({'text': _main_value_text(element), 'position': position,
                                 'role': 'device_annotation', 'justification': justification})
    output['labels'].append({'text': 'Main R/L values: exported fit snapshot.',
                             'position': [output['bounds'][0][0], output['bounds'][0][1]-0.375],
                             'role': 'device_annotation_note', 'justification': 'lowerLeft'})
    output['native_transform'] = dict(_NATIVE_TRANSFORM)


def validate_visual_plan(circuit, plan):
    """Return a normalized copy; never change the circuit or caller's plan."""
    if not isinstance(plan, dict) or plan.get('schema') != 1:
        raise ValueError('A schema-1 visual plan is required.')
    if plan.get('mapping_kind')=='unified_single_turn' and not plan.get('native_style'):
        from .native_outline import project
        plan=project(circuit,plan)
    ports = list(circuit['ports'])
    metadata = circuit.get('metadata', {})
    if metadata.get('export_reference', metadata.get('explicit_reference', True)):
        ports.append(circuit.get('reference', 'REF'))
    output = _geometry(plan, ports)
    output['schema'] = 1
    output['placements'] = []
    names = [e['name'] for e in circuit['elements']]
    for placement in plan.get('placements', []):
        name = placement['name']
        orientation = placement.get('orientation', 'R0')
        if name not in names or orientation not in _ORIENTATIONS:
            raise ValueError('Each visual placement needs an existing component and a cardinal axis.')
        item={'name': name, 'center': _point(placement['center']), 'orientation': orientation}
        for key in ('kind','winding','segment','group'):
            if key in placement:item[key]=placement[key]
        output['placements'].append(item)
    actual_names = [p['name'] for p in output['placements']]
    if len(actual_names) != len(set(actual_names)) or set(actual_names) != set(names):
        raise ValueError('Visual placements must contain every electrical component exactly once.')
    frequency = float(plan['q_frequency_hz'])
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError('Q annotation frequency must be positive and finite.')
    output['q_frequency_hz'] = frequency
    output['segments'] = _safe_data(copy.deepcopy(plan.get('segments', [])))
    output['top10'] = _safe_data(copy.deepcopy(plan.get('top10', [])))
    output['symbol'] = _geometry(plan['symbol'], ports)
    if plan.get('native_style'):
        from .native_outline import STYLE
        if plan['native_style']!=STYLE:raise ValueError('Unsupported native conductor-outline style.')
        for key in ('native_style','native_transform','native_report','native_report_context','model_id','body_bounds','metric_table_bounds'):
            if key in plan:output[key]=_safe_data(copy.deepcopy(plan[key]))
        output['center_paths']=_safe_data(copy.deepcopy(plan.get('center_paths',[])))
        from .native_crossings import protect_wires
        routes=_validate_wires(circuit,plan.get('winding_wires',[]),output['pins'])
        protected,crossings=protect_wires(circuit,routes,output['placements'])
        output['winding_wires']=_validate_wires(circuit,protected,output['pins'])
        output['native_crossings']=_safe_data(copy.deepcopy(plan.get('native_crossings',crossings)))
        output['native_reference']=circuit.get('reference','REF')
    else:
        _native_schematic_transform(output, circuit, plan)
    return output


def _validate_wires(circuit, routes, pins):
    if not isinstance(routes,list) or len(routes)>256:raise ValueError('Native winding routes must be a bounded list.')
    elements={e['name']:e for e in circuit['elements']};nodes={circuit.get('reference','REF'),*circuit['ports']}
    nodes.update(e[key] for e in circuit['elements'] if e['kind']!='mind' for key in ('n1','n2'))
    def endpoint(value):
        if not isinstance(value,dict):raise ValueError('Native route endpoint must be an object.')
        if 'component' in value:
            element=elements.get(value['component']);term=value.get('terminal')
            if not element or element['kind']=='mind' or term not in ('PLUS','MINUS'):
                raise ValueError('Native route endpoint must reference an actual device terminal.')
            return {'component':element['name'],'terminal':term},element['n1' if term=='PLUS' else 'n2']
        if 'pin' in value:
            point=_point(value.get('position'));name=value['pin']
            if not any(p['name']==name and all(abs(a-b)<1e-6 for a,b in zip(p['position'],point)) for p in pins):
                raise ValueError('Native route endpoint does not match an actual external pin.')
            return {'pin':name,'position':point},name
        if 'net' in value:
            if value['net'] not in nodes:raise ValueError('Native route endpoint names an unknown net.')
            return {'net':value['net'],'position':_point(value.get('position'))},value['net']
        raise ValueError('Native route endpoint needs a device terminal, pin or named-net anchor.')
    result=[];seen=set();point_count=0
    for index,route in enumerate(routes):
        name=str(route.get('id',index))
        if name in seen:raise ValueError('Native winding route IDs must be unique.')
        seen.add(name)
        start,snet=endpoint(route.get('start'));end,enet=endpoint(route.get('end'))
        if snet!=enet or snet!=route.get('net'):
            raise ValueError('A native winding route would short distinct circuit nets.')
        points=[_point(p) for p in route.get('points',[])];point_count+=len(points)
        if not 2<=len(points)<=128 or point_count>20000:raise ValueError('Native winding route geometry must be bounded.')
        if any(a==b for a,b in zip(points,points[1:])):raise ValueError('Native winding routes cannot contain empty segments.')
        for value,point in ((start,points[0]),(end,points[-1])):
            if 'position' in value and any(abs(a-b)>1e-6 for a,b in zip(value['position'],point)):
                raise ValueError('Native pin/net route geometry must match its declared anchor.')
        result.append({'id':name,'net':snet,'start':start,'end':end,'points':points})
    return result


def _quote(value):
    return json.dumps(str(value), ensure_ascii=True)


def _num(value):
    return format(float(value), '.12g')


def _p(point):
    return _num(point[0]) + ':' + _num(point[1])


def _rows(name, rows):
    return [name + '=list('] + ['  list(' + row + ')' for row in rows] + [')']


def visual_skill_header(plan, receipt_file):
    lines = ['snpfitHasVisual=t', 'snpfitVisualPlanHash=' + _quote(json_hash(plan)),
             'snpfitVisualReceiptFile=' + _quote(receipt_file)]
    for prefix, geometry in (('snpfitVisual', plan), ('snpfitSymbol', plan['symbol'])):
        lines.append(prefix + 'Bounds=list(' + ' '.join(_p(p) for p in geometry['bounds']) + ')')
        lines.extend(_rows(prefix+'Paths', [' '.join(_p(p) for p in path['points']) for path in geometry['paths']]))
        lines.extend(_rows(prefix+'Pins', [_quote(pin['name'])+' '+_p(pin['position'])+' '+_quote(pin['direction']) for pin in geometry['pins']]))
        lines.extend(_rows(prefix+'Labels', [_quote(label['text'])+' '+_p(label['position'])+' '+_quote(label['role'])+' '+_quote(label['justification']) for label in geometry['labels']]))
    lines.extend(_rows('snpfitVisualPlacements', [_quote(p['name'])+' '+_p(p['center'])+' '+_quote(p['orientation']) for p in plan['placements']]))
    def anchor(value):
        if 'component' in value:return value['component'],value['terminal']
        if 'pin' in value:return value['pin'],'PIN'
        return value['net'],'NET'
    routes=[]
    for route in plan.get('winding_wires',[]):
        start=anchor(route['start']);end=anchor(route['end'])
        routes.append(' '.join([_quote(route['net']),*(_quote(v) for v in (*start,*end)),
                                'list('+' '.join(_p(p) for p in route['points'])+')']))
    lines.extend(_rows('snpfitVisualWires',routes))
    return lines


def read_visual_receipt(path, plan, section):
    """Verify reopened OA anchors and, for schematics, actual component axes."""
    if section not in ('SCHEMATIC', 'SYMBOL'):
        raise ValueError('Unknown native visual receipt section.')
    text = Path(path).read_text(encoding='utf-8')
    geometry = plan if section == 'SCHEMATIC' else plan['symbol']
    actual = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == section+'_PIN':
            if len(parts) != 4 or not _IDENT.fullmatch(parts[1]):
                raise ValueError('Malformed native visual pin receipt.')
            actual.append({'name': parts[1], 'position': _point([parts[2], parts[3]])})
    remaining = list(actual)
    for pin in geometry['pins']:
        index = next((i for i, candidate in enumerate(remaining)
                      if candidate['name'] == pin['name'] and all(abs(a-b) <= 1e-6 for a,b in zip(candidate['position'], pin['position']))), None)
        if index is None:
            raise RuntimeError('Saved '+section.lower()+' pin position does not match the visual plan: '+pin['name'])
        remaining.pop(index)
    if remaining:
        raise RuntimeError('Native visual receipt contains unexpected pin anchors.')
    order_rows = re.findall(r'^'+section+r'_ORDER \(([^\n]*)\)$', text, re.M)
    if len(order_rows) != 1:
        raise RuntimeError('Saved native portOrder verification is missing.')
    order = re.findall(r'"([^"\n]+)"', order_rows[0])
    expected_order = geometry['port_order']
    if order != expected_order:
        raise RuntimeError('Saved native portOrder differs from the selected port contract.')
    graphic_rows = re.findall(r'^'+section+r'_GRAPHICS (\d+) (\d+)$', text, re.M)
    if len(graphic_rows) != 1 or tuple(map(int, graphic_rows[0])) != (len(geometry['paths']), len(geometry['labels'])):
        raise RuntimeError('Saved winding guides or Q annotations were not verified.')
    components = []
    if section == 'SCHEMATIC':
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0] == 'SCHEMATIC_COMPONENT':
                if len(parts) != 6:
                    raise ValueError('Malformed native component-position receipt.')
                components.append({'name': parts[1], 'center': _point([parts[2],parts[3]]),
                                   'master_orientation': parts[4], 'axis': parts[5]})
        if len(components) != len(plan['placements']) or len({p['name'] for p in components}) != len(components):
            raise RuntimeError('Saved native component placement count differs.')
        for expected in plan['placements']:
            found = next((p for p in components if p['name']==expected['name']), None)
            if not found or any(abs(a-b)>1e-6 for a,b in zip(found['center'],expected['center'])):
                raise RuntimeError('Saved native component position differs: '+expected['name'])
            if found['axis'] != 'NA' and found['axis'] != expected['orientation']:
                raise RuntimeError('Saved native component axis differs: '+expected['name'])
    wire_proof=None
    if section=='SCHEMATIC' and plan.get('winding_wires'):
        terminals={}
        for line in text.splitlines():
            parts=line.split()
            if parts and parts[0]=='SCHEMATIC_TERMINAL':
                if len(parts)!=5 or parts[2] not in ('PLUS','MINUS'):
                    raise ValueError('Malformed native terminal-position receipt.')
                key=(parts[1],parts[2])
                if key in terminals:raise RuntimeError('Duplicate native terminal-position receipt.')
                terminals[key]=_point(parts[3:5])
        expected_terms={(p['name'],term) for p in plan['placements'] if p.get('kind')!='mind' for term in ('PLUS','MINUS')}
        if set(terminals)!=expected_terms:raise RuntimeError('Actual native terminal-position verification is missing.')
        wire_proof=_read_wire_receipt(text,plan,terminals)
    proof={'section': section.lower(), 'pins': actual, 'port_order': order,
            'components': components, 'position_tolerance': 1e-6,
            'saved_graphics': {'paths': len(geometry['paths']), 'labels': len(geometry['labels']),
                               'geometry_and_text_verified_after_reopen': True},
            'component_anchor_basis': 'R/L/C terminal midpoint; mind instance origin',
            'verified': True}
    if wire_proof is not None:proof['center_wires']=wire_proof
    return proof


def _read_wire_receipt(text, plan, terminals):
    def anchor(value):
        if 'component' in value:return terminals[(value['component'],value['terminal'])]
        return value['position']
    def close(a,b):return all(abs(x-y)<=1e-6 for x,y in zip(a,b))
    routes=plan['winding_wires'];expected={}
    for route_index,route in enumerate(routes):
        points=copy.deepcopy(route['points']);points[0]=anchor(route['start']);points[-1]=anchor(route['end'])
        unique=[]
        for point in points:
            if not unique or point!=unique[-1]:unique.append(point)
        net=route['net']
        if net==plan.get('native_reference','REF') and net not in plan['port_order']:net='gnd!'
        for segment_index,(a,b) in enumerate(zip(unique,unique[1:])):
            expected[(route_index,segment_index)]=(net,a,b)
    found={}
    for line in text.splitlines():
        parts=line.split()
        if parts and parts[0]=='SCHEMATIC_WIRE':
            if len(parts)!=8:raise ValueError('Malformed native center-wire receipt.')
            try:key=(int(parts[1]),int(parts[2]))
            except ValueError as exc:raise ValueError('Malformed native center-wire indices.') from exc
            if key in found:raise RuntimeError('Duplicate saved native center-wire segment.')
            found[key]=(parts[3],_point(parts[4:6]),_point(parts[6:8]))
    counts=re.findall(r'^SCHEMATIC_WIRES (\d+) (\d+)$',text,re.M)
    if len(counts)!=1 or tuple(map(int,counts[0]))!=(len(routes),len(expected)) or set(found)!=set(expected):
        raise RuntimeError('Saved native center-wire count differs from the visual plan.')
    for key,(net,a,b) in expected.items():
        actual_net,c,d=found[key]
        if actual_net!=net or not ((close(a,c) and close(b,d)) or(close(a,d) and close(b,c))):
            raise RuntimeError('Saved native center-wire geometry or net differs: %d/%d'%key)
    from .native_crossings import different_net_intersections
    actual_routes=[{'id':str(key),'net':net,'points':[a,b]} for key,(net,a,b) in found.items()]
    if different_net_intersections(actual_routes):
        raise RuntimeError('Saved native wires on different nets intersect after real terminal anchoring.')
    return {'routes':len(routes),'segments':len(expected),'actual_terminal_anchors_checked':len(terminals),
            'different_net_crossings_verified_absent':True,
            'geometry_and_nets_verified_after_reopen':True}
