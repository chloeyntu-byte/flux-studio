"""Leg pin layouts: several named pins along each terminal leg.

Every generated model ends its signal (and TAP) leads in a *leg*: a lead path
whose free end carries the model's default pin (P1, S2, L1, CTAP, ...).  A
leg-pin layout replaces those default pins with user pins placed along the
legs:

    design['leg_pins'] = {
        'P1': [{'name': 'pin_1', 'ratio': 0.0, 'length': 1.0},
               {'name': 'pin_1b', 'ratio': 0.4, 'length': 1.0}],
        'P2': [{'name': 'pin_2', 'ratio': 0.0, 'length': 1.0}],
        'S1': [],                      # explicit: no pin on this leg
    }

* The key is the leg id, i.e. the name of the leg's default pin.
* ``ratio`` is the distance from the leg's free end to the pin's end-side edge,
  divided by the leg length.  When a sweep changes the leg length, every pin
  keeps its relative position, so pin spacings scale with the leg.
* ``length`` is the pin length along the leg in um; the width is the leg width.
* A leg missing from the layout keeps its default pin (for example a TAP leg
  that appears only after the TAP is enabled).  An empty list removes it.

A layout identical to the default pins is ignored, so default designs keep the
built-in winding/endpoint pairs used for L/Q/k.  Custom pins carry no
winding/endpoint; metrics on them need explicit pin assignments
(see ``emx_search.metric_key``).
"""
import copy
import math
import re

NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
DEFAULT_LENGTH = 1.0
LEG_PARTS = ('legs', 'tap')
SHIELD_PARTS = ('pgs', 'ring', 'ground_route')
MAX_PINS_PER_LEG = 32
_EPS = 1e-9


def _error(part, code, message, fields=None):
    return {'part': part, 'code': code, 'message': message, 'fields': fields or ['leg_pins']}


def _inside(point, box, tolerance):
    return (box[0][0] - tolerance <= point[0] <= box[1][0] + tolerance and
            box[0][1] - tolerance <= point[1] <= box[1][1] + tolerance)


def _length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _final_run(line):
    """The last straight run of an oriented centerline (collinear tail)."""
    end = line[-1]
    tail = [line[-2], end]
    for point in reversed(line[:-2]):
        a, b = tail[0], tail[1]
        if abs((b[0] - a[0]) * (a[1] - point[1]) - (b[1] - a[1]) * (a[0] - point[0])) > 1e-9:
            break
        tail.insert(0, point)
    return tail


def _is_leg_port(port):
    return (port.get('kind', 'signal') in ('signal', 'tap') and port.get('net') != 'GND' and
            port.get('part', 'legs') in LEG_PARTS and isinstance(port.get('bbox'), list))


def legs(geometry, grid=0.001):
    """Return the terminal legs of a generated geometry, in port order.

    Each leg is ``{id, layer, net, width, kind, part, centerline, length,
    default_port}``; ``centerline`` is oriented so its last point is the free
    end that carries the default pin.
    """
    tolerance = max(2 * grid, 1e-6)
    result, used = [], set()
    polygons = [poly for poly in geometry.get('polygons', [])
                if poly.get('part') not in SHIELD_PARTS and len(poly.get('centerline') or []) >= 2]
    for port in geometry.get('ports', []):
        if not _is_leg_port(port) or port.get('custom_leg_pin'):
            continue
        best = None
        for poly in polygons:
            if poly.get('layer') != port.get('layer'):
                continue
            line = [list(map(float, point)) for point in poly['centerline']]
            key = tuple(map(tuple, line))
            if key in used:
                continue
            if _inside(line[-1], port['bbox'], tolerance):
                oriented = line
            elif _inside(line[0], port['bbox'], tolerance):
                oriented = line[::-1]
            else:
                continue
            if poly.get('part') not in LEG_PARTS:
                # A winding or bridge path that ends in the pin: only its final
                # straight run is the leg.
                oriented = _final_run(oriented)
            rank = (poly.get('part') in LEG_PARTS, _length(oriented))
            if best is None or rank > best[3]:
                best = (key, oriented, poly, rank)
        if best is None:
            continue
        key, line, poly, _rank = best
        used.add(key)
        result.append(dict(id=port['name'], layer=port['layer'], net=port.get('net', poly.get('net')),
                           width=float(poly.get('width') or (port['bbox'][1][0] - port['bbox'][0][0])),
                           kind=port.get('kind', 'signal'), part=port.get('part', 'legs'),
                           centerline=line, length=_length(line), default_port=copy.deepcopy(port)))
    return result


