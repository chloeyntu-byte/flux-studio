"""Read attached OA technology using an isolated, task-local Cadence process.

There are deliberately no hard-coded foundry layer or via profiles. Some old
PDKs have no metal function tags: positive sheet resistance plus the native
standard-via graph identifies their metal stack without guessing layer names.
"""
import json
import math
import os
from pathlib import Path
import pwd
import re

import cadence
from cadence import NAME, job_dir, run_skill, skill


_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')
_RULES = {
    'minWidth': 'min_width', 'maxWidth': 'max_width',
    'minSpacing': 'min_spacing', 'minSameNetSpacing': 'min_same_net_spacing',
    'minDiagonalWidth': 'min_diagonal_width',
    'minDiagonalSpacing': 'min_diagonal_spacing', 'minArea': 'min_area',
}


def parse_skill(value):
    """Parse only the scalar/list subset emitted by the read-only probe."""
    tokens = _TOKEN.findall(value)
    position = 0

    def one():
        nonlocal position
        if position >= len(tokens):
            raise ValueError('Incomplete SKILL list')
        token = tokens[position]
        position += 1
        if token == '(':
            result = []
            while position < len(tokens) and tokens[position] != ')':
                result.append(one())
            if position >= len(tokens):
                raise ValueError('Unclosed SKILL list')
            position += 1
            return result
        if token == ')':
            raise ValueError('Unexpected SKILL closing parenthesis')
        if token.startswith('"'):
            return json.loads(token)
        if token == 'nil':
            return None
        if token == 't':
            return True
        try:
            return float(token) if any(c in token for c in '.eE') else int(token)
        except ValueError:
            return token

    parsed = one()
    if position != len(tokens):
        raise ValueError('Multiple values in a SKILL field')
    return parsed


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def drawing_grid(technology):
    """Return a working drawing grid; fallback values are not technology rules."""
    value = technology.get('grid')
    if _number(value) and value > 0:
        return float(value)
    dbu = technology.get('dbu_per_uu')
    if _number(dbu) and dbu > 0:
        return 1.0 / float(dbu)
    return 0.005


def _warning(code, message, **details):
    return dict(part='pdk', code=code, message=message, fields=['library'], **details)


def missing_rule_warnings(technology, layer_names=None):
    """Normalize discovery warnings and report missing rules on used metals."""
    selected = None if layer_names is None else set(layer_names)
    warnings = []
    for warning in technology.get('warnings') or []:
        if isinstance(warning, str):
            item = _warning('pdk_information', warning)
        elif isinstance(warning, dict):
            item = dict(warning)
            item.setdefault('part', 'pdk')
            item.setdefault('code', 'pdk_information')
            item.setdefault('message', '')
            item.setdefault('fields', ['library'])
            if not isinstance(item['fields'], list):
                item['fields'] = ['library']
        else:
            continue
        if selected is None or not item.get('layer') or item['layer'] in selected:
            warnings.append(item)
    for layer in technology.get('layers', []):
        name = layer.get('name')
        if selected is not None and name not in selected:
            continue
        missing = [key for key in ('min_width', 'min_spacing')
                   if not _number(layer.get(key)) or layer[key] < 0
                   or (key == 'min_width' and layer[key] == 0)]
        if missing:
            warnings.append(_warning(
                'missing_pdk_rule',
                'Metal %s has unavailable %s rules. User dimensions are retained; these missing DRC checks are not verified.'
                % (name, ', '.join({'min_width': 'minWidth', 'min_spacing': 'minSpacing'}[key] for key in missing)),
                layer=name, missing_rules=missing))
        invalid = [key for key in _RULES.values() if key not in ('min_width', 'min_spacing')
                   and layer.get(key) is not None and
                   (not _number(layer[key]) or layer[key] < 0
                    or (not key.endswith('spacing') and layer[key] == 0))]
        if invalid:
            warnings.append(_warning('missing_pdk_rule',
                                     'Metal %s has invalid optional %s rule values. These unavailable DRC checks are not verified.'
                                     % (name, ', '.join(invalid)), layer=name, missing_rules=invalid))
    grid = technology.get('grid')
    manufacturing = technology.get('manufacturing_grid', grid)
    if not (_number(manufacturing) and manufacturing > 0):
        source = technology.get('grid_source', 'database units' if _number(technology.get('dbu_per_uu')) and technology['dbu_per_uu'] > 0 else 'working fallback')
        warnings.append(_warning('missing_pdk_grid',
                                 'Manufacturing grid is unavailable. Working drawing grid %g um (%s) is not a verified manufacturing rule.'
                                 % (drawing_grid(technology), source)))
    if technology.get('order_verified') is False or any(
            layer.get('order_verified') is False for layer in technology.get('layers', [])
            if selected is None or layer.get('name') in selected):
        warnings.append(_warning('unverified_metal_order',
                                 'Physical metal order is unverified. Display rank is not a verified metal stack; unavailable layer-order checks are skipped.'))
    deduplicated = []
    seen = set()
    for warning in warnings:
        key = (warning['code'], warning['message'], warning.get('layer'))
        if key not in seen:
            seen.add(key)
            deduplicated.append(warning)
    return deduplicated


