"""Keep schematic wires electrically separate while conductor guides overlap.

Only drawing routes change.  Every fragment has the original circuit net and a
normal wire label, so a small visual gap never changes the fitted network.
"""
from __future__ import annotations

import copy
import math

_EPS = 1e-8
_GRID = .0625


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def segment_intersection(a, b, c, d):
    """Return the point/overlap endpoints, or None, including endpoint touches."""
    r = [b[i]-a[i] for i in (0, 1)]
    s = [d[i]-c[i] for i in (0, 1)]
    q = [c[i]-a[i] for i in (0, 1)]
    determinant = _cross(r, s)
    if abs(determinant) > _EPS:
        t, u = _cross(q, s)/determinant, _cross(q, r)/determinant
        if -_EPS <= t <= 1+_EPS and -_EPS <= u <= 1+_EPS:
            point = [a[i]+max(0., min(1., t))*r[i] for i in (0, 1)]
            return point, list(point)
        return None
    if abs(_cross(q, r)) > _EPS:
        return None
    length2 = sum(v*v for v in r)
    if length2 < _EPS:
        return None
    first = sum(q[i]*r[i] for i in (0, 1))/length2
    last = first+sum(s[i]*r[i] for i in (0, 1))/length2
    low, high = max(0., min(first, last)), min(1., max(first, last))
    if high < low-_EPS:
        return None
    return ([a[i]+low*r[i] for i in (0, 1)],
            [a[i]+max(low, high)*r[i] for i in (0, 1)])


def _edges(points):
    offset = 0.
    result = []
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        result.append((offset, offset+length, a, b))
        offset += length
    return result, offset


def _distance_on(edge, point):
    return edge[0]+math.dist(edge[2], point)


def _slice(points, first, last):
    edges, total = _edges(points)
    result = []
    for low, high, a, b in edges:
        if high <= first+_EPS or low >= last-_EPS:
            continue
        for distance in (max(low, first), min(high, last)):
            fraction = (distance-low)/(high-low)
            point = [round((a[i]+fraction*(b[i]-a[i]))/_GRID)*_GRID for i in (0, 1)]
            if not result or point != result[-1]:
                result.append(point)
    return result


def _merge(intervals):
    result = []
    for first, last in sorted(intervals):
        if result and first <= result[-1][1]+_EPS:
            result[-1][1] = max(last, result[-1][1])
        else:
            result.append([first, last])
    return result


def _box_interval(a, b, box):
    """Liang-Barsky clipping, retaining tangent touches as unsafe."""
    low, high = 0., 1.
    for axis in (0, 1):
        delta = b[axis]-a[axis]
        if abs(delta) < _EPS:
            if a[axis] < box[0][axis]-_EPS or a[axis] > box[1][axis]+_EPS:
                return None
            continue
        values = [(bound-a[axis])/delta for bound in (box[0][axis], box[1][axis])]
        low, high = max(low, min(values)), min(high, max(values))
        if high < low-_EPS:
            return None
    return low, high


def _device_replaces_touch(first, second, hit):
    # PLUS and MINUS both use the component center in the Python plan.
    # The native route resolver moves each to its real master pin; this
    # symbolic center touch therefore lies inside the intervening device.
    if math.dist(*hit) > _EPS:
        return False
    for end, point in (('start', first['points'][0]), ('end', first['points'][-1])):
        anchor = first.get(end, {})
        if 'component' not in anchor or math.dist(point, hit[0]) > _EPS:
            continue
        for other_end, other_point in (('start', second['points'][0]), ('end', second['points'][-1])):
            other = second.get(other_end, {})
            if (other.get('component') == anchor['component'] and
                    other.get('terminal') != anchor.get('terminal') and
                    math.dist(other_point, hit[0]) <= _EPS):
                return True
    return False


def different_net_intersections(routes):
    """Enumerate actual wire intersections, never non-electrical outlines."""
    result = []
    for i, route in enumerate(routes):
        edges, _ = _edges(route['points'])
        for j in range(i+1, len(routes)):
            if route['net'] == routes[j]['net']:
                continue
            other, _ = _edges(routes[j]['points'])
            for edge in edges:
                for other_edge in other:
                    hit = segment_intersection(edge[2], edge[3], other_edge[2], other_edge[3])
                    if hit and not _device_replaces_touch(route, routes[j], hit):
                        result.append((i, j, edge, other_edge, hit))
    return result