def default_layout(geometry, grid=0.001):
    """The layout equivalent to the generated default pins (one per leg)."""
    layout = {}
    for leg in legs(geometry, grid):
        box = leg['default_port']['bbox']
        a, b = leg['centerline'][-2], leg['centerline'][-1]
        horizontal = abs(b[0] - a[0]) > abs(b[1] - a[1])
        length = abs(box[1][0] - box[0][0]) if horizontal else abs(box[1][1] - box[0][1])
        layout[leg['id']] = [dict(name=leg['id'], ratio=0.0, length=round(length, 6))]
    return layout


def normalize_layout(layout):
    """Validate the stored structure; returns a clean copy or None."""
    if layout is None:
        return None
    if not isinstance(layout, dict):
        raise ValueError('leg_pins must be an object keyed by leg.')
    clean = {}
    for leg, pins in layout.items():
        if not isinstance(leg, str) or not NAME.fullmatch(leg):
            raise ValueError('Leg ids must be pin-style names.')
        if not isinstance(pins, list) or len(pins) > MAX_PINS_PER_LEG:
            raise ValueError('Leg %s needs a list of at most %d pins.' % (leg, MAX_PINS_PER_LEG))
        rows = []
        for pin in pins:
            if not isinstance(pin, dict):
                raise ValueError('Each leg pin must be an object.')
            name = pin.get('name')
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ValueError('Pin names start with a letter and use letters, digits and underscores (%r).' % (name,))
            try:
                ratio = float(pin.get('ratio', 0.0))
                length = float(pin.get('length', DEFAULT_LENGTH))
            except (TypeError, ValueError):
                raise ValueError('Pin %s needs numeric ratio and length.' % name)
            if isinstance(pin.get('ratio'), bool) or isinstance(pin.get('length'), bool) or \
                    not math.isfinite(ratio) or not 0 <= ratio <= 1 or not math.isfinite(length) or length <= 0:
                raise ValueError('Pin %s needs 0 <= ratio <= 1 and a positive length.' % name)
            rows.append(dict(name=name, ratio=ratio, length=length))
        clean[leg] = rows
    return clean


def is_default(layout, geometry, grid=0.001):
    if not layout:
        return True
    default = default_layout(geometry, grid)
    for leg, pins in layout.items():
        expected = default.get(leg)
        if expected is None or len(pins) != len(expected):
            return False
        for pin, reference in zip(pins, expected):
            if pin['name'] != reference['name'] or abs(pin['ratio']) > 1e-9 or abs(pin['length'] - reference['length']) > 1e-6:
                return False
    return True


def _segments_from_end(line):
    """Segments walked from the free end: (start_distance, end_distance, a, b)."""
    reverse = line[::-1]
    rows, walked = [], 0.0
    for a, b in zip(reverse, reverse[1:]):
        span = math.hypot(b[0] - a[0], b[1] - a[1])
        if span > _EPS:
            rows.append((walked, walked + span, a, b))
        walked += span
    return rows


def _snap(value, grid):
    return round(round(value / grid) * grid, 9)


