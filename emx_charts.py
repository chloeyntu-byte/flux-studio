"""Pure chart projection for frozen EMX campaigns; no rendering or side effects.

A spec uses x/y plus their *_frequency_hz for objectives, and grid_y,
slice and slice_value for a three-dimensional sweep.  IDs remain attached
 to every measured point. Missing samples are never interpolated.
"""
import math

from emx_search import SearchSpace

METRICS = ('L', 'Q', 'Lp', 'Ls', 'k', 'Qp', 'Qs')


def _rows(rows):
    return list(rows.values()) if isinstance(rows, dict) else list(rows or [])


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def axis_options(source, config, rows):
    fields = {f['name']: f for f in (source or {}).get('fields', [])}
    dimensions, groups = [], {}
    axes = config.get('axes', [])
    # SearchSpace is authoritative for validation and exact sampled values.
    space = SearchSpace(list(fields.values()), axes)
    sampled = {name: values for dim in space._axes for name, values in dim}
    for axis in axes:
        if not axis.get('enabled', True):
            continue
        name = axis['name']
        group = axis.get('group')
        if group and group in groups:
            dimensions[groups[group]]['names'].append(name)
            continue
        values = list(sampled[name])
        field = fields.get(name, {})
        category = str(field.get('type', '')).lower() in ('bool','boolean','enum','choice','select','string','str') or bool(field.get('options') or field.get('choices')) or any(not _finite(v) for v in values)
        dim = {'names': [name], 'values': values, 'categorical': category}
        if group:
            groups[group] = len(dimensions)
        dimensions.append(dim)
    samples = [s for r in _rows(rows) for s in (r.get('metrics') or [])]
    frequencies = {s.get('frequency_hz') for s in samples if _finite(s.get('frequency_hz'))}
    frequencies.update(f for f in config.get('emx', {}).get('frequencies_hz', []) if _finite(f))
    frequencies.update(o['frequency_hz'] for o in config.get('objectives', []) if _finite(o.get('frequency_hz')))
    allowed = (source or {}).get('definition', {}).get('emx_metrics') or METRICS
    return {'mode': config.get('mode', 'sweep'), 'dimensions': dimensions,
            'parameters': [n for d in dimensions for n in d['names']],
            'metrics': [m for m in METRICS if m in allowed],
            'frequencies_hz': sorted(frequencies), 'objectives': list(config.get('objectives') or [])}


def _measurement(row, name, frequency):
    matches = [s for s in (row.get('metrics') or []) if s.get('frequency_hz') == frequency]
    if len(matches) != 1:
        return None
    value = matches[0].get(name)
    return value if _finite(value) else None


def _front(rows, count):
    eligible = []
    for row in rows:
        scores = row.get('objectives') or []
        if row.get('status') != 'completed' or row.get('feasible') is not True or row.get('violation', 0) != 0 or len(scores) != count or not all(_finite(v) for v in scores):
            continue
        eligible.append(row)
    return [r for r in eligible if not any(all(a <= b for a, b in zip(o['objectives'], r['objectives'])) and any(a < b for a, b in zip(o['objectives'], r['objectives'])) for o in eligible if o is not r)]


def _label(name, frequency):
    unit = 'nH' if name in ('L', 'Lp', 'Ls') else 'dimensionless'
    return '%s (%s) @ %s Hz' % (name, unit, frequency)


def _parameter_label(source, name):
    field = next((f for f in (source or {}).get('fields', []) if f.get('name') == name), {})
    label = field.get('label') or name
    unit = field.get('unit')
    if unit:
        label += ' (%s)' % ('µm' if unit == 'um' else unit)
    return label


def _coord(value, dim):
    if not dim['categorical']:
        return value if _finite(value) else None
    for i, known in enumerate(dim['values']):
        if type(value) is type(known) and value == known:
            return i
    return None


def _ticks(dim):
    return [(i, str(v)) for i, v in enumerate(dim['values'])] if dim['categorical'] else []


