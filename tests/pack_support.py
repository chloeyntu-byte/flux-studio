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