def place(leg, pin, grid=0.001):
    """Physical placement of one pin on one leg.

    Returns ``(port, problem)``; ``problem`` is an error dict or None.  The pin
    starts ``ratio * leg length`` from the free end, is pushed back inside the
    leg, and is kept within one straight segment.
    """
    length = pin['length']
    total = leg['length']
    if length > total + _EPS:
        return None, _error('legs', 'leg_pin_too_long', 'Pin %s (%.4g um) is longer than leg %s (%.4g um).'
                            % (pin['name'], length, leg['id'], total))
    # Only the leg end clamps the pin (a pin at the end stays at the end); a pin
    # is never moved silently, so its stored ratio is its real position.
    start = min(max(0.0, pin['ratio'] * total), total - length)
    segments = _segments_from_end(leg['centerline'])
    chosen = next((row for row in segments if row[0] - _EPS <= start < row[1] - _EPS), segments[-1])
    s0, s1, a, b = chosen
    if start + length > s1 + 1e-6:
        return None, _error('legs', 'leg_pin_segment', 'Pin %s (%.4g-%.4g um from the free end) crosses a bend of leg %s; move or shorten it.'
                            % (pin['name'], start, start + length, leg['id']))
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = math.hypot(dx, dy)
    ux, uy = dx / span, dy / span
    if abs(ux) > _EPS and abs(uy) > _EPS:
        return None, _error('legs', 'leg_pin_diagonal', 'Pin %s lies on a diagonal section of leg %s; move it to a straight vertical or horizontal section.'
                            % (pin['name'], leg['id']))
    p0 = [a[0] + ux * (start - s0), a[1] + uy * (start - s0)]
    p1 = [p0[0] + ux * length, p0[1] + uy * length]
    half = leg['width'] / 2
    if abs(ux) > _EPS:
        box = [[min(p0[0], p1[0]), p0[1] - half], [max(p0[0], p1[0]), p0[1] + half]]
    else:
        box = [[p0[0] - half, min(p0[1], p1[1])], [p0[0] + half, max(p0[1], p1[1])]]
    box = [[_snap(box[0][0], grid), _snap(box[0][1], grid)], [_snap(box[1][0], grid), _snap(box[1][1], grid)]]
    if box[1][0] - box[0][0] <= _EPS or box[1][1] - box[0][1] <= _EPS:
        return None, _error('legs', 'leg_pin_grid', 'Pin %s collapses on the drawing grid.' % pin['name'])
    # Access faces the free end of the leg (the direction walked away from).
    direction = ('left' if ux > 0 else 'right') if abs(ux) > _EPS else ('bottom' if uy > 0 else 'top')
    default = leg['default_port']
    own_name = pin['name'] == leg['id']
    # A pin keeping its leg's default name keeps the default winding role, so
    # the built-in L/Q/k pairs still apply; any other pin is role-free.
    port = dict(name=pin['name'], layer=leg['layer'], bbox=box, net=leg['net'], part=leg['part'],
                kind='tap' if leg['kind'] == 'tap' else 'signal',
                winding=default.get('winding') if own_name else None,
                endpoint=default.get('endpoint') if own_name else None,
                access_direction=direction, leg=leg['id'], leg_ratio=pin['ratio'],
                leg_distance=round(start, 9), custom_leg_pin=True)
    return port, None