def _table(value):
    """Decode OA width/parallel-length spacing tables without losing evidence."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    header, data = value
    if not (isinstance(header, list) and len(header) == 2
            and isinstance(header[0], list) and isinstance(data, list)):
        return None
    axes, default = header
    entries = []
    for index in range(0, len(data) - 1, 2):
        threshold, spacing = data[index:index + 2]
        if not isinstance(threshold, list):
            threshold = [threshold]
        if _number(spacing) and all(_number(v) for v in threshold):
            entries.append({'threshold': threshold, 'value': float(spacing)})
    if not entries:
        return None
    return {'axes': axes, 'default': float(default) if _number(default) else None,
            'entries': entries}


def spacing_for(layer, width, length):
    """Required edge spacing at the selected width and parallel-run length."""
    minimum = layer.get('min_spacing')
    # Zero here means no available check, not a fabricated PDK rule. The
    # profile retains None and missing_rule_warnings reports that distinction.
    result = float(minimum) if _number(minimum) and minimum >= 0 else 0.0
    for table in layer.get('spacing_tables') or []:
        if not isinstance(table, dict):
            continue
        axes = table.get('axes')
        if not isinstance(axes, list) or not axes or any(axis not in ('width', 'length') for axis in axes):
            continue
        default = table.get('default')
        applicable = [float(default) if _number(default) and default >= 0 else 0.0]
        for entry in table.get('entries') or []:
            if not isinstance(entry, dict):
                continue
            thresholds, value = entry.get('threshold'), entry.get('value')
            if not (isinstance(thresholds, list) and len(thresholds) == len(axes)
                    and all(_number(v) for v in thresholds) and _number(value) and value >= 0):
                continue
            values = {'width': float(width), 'length': float(length)}
            if all(values[axis] >= threshold for axis, threshold in zip(axes, thresholds)):
                applicable.append(float(value))
        result = max(result, max(applicable))
    return result


def _probe_source(library, records):
    return '''
procedure(tfgenProbeTechnology()
 let((lib tf f rr function functions layer layerName v cg obj rule)
 lib=ddGetObj(%s)
 unless(lib error("TFGEN_PDK library not found"))
 tf=techGetTechFile(lib)
 unless(tf error("TFGEN_PDK library has no attached technology"))
 f=outfile(%s)
 unless(f error("TFGEN_PDK cannot open probe output"))
 fprintf(f "LIB\\t%%L\\t%%L\\t%%L\\t%%L\\n" lib~>name lib~>readPath tf~>libName tf~>path)
 rr=errset(techGetMfgGridResolution(tf) nil)
 fprintf(f "GRID\\t%%L\\n" if(rr then car(rr) else nil))
 when(isCallable('techGetDBUPerUU)
   rr=errset(techGetDBUPerUU(tf) nil)
   when(rr && car(rr) fprintf(f "DBU\\t%%L\\n" car(rr)))
 )
 functions=techGetLayerFunctions(tf)
 foreach(function functions
   fprintf(f "FUNCTION\\t%%L\\n" function)
 )
 foreach(layer tf~>layers
   layerName=layer~>name
   fprintf(f "LAYER\\t%%L\\t%%L\\n" layerName layer~>number)
   foreach(rule '("minWidth" "minSpacing" "maxWidth" "minSameNetSpacing" "minDiagonalWidth" "minDiagonalSpacing" "minArea")
     rr=errset(techGetSpacingRule(tf rule layerName) nil)
     when(rr && car(rr) fprintf(f "RULE\\t%%L\\t%%L\\t%%L\\n" layerName rule car(rr)))
   )
   when(isCallable('techGetElectricalRule)
     rr=errset(techGetElectricalRule(tf "sheetRes" layerName) nil)
     when(rr && car(rr) fprintf(f "RESISTANCE\\t%%L\\t%%L\\n" layerName car(rr)))
   )
 )
 foreach(v tf~>viaDefs
   when(v~>objType=="stdViaDef"
     fprintf(f "VIA\\t%%L\\t%%L\\t%%L\\t%%L\\n" v~>name v~>layer1~>name v~>layer2~>name v~>params)
   )
 )
 foreach(cg tf~>constraintGroups
   fprintf(f "GROUP\\t%%L\\n" cg~>name)
   foreach(obj cg~>objects
       when(member(obj~>defName '("minSpacing" "minWidth" "maxWidth" "minSameNetSpacing" "minDiagonalWidth" "minDiagonalSpacing" "viaSpacing" "minNumCut" "minArea"))
         fprintf(f "CONSTRAINT\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\n" cg~>name obj~>defName obj~>layers obj~>value obj~>ID obj~>params)
       )
   )
 )
 close(f)
 printf("TFGEN_PDK_SUCCESS\\n")
 t
 ))
if(errset(tfgenProbeTechnology() t) then exit(0) else exit(1))
''' % (skill(library), skill(str(records)))


def _normalize(records, workspace):
    info = {'layers': [], 'vias': [], 'constraint_groups': [], 'warnings': []}
    layer_data, functions, function_order, resistance = {}, {}, {}, {}
    raw_vias, constraints = [], []
    for line in records.read_text(encoding='utf-8').splitlines():
        fields = line.split('\t')
        kind = fields.pop(0)
        values = [parse_skill(v) for v in fields]
        if kind == 'LIB':
            info.update(zip(('library', 'library_path', 'technology', 'technology_path'), values))
        elif kind == 'GRID':
            info['manufacturing_grid'] = values[0]
        elif kind == 'DBU' and _number(values[0]) and values[0] > 0:
            info['dbu_per_uu'] = float(values[0])
        elif kind == 'FUNCTION':
            entry = values[0]
            if isinstance(entry, list) and len(entry) >= 2:
                functions[entry[0]] = entry[1]
                if len(entry) >= 3 and _number(entry[2]):
                    function_order[entry[0]] = float(entry[2])
        elif kind == 'LAYER':
            layer_data.setdefault(values[0], {}).update({'name': values[0], 'number': values[1]})
        elif kind == 'RULE' and values[1] in _RULES:
            data = layer_data.setdefault(values[0], {'name': values[0]})
            rule, value = values[1:3]
            if _number(value) and value >= 0 and (value > 0 or rule.endswith('Spacing')):
                data[_RULES[rule]] = float(value)
            elif rule == 'minSpacing':
                _add_spacing_table(data, value)
        elif kind == 'RESISTANCE' and _number(values[1]):
            resistance[values[0]] = float(values[1])
        elif kind == 'VIA':
            raw_vias.append(values)
        elif kind == 'GROUP':
            info['constraint_groups'].append(values[0])
        elif kind == 'CONSTRAINT':
            constraints.append(dict(zip(('group', 'rule', 'layers', 'value', 'id', 'params'), values)))

    # Identity is independent of optional line-width or spacing constraints.
    # Function tags are authoritative. Old PDKs need both native via endpoints
    # and positive electrical sheet resistance; a suggestive name is insufficient.
    endpoints = {value for via in raw_vias if len(via) >= 3 for value in via[1:3]}
    candidates = {name for name in endpoints if functions.get(name) == 'metal'
                  or (name not in functions and resistance.get(name, 0) > 0)}
    candidates.update(name for name, function in functions.items() if function == 'metal')
    if not candidates:
        raise ValueError('The attached technology has no verifiable metal functions or resistance rules. Metal layers cannot be identified reliably.')
    for name in candidates:
        layer_data.setdefault(name, {'name': name, 'number': None})

    # Other groups can describe optional routing or DFM constraints. Preserve
    # their provenance, but do not silently enforce an unbound group as foundry DRC.
    bound_constraints = [constraint for constraint in constraints if constraint['group'] == 'foundry']
    unbound = [constraint for constraint in constraints if constraint['group'] != 'foundry']
    if unbound:
        info['unbound_constraints'] = unbound
    for constraint in bound_constraints:
        layers = constraint['layers']
        if not isinstance(layers, list) or len(layers) != 1 or layers[0] not in candidates:
            continue
        data = layer_data[layers[0]]
        rule, value = constraint['rule'], constraint['value']
        if rule in _RULES and _number(value) and value >= 0 and (value > 0 or rule.endswith('Spacing')):
            key = _RULES[rule]
            old = data.get(key)
            data[key] = min(old, float(value)) if key == 'max_width' and old is not None else max(old or 0, float(value))
        elif rule == 'minSpacing':
            _add_spacing_table(data, value)

    relations = sorted({(via[1], via[2]) for via in raw_vias if len(via) >= 3
                        and via[1] in candidates and via[2] in candidates})
    for raw in raw_vias:
        if len(raw) != 4:
            continue
        via_name, lower, upper, params = raw
        if lower not in candidates or upper not in candidates:
            continue
        invalid = _invalid_via_fields(params, layer_data)
        if invalid:
            info['warnings'].append(_warning(
                'unavailable_native_via',
                'Native via %s has unavailable or invalid %s geometry. This via definition cannot be used; recognized metals remain available.'
                % (via_name, ', '.join(invalid)), via=via_name, missing_rules=invalid))
            continue
        via = {'name': via_name, 'lower': lower, 'upper': upper,
               'cut_layer': params[0], 'cut_size': params[1:3],
               'cut_spacing': params[5], 'default_cut_spacing': params[5],
               'lower_enclosure': params[6], 'upper_enclosure': params[7],
               'lower_offset': params[8], 'upper_offset': params[9], 'origin_offset': params[10]}
        additional = []
        for constraint in bound_constraints:
            if constraint['layers'] != [via['cut_layer']] or constraint['rule'] != 'viaSpacing':
                continue
            value = constraint['value']
            if (isinstance(value, list) and len(value) == 3 and all(_number(v) for v in value)
                    and value[0] >= 0 and value[0] == int(value[0]) and min(value[1:]) >= 0):
                additional.append({'threshold': int(value[0]), 'spacing': [float(value[1]), float(value[2])]})
            elif _number(value) and value >= 0:
                pairs = constraint.get('params') or []
                params_by_name = dict(pair for pair in pairs if isinstance(pair, list) and len(pair) == 2) if isinstance(pairs, list) else {}
                count, distance = params_by_name.get('numCuts'), params_by_name.get('distance')
                if _number(count) and count >= 0 and count == int(count):
                    item = {'threshold': int(count), 'spacing': [float(value), float(value)]}
                    if _number(distance) and distance >= 0:
                        item['neighbor_distance'] = distance
                    additional.append(item)
        via['array_spacing_rules'] = additional
        via['array_cut_spacing'] = [max([float(params[5][axis])] + [entry['spacing'][axis] for entry in additional])
                                    for axis in (0, 1)]
        via['minimum_landing'] = [float(params[axis + 1]) + 2 * max(float(params[6][axis]), float(params[7][axis]))
                                  for axis in (0, 1)]
        info['vias'].append(via)

    # Native function order and native via endpoint relations establish physical
    # order. OA/stream layer numbers and numeric substrings in names never do.
    complete_functions = (all(name in function_order for name in candidates)
                          and len({function_order[name] for name in candidates}) == len(candidates)
                          and all(function_order[lower] < function_order[upper] for lower, upper in relations))
    if complete_functions:
        ordered = sorted(candidates, key=function_order.__getitem__)
        source, verified = 'native_layer_functions', True
    else:
        ordered = _unique_stack(candidates, relations)
        source, verified = 'native_via_endpoints', ordered is not None
        if ordered is None:
            # This is explicitly UI display rank, not inferred vertical order.
            ordered = sorted(candidates)
            source = 'display_rank_only'
    info.update(order_verified=verified, order_source=source,
                order_relations=[list(relation) for relation in relations])
    for index, name in enumerate(ordered):
        data = layer_data[name]
        data.update(order=index, order_verified=verified, order_source=source,
                    function=functions.get(name, 'metal from sheet resistance and native vias'),
                    sheet_resistance=resistance.get(name))
        if name in function_order:
            data['native_function_order'] = function_order[name]
        for key in _RULES.values():
            data.setdefault(key, None)
        # minSameNetSpacing is distinct from the unavailable inter-net rule.
        info['layers'].append(data)
    manufacturing = info.get('manufacturing_grid')
    if _number(manufacturing) and manufacturing > 0:
        info['grid'], info['grid_source'] = float(manufacturing), 'manufacturing_grid'
    else:
        info['manufacturing_grid'] = None
        info['grid'] = drawing_grid(info)
        info['grid_source'] = 'database_units' if info.get('dbu_per_uu') else 'working_fallback'
    info['workspace'] = str(workspace)
    info['recommended'] = {'body': ordered[-2] if len(ordered) > 1 else ordered[-1], 'crossing': ordered[-1],
                           'pgs': ordered[0], 'ring': ordered[-1]}
    info['evidence'] = {'records': str(records),
                        'method': 'live attached OA technology: metal functions/electrical rules/native via endpoints/spacing API/foundry constraints; other groups retained without assumed binding',
                        'no_foundry_signoff': True}
    if any(layer.get('spacing_tables') for layer in info['layers']):
        info['warnings'].append(_warning('pdk_spacing_tables',
                                        'Some metal spacing rules depend on width and parallel-run length. Available spacing tables are validated.'))
    info['warnings'] = missing_rule_warnings(info)
    return info


def _add_spacing_table(data, value):
    table = _table(value)
    if not table:
        return
    tables = data.setdefault('spacing_tables', [])
    if table not in tables:
        tables.append(table)
    if _number(table['default']) and table['default'] >= 0:
        data['min_spacing'] = max(data.get('min_spacing') or 0, table['default'])


def _invalid_via_fields(params, layers):
    keys = ('cut_layer', 'cut_size_x', 'cut_size_y', 'rows', 'columns', 'cut_spacing',
            'lower_enclosure', 'upper_enclosure', 'lower_offset', 'upper_offset', 'origin_offset')
    if not isinstance(params, list):
        return ['native_params']
    invalid = [keys[index] for index in (0, 1, 2, 5, 6, 7, 8, 9, 10) if index >= len(params)]
    if len(params) > 0 and (not isinstance(params[0], str) or params[0] not in layers):
        invalid.append('cut_layer')
    for index in (1, 2):
        if index < len(params) and (not _number(params[index]) or params[index] <= 0):
            invalid.append(keys[index])
    for index in (5, 6, 7, 8, 9, 10):
        if index >= len(params):
            continue
        pair = params[index]
        if not (isinstance(pair, list) and len(pair) == 2 and all(_number(v) for v in pair)
                and (index >= 8 or min(pair) >= 0)):
            invalid.append(keys[index])
    return list(dict.fromkeys(invalid))


def _unique_stack(candidates, relations):
    incoming = {name: set() for name in candidates}
    for lower, upper in relations:
        incoming[upper].add(lower)
    remaining, ordered = set(candidates), []
    while remaining:
        available = [name for name in remaining if not incoming[name] & remaining]
        if len(available) != 1:
            return None
        selected = available[0]
        ordered.append(selected)
        remaining.remove(selected)
    return ordered


def load_profile(filename):
    """Read a user-supplied offline technology; never invoke native tools."""
    path = Path(filename).expanduser()
    if path.stat().st_size > 5*1024*1024:
        raise ValueError('Technology JSON exceeds 5 MB')
    tech = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(tech, dict) or tech.get('schema') != 'flux.technology.v1':
        raise ValueError('Expected flux.technology.v1')
    layers, vias = tech.get('layers'), tech.get('vias', [])
    if not isinstance(layers, list) or not layers or not isinstance(vias, list):
        raise ValueError('Technology needs a nonempty layers list and a vias list')
    if not _number(tech.get('grid')) or tech['grid'] <= 0:
        raise ValueError('Technology grid must be positive and finite')
    names, orders, via_names = set(), set(), set()
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get('name'), str) or not NAME.fullmatch(layer['name']):
            raise ValueError('Each metal needs a portable name')
        if layer['name'] in names or not _number(layer.get('order')) or layer['order'] in orders:
            raise ValueError('Metal names and physical orders must be unique')
        names.add(layer['name']); orders.add(layer['order'])
        for key in _RULES.values():
            value = layer.get(key)
            if value is not None and (not _number(value) or value < 0):
                raise ValueError('Invalid metal rule: '+key)
    for via in vias:
        if not isinstance(via, dict) or not isinstance(via.get('name'), str) or not NAME.fullmatch(via['name']) or via['name'] in via_names:
            raise ValueError('Via names must be valid and unique')
        via_names.add(via['name'])
        if via.get('lower') not in names or via.get('upper') not in names or via['lower'] == via['upper']:
            raise ValueError('Each via must connect two configured metals')
        if not isinstance(via.get('cut_layer'), str) or not NAME.fullmatch(via['cut_layer']):
            raise ValueError('Each via needs a valid cut layer')
        for key in ('cut_size', 'cut_spacing', 'lower_enclosure', 'upper_enclosure'):
            pair = via.get(key)
            if not isinstance(pair, list) or len(pair) != 2 or any(not _number(v) or v < 0 or (key == 'cut_size' and v == 0) for v in pair):
                raise ValueError('Invalid via geometry: '+key)
    return tech


def load_technology(library, workspace, run_dir=None, cds_lib=None):
    """Discover a library's live attached technology in the requested workspace."""
    if not isinstance(library, str) or not NAME.fullmatch(library):
        raise ValueError('Library names must start with a letter and contain only letters, digits and underscores (up to 80 characters).')
    workspace = Path(workspace).expanduser().resolve()
    if workspace.is_file():
        if workspace.name != 'cds.lib':
            raise ValueError('The workspace file must be cds.lib.')
        workspace = workspace.parent
    mapping = cadence.runtime_config.resolve_mapping(workspace, cds_lib)
    if not mapping.is_file():
        raise ValueError('The selected workspace has no cds.lib.')
    directory = Path(run_dir).resolve() if run_dir is not None else job_dir('technology')
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=False)
        directory.chmod(0o755)
        if os.geteuid() == 0:
            account = pwd.getpwnam(cadence.OWNER)
            os.chown(directory, account.pw_uid, account.pw_gid)
    records = directory / 'technology.tsv'
    stdout = run_skill(_probe_source(library, records), workspace, directory, cds_lib=str(mapping) if cds_lib else None)
    if 'TFGEN_PDK_SUCCESS' not in stdout or not records.is_file():
        raise RuntimeError('The PDK query returned no success marker. See this task\'s logs.')
    result = _normalize(records, workspace)
    result['run_dir'] = str(directory)
    result['cds_lib'] = str(mapping)
    (directory / 'technology.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
