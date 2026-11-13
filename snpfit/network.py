"""Touchstone S data, explicit port mapping and impedance conversions.

Only positive real reference impedances are supported. Unsupported mixed-mode
or compressed matrices are rejected rather than silently reinterpreted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import re
import numpy as np


@dataclass
class Network:
    freq: np.ndarray
    s: np.ndarray
    z0: np.ndarray
    port_names: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.freq = np.asarray(self.freq, dtype=float)
        self.s = np.asarray(self.s, dtype=complex)
        if self.s.ndim != 3 or self.s.shape[1] != self.s.shape[2]:
            raise ValueError('S must have shape (frequency, port, port)')
        self.z0 = np.broadcast_to(np.asarray(self.z0, dtype=float),
                                  (self.s.shape[1],)).copy()
        if self.s.shape[0] != len(self.freq) or len(self.freq) < 2:
            raise ValueError('At least two complete frequency samples are required')
        if not np.all(np.isfinite(self.freq)) or np.any(self.freq < 0):
            raise ValueError('Frequencies must be finite and nonnegative')
        if np.any(np.diff(self.freq) <= 0):
            raise ValueError('Frequencies must be strictly increasing')
        if not np.all(np.isfinite(self.s)):
            raise ValueError('Nonfinite S data')
        if not np.all(np.isfinite(self.z0)) or np.any(self.z0 <= 0):
            raise ValueError('Only finite positive real reference impedances are supported')
        if not self.port_names:
            self.port_names = [f'P{i+1}' for i in range(self.nports)]
        if len(self.port_names) != self.nports:
            raise ValueError('Port-name count does not match data')

    @property
    def nports(self):
        return self.s.shape[1]

    @property
    def frequencies(self):
        return self.freq

    def band(self, low: float, high: float):
        if not (np.isfinite(low) and np.isfinite(high) and 0 <= low < high):
            raise ValueError('Use a nonnegative, increasing frequency band')
        # No nearest-point substitution or extrapolation.
        if (low != 0 and low < self.freq[0] * (1-1e-12)) or high > self.freq[-1] * (1+1e-12):
            raise ValueError('Requested band extends outside the SNP data')
        mask = (self.freq > 0) & (self.freq >= low * (1-1e-12)) & (self.freq <= high * (1+1e-12))
        if np.count_nonzero(mask) < 5:
            raise ValueError('The band must contain at least five original SNP samples')
        return Network(self.freq[mask], self.s[mask], self.z0,
                       list(self.port_names), {**self.metadata, 'requested_band_hz': [low, high],
                       'dc_excluded_from_lq': bool(np.any(self.freq == 0)),
                       'actual_positive_sample_band_hz': [float(self.freq[mask][0]),float(self.freq[mask][-1])]})

    def mapped(self, order: list[int], *, short_unused: bool = False):
        """Reorder ports; explicitly short all omitted ports to the common REF."""
        if len(set(order)) != len(order) or any(p < 0 or p >= self.nports for p in order):
            raise ValueError('Port mapping must contain distinct valid port numbers')
        omitted = [p for p in range(self.nports) if p not in order]
        if omitted and not short_unused:
            raise ValueError('Unused ports require explicit confirmation of short-to-REF termination')
        if omitted:
            y = s_to_y(self.s, self.z0)[:, order][:, :, order]
            s = y_to_s(y, self.z0[order])
        else:
            s = self.s[:, order][:, :, order]
        return Network(self.freq.copy(), s, self.z0[order],
                       [self.port_names[p] for p in order],
                       {**self.metadata, 'port_order_1based': [p+1 for p in order],
                        'shorted_ports_1based': [p+1 for p in omitted]})


def s_to_z(s, z0):
    s = np.asarray(s, complex)
    eye = np.eye(s.shape[-1])
    d = np.sqrt(np.asarray(z0, float))
    return d[:, None] * np.linalg.solve(eye-s, eye+s) * d[None, :]


def s_to_y(s, z0):
    s = np.asarray(s, complex)
    eye = np.eye(s.shape[-1])
    d = 1 / np.sqrt(np.asarray(z0, float))
    return d[:, None] * np.linalg.solve(eye+s, eye-s) * d[None, :]


def z_to_s(z, z0):
    z = np.asarray(z, complex)
    d = 1 / np.sqrt(np.asarray(z0, float))
    a = d[:, None] * z * d[None, :]
    eye = np.eye(z.shape[-1])
    return np.linalg.solve(a+eye, a-eye)


def y_to_s(y, z0):
    y = np.asarray(y, complex)
    d = np.sqrt(np.asarray(z0, float))
    a = d[:, None] * y * d[None, :]
    eye = np.eye(y.shape[-1])
    return np.linalg.solve(eye+a, eye-a)


def read_touchstone(path: str | Path, *, network_class=Network) -> Network:
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    lines = raw.decode('utf-8-sig').splitlines()
    match = re.search(r'\.s(\d+)p$', path.name, re.I)
    nports = int(match.group(1)) if match else None
    units, parameter, form, scalar_z0 = 'ghz', 's', 'ma', 50.0
    reference = None
    declared_nfreq = None
    version = '1.0'
    order2 = '21_12'
    section = 'network'
    tokens = []
    names = {}
    option_seen = False
    for rawline in lines:
        data, _, comment = rawline.partition('!')
        label = re.search(r'Port\s*\[\s*(\d+)\s*\]\s*=\s*(.+)', comment, re.I)
        if label:
            names[int(label.group(1))-1] = label.group(2).strip()
        line = data.strip()
        if not line:
            continue
        if line.startswith('#'):
            if option_seen:
                raise ValueError('Multiple option lines are not supported')
            option_seen = True
            opts = line[1:].lower().split()
            for opt in opts:
                if opt in ('hz', 'khz', 'mhz', 'ghz'):
                    units = opt
                elif opt in ('s', 'y', 'z', 'h', 'g'):
                    parameter = opt
                elif opt in ('ri', 'ma', 'db'):
                    form = opt
            if 'r' in opts:
                rvals = opts[opts.index('r')+1:]
                if not rvals:
                    raise ValueError('Missing reference impedance')
                vals = [_float(v) for v in rvals]
                scalar_z0 = vals[0]
                if len(vals) > 1:
                    reference = vals
            continue
        if line.startswith('['):
            key, sep, value = line[1:].partition(']')
            if not sep:
                raise ValueError('Malformed Touchstone keyword')
            key, value = key.strip().lower(), value.strip()
            if key == 'version':
                version = value
            elif key == 'number of ports':
                declared = int(value)
                if nports is not None and nports != declared:
                    raise ValueError('Filename and declared port counts disagree')
                nports = declared
            elif key == 'number of frequencies':
                declared_nfreq = int(value)
            elif key == 'two-port data order':
                order2 = value.lower()
            elif key == 'reference':
                reference = [_float(v) for v in value.split()]
                section = 'reference'
                continue
            elif key == 'matrix format':
                if value.lower() != 'full':
                    raise ValueError('Only full Touchstone matrices are supported')
            elif key == 'mixed-mode order':
                raise ValueError('Use single-ended EMX SNP; mixed-mode input is not supported')
            elif key == 'network data':
                section = 'network'
            elif key in ('noise data', 'begin information'):
                section = 'skip'
            elif key in ('end information',):
                section = 'network'
            elif key == 'end':
                section = 'end'
            elif key not in ('number of noise frequencies',):
                raise ValueError(f'Unsupported Touchstone keyword: {key}')
            continue
        if section == 'reference':
            reference.extend(_float(v) for v in line.split())
        elif section == 'network':
            tokens.extend(_float(v) for v in line.split())
    if parameter != 's':
        raise ValueError('Select an S-parameter SNP file, not Y/Z/H/G data')
    if nports is None or not 1 <= nports <= 16:
        raise ValueError('Use .sNp or a declared port count between 1 and 16')
    if order2 not in ('21_12', '12_21'):
        raise ValueError('Unsupported two-port matrix ordering')
    stride = 1 + 2 * nports*nports
    if not tokens or len(tokens) % stride:
        raise ValueError('Incomplete network record or unsupported noise block')
    a = np.asarray(tokens, dtype=float).reshape(-1, stride)
    if declared_nfreq is not None and len(a) != declared_nfreq:
        raise ValueError('Declared frequency count does not match complete data')
    pairs = a[:, 1:].reshape(len(a), nports*nports, 2)
    if form == 'ri':
        values = pairs[..., 0] + 1j*pairs[..., 1]
    else:
        mag = pairs[..., 0] if form == 'ma' else 10**(pairs[..., 0]/20)
        values = mag * np.exp(1j*np.deg2rad(pairs[..., 1]))
    s = values.reshape(-1, nports, nports)
    if nports == 2 and order2 == '21_12':
        s = s.transpose(0, 2, 1)
    z0 = np.full(nports, scalar_z0) if reference is None else np.asarray(reference)
    if len(z0) != nports:
        raise ValueError('Reference impedance count must equal port count')
    return network_class(a[:, 0] * {'hz':1., 'khz':1e3, 'mhz':1e6, 'ghz':1e9}[units],
                   s, z0, [names.get(i, f'P{i+1}') for i in range(nports)],
                   {'source': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'touchstone_version': version, 'format': form,
                    'reference_ohm': z0.tolist()})


def write_touchstone(network: Network, path: str | Path):
    path = Path(path)
    if not np.all(network.z0 == network.z0[0]):
        raise ValueError('Writer currently requires common real reference impedance')
    lines = [f'# Hz S RI R {network.z0[0]:.16g}']
    lines += [f'! Port[{i+1}] = {n}' for i,n in enumerate(network.port_names)]
    for freq, matrix in zip(network.freq, network.s):
        values = matrix.T.flat if network.nports == 2 else matrix.flat
        line = [f'{freq:.16e}']
        for v in values:
            line.extend((f'{v.real:.16e}', f'{v.imag:.16e}'))
        lines.append(' '.join(line))
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')


def _float(token):
    return float(token.replace('D', 'e').replace('d', 'e'))