def apply_layout(result, layout, grid=0.001):
    """Replace default leg pins by the layout's pins; returns ``result``.

    Errors (unknown leg, overlapping pins, pins that do not fit, a name reused
    across windings/TAP or taken from another leg's default) block the layout.
    A name repeated on legs of the same winding only warns: EMX and OA treat
    same-named pins as one terminal with several pin figures.
    """
    if not isinstance(result, dict) or not isinstance(result.get('geometry'), dict):
        return result
    try:
        layout = normalize_layout(layout)
    except ValueError as exc:
        result.setdefault('errors', []).append(_error('legs', 'leg_pin_layout', str(exc)))
        result['ok'] = False
        return result
    geometry = result['geometry']
    if not layout or not geometry.get('ports') or is_default(layout, geometry, grid):
        return result
    found = {leg['id']: leg for leg in legs(geometry, grid)}
    errors, warnings = [], []
    for leg in layout:
        if leg not in found:
            errors.append(_error('legs', 'leg_pin_unknown_leg', 'Leg %s does not exist in this layout (available: %s).'
                                 % (leg, ', '.join(found) or 'none')))
    ports, placed = [], []
    replaced = set()
    for leg_id, leg in found.items():
        if leg_id not in layout:
            continue
        replaced.add(leg_id)
        for pin in layout[leg_id]:
            port, problem = place(leg, pin, grid)
            if problem:
                errors.append(problem)
                continue
            placed.append(port)
    for problem in overlaps(placed):
        errors.append(problem)
    names = [port['name'] for port in placed]
    reserved = {port['name'] for port in geometry['ports'] if not (_is_leg_port(port) and port['name'] in replaced)}
    for name in sorted(set(names)):
        count = names.count(name)
        if name in reserved:
            errors.append(_error('legs', 'leg_pin_reserved_name', 'Pin name %s is already used by another pin of this layout.' % name))
        elif count > 1:
            warnings.append(_error('legs', 'leg_pin_duplicate_name',
                                   'Pin name %s is used %d times; EMX treats same-named pins as one shorted terminal.' % (name, count)))
    for port in placed:
        if port['name'] in found and port['leg'] != port['name']:
            errors.append(_error('legs', 'leg_pin_foreign_default', 'Pin name %s is the default name of leg %s; use it only on that leg.'
                                 % (port['name'], port['name'])))
    nets = {}
    for port in placed:
        nets.setdefault(port['name'], set()).add(port['net'])
    for name, values in sorted(nets.items()):
        if len(values) > 1:
            errors.append(_error('legs', 'leg_pin_net_conflict', 'Pin name %s is used on legs of different windings (%s); one terminal cannot join separate nets.'
                                 % (name, ', '.join(sorted(map(str, values))))))
    kinds = {}
    for port in placed:
        kinds.setdefault(port['name'], set()).add(port['kind'])
    for name, values in kinds.items():
        if len(values) > 1:
            errors.append(_error('legs', 'leg_pin_kind_conflict', 'Pin name %s is used on both a signal leg and a TAP leg.' % name))
    if errors:
        result.setdefault('errors', []).extend(errors)
        result['ok'] = False
        return result
    for port in geometry['ports']:
        if _is_leg_port(port) and port['name'] in replaced:
            continue
        ports.append(port)
    # Keep leg order stable: custom leg pins first, in leg order, then the rest.
    geometry['ports'] = placed + ports
    geometry.setdefault('metrics', {})['leg_pins'] = dict(
        custom=True, legs={leg_id: round(leg['length'], 9) for leg_id, leg in found.items()},
        pins=[dict(name=p['name'], leg=p['leg'], distance=p['leg_distance']) for p in placed])
    result.setdefault('warnings', []).extend(warning for warning in warnings if warning not in result['warnings'])
    return result


def overlaps(ports):
    """Errors for pins whose snapped rectangles overlap on the same layer."""
    problems = []
    for i, a in enumerate(ports):
        for b in ports[:i]:
            if a['layer'] != b['layer']:
                continue
            (ax0, ay0), (ax1, ay1) = a['bbox']
            (bx0, by0), (bx1, by1) = b['bbox']
            if min(ax1, bx1) - max(ax0, bx0) > 1e-9 and min(ay1, by1) - max(ay0, by0) > 1e-9:
                where = a['leg'] if a.get('leg') == b.get('leg') else '%s/%s' % (b.get('leg'), a.get('leg'))
                problems.append(_error('legs', 'leg_pin_overlap', 'Pins %s and %s overlap on leg %s.' % (b['name'], a['name'], where)))
    return problems


def requires_pin_mapping(layout):
    """True when pins differ in name or count from one default-named pin per leg.

    Then the built-in winding pairs no longer describe the terminals, and every
    EMX objective/constraint must name its pins explicitly.
    """
    for leg, pins in (layout or {}).items():
        if len(pins) != 1 or pins[0].get('name') != leg:
            return True
    return False


def signal_pin_names(geometry):
    """Distinct non-ground pin names, in order."""
    names = []
    for port in geometry.get('ports', []):
        if port.get('net') != 'GND' and port.get('kind', 'signal') != 'ground' and port['name'] not in names:
            names.append(port['name'])
    return names


def layout_pin_names(layout, default_names=()):
    """Pin names implied by a stored layout plus legs it does not mention."""
    names = []
    layout = layout or {}
    for leg in default_names:
        pins = layout.get(leg)
        for name in ([leg] if pins is None else [pin['name'] for pin in pins]):
            if name not in names:
                names.append(name)
    for leg, pins in layout.items():
        if leg in default_names:
            continue
        for pin in pins:
            if pin['name'] not in names:
                names.append(pin['name'])
    return names
