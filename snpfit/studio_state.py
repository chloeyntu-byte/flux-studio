"""JSON session state and source identity for independent SNPFit Studio pages."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid

SESSION_SCHEMA = 'snpfit-studio-session'
SESSION_VERSION = 1
MAX_SESSION_BYTES = 128 * 1024 * 1024
MAX_PAGES = 64


def default_config():
    return {
        'source_path': '', 'model_id': '', 'port_order': '',
        'short_unused': False, 'ports_confirmed': False,
        'fmin_ghz': '1', 'fmax_ghz': '50',
        'l_pct': '', 'q_pct': '', 'k_pct': '5', 'srf_pct': '2',
        'network_pct': '5', 'component_limit': 20,
        'quick_seconds': 5, 'tune_seconds': '60', 'upgrade_seconds': '90',
        'topology_id': 'auto', 'workspace': '',
        'library': 'SNPFit_models', 'cell': 'passive_fit',
        'env_script': '', 'virtuoso': '', 'ocean': '', 'spectre': '',
        'native_user': '', 'existing_library_path': '',
        'visual_reference': 'auto', 'q_annotation_ghz': '',
        'native_transformer_style': 'coplanar',
    }


def new_page(title=None):
    return {
        'id': uuid.uuid4().hex, 'title': title or 'Untitled SNP',
        'config': default_config(), 'source': None, 'source_valid': False,
        'results': {'quick': None, 'current': None, 'before_upgrade': None, 'upgraded': None},
        'logs': [], 'status': 'Load a Touchstone file to begin.',
        'warning': '', 'results_stale': False,
        'view': {'xscale': 'log', 'grid': True, 'show_markers': True,
                 'show_srf': True, 'show_tolerance': True, 'winding': 'primary', 'selected_metric': 'All metrics',
                 'diagram_scale': 'Fit'},
    }


def model_label(spec):
    ident = spec if isinstance(spec, str) else (spec.get('id') if isinstance(spec, dict) else spec.id)
    parts = ident.split('_')
    device = 'Transformer' if ident.startswith('transformer') else 'Inductor'
    mode = 'Differential' if '_dm' in ident else 'Single-ended'
    if '_dual_tap' in ident:
        tap = 'Dual taps'
    elif '_primary_tap' in ident:
        tap = 'Primary tap'
    elif '_secondary_tap' in ident:
        tap = 'Secondary tap'
    elif '_tap' in ident:
        tap = 'Center tap'
    else:
        tap = 'No tap'
    ground = 'GND pin' if ident.endswith('_gnd') else 'Internal reference'
    return '%s | %s | %s | %s' % (device, mode, tap, ground)


def topology_label(candidate):
    key = candidate if isinstance(candidate, str) else (candidate.get('id') if isinstance(candidate, dict) else candidate.id)
    labels = {
        'auto': 'Automatic initial structure', 'series_rl': 'Series R-L',
        'coupled_rl': 'Coupled R-L', 'compact_pi': 'Compact RLC',
        'parasitic_rlck': 'Compact RLCK', 'substrate_skin': 'Substrate and skin loss',
        'distributed_2': 'Distributed: 2 sections', 'distributed_3': 'Distributed: 3 sections',
        'distributed_bridge': 'Distributed: 3 sections + bridges',
    }
    return labels.get(key, key.replace('_', ' ').capitalize())


def _safe_json(value):
    """Use JSON conversion as the data boundary; never execute state content."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('Session contains unsupported or non-finite data: %s' % exc) from exc
    if len(text.encode('utf-8')) > MAX_SESSION_BYTES:
        raise ValueError('Session exceeds the 128 MiB size limit.')
    return json.loads(text)


def _source_path(page):
    config = page.get('config') or {}
    source = page.get('source') or {}
    return str(config.get('source_path') or source.get('path') or source.get('source_path') or '')


def _source_sha(page):
    source = page.get('source') or {}
    return str(source.get('sha256') or source.get('source_sha256') or '')


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_source_identity(page):
    path, expected = _source_path(page), _source_sha(page)
    info = {'valid': False, 'status': 'unloaded', 'path': path,
            'expected_sha256': expected, 'actual_sha256': None, 'message': ''}
    if not path:
        info['message'] = 'No SNP file is loaded.'
        return info
    file = Path(path).expanduser()
    if not file.is_file():
        info.update(status='missing', message='SNP file is missing. Cached plots remain available; load the source before continuing.')
        return info
    try:
        actual = _sha256(file)
    except OSError as exc:
        info.update(status='unreadable', message='SNP file cannot be read: %s' % exc)
        return info
    info['actual_sha256'] = actual
    if not expected:
        info.update(status='identity_unknown', message='Saved source identity is unavailable. Load the source before continuing.')
    elif expected != actual:
        info.update(status='changed', message='SNP file has changed since this session was saved. Cached results are historical; load the source again.')
    else:
        info.update(valid=True, status='ok', message='Source identity verified.')
    return info