def protect_wires(circuit, routes, placements=(), gap=.375):
    """Split unsafe crossings into labelled fragments, preserving every anchor.

    The gap is clearance on either side of a crossing. Real component endpoints
    have a 0.375-unit protected region because native masters replace the plan's
    center coordinates with their actual pin positions. A crossing there must
    instead be cut in the other wire, or moved by the geometry planner.
    """
    if not math.isfinite(gap) or gap < .125 or gap > 2:
        raise ValueError('Native crossing clearance must be between 0.125 and 2.')
    routes = copy.deepcopy(routes)
    edges_lengths = [_edges(route['points']) for route in routes]
    removals = [[] for _ in routes]
    events = []
    junctions = [[] for _ in routes]
    for i, route in enumerate(routes):
        for j in range(i+1,len(routes)):
            if route['net'] != routes[j]['net']:
                continue
            for edge in edges_lengths[i][0]:
                for other in edges_lengths[j][0]:
                    hit=segment_intersection(edge[2],edge[3],other[2],other[3])
                    if hit:
                        junctions[i].extend(_distance_on(edge,p) for p in hit)
                        junctions[j].extend(_distance_on(other,p) for p in hit)

    def safe_interval(index, interval):
        route = routes[index]
        start = .375 if 'component' in route['start'] else _GRID
        finish = .375 if 'component' in route['end'] else _GRID
        return (interval[0] >= start-_EPS and interval[1] <= edges_lengths[index][1]-finish+_EPS
                and not any(interval[0]-_GRID < point < interval[1]+_GRID for point in junctions[index]))

    # A wire must not pass through another R/L/C body or its real terminal.
    # This is deliberately independent of wire/wire intersections: component
    # bodies replace part of a route in the native schematic.
    elements = {element['name']: element for element in circuit['elements']}
    for index, route in enumerate(routes):
        own = {route[end].get('component') for end in ('start', 'end')}
        for placement in placements:
            name = placement['name']
            if name in own or elements[name]['kind'] == 'mind':
                continue
            vertical = placement['orientation'] in ('R90', 'R270')
            half = (.3125, .4375) if vertical else (.4375, .3125)
            center = placement['center']
            box = [[center[i]-half[i] for i in (0, 1)],
                   [center[i]+half[i] for i in (0, 1)]]
            for edge in edges_lengths[index][0]:
                clipped = _box_interval(edge[2], edge[3], box)
                if clipped is None:
                    continue
                length = edge[1]-edge[0]
                interval = [edge[0]+clipped[0]*length-.125,
                            edge[0]+clipped[1]*length+.125]
                if not safe_interval(index, interval):
                    raise ValueError('Native crossing reaches a protected device/pin anchor: '+route['id']+' / '+name)
                removals[index].append(interval)
                events.append({'kind': 'device_clearance', 'route': route['id'], 'component': name})

    for i, j, edge, other, hit in different_net_intersections(routes):
        candidates = []
        for index, item in ((i, edge), (j, other)):
            distances = [_distance_on(item, point) for point in hit]
            interval = [min(distances)-gap, max(distances)+gap]
            if safe_interval(index, interval):
                candidates.append((index, interval))
        if not candidates:
            raise ValueError('Distinct native wire nets meet at protected anchors: '+routes[i]['id']+' / '+routes[j]['id'])
        # Stable priority independent of input ordering: keep the earlier ID
        # continuous and put a labelled break into the later one.
        index, interval = max(candidates, key=lambda item: routes[item[0]]['id'])
        removals[index].append(interval)
        events.append({'kind': 'wire_crossing', 'routes': [routes[i]['id'], routes[j]['id']],
                       'broken_route': routes[index]['id'], 'points': [list(p) for p in hit]})

    output = []
    for index, route in enumerate(routes):
        cuts = _merge(removals[index])
        if not cuts:
            output.append(route)
            continue
        total = edges_lengths[index][1]
        ranges = []
        first = 0.
        for low, high in cuts:
            ranges.append((first, low))
            first = high
        ranges.append((first, total))
        for number, (low, high) in enumerate(ranges):
            points = _slice(route['points'], low, high)
            if len(points) < 2:
                continue
            part = copy.deepcopy(route)
            part.update(id=route['id']+'__gap_'+str(number), points=points,
                        start=copy.deepcopy(route['start']) if low == 0 else {'net': route['net'], 'position': points[0]},
                        end=copy.deepcopy(route['end']) if high == total else {'net': route['net'], 'position': points[-1]})
            output.append(part)
    if different_net_intersections(output):
        raise ValueError('A native wire crossing remained after clearance splitting.')
    return output, {'schema': 1, 'clearance': gap, 'strategy': 'same_net_labelled_gaps',
                    'events': events, 'input_routes': len(routes), 'output_routes': len(output),
                    'different_net_crossings_remaining': 0}