def _triangulate(points):
    """Triangulate unique measured XY coordinates without creating vertices."""
    unique = {}
    for index, point in enumerate(points):
        key = (point['x'], point['y'])
        if key in unique and point['z'] != points[unique[key]]['z']:
            return [], 'Multiple heights share an XY coordinate; showing measured points only.'
        unique.setdefault(key, index)
    if len(unique) < 3:
        return [], 'Fewer than three unique XY coordinates; showing measured points only.'
    xs, ys = zip(*unique.keys())
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    if xmin == xmax or ymin == ymax:
        return [], 'Collinear XY coordinates; showing measured points only.'
    xy = [((x-xmin)/(xmax-xmin), (y-ymin)/(ymax-ymin)) for x,y in unique]
    indices = list(unique.values())
    n = len(xy)
    xy.extend([(-100000.,-100000.), (100000.,-100000.), (0.,100000.)])
    triangles = [(n,n+1,n+2)]
    def inside(tri, p):
        a,b,c = (xy[i] for i in tri)
        ax,ay = a[0]-p[0],a[1]-p[1]
        bx,by = b[0]-p[0],b[1]-p[1]
        cx,cy = c[0]-p[0],c[1]-p[1]
        determinant = ((ax*ax+ay*ay)*(bx*cy-by*cx) - (bx*bx+by*by)*(ax*cy-ay*cx) + (cx*cx+cy*cy)*(ax*by-ay*bx))
        orientation = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        return determinant*orientation > 1e-14
    for i in range(n):
        bad = [t for t in triangles if inside(t,xy[i])]
        edges = {}
        for a,b,c in bad:
            for edge in ((a,b),(b,c),(c,a)):
                key = tuple(sorted(edge))
                edges[key] = edges.get(key,0)+1
        triangles = [t for t in triangles if t not in bad]
        for a,b in sorted(edge for edge,count in edges.items() if count == 1):
            area = (xy[b][0]-xy[a][0])*(xy[i][1]-xy[a][1])-(xy[b][1]-xy[a][1])*(xy[i][0]-xy[a][0])
            if abs(area) > 1e-14:
                triangles.append((a,b,i) if area > 0 else (b,a,i))
    local = [t for t in triangles if all(i < n for i in t)]
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    def half(seq):
        h = []
        for point in seq:
            while len(h) >= 2 and cross(h[-2], h[-1], point) <= 0:
                h.pop()
            h.append(point)
        return h
    ordered = sorted(xy[:n])
    hull = half(ordered)[:-1] + half(reversed(ordered))[:-1]
    hull_area = abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(hull, hull[1:]+hull[:1]))) / 2
    face_area = sum(abs(cross(xy[a], xy[b], xy[c])) / 2 for a,b,c in local)
    used = {i for t in local for i in t}
    if not local or len(used) != n or hull_area <= 1e-14 or abs(face_area-hull_area) > max(1e-12, hull_area*1e-9):
        return [], 'Collinear or numerically degenerate XY coordinates; showing measured points only.'
    result = sorted(tuple(indices[i] for i in t) for t in local)
    return result, ''


