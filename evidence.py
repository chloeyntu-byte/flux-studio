"""Read back real OA geometry and compare it with the preview contract."""
import ast
from collections import Counter
import math
import re


def sexpr(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text)
    index = 0

    def parse():
        nonlocal index
        token = tokens[index]
        index += 1
        if token == '(':
            result = []
            while tokens[index] != ')':
                result.append(parse())
            index += 1
            return result
        if token.startswith('"'):
            return ast.literal_eval(token)
        if token == 'nil':
            return None
        try:
            return float(token)
        except ValueError:
            return token
    result = parse()
    if index != len(tokens):
        raise ValueError('Unexpected trailing SKILL data')
    return result


def close_numbers(a, b, tol=1e-6):
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(close_numbers(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, abs_tol=tol, rel_tol=0)
    return a == b


def verify_readback(readback, geometry, grid):
    rows = [line.split('\t') for line in readback.splitlines()]
    polygons = [row for row in rows if row[0] == 'POLYGON' and sexpr(row[1]) == 'polygon']
    expected_polygons = geometry['polygons']
    if len(polygons) != len(expected_polygons):
        raise RuntimeError('OA polygon count does not match the applied geometry.')
    # OA list order is implementation dependent; compare canonical cyclic rings.
    def canonical(points):
        points = [tuple(round(float(c), 6) for c in p) for p in points]
        if points[0] == points[-1]:
            points.pop()
        reverse = list(reversed(points))
        return min([tuple(points[i:] + points[:i]) for i in range(len(points))] +
                   [tuple(reverse[i:] + reverse[:i]) for i in range(len(reverse))])
    expected = sorted((p['layer'], p.get('net', 'GND'), canonical(p['points'])) for p in expected_polygons)
    actual = sorted((sexpr(r[2])[0], sexpr(r[5]), canonical(sexpr(r[3]))) for r in polygons)
    if actual != expected:
        raise RuntimeError('OA polygon coordinates, layers or nets do not match the applied geometry.')
    actual_vias = [row for row in rows if row[0] == 'VIA']
    if len(actual_vias) != len(geometry['vias']):
        raise RuntimeError('OA native via count does not match the applied geometry.')
    expected_vias = sorted(geometry['vias'], key=lambda v: (v['via_def'], tuple(v['origin'])))
    actual_vias.sort(key=lambda r: (sexpr(r[1]), tuple(sexpr(r[2]))))
    via_figures = [r for r in rows if r[0] == 'VIAFIG']
    total_cuts = 0
    for via, row in zip(expected_vias, actual_vias):
        if sexpr(row[1]) != via['via_def'] or not close_numbers(sexpr(row[2]), via['origin'], grid/2):
            raise RuntimeError('OA native via definition or origin does not match the applied geometry.')
        if sexpr(row[5]) != via['net']:
            raise RuntimeError('OA native via net does not match the applied geometry.')
        span = [via['columns'] * via['cut_size'][0] + (via['columns']-1) * via['cut_spacing'][0],
                via['rows'] * via['cut_size'][1] + (via['rows']-1) * via['cut_spacing'][1]]
        enc = [max(via['lower_enclosure'][k], via['upper_enclosure'][k]) for k in (0, 1)]
        predicted = [[via['origin'][k] - span[k]/2 - enc[k] for k in (0, 1)],
                     [via['origin'][k] + span[k]/2 + enc[k] for k in (0, 1)]]
        if not close_numbers(sexpr(row[3]), predicted, grid/2 + 1e-6):
            raise RuntimeError('OA native via enclosure dimensions do not match the calculated rules.')
        master_figures = [r for r in via_figures
                          if sexpr(r[2]) == via['via_def'] and close_numbers(sexpr(r[1]), via['origin'])]
        # Master figures are local to the via origin. Check both conductor
        # layers: the combined via bbox only exposes the larger enclosure.
        for side in ('lower', 'upper'):
            enclosure = via[side + '_enclosure']
            expected_metal = [[-span[k]/2 - enclosure[k] for k in (0, 1)],
                              [span[k]/2 + enclosure[k] for k in (0, 1)]]
            actual_metal = [sexpr(r[4]) for r in master_figures if sexpr(r[3]) == via[side]]
            if not close_numbers(actual_metal, [expected_metal], grid/2 + 1e-6):
                raise RuntimeError('OA native via %s metal enclosure does not match the calculated rules.' % side)
        actual_cuts = sorted(sexpr(r[4]) for r in master_figures
                             if sexpr(r[3]) == via['cut_layer'])
        expected_cuts = []
        for iy in range(via['rows']):
            for ix in range(via['columns']):
                x = (ix - (via['columns'] - 1)/2) * (via['cut_size'][0] + via['cut_spacing'][0])
                y = (iy - (via['rows'] - 1)/2) * (via['cut_size'][1] + via['cut_spacing'][1])
                expected_cuts.append([[x - via['cut_size'][0]/2, y - via['cut_size'][1]/2],
                                      [x + via['cut_size'][0]/2, y + via['cut_size'][1]/2]])
        if not close_numbers(actual_cuts, sorted(expected_cuts), grid/2 + 1e-6):
            raise RuntimeError('OA native via rows/columns/cutSpacing/cutSize do not match the applied geometry.')
        total_cuts += via['rows'] * via['columns']
    cuts = [r for r in rows if r[0] == 'VIAFIG' and sexpr(r[3]) in {v['cut_layer'] for v in geometry['vias']}]
    if len(cuts) != total_cuts:
        raise RuntimeError('OA native via master cut count does not match the calculated array.')
    actual_ports = [r for r in rows if r[0] == 'PORT']
    if len(actual_ports) != len(geometry['ports']):
        raise RuntimeError('OA port count does not match the applied geometry.')
    by_name = {r[1]: r for r in actual_ports}
    pin_access = {r[1]: r for r in rows if r[0] == 'PINACCESS'}
    for port in geometry['ports']:
        row = by_name.get(port['name'])
        if not row or sexpr(row[2]) != port['net'] or not close_numbers(sexpr(row[3]), [port['bbox']], grid/2):
            raise RuntimeError('OA port name, net, terminal binding or bounds do not match the applied geometry.')
        if sexpr(row[4]) != [[port['layer'], 'drawing']]:
            raise RuntimeError('OA port metal layer does not match the applied geometry.')
        if port.get('access_direction'):
            direction = pin_access.get(port['name'])
            if not direction or sexpr(direction[2]) != [port['access_direction']] or sexpr(direction[3]) != 'inputOutput':
                raise RuntimeError('OA native pin access direction or terminal direction does not match the extraction contract.')
    expected_groups = Counter()
    for figures in (geometry['polygons'], geometry['vias'], geometry['ports']):
        for figure in figures:
            part = figure.get('part', 'body')
            part = 'pgs' if part in ('pgs', 'ground_route') else ('ring' if part == 'ring' else 'body')
            expected_groups['TFGEN_' + part.upper()] += 1
    actual_group_rows = [r for r in rows if r[0] == 'GROUP']
    actual_groups = {r[1]: int(r[2]) for r in actual_group_rows}
    if len(actual_group_rows) != len(expected_groups) or actual_groups != dict(expected_groups):
        raise RuntimeError('OA group names or figure counts do not match the applied geometry.')
    return {'polygons_equal': True, 'ports_equal': True, 'pin_access_directions_equal': True, 'native_via_bounds_equal': True,
            'native_metal_enclosures_equal': True, 'groups_equal': True,
            'native_cut_arrays_equal': True, 'native_cut_count': total_cuts,
            'grid': grid, 'readback': 'actual saved OA polygons and native via masters'}
