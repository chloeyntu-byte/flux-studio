"""Shared passive-device geometry engine, in micrometres.

Body models (the built-in differential inductor and any structure pack) build
their windings with ``GeometryBuilder`` and hand the body-only result to
``postprocess_geometry``, which adds the optional PGS, ground ring and ground
routing and validates the complete layout.

No third-party geometry package is required.  This is a generator-level
screen, not a replacement for the foundry DRC/LVS/RF signoff flow.
"""
from __future__ import division

import copy
import math
from collections import deque
from decimal import Decimal
from technology import spacing_for, drawing_grid, missing_rule_warnings


DEFAULT_PARAMETERS = {
    "pgs_extent": 95.0,
    "pgs_width": 0.4,
    "pgs_spine_width": None,
    "pgs_spacing": 4.0,
    "pgs_shape": "octagon",
    "ring_gap": 20.0,
    "ring_width": 5.0,
}
DEFAULTS = DEFAULT_PARAMETERS
DEFAULT_PARAMETERS.update({"pgs_enabled": True, "ring_enabled": True, "ring_extent": 103.0, "ring_shape": "square", "pgs_connect_ring": False, "ground_route_width": 1.0})

# Native OA readback expands every cut into actual shapes.  Reject excessive
# arrays rather than under-filling a legal metal overlap to hide the cost.
MAX_CUTS_PER_ARRAY = 10000
MAX_TOTAL_CUTS = 50000

PARAMETER_SCHEMA = [
    {"name": "pgs_enabled", "label": "Include PGS", "part": "pgs", "default": True, "type": "boolean", "import_default": True},
    {"name": "ring_enabled", "label": "Include GND ring", "part": "ring", "default": True, "type": "boolean", "import_default": True},
    {"name": "pgs_extent", "label": "PGS physical extent", "part": "pgs", "default": 95, "min": 5, "max": 1000, "step": 0.1, "unit": "um"},
    {"name": "pgs_width", "label": "PGS arm width", "part": "pgs", "default": 0.4, "min": 0.1, "max": 2, "step": 0.1, "unit": "um"},
    {"name": "pgs_spine_width", "label": "PGS spine width (blank follows arm width)", "part": "pgs", "default": None, "optional": True, "min": 0.1, "max": 2, "step": 0.1, "unit": "um"},
    {"name": "pgs_spacing", "label": "PGS arm edge spacing", "part": "pgs", "default": 4, "min": 0.1, "max": 100, "step": 0.1, "unit": "um"},
    {"name": "pgs_shape", "label": "PGS outline", "part": "pgs", "default": "octagon", "options": ["octagon", "square", "circle"]},
    {"name": "ring_extent", "label": "Ring inner extent", "part": "ring", "default": 103, "min": 5, "max": 2000, "step": 0.1, "unit": "um"},
    {"name": "ring_shape", "label": "Ground ring outline", "part": "ring", "default": "square", "options": ["square", "octagon"]},
    {"name": "pgs_connect_ring", "label": "Connect PGS to ring", "part": "pgs", "default": False, "type": "boolean"},
    {"name": "ground_route_width", "label": "Ground routing width", "part": "ground_route", "default": 1, "min": 0.1, "max": 100, "step": 0.1, "unit": "um"},
    {"name": "ring_width", "label": "Ground ring width", "part": "ring", "default": 5, "min": 0.1, "max": 100, "step": 0.1, "unit": "um"},
]


def shielding_state(parameters):
    """Absent switches preserve old designs; link is effective only with both."""
    p = parameters or {}
    pgs = p.get('pgs_enabled', True) is True
    ring = p.get('ring_enabled', True) is True
    return pgs, ring, pgs and ring and p.get('pgs_connect_ring', False) is True


def shielding_parameter_active(name, parameters):
    if name in ('pgs_enabled', 'ring_enabled'):
        return True
    pgs, ring, linked = shielding_state(parameters)
    if name == 'pgs_connect_ring':
        return pgs and ring
    if name.startswith('pgs_'):
        return pgs
    if name.startswith('ring_'):
        return ring
    if name.startswith('ground_route'):
        return linked
    return True


def shielding_role_active(role, parameters):
    pgs, ring, linked = shielding_state(parameters)
    return {'pgs': pgs, 'ring': ring, 'ground_route': linked}.get(role, True)


def _error(part, code, message, fields=None):
    return {"part": part, "code": code, "message": message, "fields": fields or []}


def _rule_number(rule, key):
    value = rule.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return 0.0


def _spacing(rule, width, length, same_net=False):
    spacing = max(spacing_for(rule, width, length), _rule_number(rule, "min_diagonal_spacing"))
    if same_net:
        spacing = max(spacing, _rule_number(rule, "min_same_net_spacing"))
    return spacing


def _bbox(points):
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _rect(box):
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(p, a, b, eps=1e-8):
    return abs(_cross(a, b, p)) < eps and min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps


def _segments_intersect(a, b, c, d):
    v1, v2, v3, v4 = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if ((v1 > 0 and v2 < 0) or (v1 < 0 and v2 > 0)) and ((v3 > 0 and v4 < 0) or (v3 < 0 and v4 > 0)):
        return True
    return _on_segment(c, a, b) or _on_segment(d, a, b) or _on_segment(a, c, d) or _on_segment(b, c, d)


def _point_inside(p, polygon):
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if _on_segment(p, a, b):
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            xx = (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]
            if p[0] < xx:
                inside = not inside
    return inside


def _point_segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    if den < 1e-20:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def polygon_distance(pa, pb):
    """Exact segment distance for simple polygons; overlapping shapes return 0."""
    if _point_inside(pa[0], pb) or _point_inside(pb[0], pa):
        return 0.0
    best = float("inf")
    for a, b in zip(pa, pa[1:] + pa[:1]):
        for c, d in zip(pb, pb[1:] + pb[:1]):
            if _segments_intersect(a, b, c, d):
                return 0.0
            best = min(best, _point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
                       _point_segment_distance(c, a, b), _point_segment_distance(d, a, b))
    return best


def _bbox_distance(a, b):
    return math.hypot(max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0))


def _line_intersection(a, av, b, bv):
    den = av[0] * bv[1] - av[1] * bv[0]
    if abs(den) < 1e-12:
        return [a[0], a[1]]
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = (dx * bv[1] - dy * bv[0]) / den
    return [a[0] + t * av[0], a[1] + t * av[1]]