def build_chart(spec, source, config, rows, pareto_ids=None):
    """Project the frozen campaign. Raises ValueError for invalid chart specs.

    Full-dimensional Pareto is calculated from completed, feasible score vectors;
    pareto_ids is accepted for callers but never used as a substitute for evidence.
    """
    options = axis_options(source, config, rows)
    mode = config.get('mode', 'sweep')
    if spec.get('mode', mode) != mode or mode not in ('sweep', 'optimize'):
        raise ValueError('chart mode does not match campaign')
    rows = _rows(rows)
    out = {'type': None, 'series': [], 'points': [], 'x_label': '', 'y_label': '',
           'color_label': '', 'z_label': '', 'triangles': [], 'x_grid': [], 'y_grid': [], 'x_ticks': [], 'y_ticks': [], 'notes': [], 'conflicts': []}
    if mode == 'optimize':
        objectives = options['objectives']
        if len(objectives) < 1:
            raise ValueError('optimization chart needs at least one objective')
        chosen = []
        for key in ('x', 'y') + (('z',) if len(objectives) == 3 else ()):
            source_key = 'color' if key == 'z' and 'z' not in spec else key
            pair = (spec.get(source_key), spec.get(source_key + '_frequency_hz'))
            if pair[0] not in options['metrics'] or pair[1] not in options['frequencies_hz']:
                raise ValueError('%s must select an available metric and exact frequency' % key)
            chosen.append(pair)
        if len(set(chosen)) != len(chosen):
            raise ValueError('metric and frequency selections must be distinct')
        out['type'] = 'surface' if len(objectives) == 3 else 'scatter'
        out['x_label'], out['y_label'] = (_label(*pair) for pair in chosen[:2])
        if len(chosen) == 3:
            out['z_label'] = _label(*chosen[2])
        for row in _front(rows, len(objectives)):
            vals = [_measurement(row, *pair) for pair in chosen]
            if any(v is None for v in vals) or row.get('id') is None:
                continue
            p = {'x': vals[0], 'y': vals[1], 'id': str(row['id'])}
            if len(vals) == 3:
                p['z'] = vals[2]
            out['points'].append(p)
        out['series'] = [{'label': 'Full Pareto front', 'points': out['points']}]
        if len(objectives) == 3:
            out['triangles'], note = _triangulate(out['points'])
            if note:
                out['notes'].append(note)
            else:
                out['notes'].append('Facets connect measured Pareto vertices; they do not add measured candidates.')
        if len(objectives) > 3:
            out['notes'].append('Projection of the full-dimensional Pareto front.')
    else:
        dims = options['dimensions']
        lookup = {n: i for i, d in enumerate(dims) for n in d['names']}
        x, metric = spec.get('x'), spec.get('y')
        frequency = spec.get('y_frequency_hz')
        if x not in lookup or metric not in options['metrics'] or not _finite(frequency):
            raise ValueError('sweep requires a parameter, available metric, and frequency')
        xi = lookup[x]
        xdim = dims[xi]
        # Resolve paired axes independently because the chosen parameter may differ.
        space = SearchSpace((source or {}).get('fields', []), config.get('axes', []))
        sampled = {name: values for dim in space._axes for name, values in dim}
        xdim = dict(xdim, values=list(sampled[x]))
        heatmap = len(dims) == 3 or (len(dims) == 2 and spec.get('style') != 'line')
        out.update(type='heatmap' if heatmap else 'line', x_label=_parameter_label(source, x),
                   y_label=_label(metric, frequency), x_ticks=_ticks(xdim))
        if heatmap:
            gy = spec.get('grid_y') or (next((d['names'][0] for i,d in enumerate(dims) if i != xi), None) if len(dims) == 2 else None)
            sl = spec.get('slice')
            if gy not in lookup or lookup[gy] == xi:
                raise ValueError('heatmap requires distinct x and grid_y')
            gdim = dict(dims[lookup[gy]], values=list(sampled[gy]))
            if len(dims) == 3:
                if sl not in lookup or len({xi, lookup[gy], lookup[sl]}) != 3 or 'slice_value' not in spec:
                    raise ValueError('heatmap requires distinct x, grid_y, slice and slice_value')
                sdim = dict(dims[lookup[sl]], values=list(sampled[sl]))
                if not any(type(spec['slice_value']) is type(v) and spec['slice_value'] == v or (_finite(spec['slice_value']) and _finite(v) and spec['slice_value'] == v) for v in sdim['values']):
                    raise ValueError('slice value is not a configured coordinate')
            out.update(y_label=_parameter_label(source, gy), color_label=_label(metric, frequency), y_ticks=_ticks(gdim),
                       x_grid=[_coord(v, xdim) for v in xdim['values']],
                       y_grid=[_coord(v, gdim) for v in gdim['values']])
            for row in rows:
                values = row.get('values') or {}
                if x not in values or gy not in values or row.get('status') != 'completed' or row.get('id') is None:
                    continue
                if len(dims) == 3 and (sl not in values or (type(values[sl]) is not type(spec['slice_value']) and not (_finite(values[sl]) and _finite(spec['slice_value']))) or values[sl] != spec['slice_value']):
                    continue
                xv, yv, z = _coord(values.get(x), xdim), _coord(values.get(gy), gdim), _measurement(row, metric, frequency)
                if xv is not None and yv is not None and z is not None:
                    out['points'].append({'x': xv, 'y': yv, 'color': z, 'id': str(row['id'])})
            label = '%s = %s' % (_parameter_label(source, sl), spec['slice_value']) if len(dims) == 3 else metric
            out['series'] = [{'label': label, 'points': out['points']}]
        else:
            groups = {}
            others = [i for i in range(len(dims)) if i != xi]
            for row in rows:
                values = row.get('values') or {}
                if x not in values or any(dims[i]['names'][0] not in values for i in others):
                    continue
                xv = _coord(values[x], xdim)
                if xv is None:
                    continue
                key = tuple(values.get(dims[i]['names'][0]) for i in others)
                groups.setdefault(key, []).append((xv, row))
            for key, members in groups.items():
                members.sort(key=lambda item: next((i for i, raw in enumerate(xdim['values']) if _coord(raw, xdim) == item[0]), len(xdim['values'])))
                label = ', '.join('%s=%s' % (_parameter_label(source, dims[i]['names'][0]), v) for i, v in zip(others, key)) or metric
                series = {'label': label, 'points': []}
                last_index = None
                for xv, row in members:
                    val = _measurement(row, metric, frequency) if row.get('status') == 'completed' else None
                    if val is None or row.get('id') is None:
                        last_index = None
                        continue
                    try:
                        index = next(i for i, raw in enumerate(xdim['values']) if _coord(raw, xdim) == xv)
                    except StopIteration:
                        index = None
                    p = {'x': xv, 'y': val, 'id': str(row['id']), 'break_before': last_index is None or index is None or index != last_index + 1}
                    last_index = index
                    series['points'].append(p)
                    out['points'].append(p)
                if series['points']:
                    out['series'].append(series)
    if out['type'] == 'heatmap':
        coords = {}
        for p in out['points']:
            coords.setdefault((p['x'], p['y']), []).append(p['id'])
        out['conflicts'] = [{'x': xy[0], 'y': xy[1], 'ids': ids} for xy, ids in coords.items() if len(ids) > 1]
        if out['conflicts']:
            out['notes'].append('Multiple candidates share heatmap coordinates; values were not averaged.')
    return out