def _page_normalize(page):
    if not isinstance(page, dict):
        raise ValueError('Every session page must be a JSON object.')
    data = copy.deepcopy(page)
    if not isinstance(data.get('id'), str) or not data['id'] or len(data['id']) > 128:
        raise ValueError('A page has an invalid identity.')
    if not isinstance(data.get('title'), str) or len(data['title']) > 256:
        raise ValueError('A page has an invalid title.')
    config = data.get('config', {})
    if not isinstance(config, dict):
        raise ValueError('Page configuration must be an object.')
    data['config'] = {**default_config(), **config}
    for flag in ('short_unused', 'ports_confirmed'):
        if not isinstance(data['config'][flag], bool):
            raise ValueError('%s must be true or false.' % flag)
    if data.get('source') is not None and not isinstance(data['source'], dict):
        raise ValueError('Page source metadata must be an object.')
    results = data.get('results')
    if results is None:
        results = {name: data.get(alias) for name, alias in (
            ('quick', 'quick'), ('current', 'current'), ('before_upgrade', 'before_upgrade'), ('upgraded', 'after_upgrade'))}
    if not isinstance(results, dict):
        raise ValueError('Page results must be an object.')
    for name in ('quick', 'current', 'before_upgrade', 'upgraded'):
        if results.get(name) is not None and not isinstance(results[name], dict):
            raise ValueError('Saved result %s must be an object or null.' % name)
    data['results'] = {'quick': None, 'current': None, 'before_upgrade': None, 'upgraded': None, **results}
    if not isinstance(data.get('logs', []), (list, str)):
        raise ValueError('Page logs must be a list or text.')
    if not isinstance(data.get('view', {}), dict):
        raise ValueError('Page view state must be an object.')
    data.setdefault('logs', [])
    data.setdefault('status', 'Session restored.')
    data.setdefault('warning', '')
    data.setdefault('source_valid', False)
    data.setdefault('results_stale', False)
    data.setdefault('view', {})
    return data


def _invalidate_historical(page, reason):
    page['source_valid'] = False
    page['results_stale'] = True
    page['requires_source_reload'] = True
    page['warning'] = reason
    page['status'] = 'Source verification required. Cached results are historical.'
    # Keep evidence and curves, but do not allow a restored boolean to qualify
    # a different/missing physical source for fitting or native generation.
    for snapshot in page.get('results', {}).values():
        if isinstance(snapshot, dict):
            snapshot.setdefault('historical_passed', bool(snapshot.get('passed')))
            snapshot.setdefault('historical_native_verified', bool(snapshot.get('native_verified')))
            snapshot['passed'] = False
            snapshot['native_verified'] = False
            snapshot['source_identity_valid'] = False
            snapshot['restored_source_invalid'] = True


def save_session(path, pages, active_id, window_state=None):
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError('Save between 1 and %d pages.' % MAX_PAGES)
    normalized = [_page_normalize(p) for p in pages]
    ids = [p['id'] for p in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError('Page identities must be unique.')
    if active_id not in ids:
        raise ValueError('The active page is not part of this session.')
    content = _safe_json({'schema': SESSION_SCHEMA, 'version': SESSION_VERSION,
                          'saved_utc': datetime.now(timezone.utc).isoformat(),
                          'pages': normalized, 'active_id': active_id,
                          'window_state': window_state or {}})
    target = Path(path).expanduser()
    resolved_target = target.resolve()
    for page in normalized:
        source_path = _source_path(page)
        if source_path and Path(source_path).expanduser().resolve() == resolved_target:
            raise ValueError('A session cannot overwrite a loaded SNP source file.')
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        if not target.is_file():
            raise ValueError('Session destination is not a regular file.')
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        candidate = target.with_name(target.name + '.bak-' + stamp)
        suffix = 1
        while candidate.exists():
            candidate = target.with_name(target.name + '.bak-' + stamp + '-%d' % suffix)
            suffix += 1
        shutil.copy2(target, candidate)
        backup = str(candidate)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'path': str(target), 'backup': backup, 'page_count': len(normalized)}


def load_session(path):
    target = Path(path).expanduser()
    if target.stat().st_size > MAX_SESSION_BYTES:
        raise ValueError('Session exceeds the 128 MiB size limit.')
    def reject_constant(value):
        raise ValueError('Non-finite number is not allowed in a session: ' + value)
    try:
        payload = json.loads(target.read_text(encoding='utf-8'), parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid session JSON: %s' % exc) from exc
    if not isinstance(payload, dict) or payload.get('schema') != SESSION_SCHEMA:
        raise ValueError('This file is not an SNPFit Studio session.')
    if payload.get('version') != SESSION_VERSION:
        raise ValueError('Unsupported session version: %r' % payload.get('version'))
    pages = payload.get('pages')
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError('A session must contain 1 to %d pages.' % MAX_PAGES)
    pages = [_page_normalize(p) for p in pages]
    ids = [p['id'] for p in pages]
    if len(ids) != len(set(ids)):
        raise ValueError('Page identities must be unique.')
    active = payload.get('active_id')
    if active not in ids:
        raise ValueError('Saved active page identity is invalid.')
    warnings = []
    for page in pages:
        check = validate_source_identity(page)
        page['source_validation'] = check
        if check['valid']:
            page['source_valid'] = True
            page['requires_source_reload'] = False
        elif check['status'] != 'unloaded':
            _invalidate_historical(page, check['message'])
            warnings.append('%s: %s' % (page['title'], check['message']))
        else:
            page['source_valid'] = False
            if any(isinstance(item, dict) for item in page.get('results', {}).values()):
                _invalidate_historical(page, 'Saved results have no verified SNP source. Load the source before continuing.')
                warnings.append('%s: No verified source identity.' % page['title'])
    return {**payload, 'pages': pages, 'active_id': active, 'warnings': warnings}