def _stroke(points, width, join_style="miter"):
    """Butt-ended, exact Euclidean width with mitered 45/90 degree joins."""
    clean = []
    for point in points:
        if not clean or math.hypot(point[0] - clean[-1][0], point[1] - clean[-1][1]) > 1e-9:
            clean.append(point)
    if len(clean) < 2:
        raise ValueError("A path requires at least two distinct points")
    dirs = []
    for a, b in zip(clean[:-1], clean[1:]):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        dirs.append(((b[0] - a[0]) / length, (b[1] - a[1]) / length))
    sides = []
    for sign in (1, -1):
        offsets = [(-v[1] * width / 2 * sign, v[0] * width / 2 * sign) for v in dirs]
        edge = [[clean[0][0] + offsets[0][0], clean[0][1] + offsets[0][1]]]
        for i in range(1, len(clean) - 1):
            p = clean[i]
            a = [p[0] + offsets[i - 1][0], p[1] + offsets[i - 1][1]]
            b = [p[0] + offsets[i][0], p[1] + offsets[i][1]]
            # A straight bevel between 45-degree offsets introduces a 22.5-
            # degree edge at a 135-degree reversal. Keep the octilinear miter;
            # lead routing must avoid that reversal rather than mask it.
            edge.append(_line_intersection(a, dirs[i - 1], b, dirs[i]))
        edge.append([clean[-1][0] + offsets[-1][0], clean[-1][1] + offsets[-1][1]])
        sides.append(edge)
    return sides[0] + list(reversed(sides[1]))


