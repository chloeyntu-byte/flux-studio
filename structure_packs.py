"""Load structure packs: add-on winding models registered at start-up.

The open-source edition ships one built-in structure (the differential
inductor).  Every other structure -- transformers, single-ended or tapped
inductors, baluns, house-style variants -- is delivered as a *structure pack*:
a Python file or package that defines ``register(flux)`` and calls
``flux.register_model(definition, builder)`` for each model it provides.

Packs are searched, in order, in

1. every entry of ``FLUX_PACKS`` (``os.pathsep``-separated).  An entry may be
   a pack file, a pack package directory, or a directory of packs;
2. ``packs/`` next to this file.

Set ``FLUX_PACKS_DISABLE=1`` to skip discovery (built-in models only).
A pack that fails to load is reported and skipped; models it managed to
register before failing are removed again.  See docs/STRUCTURE_PACKS.md.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import sys
import threading
import traceback

API_VERSION = 1
PACK_DIR = Path(__file__).resolve().parent / 'packs'
_LOCK = threading.RLock()
_STATE = {'loaded': False, 'packs': []}


class PackAPI(object):
    """The object handed to a pack's ``register(flux)``."""
    api_version = API_VERSION

    def __init__(self, path):
        import backend
        import geometry
        import structures
        import technology
        self.pack_path = Path(path)
        self.geometry = geometry
        self.technology = technology
        self.screen_parameters = structures.screen_parameters
        self.builtin_definitions = structures.MODEL_DEFINITIONS
        self.build_builtin = structures.generate_model
        self.registered = []
        self._backend = backend

    def register_model(self, definition, builder):
        self._backend.register_model(definition, builder)
        self.registered.append(definition['id'])


def search_paths(environ=None):
    environ = os.environ if environ is None else environ
    paths = [Path(item).expanduser() for item in environ.get('FLUX_PACKS', '').split(os.pathsep) if item.strip()]
    paths.append(PACK_DIR)
    return paths


def _is_package(path):
    return path.is_dir() and (path / '__init__.py').is_file()


def discover(environ=None, problems=None):
    """Return pack locations (files or package directories), de-duplicated.

    An unreadable search location is skipped; when ``problems`` is a list a
    report entry is appended for it.
    """
    found, seen = [], set()
    for root in search_paths(environ):
        try:
            if root.is_file() and root.suffix == '.py':
                candidates = [root]
            elif _is_package(root):
                candidates = [root]
            elif root.is_dir():
                candidates = sorted(item for item in root.iterdir()
                                    if not item.name.startswith(('_', '.')) and
                                    ((item.is_file() and item.suffix == '.py') or _is_package(item)))
            else:
                continue
        except OSError as exc:
            if problems is not None:
                problems.append({'name': root.name or str(root), 'source': root.name, 'version': None,
                                 'models': [], 'error': _public_error('%s: %s' % (type(exc).__name__, exc)),
                                 'error_detail': '%s: %s' % (type(exc).__name__, exc)})
            continue
        for item in candidates:
            key = item.resolve()
            if key not in seen:
                seen.add(key)
                found.append(item)
    return found


_PATH = re.compile(r'(?<![\w.])(?:[A-Za-z]:)?(?:[\\/][^\\/\s\'"]+){2,}[\\/]?')


def _public_error(text):
    """Error text for reports: absolute paths are reduced to their last part."""
    def short(match):
        parts = [part for part in re.split(r'[\\/]', match.group(0)) if part]
        return '.../' + parts[-1] if parts else '...'
    return _PATH.sub(short, str(text))


def _module_name(path):
    slug = re.sub(r'\W', '_', path.stem if path.is_file() else path.name)
    digest = hashlib.sha1(str(path.resolve()).encode('utf-8')).hexdigest()[:8]
    return 'flux_pack_%s_%s' % (slug, digest)


def load_pack(path):
    """Import one pack and run its ``register(flux)``; returns a report dict."""
    import backend
    path = Path(path)
    entry = {'name': path.stem if path.is_file() else path.name, 'source': path.name,
             'version': None, 'models': [], 'error': None}
    api = None
    name = _module_name(path)
    try:
        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, str(path))
        else:
            spec = importlib.util.spec_from_file_location(name, str(path / '__init__.py'),
                                                          submodule_search_locations=[str(path)])
        if spec is None or spec.loader is None:
            raise ImportError('Not an importable Python pack.')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        entry['name'] = str(getattr(module, 'PACK_NAME', entry['name']))
        entry['version'] = getattr(module, 'PACK_VERSION', None)
        required = getattr(module, 'FLUX_PACK_API', API_VERSION)
        if required != API_VERSION:
            raise ImportError('Pack needs structure-pack API %r; this Flux provides %r.' % (required, API_VERSION))
        register = getattr(module, 'register', None)
        if not callable(register):
            raise ImportError('Pack does not define register(flux).')
        api = PackAPI(path)
        register(api)
        if not api.registered:
            raise ValueError('Pack registered no models.')
        entry['models'] = list(api.registered)
    except Exception as exc:
        for model in (api.registered if api else []):
            backend.unregister_model(model)
        sys.modules.pop(name, None)
        entry['error_detail'] = '%s: %s' % (type(exc).__name__, exc)
        entry['error'] = _public_error(entry['error_detail'])
        entry['traceback'] = traceback.format_exc(limit=4)
        entry['models'] = []
    return entry


def ensure_loaded(environ=None):
    """Load every discoverable pack exactly once per process."""
    with _LOCK:
        if _STATE['loaded']:
            return _STATE['packs']
        _STATE['loaded'] = True
        environ = os.environ if environ is None else environ
        if environ.get('FLUX_PACKS_DISABLE', '').strip() not in ('', '0'):
            return _STATE['packs']
        problems = []
        for path in discover(environ, problems):
            _STATE['packs'].append(load_pack(path))
        _STATE['packs'].extend(problems)
        return _STATE['packs']


def report():
    """Public summary (no tracebacks, no absolute paths)."""
    return [{key: entry[key] for key in ('name', 'source', 'version', 'models', 'error')}
            for entry in _STATE['packs']]


def reset():
    """Forget loaded packs and their models (used by tests)."""
    import backend
    with _LOCK:
        for entry in _STATE['packs']:
            for model in entry.get('models', []):
                backend.unregister_model(model)
        _STATE['packs'] = []
        _STATE['loaded'] = False
