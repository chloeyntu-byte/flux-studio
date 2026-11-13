"""Read-only local winding-segment annotations, independent of the RF fitter.

The built-in winding ladder identifies a main magnetic section plus its series
resistance, skin branch and optional LC loss cells. This local impedance is not
the loaded winding impedance. Mutual energy and terminal/substrate shunts remain
in the fitted circuit but are excluded from this annotation.
"""
from __future__ import annotations

import copy
import math
import re
from collections import Counter

from .models import get_model


SCOPE = 'Local equivalent Q; mutual coupling and shunt loading excluded.'


def _ends(element):
    if not element or 'n1' not in element or 'n2' not in element:
        return None
    return (element['n1'], element['n2'])


def _other(element, node):
    pair = _ends(element)
    if pair is None or pair[0] == pair[1] or node not in pair:
        return None
    return pair[1] if node == pair[0] else pair[0]


def _model_branches(circuit, model_id):
    identity = model_id or circuit.get('metadata', {}).get('model_id')
    try:
        spec = get_model(identity)
    except (KeyError, TypeError, ValueError):
        return []
    if set(spec.port_roles) != set(circuit.get('ports', [])):
        return []
    reference = circuit.get('reference', 'REF')
    result = []
    for winding, pair in enumerate(spec.winding_pairs):
        tap = 'TAP' if spec.device == 'inductor' else ('PTAP' if winding == 0 else 'STAP')
        sequence = [pair[0], tap, pair[1]] if tap in spec.tap_roles else list(pair)
        sequence = [reference if node == 'REF' else node for node in sequence]
        label = 'winding' if len(spec.winding_pairs) == 1 else ('primary' if winding == 0 else 'secondary')
        for a, b in zip(sequence[:-1], sequence[1:]):
            result.append({'winding': label, 'start_net': a, 'end_net': b})
    return result


def _loss_blocks(elements):
    """Recognize only the exact built-in R || (L + C) connectivity."""
    blocks = []
    for name, resistor in elements.items():
        match = re.fullmatch(r'rfit_loss_(\d+)', name)
        if not match or resistor.get('kind') != 'res':
            continue
        number = match.group(1)
        lind = elements.get('lfit_loss_' + number)
        cap = elements.get('cfit_loss_' + number)
        if not lind or lind.get('kind') != 'ind' or not cap or cap.get('kind') != 'cap':
            continue
        rp, lp, cp = _ends(resistor), _ends(lind), _ends(cap)
        if not rp or not lp or not cp or len(set(rp)) != 2:
            continue
        shared = set(lp) & set(cp)
        if len(shared) != 1:
            continue
        middle = next(iter(shared))
        if middle in rp or {_other(lind, middle), _other(cap, middle)} != set(rp):
            continue
        blocks.append({'id': int(number), 'resistor': name, 'inductor': lind['name'],
                       'capacitor': cap['name'], 'start_net': rp[0], 'end_net': rp[1],
                       'junction_net': middle})
    return sorted(blocks, key=lambda block: block['id'])


def _loss_path(first, last, blocks):
    if first == last:
        return []
    paths = []
    visits = 0
    bounded = True
    def visit(node, path, used):
        nonlocal visits, bounded
        visits += 1
        if visits > 1024:
            bounded = False
            return
        if node == last:
            paths.append(path)
            return
        if len(path) >= len(blocks) or len(paths) > 1:
            return
        for block in blocks:
            if block['id'] in used:
                continue
            a, b = block['start_net'], block['end_net']
            if node in (a, b):
                following = b if node == a else a
                visit(following, path + [block], used | {block['id']})
    visit(first, [], set())
    return paths[0] if bounded and len(paths) == 1 else None


def _unassigned(element, reason):
    return {'id': element['name'], 'winding': 'unassigned', 'branch_index': None,
            'ordinal': None, 'winding_ordinal': None, 'start_net': element.get('n1'),
            'end_net': element.get('n2'), 'branch_start_net': None, 'branch_end_net': None,
            'main_l': element['name'], 'resistor': None, 'skin_l': None, 'skin_r': None,
            'loss_cells': [], 'element_names': [element['name']], 'auxiliary_elements': [],
            'supported': False, 'is_main': False, 'status': 'unassigned', 'reason': reason,
            'scope': SCOPE, 'start_fraction': None, 'end_fraction': None, 'center_fraction': None}