def _octilinear_direction(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return (0, 0)
    if abs(dx) < 1e-9:
        return (0, 1 if dy > 0 else -1)
    if abs(dy) < 1e-9:
        return (1 if dx > 0 else -1, 0)
    if abs(abs(dx) - abs(dy)) < 1e-8:
        return (1 if dx > 0 else -1, 1 if dy > 0 else -1)
    raise ValueError("A path segment is neither horizontal, vertical, nor 45 degrees")


def _grid_centerline(points, grid):
    """Nearest integer-grid path subject to exact eight-direction constraints.

    Each vertex moves by at most one grid in each coordinate. Independently
    rounding diagonal endpoints does not preserve their supporting equation.
    A small dynamic program keeps those equations exact without accumulating
    rounding drift along a many-segment path. Mirrored candidates have the same
    order and cost, preserving left/right symmetry.
    """
    clean = []
    for point in points:
        if not clean or math.hypot(point[0] - clean[-1][0], point[1] - clean[-1][1]) > 1e-9:
            clean.append(point)
    if len(clean) < 2:
        raise ValueError("A path requires at least two distinct points")
    directions = [_octilinear_direction(a, b) for a, b in zip(clean, clean[1:])]

    def candidates(value):
        low, high = int(math.ceil(value - 1 - 1e-9)), int(math.floor(value + 1 + 1e-9))
        return sorted(range(low, high + 1), key=lambda v: ((v - value) ** 2, abs(v), v if value >= 0 else -v))

    choices = []
    for x, y in clean:
        x, y = x / grid, y / grid
        choices.append([(xx, yy, (xx - x) ** 2 + (yy - y) ** 2)
                        for xx in candidates(x) for yy in candidates(y)])
    states = [(8 * option[2], None) for option in choices[0]]
    history = [states]
    for index in range(1, len(clean)):
        direction = directions[index - 1]
        next_states = []
        weight = 8 if index == len(clean) - 1 else 1
        for x, y, cost in choices[index]:
            best = (float("inf"), None)
            for previous, (px, py, _) in enumerate(choices[index - 1]):
                dx, dy = x - px, y - py
                valid = (dx == 0 and dy == 0) or (
                    (dx == 0 if direction[0] == 0 else dx * direction[0] > 0) and
                    (dy == 0 if direction[1] == 0 else dy * direction[1] > 0) and
                    (direction[0] == 0 or direction[1] == 0 or abs(dx) == abs(dy)))
                candidate = states[previous][0] + weight * cost
                if valid and candidate < best[0] - 1e-12:
                    best = (candidate, previous)
            next_states.append(best)
        states = next_states
        history.append(states)
    selected = min(range(len(states)), key=lambda index: states[index][0])
    if not math.isfinite(states[selected][0]):
        raise ValueError("The path cannot be placed on the manufacturing grid within one grid per vertex")
    result = []
    for index in range(len(clean) - 1, -1, -1):
        result.append(list(choices[index][selected][:2]))
        selected = history[index][selected][1]
    result.reverse()
    return [point for index, point in enumerate(result) if index == 0 or point != result[index - 1]]


def _integer_intersection(first, second):
    a, b, c = first
    d, e, f = second
    determinant = a * e - b * d
    if determinant == 0:
        raise ValueError("Parallel supporting lines have no unique intersection")
    nx, ny = c * e - b * f, a * f - c * d
    if nx % determinant or ny % determinant:
        raise ValueError("Diagonal supporting lines require matching grid parity")
    return [nx // determinant, ny // determinant]


def _grid_offset_line(point, direction, width_units, side):
    nx, ny = -direction[1], direction[0]
    # Nearest diagonal support preserves physical width within one grid.
    offset = (int(round(width_units / math.sqrt(2)))
              if nx and ny else int(round(width_units / 2)))
    return (nx, ny, nx * point[0] + ny * point[1] + side * offset)


def _grid_cap_line(point, direction, offset_line=None, end=1):
    limit = direction[0] * point[0] + direction[1] * point[1]
    if direction[0] and direction[1] and offset_line is not None and (limit - offset_line[2]) % 2:
        # A half-grid cap would violate OA manufacturing grid. Extend outward
        # by at most grid/sqrt(2) so its parity matches the side boundaries.
        limit += end
    return (direction[0], direction[1], limit)


def _clean_grid_polygon(points):
    result = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    if len(result) > 1 and result[0] == result[-1]:
        result.pop()
    changed = True
    while changed and len(result) > 3:
        changed = False
        for index, point in enumerate(result):
            before, after = result[index - 1], result[(index + 1) % len(result)]
            if _cross(before, point, after) == 0 and _on_segment(point, before, after):
                result.pop(index)
                changed = True
                break
    return result


def _grid_stroke(points, width_units):
    """Mitered stroke using integer axis/diagonal supporting equations."""
    directions = [_octilinear_direction(a, b) for a, b in zip(points, points[1:])]
    sides = []
    for side in (1, -1):
        lines = [_grid_offset_line(point, direction, width_units, side)
                 for point, direction in zip(points, directions)]
        edge = [_integer_intersection(lines[0], _grid_cap_line(points[0], directions[0], lines[0], -1))]
        for index, point in enumerate(points[1:-1], 1):
            previous, following = directions[index - 1], directions[index]
            if previous == following:
                edge.append(_integer_intersection(lines[index], _grid_cap_line(point, following, lines[index])))
            elif previous == (-following[0], -following[1]):
                raise ValueError("A path reverses direction by 180 degrees")
            else:
                edge.append(_integer_intersection(lines[index - 1], lines[index]))
        edge.append(_integer_intersection(lines[-1], _grid_cap_line(points[-1], directions[-1], lines[-1])))
        sides.append(edge)
    return _clean_grid_polygon(sides[0] + list(reversed(sides[1])))


def _octilinear_grid_hull(points):
    """Convex join cover with exclusively integer axis/diagonal edges."""
    normals = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
    lines = []
    for nx, ny in normals:
        limit = max(nx * x + ny * y for x, y in points)
        if nx and ny and limit % 2:
            limit += 1
        lines.append((nx, ny, limit))
    vertices = []
    for index, first in enumerate(lines):
        for second in lines[index + 1:]:
            if first[0] * second[1] == first[1] * second[0]:
                continue
            point = _integer_intersection(first, second)
            if all(nx * point[0] + ny * point[1] <= limit for nx, ny, limit in lines):
                vertices.append(point)
    return _clean_grid_polygon([[int(x), int(y)] for x, y in _convex_hull(vertices)])


def _grid_join(before, point, after, width_units):
    first_direction, second_direction = _octilinear_direction(before, point), _octilinear_direction(point, after)
    candidates = []
    for side in (1, -1):
        first = _grid_offset_line(point, first_direction, width_units, side)
        second = _grid_offset_line(point, second_direction, width_units, side)
        candidates.extend([_integer_intersection(first, _grid_cap_line(point, first_direction, first)),
                           _integer_intersection(second, _grid_cap_line(point, second_direction, second, -1))])
        if first_direction != second_direction:
            candidates.append(_integer_intersection(first, second))
    return _octilinear_grid_hull(candidates)


def _convex_hull(points):
    points = sorted(set((float(x), float(y)) for x, y in points))
    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 1e-12:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 1e-12:
            upper.pop()
        upper.append(point)
    return [list(point) for point in lower[:-1] + upper[:-1]]


def _simple_polygon(poly):
    edges = list(zip(poly, poly[1:] + poly[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            if _segments_intersect(a, b, edges[j][0], edges[j][1]):
                return False
    return True


def _native_pair(value, positive=False):
    """Native cut/enclosure vectors must be real finite geometry."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    return all(isinstance(v, (float, int)) and not isinstance(v, bool) and
               math.isfinite(v) and (v > 0 if positive else v >= 0) for v in value)


def _chain(technology, source, target):
    adjacency = {}
    for via in technology.get("vias", []):
        if not isinstance(via, dict) or not all(isinstance(via.get(key), str) and via[key]
                                                for key in ("name", "lower", "upper")):
            continue
        if (not _native_pair(via.get("cut_size"), positive=True) or
                not all(_native_pair(via.get(key)) for key in
                        ("cut_spacing", "lower_enclosure", "upper_enclosure"))):
            continue
        if any(not isinstance(via.get(key), (list, tuple)) or len(via[key]) != 2 or
               any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
                   for v in via[key]) for key in ("lower_offset", "upper_offset", "origin_offset") if key in via):
            continue
        adjacency.setdefault(via["lower"], []).append((via["upper"], via))
        adjacency.setdefault(via["upper"], []).append((via["lower"], via))
    queue = deque([(source, [])])
    seen = {source}
    while queue:
        here, steps = queue.popleft()
        if here == target:
            return steps
        for nxt, via in adjacency.get(here, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, steps + [via]))
    return None


class _UnionFind(object):
    def __init__(self, n):
        self.parent = list(range(n))

    def root(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        a, b = self.root(a), self.root(b)
        if a != b:
            self.parent[b] = a


class GeometryBuilder(object):
    """Shared geometry/via contract for every passive-device body model."""
    def __init__(self, parameters, layers, technology, result=None):
        self.parameters = dict(DEFAULT_PARAMETERS)
        self.parameters.update(parameters or {})
        legacy_ring = "ring_extent" not in (parameters or {}) and "ring_gap" in (parameters or {})
        self.layers = dict(layers)
        self.technology = technology
        self.rules = {r["name"]: r for r in technology.get("layers", [])}
        self.grid = drawing_grid(technology)
        self.result = result or {"ok": False, "errors": [], "warnings": [], "parameters": self.parameters,
                                 "geometry": {"polygons": [], "vias": [], "ports": [], "bounds": [[0, 0], [0, 0]], "metrics": {}}}
        self.result["parameters"] = self.parameters
        self.result.setdefault("_legacy_ring_gap", legacy_ring)
        self.geometry = self.result["geometry"]
        self.errors = self.result["errors"]
        self.warnings = self.result["warnings"]
        for warning in missing_rule_warnings(technology, {metal for role, metal in self.layers.items() if shielding_role_active(role, self.parameters)}):
            if warning not in self.warnings:
                self.warnings.append(warning)

    def array_spacing(self, via):
        spacing = via.get("array_cut_spacing")
        if _native_pair(spacing):
            return spacing
        if "array_cut_spacing" in via:
            warning = _error("technology", "unverified_array_spacing",
                "Native via {} has no verified supplementary array-spacing rule; using its verified native cut spacing. Run foundry DRC after generation.".format(via["name"]), ["library"])
            if warning not in self.warnings:
                self.warnings.append(warning)
        return via["cut_spacing"]

    def rule_issue(self, part, code, message, fields=None):
        """Keep numeric foundry limits distinct from invalid physical topology."""
        issue = _error(part, code, message, fields)
        target = self.warnings if self.parameters.get("rule_policy", "strict") == "warn" else self.errors
        if issue not in target:
            target.append(issue)

    def tap_exit_x(self, origin, width):
        """Avoid the fixed central ground via without changing its shielding."""
        if not shielding_state(self.parameters)[2]:
            return origin[0]
        route, ring = self.layers.get('ground_route'), self.layers.get('ring')
        if route not in self.rules or ring not in self.rules:
            return origin[0]
        pad = self.landing_size(route, ring, self.parameters.get('ground_route_width', 1.))
        if pad is None:
            return origin[0]
        spacing = _spacing(self.rules.get(self.layers.get('tap'), {}), width,
                           2 * self.parameters.get('ring_extent', 100))
        needed = pad[0] / 2 + width / 2 + spacing + 4 * self.grid
        if abs(origin[0]) >= needed:
            return origin[0]
        exit_x = math.ceil(needed / self.grid) * self.grid
        self.warnings.append(_error('tap', 'tap_ground_avoidance',
            'TAP exits at x={:.3f} um to clear the fixed centre-line ground-ring via; shielding geometry is retained.'.format(exit_x),
            ['center_tap', 'tap', 'ground_route']))
        return exit_x

    def snap(self, value):
        return round(round(float(value) / self.grid) * self.grid, 9)

    def add_polygon(self, points, layer, part, net, metadata=None, **extra):
        poly = {"points": [[self.snap(x), self.snap(y)] for x, y in points], "layer": layer, "part": part, "net": net}
        if metadata:
            poly.update(metadata)
        poly.update(extra)
        try:
            for a, c in zip(points, points[1:] + points[:1]):
                _octilinear_direction(a, c)
            integer_points = [[int(round(x / self.grid)), int(round(y / self.grid))] for x, y in poly["points"]]
            for a, c in zip(integer_points, integer_points[1:] + integer_points[:1]):
                dx, dy = c[0] - a[0], c[1] - a[1]
                if dx and dy and abs(dx) != abs(dy):
                    raise ValueError("Independent polygon rounding does not preserve its 45-degree edge")
        except ValueError as problem:
            self.errors.append(_error(part, "non_octilinear_polygon", str(problem)))
            poly["points"] = []
            return poly
        self.geometry["polygons"].append(poly)
        return poly

    def add_path(self, points, width, layer, part, net, **extra):
        rule = self.rules.get(layer, {})
        diagonal = any(abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8 for a, b in zip(points, points[1:]))
        margin = self.grid * (math.sqrt(2) if diagonal else 1)
        minimum = max(_rule_number(rule, "min_width"), _rule_number(rule, "min_diagonal_width") if diagonal else 0)
        if width < minimum + margin or (_rule_number(rule, "max_width") > 0 and width > _rule_number(rule, "max_width") - margin):
            self.rule_issue(part, "metal_width", "Requested path width is outside the PDK width limits, including manufacturing-grid margin.", ["inner_width", "outer_width", "ground_route_width"])
        try:
            integer_points = _grid_centerline(points, self.grid)
            if len(integer_points) < 2:
                raise ValueError("The path collapses on the manufacturing grid")
            polygon = _grid_stroke(integer_points, width / self.grid)
        except ValueError as problem:
            self.errors.append(_error(part, "non_octilinear_centerline", str(problem), ["inner_width", "outer_width"]))
            return {"points": [], "layer": layer, "part": part, "net": net}

        def physical(vertices):
            return [[self.snap(x * self.grid), self.snap(y * self.grid)] for x, y in vertices]

        extra.update({"centerline": physical(integer_points), "width": width})
        short_jog = len(integer_points) > 2 and any(a[0] == c[0] and 0 < abs(a[1] - c[1]) * self.grid < width
                                                   for a, c in zip(integer_points, integer_points[1:]))
        if _simple_polygon(polygon) and not short_jog:
            return self.add_polygon(physical(polygon), layer, part, net, extra)
        # Short automatic jogs can make a miter outline fold over itself. Emit
        # its exact straight strips plus filled joins rather than a notch or a
        # self-intersecting OA polygon. Every strip retains its full width.
        created = []
        for a, c in zip(integer_points, integer_points[1:]):
            created.append(self.add_polygon(physical(_grid_stroke([a, c], width / self.grid)), layer, part, net, extra, path_piece="segment"))
        for a, pnt, c in zip(integer_points, integer_points[1:], integer_points[2:]):
            hull = _grid_join(a, pnt, c, width / self.grid)
            if len(hull) >= 3:
                created.append(self.add_polygon(physical(hull), layer, part, net, extra, path_piece="filled_join"))
        return created[0]

    def landing_size(self, lower, upper, width, landing_length=None):
        chain = _chain(self.technology, lower, upper)
        if not chain:
            self.errors.append(_error("crossing", "no_native_via_chain", "No usable native PDK via chain with verified cut size, spacing and enclosure connects the selected layers.", ["body", "crossing", "ground_route"]))
            return None
        pad_x, pad_y = float(landing_length or width), float(width)
        for via in chain:
            for field in ("lower_offset", "upper_offset", "origin_offset"):
                if any(abs(float(v)) > 1e-9 for v in via.get(field, [0, 0])):
                    self.errors.append(_error("crossing", "unsupported_via_offset", "Non-zero native-via offsets are not supported by this generator.", ["body", "crossing", "ground_route"]))
                    return None
            sx, sy = self.array_spacing(via)
            ex = max(via["lower_enclosure"][0], via["upper_enclosure"][0])
            ey = max(via["lower_enclosure"][1], via["upper_enclosure"][1])
            pad_x = max(pad_x, 2 * via["cut_size"][0] + sx + 2 * ex + 2 * self.grid)
            pad_y = max(pad_y, via["cut_size"][1] + 2 * ey + 2 * self.grid)
            for metal in (via["lower"], via["upper"]):
                rule = self.rules.get(metal, {})
                minimum = _rule_number(rule, "min_width") + 2 * self.grid
                pad_x, pad_y = max(pad_x, minimum), max(pad_y, minimum)
                pad_x = max(pad_x, _rule_number(rule, "min_area") / pad_y)
        return (math.ceil(pad_x / self.grid) * self.grid, math.ceil(pad_y / self.grid) * self.grid)

    def add_via_stack(self, origin, lower, upper, width, part, net, landing_length=None):
        dimensions = self.landing_size(lower, upper, width, landing_length)
        if dimensions is None:
            return None
        px, py = dimensions
        x, y = [self.snap(v) for v in origin]
        bbox = [[self.snap(x - px / 2), self.snap(y - py / 2)], [self.snap(x + px / 2), self.snap(y + py / 2)]]
        metals = set()
        for definition in _chain(self.technology, lower, upper):
            metals.update((definition["lower"], definition["upper"]))
            ex = max(definition["lower_enclosure"][0], definition["upper_enclosure"][0])
            ey = max(definition["lower_enclosure"][1], definition["upper_enclosure"][1])
            cx, cy = definition["cut_size"]
            sx, sy = self.array_spacing(definition)
            columns = int(math.floor((px - 2 * ex + sx + 1e-9) / (cx + sx)))
            rows = int(math.floor((py - 2 * ey + sy + 1e-9) / (cy + sy)))
            if min(rows, columns) < 1:
                self.errors.append(_error(part, "via_array_fit", "A native via cannot fit the calculated landing.", ["inner_width", "outer_width", "ground_route_width"]))
                continue
            if rows * columns > MAX_CUTS_PER_ARRAY:
                self.errors.append(_error(part, "array_too_large", "A native via array exceeds the 10,000-cut limit; reduce width or shorten the via chain.", ["inner_width", "outer_width", "ground_route_width"]))
            full_x = columns * cx + (columns - 1) * sx + 2 * ex
            full_y = rows * cy + (rows - 1) * sy + 2 * ey
            self.geometry["vias"].append({"origin": [x, y], "bbox": [[self.snap(x - full_x / 2), self.snap(y - full_y / 2)], [self.snap(x + full_x / 2), self.snap(y + full_y / 2)]],
                "via_def": definition["name"], "rows": rows, "columns": columns, "lower": definition["lower"], "upper": definition["upper"], "cut_layer": definition.get("cut_layer"),
                "cut_size": definition["cut_size"], "cut_spacing": [sx, sy], "lower_enclosure": definition["lower_enclosure"], "upper_enclosure": definition["upper_enclosure"],
                "enclosures": {"lower": definition["lower_enclosure"], "upper": definition["upper_enclosure"]}, "part": part, "net": net})
        for metal in sorted(metals):
            rule = self.rules.get(metal, {})
            short_edge = min(px, py)
            if short_edge < _rule_number(rule, "min_width") + self.grid or (_rule_number(rule, "max_width") > 0 and short_edge > _rule_number(rule, "max_width") - self.grid) or px * py < _rule_number(rule, "min_area"):
                fields = ["ground_route_width", "ground_route", "ring"] if part == "pgs" else ["inner_width", "outer_width", "body", "crossing"]
                self.rule_issue(part, "landing_width", "The native-via landing on {} violates its metal width or minimum-area limits.".format(metal), fields)
            self.add_polygon(_rect([bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1]]), metal, part, net, landing=True, width=short_edge, feature="via_landing")
        return {"bbox": bbox, "width": px, "height": py}


def validate_geometry(geometry, technology, expected_windings=None, rule_policy="strict", warnings=None):
    """Independently screen same-layer shorts/clearance and native via coverage."""
    errors = []
    numeric_issues = warnings if rule_policy == "warn" and warnings is not None else errors
    polys = geometry.get("polygons", [])
    vias = geometry.get("vias", [])
    rules = {layer["name"]: layer for layer in technology.get("layers", [])}
    grid = drawing_grid(technology)
    decimal_grid = Decimal(str(grid))

    def check_coordinates(points, closed, part, label):
        integers = []
        for point in points:
            values = [Decimal(str(value)) / decimal_grid for value in point]
            if any(value != value.to_integral_value() for value in values):
                errors.append(_error(part, "manufacturing_grid", "{} contains a coordinate outside the exact manufacturing grid.".format(label)))
                return
            integers.append([int(value) for value in values])
        if closed is None:
            return
        following = integers[1:] + integers[:1] if closed else integers[1:]
        for first, second in zip(integers, following):
            dx, dy = second[0] - first[0], second[1] - first[1]
            if dx and dy and abs(dx) != abs(dy):
                errors.append(_error(part, "non_octilinear_edge", "{} contains an edge outside the exact 0/45/90-degree directions.".format(label)))
                return

    for poly in polys:
        check_coordinates(poly["points"], True, poly["part"], "Metal polygon")
        if poly.get("centerline"):
            check_coordinates(poly["centerline"], False, poly["part"], "Path centreline")
    for via in vias:
        check_coordinates(via["bbox"], None, via["part"], "Via bounding box")
        check_coordinates([via["origin"]], None, via["part"], "Via origin")
    for port in geometry.get("ports", []):
        check_coordinates(port["bbox"], None, port.get("part", "legs"), "Port bounding box")
    # Coordinate snapping can reduce a diagonal separation by up to sqrt(2)*grid.
    tolerance = grid * 1.5 + 1e-8
    boxes = [_bbox(p["points"]) for p in polys]
    uf = _UnionFind(len(polys))
    for i, poly in enumerate(polys):
        if not _simple_polygon(poly["points"]):
            errors.append(_error(poly["part"], "self_intersection", "A generated polygon intersects itself; increase its dimensions or clearance."))
        for j in range(i):
            other = polys[j]
            if poly["layer"] != other["layer"]:
                continue
            # All PGS branches intentionally meet their diagonal spines; equal
            # pitch and analytical minimum-arm spacing screen their geometry.
            if (poly["net"] == other["net"] == "GND" and poly["part"] == other["part"] == "pgs" and
                    poly.get("feature") in ("arm", "spine") and other.get("feature") in ("arm", "spine")):
                continue
            rule = rules.get(poly["layer"], {})
            width = max(float(poly.get("width", 0)), float(other.get("width", 0)))
            parallel_length = max(boxes[i][2] - boxes[i][0], boxes[i][3] - boxes[i][1],
                                  boxes[j][2] - boxes[j][0], boxes[j][3] - boxes[j][1])
            clearance = _spacing(rule, width, parallel_length, poly["net"] == other["net"])
            if _bbox_distance(boxes[i], boxes[j]) > clearance + tolerance:
                continue
            distance = polygon_distance(poly["points"], other["points"])
            if poly["net"] == other["net"]:
                intentional_ground_port = geometry.get("metrics", {}).get("pgs_connect_ring") and (poly.get("landing") or other.get("landing"))
                if {poly["part"], other["part"]} == {"pgs", "ring"} and distance <= 1e-8 and not intentional_ground_port:
                    errors.append(_error("ring", "pgs_ring_contact", "PGS touches the ring on the same metal, creating an unintended closed shielding path; increase ring extent or select another metal.", ["ring_extent", "ring"]))
                if distance <= 1e-8:
                    uf.union(i, j)
                # These pieces are the strips and filled joins of one path.
                # Their apparent pairwise gap lies inside the connecting strip;
                # applying spacing to its internal decomposition is incorrect.
                elif (distance + tolerance < clearance and not (
                      (poly.get("path_piece") and other.get("path_piece") and
                       poly.get("centerline") == other.get("centerline") and
                       poly.get("feature") == other.get("feature")) or
                      (poly.get("connector_id") and poly.get("connector_id") == other.get("connector_id") and
                       ((poly.get("landing") and other.get("path_piece")) or
                        (other.get("landing") and poly.get("path_piece")))))):
                    numeric_issues.append(_error(poly["part"], "same_net_spacing", "Separated shapes on {} for net {} are {:.3f} um apart; the required clearance is {:.3f} um.".format(poly["layer"], poly["net"], distance, clearance)))
                continue
            if distance <= 1e-8:
                part = "ring" if "ring" in (poly["part"], other["part"]) else poly["part"]
                errors.append(_error(part, "same_layer_short", "On {}, nets {} and {} touch or overlap.".format(poly["layer"], poly["net"], other["net"]), ["ring_extent", "ring"] if part == "ring" else []))
            elif distance + tolerance < clearance:
                numeric_issues.append(_error(poly["part"], "metal_spacing", "On {}, nets {} and {} are {:.3f} um apart; the required clearance is {:.3f} um.".format(poly["layer"], poly["net"], other["net"], distance, clearance)))
            if len(errors) > 30:
                return errors, {}
    for via in vias:
        by_layer = {}
        for layer, enclosure in ((via["lower"], via["enclosures"]["lower"]), (via["upper"], via["enclosures"]["upper"])):
            cut_w = via["columns"] * via["cut_size"][0] + (via["columns"] - 1) * via["cut_spacing"][0]
            cut_h = via["rows"] * via["cut_size"][1] + (via["rows"] - 1) * via["cut_spacing"][1]
            x, y = via["origin"]
            required = _rect([x - cut_w / 2 - enclosure[0], y - cut_h / 2 - enclosure[1],
                              x + cut_w / 2 + enclosure[0], y + cut_h / 2 + enclosure[1]])
            matches = [i for i, poly in enumerate(polys) if poly["layer"] == layer and poly["net"] == via["net"] and all(_point_inside(p, poly["points"]) for p in required)]
            if not matches:
                errors.append(_error(via["part"], "via_coverage", "Native via {} has incomplete metal enclosure on {}.".format(via["via_def"], layer)))
            by_layer[layer] = matches
        for left in by_layer.get(via["lower"], []):
            for right in by_layer.get(via["upper"], []):
                uf.union(left, right)
    components = {}
    for net in (expected_windings if expected_windings is not None else geometry.get("metrics", {}).get("expected_windings", ("P", "S"))):
        nodes = [i for i, poly in enumerate(polys) if poly["net"] == net]
        roots = {uf.root(i) for i in nodes}
        components[net] = len(roots)
        if len(roots) != 1:
            errors.append(_error("body", "disconnected_winding", "Winding {} has {} disconnected regions; exactly one is required.".format(net, len(roots))))
    if geometry.get("metrics", {}).get("pgs_connect_ring"):
        ground_roots = []
        for port in geometry.get("ports", []):
            if port["name"] not in ("GND_PGS", "GND_RING"):
                continue
            point = [(port["bbox"][0][k] + port["bbox"][1][k]) / 2 for k in (0, 1)]
            matches = [i for i, poly in enumerate(polys) if poly["net"] == "GND" and poly["layer"] == port["layer"] and _point_inside(point, poly["points"])]
            ground_roots.append({uf.root(i) for i in matches})
        if len(ground_roots) != 2 or not ground_roots[0].intersection(ground_roots[1]):
            errors.append(_error("pgs", "ground_disconnected", "The requested PGS-to-ring routing is physically disconnected.", ["pgs_connect_ring", "ground_route"]))
    return errors, components


def postprocess_geometry(result, layers, technology, body_extent=None, expected_windings=("P", "S")):
    """Add independent shield/ring/ground routing and validate any body model.

    Input is a body-only result with the usual polygons/vias/ports contract.
    ``body_extent`` is the coil's outer apothem/half-width, excluding terminals.
    Legacy ring_gap is imported once; saved physical ring_extent stays fixed.
    """
    b = GeometryBuilder(result.get("parameters", {}), layers, technology, result)
    p, geometry, errors = b.parameters, b.geometry, b.errors
    for name in ('pgs_enabled', 'ring_enabled'):
        if not isinstance(p[name], bool):
            errors.append(_error('pgs' if name.startswith('pgs') else 'ring', 'parameter_type',
                                 '{} must be true or false.'.format(name), [name]))
    if errors:
        result.pop('_legacy_ring_gap', None)
        return result
    pgs_enabled, ring_enabled, linked = shielding_state(p)
    for entry in PARAMETER_SCHEMA:
        if entry["part"] not in ("pgs", "ring", "ground_route"):
            continue
        key = entry["name"]
        if not shielding_parameter_active(key, p):
            continue
        if entry.get("optional") and p[key] in (None, ""):
            p[key] = None
            continue
        if entry.get("type") == "boolean":
            if not isinstance(p[key], bool):
                errors.append(_error(entry["part"], "parameter_type", "{} must be true or false.".format(key), [key]))
            continue
        if entry.get("options"):
            if p[key] not in entry["options"]:
                errors.append(_error(entry["part"], "invalid_option", "Unsupported {} option.".format(key), [key]))
            continue
        try:
            if isinstance(p[key], bool):
                raise ValueError()
            p[key] = float(p[key])
            if not math.isfinite(p[key]) or not entry["min"] <= p[key] <= entry["max"]:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            errors.append(_error(entry["part"], "parameter_range", "{} must be between {} and {} um.".format(key, entry["min"], entry["max"]), [key]))
    for role in ("pgs", "ring"):
        if shielding_role_active(role, p) and layers.get(role) not in b.rules:
            errors.append(_error(role, "unknown_layer", "Select a verified PDK metal for {}.".format(role), [role]))
    if errors:
        result.pop("_legacy_ring_gap", None)
        return result
    extent = float(body_extent if body_extent is not None else geometry.get("metrics", {}).get("body_extent", 0))
    if result.pop("_legacy_ring_gap", False) and ring_enabled:
        try:
            if isinstance(p["ring_gap"], bool):
                raise ValueError()
            p["ring_gap"] = float(p["ring_gap"])
            if not math.isfinite(p["ring_gap"]) or not 0.1 <= p["ring_gap"] <= 200:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            errors.append(_error("ring", "parameter_range", "Legacy ring_gap must be between 0.1 and 200 um.", ["ring_extent"]))
            return result
        p["ring_extent"] = extent + p["ring_gap"]
        b.warnings.append(_error("ring", "legacy_ring_import", "Legacy ring clearance was imported as a fixed physical ring extent.", ["ring_extent"]))
    used_body_metals = {poly["layer"] for poly in geometry["polygons"] if poly.get("net") != "GND"}
    if not used_body_metals or any(metal not in b.rules for metal in used_body_metals):
        errors.append(_error("body", "unknown_body_metal", "The body uses an unknown or empty metal stack."))
    elif pgs_enabled and not all(b.rules[metal].get("order_verified", b.technology.get("order_verified", True))
                 for metal in used_body_metals | {layers["pgs"]}):
        warning = _error("pgs", "unverified_layer_order",
            "The PDK does not expose a verified physical metal order. The PGS-below-body check is unavailable; verify the selected stack in the PDK before signoff.", ["pgs", "body"])
        if warning not in b.warnings:
            b.warnings.append(warning)
    elif pgs_enabled and b.rules[layers["pgs"]]["order"] >= min(b.rules[metal]["order"] for metal in used_body_metals):
        errors.append(_error("pgs", "pgs_layer_order", "PGS metal must be below every body and via-landing metal.", ["pgs"]))
    spine_width = p["pgs_width"] if p["pgs_spine_width"] is None else p["pgs_spine_width"]
    for key, width, role, diagonal in (("pgs_width", p["pgs_width"], "pgs", True), ("pgs_spine_width", spine_width, "pgs", True), ("ring_width", p["ring_width"], "ring", p["ring_shape"] == "octagon")):
        if not shielding_role_active(role, p):
            continue
        rule = b.rules[layers[role]]
        minimum = max(_rule_number(rule, "min_width"), _rule_number(rule, "min_diagonal_width") if diagonal else 0)
        margin = b.grid * (math.sqrt(2) if diagonal else 1)
        if width < minimum + margin or (_rule_number(rule, "max_width") > 0 and width > _rule_number(rule, "max_width") - margin):
            b.rule_issue(role, "metal_width", "{} is outside the PDK width limits, including grid margin.".format(key), [key])
    if pgs_enabled:
        clearance = _spacing(b.rules[layers["pgs"]], max(p["pgs_width"], spine_width), 2 * p["pgs_extent"], True)
        if p["pgs_spacing"] < clearance + 2 * b.grid:
            b.rule_issue("pgs", "pgs_spacing", "PGS arm spacing must be at least {:.4f} um.".format(clearance + 2 * b.grid), ["pgs_spacing"])
    if ring_enabled and p["ring_extent"] <= extent + b.grid:
        errors.append(_error("ring", "ring_enclosure", "The ring inner extent must exceed the body outline; change the ring explicitly.", ["ring_extent"]))
    if pgs_enabled and p["pgs_extent"] / (p["pgs_width"] + p["pgs_spacing"]) > 250:
        errors.append(_error("pgs", "pgs_arm_limit", "PGS is too dense: at most 250 arm pairs per quadrant are supported.", ["pgs_extent", "pgs_spacing"]))
    if errors:
        return result

    count = 0
    if pgs_enabled:
        pw, pe = p["pgs_width"], p["pgs_extent"]
        shape = p["pgs_shape"]
        b.add_polygon(_rect([-1, -1, 1, 1]), layers["pgs"], "pgs", "GND", group="PGS", feature="center")
        geometry["ports"].append({"name": "GND_PGS", "layer": layers["pgs"], "bbox": [[-1, -1], [1, 1]], "net": "GND", "part": "pgs"})
        if shape == "square":
            tmax = pe - spine_width / (2 * math.sqrt(2))
        elif shape == "circle":
            tmax = math.sqrt(pe * pe - spine_width * spine_width / 4) / math.sqrt(2)
        else:
            tmax = pe / math.sqrt(2)
        first = max(1 + pw / 2 + p["pgs_spacing"], pw + p["pgs_spacing"])
        pitch = pw + p["pgs_spacing"]
        count = 0
        for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
            b.add_path([[0, 0], [sx * tmax, sy * tmax]], spine_width, layers["pgs"], "pgs", "GND", group="PGS", feature="spine")
            t = first
            while t < tmax - pw:
                end = pe
                if shape == "octagon":
                    end = min(pe, math.sqrt(2) * pe - t - pw / 2)
                elif shape == "circle":
                    end = math.sqrt(max(0, pe * pe - (t + pw / 2) ** 2))
                end = math.floor((end + 1e-10) * 10) / 10
                min_length = max(pw, _rule_number(b.rules[layers["pgs"]], "min_area") / pw)
                if end > t + min_length:
                    for line in ([[sx * t, sy * t], [sx * end, sy * t]], [[sx * t, sy * t], [sx * t, sy * end]]):
                        b.add_path(line, pw, layers["pgs"], "pgs", "GND", group="PGS", feature="arm", endpoint_step=0.1)
                        count += 1
                t += pitch
    if ring_enabled:
        ri, ro = p["ring_extent"], p["ring_extent"] + p["ring_width"]
        if p["ring_shape"] == "square":
            for box in ([-ro, ri, ro, ro], [-ro, -ro, ro, -ri], [-ro, -ri, -ri, ri], [ri, -ri, ro, ri]):
                b.add_polygon(_rect(box), layers["ring"], "ring", "GND", group="GND_RING", width=p["ring_width"])
        else:
            ai, ao = (math.sqrt(2) - 1) * ri, (math.sqrt(2) - 1) * ro
            quadrant = [[0, ri], [ai, ri], [ri, ai], [ri, 0], [ro, 0], [ro, ao], [ao, ro], [0, ro]]
            for quarter in range(4):
                def rotate(point):
                    x, y = point
                    for unused in range(quarter):
                        x, y = -y, x
                    return [x, y]
                b.add_polygon([rotate(point) for point in quadrant], layers["ring"], "ring", "GND", group="GND_RING", width=p["ring_width"])
        geometry["ports"].append({"name": "GND_RING", "layer": layers["ring"], "bbox": [[b.snap(-p["ring_width"] / 2), b.snap(ri)], [b.snap(p["ring_width"] / 2), b.snap(ro)]], "net": "GND", "part": "ring"})
    geometry["metrics"].update({"expected_windings": list(expected_windings), "body_extent": extent,
        "body_metals": sorted(used_body_metals), "pgs_enabled": pgs_enabled, "ring_enabled": ring_enabled,
        "ring_inner_radius": ri if ring_enabled else None, "ring_outer_radius": ro if ring_enabled else None,
        "actual_ring_body_gap": ri - extent if ring_enabled else None,
        "ring_shape": p["ring_shape"] if ring_enabled else None, "pgs_arm_count": count,
        "pgs_pitch": pitch if pgs_enabled else None, "pgs_spine_width": spine_width if pgs_enabled else None,
        "pgs_shape": shape if pgs_enabled else None, "actual_pgs_extent": None, "pgs_connect_ring": linked})
    if p.get("pgs_connect_ring") is True and not (pgs_enabled and ring_enabled):
        b.warnings.append(_error("ground_route", "ground_link_inactive",
            "PGS-to-ring connection is inactive because one or both shields are disabled; its saved setting is retained.",
            ["pgs_enabled", "ring_enabled", "pgs_connect_ring"]))
    if linked:
        route = layers.get("ground_route")
        if route not in b.rules:
            errors.append(_error("pgs", "unknown_ground_route", "Select a verified metal for the ground routing.", ["ground_route"]))
        elif route == layers["pgs"] or route in used_body_metals:
            errors.append(_error("pgs", "ground_route_conflict", "Ground routing metal must differ from PGS and every body/via-landing metal.", ["ground_route"]))
        elif route == layers["ring"]:
            errors.append(_error("pgs", "ground_route_ring_same_layer", "Ground routing and ring must use different metals to provide a native via at both endpoints.", ["ground_route", "ring"]))
        else:
            centre_size = b.landing_size(layers["pgs"], route, 2.0, 2.0)
            ring_size = b.landing_size(route, layers["ring"], p["ground_route_width"])
            if centre_size and (centre_size[0] > 2 + 1e-8 or centre_size[1] > 2 + 1e-8):
                errors.append(_error("pgs", "center_via_fit", "The central native via stack cannot fit the fixed 2 x 2 um PGS square; select another routing metal.", ["ground_route"]))
            if ring_size and ring_size[1] > p["ring_width"] + 1e-8:
                errors.append(_error("ring", "ring_via_fit", "Ring width must be at least {:.3f} um to contain the endpoint via stack.".format(ring_size[1]), ["ring_width", "ground_route", "ring"]))
            if not errors:
                end_y = b.snap(ri + p["ring_width"] / 2)
                b.add_path([[0, 0], [0, end_y]], p["ground_route_width"], route, "pgs", "GND", feature="ground_route")
                b.add_via_stack([0, 0], layers["pgs"], route, 2.0, "pgs", "GND", 2.0)
                b.add_via_stack([0, end_y], route, layers["ring"], p["ground_route_width"], "pgs", "GND")
    elif pgs_enabled and ring_enabled:
        b.warnings.append(_error("pgs", "pgs_ground_connection", "PGS and ring are separate physical conductors; connect the two ground pins in the parent layout if required."))
    if pgs_enabled:
        pgs_points = [xy for poly in geometry["polygons"] if poly["part"] == "pgs" and poly.get("feature") not in ("ground_route", "via_landing") for xy in poly["points"]]
        geometry["metrics"]["actual_pgs_extent"] = max(abs(v) for xy in pgs_points for v in xy)
        outside = any(max(abs(x), abs(y)) > pe + b.grid + 1e-8 for x, y in pgs_points)
        if shape == "octagon":
            outside = outside or any(abs(x) + abs(y) > math.sqrt(2) * pe + 2 * b.grid + 1e-8 for x, y in pgs_points)
        if shape == "circle":
            outside = outside or any(math.hypot(x, y) > pe + 2 * b.grid + 1e-8 for x, y in pgs_points)
        if outside:
            errors.append(_error("pgs", "pgs_outline", "PGS metal exceeds the selected physical outline.", ["pgs_extent", "pgs_width", "pgs_spine_width"]))
    total_cuts = sum(v["rows"] * v["columns"] for v in geometry["vias"])
    geometry["metrics"].update({"native_cut_count": total_cuts, "via_array_count": len(geometry["vias"]), "grid": b.grid})
    if total_cuts > MAX_TOTAL_CUTS:
        errors.append(_error("crossing", "array_too_large", "The design exceeds the 50,000 native-cut limit.", ["inner_width", "outer_width", "ground_route_width"]))
    boxes = [_bbox(poly["points"]) for poly in geometry["polygons"]]
    if boxes:
        geometry["bounds"] = [[min(box[0] for box in boxes), min(box[1] for box in boxes)], [max(box[2] for box in boxes), max(box[3] for box in boxes)]]
    # Terminals specify a small source face at their real conductor boundary.
    # TAPs retain their winding net, and are independent extraction terminals.
    for port in geometry["ports"]:
        port.setdefault("kind", "ground" if port.get("net") == "GND" else "signal")
        port.setdefault("winding", port.get("net"))
        if "access_direction" not in port:
            port["access_direction"] = "top" if port["bbox"][0][1] > 0 else "bottom"
        if port["kind"] != "ground":
            port.setdefault("endpoint", "positive" if port["name"].endswith("1") else "negative")
    diagnostics, components = validate_geometry(geometry, technology,
        rule_policy=p.get("rule_policy", "strict"), warnings=b.warnings)
    errors.extend(diagnostics)
    geometry["metrics"]["winding_components"] = components
    b.warnings.append(_error("body", "signoff_required", "Generated geometry and native-via coverage were checked; foundry DRC and RF verification are still required."))
    result["ok"] = not errors
    return result
