"""Snapshot report of the complete fitted winding response for native drawings.

This reporting path does not fit parameters or evaluate acceptance.  Its port
fixtures are the same SE/DM, open-tap and matched-common-mode conventions used
by ``fitting._measure_s``.  It retains undefined/lossless values as explicit
JSON-safe cells rather than letting the fit-only finite-Q guard hide them.
"""
from __future__ import annotations

import math
import numpy as np

from .circuit import y_from_s
from .fitting import _open_modal_impedance
from .models import get_model
from .stable_network import simulate_stable

DEFAULT_FREQUENCIES_HZ = (5e9, 10e9, 20e9)


def _frequencies(values):
    values = DEFAULT_FREQUENCIES_HZ if values is None else values
    if not isinstance(values, (list, tuple, np.ndarray)) or not 1 <= len(values) <= 6:
        raise ValueError('Native metric table needs one to six positive frequencies.')
    if any(isinstance(v, (bool, np.bool_)) for v in values):
        raise ValueError('Native metric frequencies must be positive finite numbers.')
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or not np.all(np.isfinite(result)) or np.any(result <= 0):
        raise ValueError('Native metric frequencies must be positive finite numbers.')
    if len(np.unique(result)) != len(result):
        raise ValueError('Native metric frequencies must not repeat.')
    return result


def _impedance(s, z0, spec, fixture, ideal=False):
    """The impedance-only part of the established fixture, including open taps.

    Keeping Q formatting out of this function allows a truly zero real part to
    be reported as lossless.  No conductance or impedance floor is introduced.
    """
    roles = list(spec.port_roles)
    if spec.mode != 'dm' or fixture == 'sampled_open':
        y = y_from_s(s, z0)
        if spec.device == 'inductor' and spec.mode == 'se':
            retained = [roles.index('P'), roles.index('N')]
            opened = [i for i in range(len(roles)) if i not in retained]
            reduced = y[:, retained, :][:, :, retained]
            if opened:
                reduced = reduced - (y[:, retained, :][:, :, opened]
                    @ np.linalg.solve(y[:, opened, :][:, :, opened], y[:, opened, :][:, :, retained]))
            result = (1 / reduced[:, 0, 0])[:, None, None]
            return 1j*result.imag if ideal else result
        weights = np.zeros((len(roles), len(spec.winding_pairs)))
        for j, (a, b) in enumerate(spec.winding_pairs):
            if a != 'REF':
                weights[roles.index(a), j] = 1
            if b != 'REF':
                weights[roles.index(b), j] = -1
        result = _open_modal_impedance(y, weights)
        return 1j*result.imag if ideal else result
    retained = [i for i, role in enumerate(roles) if role not in spec.tap_roles]
    opened = [i for i, role in enumerate(roles) if role in spec.tap_roles]
    reduced = s[:, retained, :][:, :, retained]
    if opened:
        reduced = reduced + (s[:, retained, :][:, :, opened]
            @ np.linalg.solve(np.eye(len(opened)) - s[:, opened, :][:, :, opened],
                              s[:, opened, :][:, :, retained]))
    retained_roles = [roles[i] for i in retained]
    count = len(spec.winding_pairs)
    transform = np.zeros((len(retained), len(retained)))
    differential_z0 = []
    for j, (a, b) in enumerate(spec.winding_pairs):
        ai, bi = retained_roles.index(a), retained_roles.index(b)
        if not np.isclose(z0[roles.index(a)], z0[roles.index(b)], rtol=1e-12, atol=0):
            raise ValueError('Mixed-mode paired ports need equal real Z0; explicitly renormalize first.')
        transform[j, ai], transform[j, bi] = 1 / np.sqrt(2), -1 / np.sqrt(2)
        transform[j + count, ai], transform[j + count, bi] = 1 / np.sqrt(2), 1 / np.sqrt(2)
        differential_z0.append(2 * z0[roles.index(a)])
    smm = transform[None] @ reduced @ transform.T[None]
    sdd = smm[:, :count, :count]
    scales = np.sqrt(differential_z0)
    result = np.linalg.solve(np.eye(count) - sdd, np.eye(count) + sdd) * scales[:, None] * scales[None, :]
    if ideal:
        # No resistor exists. Only conversion into the matched common-mode
        # loads could dissipate power. If it vanishes at roundoff resolution,
        # use the known ideal zero loss instead of displaying signed 1e15 Q
        # produced by the S-to-Z round trip. Resistive fitted Q is never clipped.
        no_conversion = np.max(np.abs(smm[:, count:, :count]), axis=(1,2)) <= 32*np.finfo(float).eps
        result[no_conversion] = 1j*result[no_conversion].imag
    return result


def _cell(value=None, status='finite'):
    if status != 'finite':
        return {'value': None, 'text': 'Inf' if status == 'lossless' else '-Inf' if status == 'lossless_negative' else 'N/A',
                'status': status}
    value = float(value)
    if not math.isfinite(value):
        return {'value': None, 'text': 'N/A', 'status': 'undefined'}
    return {'value': value, 'text': format(value, '.4g'), 'status': 'finite'}