def describe_segments(circuit, model_id=None):
    """Describe exact built-in segment groups without altering the circuit.

    ``branch_index`` is zero based in model role order. ``ordinal`` is one based
    inside that branch; ``winding_ordinal`` runs across a tap as a whole winding.
    Fractions are uniform display positions within the branch, not fitted metal
    lengths. Every inductor appears exactly once in ``element_names`` across the
    returned rows; an unsupported inductor receives an unassigned row.
    """
    listed = list(circuit.get('elements', []))
    elements = {element.get('name'): element for element in listed}
    if len(elements) != len(listed):
        return [_unassigned(e, 'Duplicate element identities prevent segment assignment.')
                for e in listed if e.get('kind') == 'ind']
    branches = _model_branches(circuit, model_id)
    if not branches:
        return [_unassigned(e, 'No compatible dedicated winding model was supplied.')
                for e in listed if e.get('kind') == 'ind']
    blocks = _loss_blocks(elements)
    rows = []
    for element in listed:
        match = re.fullmatch(r'lw(\d+)_(\d+)', element.get('name', ''))
        if element.get('kind') != 'ind' or not match:
            continue
        branch_index, section = map(int, match.groups())
        if branch_index >= len(branches) or not _ends(element):
            continue
        suffix = 'w%d_%d' % (branch_index, section)
        resistor = elements.get('r' + suffix)
        if resistor and resistor.get('kind') != 'res':
            continue
        if resistor:
            paths = []
            for join in _ends(resistor) or ():
                path = _loss_path(join, element['n1'], blocks)
                if path is not None:
                    paths.append((_other(resistor, join), path))
            if len(paths) != 1 or paths[0][0] is None:
                continue
            start, loss = paths[0]
        else:
            # A built-in named ideal series L can be annotated as lossless;
            # the complete model branch continuity is still checked below.
            start, loss = element['n1'], []
        skin_l, skin_r = elements.get('lskin' + suffix), elements.get('rskin' + suffix)
        end = element['n2']
        if bool(skin_l) != bool(skin_r):
            continue
        if skin_l:
            if (skin_l.get('kind') != 'ind' or skin_r.get('kind') != 'res'
                    or set(_ends(skin_l) or ()) != set(_ends(skin_r) or ())):
                continue
            end = _other(skin_l, element['n2'])
            if end is None:
                continue
        names = [element['name']]
        if resistor:
            names.append(resistor['name'])
        if skin_l:
            names.extend((skin_l['name'], skin_r['name']))
        for block in loss:
            names.extend(block[key] for key in ('resistor', 'inductor', 'capacitor'))
        branch = branches[branch_index]
        rows.append({'id': element['name'], 'winding': branch['winding'],
                     'branch_index': branch_index, 'ordinal': section + 1,
                     'winding_ordinal': None, 'start_net': start, 'end_net': end,
                     'branch_start_net': branch['start_net'], 'branch_end_net': branch['end_net'],
                     'main_l': element['name'], 'resistor': resistor['name'] if resistor else None,
                     'skin_l': skin_l['name'] if skin_l else None,
                     'skin_r': skin_r['name'] if skin_r else None,
                     'loss_cells': copy.deepcopy(loss), 'element_names': names,
                     'auxiliary_elements': [n for n in names if n not in (element['name'], resistor['name'] if resistor else None)],
                     'supported': True, 'is_main': True, 'status': 'ready', 'reason': '',
                     'scope': SCOPE})
    ownership = Counter(name for row in rows for name in row['element_names'])
    conflicts = {name for name, count in ownership.items() if count > 1}
    rows = [row for row in rows if not conflicts.intersection(row['element_names'])]
    rows.sort(key=lambda row: (row['branch_index'], row['ordinal']))
    for index, branch in enumerate(branches):
        chain = [row for row in rows if row['branch_index'] == index]
        complete = (bool(chain) and chain[0]['start_net'] == branch['start_net']
                    and chain[-1]['end_net'] == branch['end_net']
                    and all(a['end_net'] == b['start_net'] for a, b in zip(chain[:-1], chain[1:])))
        for position, row in enumerate(chain):
            row.update(start_fraction=position / len(chain), end_fraction=(position + 1) / len(chain),
                       center_fraction=(position + .5) / len(chain))
            if not complete:
                row.update(supported=False, status='unsupported_structure',
                           reason='Main segments do not form the complete model branch.')
    winding_counts = {}
    for row in rows:
        winding_counts[row['winding']] = winding_counts.get(row['winding'], 0) + 1
        row['winding_ordinal'] = winding_counts[row['winding']]
    assigned = {name for row in rows for name in row['element_names']}
    for element in listed:
        if element.get('kind') == 'ind' and element['name'] not in assigned:
            rows.append(_unassigned(element, 'No verified main-segment loss group matches this inductor.'))
    return rows


