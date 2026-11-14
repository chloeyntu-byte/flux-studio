"""Helpers that load the example structure pack for one test, without leaking it.

Tests never depend on what is installed in the repository ``packs/`` folder:
discovery is pointed at the example pack only, and the pack is unloaded again
when the test ends.
"""
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import backend
import structure_packs

EXAMPLE_PACK = ROOT / 'examples' / 'packs' / 'example_tapped_inductor.py'
EXAMPLE_MODEL = 'example_tapped_inductor'


def isolate_packs(test, flux_packs=None, disable=False):
    """Make structure-pack discovery deterministic for ``test`` and undo it afterwards."""
    empty = tempfile.TemporaryDirectory(prefix='flux-empty-packs-')
    test.addCleanup(empty.cleanup)
    environment = {'FLUX_PACKS': flux_packs if flux_packs is not None else ''}
    if disable:
        environment['FLUX_PACKS_DISABLE'] = '1'
    else:
        environment.pop('FLUX_PACKS_DISABLE', None)
    environ = mock.patch.dict(os.environ, environment)
    environ.start()
    test.addCleanup(environ.stop)
    if not disable:
        os.environ.pop('FLUX_PACKS_DISABLE', None)
    directory = mock.patch.object(structure_packs, 'PACK_DIR', Path(empty.name))
    directory.start()
    test.addCleanup(directory.stop)
    structure_packs.reset()
    test.addCleanup(structure_packs.reset)
    return empty.name


def install_example_pack(test):
    """Load examples/packs/example_tapped_inductor.py for the duration of ``test``."""
    isolate_packs(test, str(EXAMPLE_PACK))
    models = [item['id'] for item in backend.models()]
    if EXAMPLE_MODEL not in models:
        raise AssertionError('Example pack did not load: %r' % (backend.structure_pack_report(),))
    return EXAMPLE_MODEL


def register_temporary_model(test, definition, builder):
    """Register a model directly through backend.register_model for one test."""
    backend.register_model(definition, builder)
    test.addCleanup(backend.unregister_model, definition['id'])


PAIR_MODEL = 'test_pair_transformer'


def _transform_points(value, dx, mirror):
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
        return [value[0] + dx, -value[1] if mirror else value[1]]
    if isinstance(value, list):
        return [_transform_points(item, dx, mirror) for item in value]
    return value


def _transform_box(box, dx, mirror):
    (x0, y0), (x1, y1) = _transform_points(box, dx, mirror)
    return [[min(x0, x1), min(y0, y1)], [max(x0, x1), max(y0, y1)]]


def _winding(geometry, winding, dx, mirror, keep_shields):
    """Rename one differential-inductor body into winding ``winding`` (P or S)."""
    rename = {'L1': winding + '1', 'L2': winding + '2'}
    flip = {'top': 'bottom', 'bottom': 'top'}
    polygons, vias, ports = [], [], []
    for poly in geometry['polygons']:
        if poly.get('part') in ('pgs', 'ring', 'ground_route') and not keep_shields:
            continue
        poly = dict(poly)
        for key in ('points', 'centerline'):
            if key in poly:
                poly[key] = _transform_points(poly[key], dx, mirror)
        if poly.get('net') == 'L':
            poly['net'] = winding
        if poly.get('terminal_route') in rename:
            poly['terminal_route'] = rename[poly['terminal_route']]
        polygons.append(poly)
    for via in geometry['vias']:
        if via.get('part') in ('pgs', 'ring', 'ground_route') and not keep_shields:
            continue
        via = dict(via, origin=_transform_points(via['origin'], dx, mirror),
                   bbox=_transform_box(via['bbox'], dx, mirror))
        if via.get('net') == 'L':
            via['net'] = winding
        vias.append(via)
    for port in geometry['ports']:
        if port['name'] not in rename and not (keep_shields and port.get('net') == 'GND'):
            continue
        port = dict(port, bbox=_transform_box(port['bbox'], dx, mirror))
        if port['name'] in rename:
            port.update(name=rename[port['name']], net=winding, winding=winding)
        if mirror and port.get('access_direction') in flip:
            port['access_direction'] = flip[port['access_direction']]
        ports.append(port)
    return polygons, vias, ports


def pair_builder(parameters, layers, technology, variant):
    """Two synthetic windings built from the built-in differential inductor.

    The primary keeps the built-in orientation (leads at the bottom); the
    secondary is mirrored and shifted so its leads face the top.  Test only.
    """
    import structures
    base = structures.generate_model(dict(parameters, center_tap=False), layers, technology, 'differential_inductor')
    if not base.get('ok'):
        return base
    geometry = base['geometry']
    shift = 2.5 * max(abs(v) for corner in geometry['bounds'] for v in corner)
    primary = _winding(geometry, 'P', 0.0, False, True)
    secondary = _winding(geometry, 'S', shift, True, False)
    ports = primary[2][:2] + secondary[2][:2] + primary[2][2:]
    combined = dict(geometry, polygons=primary[0] + secondary[0], vias=primary[1] + secondary[1], ports=ports,
                    bounds=[[geometry['bounds'][0][0], -geometry['bounds'][1][1]],
                            [geometry['bounds'][1][0] + shift, geometry['bounds'][1][1]]])
    return dict(base, geometry=combined)


def register_pair_transformer(test):
    """Register a synthetic two-winding model (P1/P2, S1/S2) for one test."""
    import structures
    builtin = next(item for item in structures.MODEL_DEFINITIONS if item['id'] == 'differential_inductor')
    schema = [dict(field) for field in builtin['parameter_schema'] if field['name'] != 'center_tap']
    definition = dict(id=PAIR_MODEL, title='Test pair', description='Test-only two-winding model.',
                      defaults={field['name']: builtin['defaults'][field['name']] for field in schema
                                if field['name'] in builtin['defaults']},
                      parameter_schema=schema, layer_roles=[r for r in builtin['layer_roles'] if r != 'tap'],
                      emx_metrics=['Lp', 'Ls', 'k', 'Qp', 'Qs'])
    register_temporary_model(test, definition, pair_builder)
    return PAIR_MODEL
