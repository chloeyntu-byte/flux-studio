"""Native-only overlapping single-turn transformer illustrations.

These paths describe the winding silhouette, not a recovered layout.  The
coplanar profile follows the implicit transformer's alternating outer/inner
half turns and radial crossover.  It deliberately allows projected crossings;
electrical wires receive independent crossing protection during native export.
"""
from __future__ import annotations

import copy


def transformer_style(raw):
    """Resolve an explicit native selection, then known layout metadata."""
    selected = raw.get('native_transformer_style')
    if selected is not None:
        if selected not in ('coplanar', 'stacked'):
            raise ValueError('Native transformer style must be coplanar or stacked.')
        return selected
    contexts = [raw, raw.get('provenance', {}), raw.get('layout_metadata', {}),
                raw.get('native_report_context', {})]
    for context in contexts:
        if not isinstance(context, dict):
            continue
        for key in ('geometry_kind', 'transformer_style', 'winding_style', 'layout_style'):
            value = str(context.get(key, '')).lower()
            if value in ('stacked', 'stacked_transformer', 'different_layer', 'different_layers'):
                return 'stacked'
    return 'coplanar'


def overlapping_paths(paths, style, grid=.0625):
    """Return common-center TF turns while retaining path identities/taps.

    Primary terminals face up and secondary terminals face down.  Coplanar P
    uses the left outer/right inner halves; S uses left inner/right outer.
    Their radial transitions lie on the opposite side from their terminal gap.
    Stacked turns retain the original large/small, common-center octagons.
    """
    result = copy.deepcopy(paths)
    primary = next((p for p in result if p.get('role') == 'winding'
                    and p.get('winding') == 'primary'), None)
    secondary = next((p for p in result if p.get('role') == 'winding'
                      and p.get('winding') == 'secondary'), None)
    if primary is None or secondary is None:
        return result
    if style not in ('coplanar', 'stacked'):
        raise ValueError('Native transformer style must be coplanar or stacked.')
    if style == 'stacked':
        return result

    def snap(value):
        return round(float(value) / grid) * grid

    p, s = primary['points'], secondary['points']
    outer_x = max(abs(v[0]) for v in p)
    outer_y = -min(v[1] for v in p)
    inner_x = max(abs(v[0]) for v in s)
    inner_y = max(v[1] for v in s)
    # A two-grid radial separation keeps both ends of the centered 45-degree
    # radial bridge on the native center-path grid at every finite scale.
    radial = max(2 * grid, round((outer_y - inner_y) / (2 * grid)) * 2 * grid)
    inner_y = outer_y - radial
    outer_chamfer = outer_x - abs(p[2][0])
    inner_chamfer = inner_x - abs(s[2][0])
    ax, ay = outer_x - outer_chamfer, outer_y - outer_chamfer
    bx, by = inner_x - inner_chamfer, inner_y - inner_chamfer
    half_bridge = radial / 2
    # Keep a generous empty terminal gap.  The legs do not migrate to the
    # outside of the drawing to suppress the allowed projected crossings.
    gap = max(abs(p[0][0]), abs(s[0][0]))
    leg_y = max(abs(v[1]) for v in (p[0], p[-1], s[0], s[-1]))
    primary['points'] = [[-gap, leg_y], [-gap, outer_y], [-ax, outer_y],
                         [-outer_x, ay], [-outer_x, -ay], [-ax, -outer_y],
                         [-half_bridge, -outer_y], [half_bridge, -inner_y],
                         [bx, -inner_y], [inner_x, -by], [inner_x, by],
                         [bx, inner_y], [gap, inner_y], [gap, leg_y]]
    secondary['points'] = [[-gap, -leg_y], [-gap, -inner_y], [-bx, -inner_y],
                           [-inner_x, -by], [-inner_x, by], [-bx, inner_y],
                           [-half_bridge, inner_y], [half_bridge, outer_y],
                           [ax, outer_y], [outer_x, ay], [outer_x, -ay],
                           [ax, -outer_y], [gap, -outer_y], [gap, -leg_y]]
    for path in (primary, secondary):
        path['points'] = [[snap(value) for value in point] for point in path['points']]
    return result


def crossing_keepouts(paths, winding, segment_intersection, clearance=.75):
    """Arclength intervals where a winding crosses another path.

    Native R/L bodies and their short real terminal stubs must not straddle a
    projected crossing.  Drawing outlines remain continuous through it.
    """
    from math import hypot
    target = next(p for p in paths if p.get('role') == 'winding'
                  and p.get('winding') == winding)
    intervals = []
    offset = 0.
    for a, b in zip(target['points'], target['points'][1:]):
        for other in paths:
            if other is target or other.get('winding') == winding:
                continue
            for c, d in zip(other['points'], other['points'][1:]):
                overlap = segment_intersection(a, b, c, d)
                if overlap is None:
                    continue
                start, stop = sorted(offset + hypot(p[0] - a[0], p[1] - a[1])
                                     for p in overlap)
                intervals.append((start - clearance, stop + clearance))
        offset += hypot(b[0] - a[0], b[1] - a[1])
    return intervals