def _value(elements, name, kind, allow_zero=False):
    element = elements[name]
    value = float(element['value'])
    if element.get('kind') != kind or not math.isfinite(value) or (value < 0 if allow_zero else value <= 0):
        raise ValueError('Invalid local segment element value.')
    return value


def _parallel_r_x(resistance, reactance):
    """R || jX without squaring large impedances or dividing by a zero X."""
    if resistance == 0 or reactance == 0:
        return 0j
    if math.isnan(reactance):
        raise ValueError('Undefined local reactance.')
    if math.isinf(reactance):
        return complex(resistance)
    if abs(reactance) <= resistance:
        ratio = reactance / resistance
        denominator = 1 + ratio * ratio
        return complex(resistance * ratio * ratio / denominator, resistance * ratio / denominator)
    ratio = resistance / reactance
    denominator = 1 + ratio * ratio
    return complex(resistance / denominator, resistance * ratio / denominator)


def evaluate_segments(circuit, frequency_hz, model_id=None):
    """Evaluate local segment Q at one frequency; all returned values are JSON safe.

    Lossless Q is represented by ``q=None, status='lossless'`` (display infinity),
    not by a nonfinite JSON number. Negative/zero reactance retains signed Q and
    ``status='non_inductive'``. Unsupported groups retain their actual identities
    with no invented Q. Full loaded/mutually coupled winding Q is unchanged.
    """
    frequency_hz = float(frequency_hz)
    omega = 2 * math.pi * frequency_hz
    if frequency_hz <= 0 or not math.isfinite(frequency_hz) or not math.isfinite(omega):
        raise ValueError('Segment Q frequency must be positive and finite.')
    elements = {element.get('name'): element for element in circuit.get('elements', [])}
    rows = describe_segments(circuit, model_id)
    for row in rows:
        row.update(frequency_hz=frequency_hz, q=None, z_real=None, z_imag=None, l_effective_h=None)
        if not row['supported']:
            continue
        try:
            resistance = _value(elements, row['resistor'], 'res', True) if row['resistor'] else 0.
            inductance = _value(elements, row['main_l'], 'ind')
            z = complex(resistance, omega * inductance)
            if row['skin_l']:
                skin_x = omega * _value(elements, row['skin_l'], 'ind')
                skin_r = _value(elements, row['skin_r'], 'res', True)
                z += _parallel_r_x(skin_r, skin_x)
            for block in row['loss_cells']:
                cap = _value(elements, block['capacitor'], 'cap')
                lind = _value(elements, block['inductor'], 'ind')
                res = _value(elements, block['resistor'], 'res', True)
                cap_product = omega * cap
                lc_x = omega * lind - (1 / cap_product if cap_product else math.inf)
                z += _parallel_r_x(res, lc_x)
            if not math.isfinite(z.real) or not math.isfinite(z.imag) or z.real < 0:
                raise ValueError('Local segment impedance is not finite and passive.')
            effective_l = z.imag / omega
            if not math.isfinite(effective_l):
                raise ValueError('Local effective inductance exceeds the numeric range.')
            row.update(z_real=float(z.real), z_imag=float(z.imag), l_effective_h=float(effective_l))
            if z.real == 0:
                row.update(status='lossless' if z.imag > 0 else 'non_inductive',
                           reason='No local resistive loss.' if z.imag > 0 else 'The local equivalent is not inductive at this frequency.')
            else:
                q = z.imag / z.real
                if not math.isfinite(q):
                    raise ValueError('Local segment Q exceeds the numeric range.')
                row.update(q=float(q), status='finite' if z.imag > 0 else 'non_inductive',
                           reason='' if z.imag > 0 else 'The local equivalent is not inductive at this frequency.')
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
            row.update(status='undefined', reason=str(error), q=None,
                       z_real=None, z_imag=None, l_effective_h=None)
    return rows


def rank_segments(rows, limit=10):
    """Return lowest signed finite local Q first, then lossless and undefined.

    Ties use branch/section/identity, independent of the input row order. Rows are
    copied and never padded to ten; unsupported status remains visible.
    """
    if isinstance(limit, bool) or int(limit) != limit or limit < 0:
        raise ValueError('Segment rank limit must be a nonnegative integer.')
    def key(row):
        q = row.get('q')
        finite = isinstance(q, (int, float)) and math.isfinite(q)
        category = 0 if finite else (1 if row.get('status') == 'lossless' else 2)
        return (category, float(q) if finite else 0.,
                row.get('branch_index') if row.get('branch_index') is not None else math.inf,
                row.get('ordinal') if row.get('ordinal') is not None else math.inf,
                str(row.get('id', '')))
    return copy.deepcopy(sorted(rows, key=key)[:int(limit)])