def build_report(circuit, model_id=None, frequencies_hz=None, fit_band_hz=None, z0=None, fixture=None):
    """Return JSON-safe metric rows; values in L rows use nH, Q/k are unitless.

    ``z0`` is in mapped model-port order.  Pass the actual selected source Z0
    for differential common-mode loading; omitted Z0 defaults to metadata or
    50 Ohm.  This is fitted-response reporting, not SNP interpolation or a new
    accuracy claim.  User frequency order is retained.
    """
    metadata = circuit.get('metadata', {})
    spec = get_model(model_id or metadata.get('model_id') or circuit.get('model_id'))
    if len(circuit['ports']) != spec.nports:
        raise ValueError('Native metric model must match the circuit port count/order.')
    # Native integration can alias terminal names, while retaining model-role
    # order. The response matrix is in that order; names are not the fixture.
    fixture = ('sampled_open' if metadata.get('acceptance_scope') == 'original_sampled_points_only'
               else 'studio') if fixture in (None, 'auto') else fixture
    if fixture not in ('studio', 'sampled_open'):
        raise ValueError('Native metric fixture must be studio or sampled_open.')
    frequencies = _frequencies(frequencies_hz)
    z0_default = z0 is None and 'z0_ohm' not in metadata
    z0 = np.broadcast_to(np.asarray(metadata.get('z0_ohm', 50.) if z0 is None else z0, dtype=float), (spec.nports,)).copy()
    if not np.all(np.isfinite(z0)) or np.any(z0 <= 0):
        raise ValueError('Native metric Z0 must be positive real and in mapped port order.')
    if fixture == 'studio' and spec.mode == 'dm':
        roles = list(spec.port_roles)
        for a, b in spec.winding_pairs:
            if not np.isclose(z0[roles.index(a)], z0[roles.index(b)], rtol=1e-12, atol=0):
                raise ValueError('Mixed-mode paired ports need equal real Z0; explicitly renormalize first.')
    band = fit_band_hz if fit_band_hz is not None else metadata.get('selected_band_hz', metadata.get('sampled_band_hz'))
    if band is not None:
        band = np.asarray(band, dtype=float)
        if band.shape != (2,) or not np.all(np.isfinite(band)) or not 0 < band[0] < band[1]:
            raise ValueError('Native metric fit band must contain positive increasing endpoints.')
    columns = [{'frequency_hz': float(f), 'label': format(f/1e9, '.6g') + ' GHz',
                'in_fit_band': None if band is None else bool(band[0] <= f <= band[1])} for f in frequencies]
    names = ['winding'] if spec.device == 'inductor' else ['primary', 'secondary']
    rows = []
    error = None
    ideal = not any(e['kind'] == 'res' for e in circuit['elements'])
    def solve(f):
        with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
            return _impedance(simulate_stable(circuit, f, z0), z0, spec, fixture, ideal=ideal)
    try:
        z = solve(frequencies)
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as exc:
        # A pole/unresolved fixture in one column must not erase other columns.
        z = np.full((len(frequencies), len(names), len(names)), np.nan+1j*np.nan)
        failures = []
        for i, f in enumerate(frequencies):
            try:
                z[i] = solve(np.asarray([f]))[0]
            except (ValueError, np.linalg.LinAlgError, FloatingPointError) as item:
                failures.append(format(f/1e9, '.6g')+' GHz: '+str(item))
        error = '; '.join(failures) or str(exc)
    for index, name in enumerate(names):
        suffix = '' if len(names) == 1 else ('p' if index == 0 else 's')
        l_cells, q_cells = [], []
        for k, f in enumerate(frequencies):
            value = None if z is None else z[k, index, index]
            if value is None or not np.isfinite(value):
                l_cells.append(_cell(status='undefined'));q_cells.append(_cell(status='undefined'))
                continue
            l_cells.append(_cell(value.imag / (2*np.pi*f) * 1e9))
            if value.real == 0:
                status = 'lossless' if value.imag > 0 else 'lossless_negative' if value.imag < 0 else 'undefined'
                q_cells.append(_cell(status=status))
            else:
                q_cells.append(_cell(value.imag / value.real))
        rows.extend([{'key': 'l_' + name, 'label': 'L'+suffix+' (nH)', 'unit': 'nH', 'cells': l_cells},
                     {'key': 'q_' + name, 'label': 'Q'+suffix, 'unit': '1', 'cells': q_cells}])
    k_cells = []
    for index in range(len(frequencies)):
        if spec.device != 'transformer':
            k_cells.append(_cell(status='not_applicable'))
        elif z is None or not np.all(np.isfinite(z[index])):
            k_cells.append(_cell(status='undefined'))
        elif z[index, 0, 0].imag <= 0 or z[index, 1, 1].imag <= 0:
            k_cells.append(_cell(status='noninductive'))
        else:
            x1, x2 = z[index, 0, 0].imag, z[index, 1, 1].imag
            k_cells.append(_cell(z[index, 0, 1].imag / (math.sqrt(x1)*math.sqrt(x2))))
    rows.append({'key': 'k', 'label': 'k (signed)', 'unit': '1', 'cells': k_cells})
    common_mode = ('open' if fixture == 'sampled_open' else 'matched') if spec.mode == 'dm' else 'specified_single_ended_return'
    notes = ['Fitted response; open taps and other winding; '+
             (common_mode+' common mode.' if spec.mode == 'dm' else 'selected single-ended return.'),
             'k is signed effective response coupling; N/A when not inductive or not applicable.']
    if any(c['in_fit_band'] is False for c in columns):
        notes.append('* Outside fitted band: model prediction only.')
    if error:
        notes.append('Metric fixture unresolved: ' + error)
    return {'schema': 1, 'model_id': spec.id, 'frequencies_hz': frequencies.tolist(), 'columns': columns,
            'rows': rows, 'fit_band_hz': None if band is None else band.tolist(), 'z0_ohm': z0.tolist(),
            'z0_defaulted_50_ohm': z0_default, 'notes': notes,
            'fixture': {'id': fixture, 'mode': spec.mode, 'tap_termination': 'open', 'other_winding_termination': 'open',
                        'common_mode': common_mode},
            'status': ('unresolved' if not np.any(np.isfinite(z)) else 'partial') if error else 'evaluated', 'error': error,
            'basis': 'complete fitted circuit at requested positive frequencies; no source interpolation'}
