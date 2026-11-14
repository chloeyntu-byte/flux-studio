"""Read campaign display metrics from the retained Touchstone artifact."""
import copy
import hashlib
import json
from pathlib import Path


def hydrate_row(row, metric_specs=None):
    """``metric_specs`` adds pin-assigned metrics (see emx_search.metric_specs)."""
    result = copy.deepcopy(row)
    if result.get('status') != 'completed':
        result['metrics'] = []
        return result
    result['metrics'] = []
    try:
        path = Path(result['snp_path'])
        network = result.get('network') or {}
        if not network.get('ports') or not network.get('frequencies_hz'):
            with (Path(result['job_dir']) / 'job.json').open(encoding='utf-8') as stream:
                job = json.load(stream)
            network = dict(ports=job['ports'], frequencies_hz=job['frequencies_hz'], snp_sha256=network.get('snp_sha256'))
        before = path.stat()
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
        digest = hasher.hexdigest()
        expected = network.get('snp_sha256')
        if expected and digest.lower() != str(expected).lower():
            raise ValueError('SNP SHA-256 differs from the campaign record')
        from emx_adapter import read_touchstone, summarize_network
        from emx_campaign import _metrics
        ports = network['ports']
        parsed = read_touchstone(path, len(ports['signals']), network['frequencies_hz'])
        summary = summarize_network(parsed, ports, metric_specs)
        result['metrics'] = _metrics(summary)
        if summary.get('pin_metric_errors'):
            result['pin_metric_errors'] = summary['pin_metric_errors']
        after = path.stat()
        if (before.st_ino, before.st_size, getattr(before, 'st_mtime_ns', before.st_mtime)) != (after.st_ino, after.st_size, getattr(after, 'st_mtime_ns', after.st_mtime)):
            raise ValueError('SNP changed while reading')
        result.pop('data_error', None)
    except Exception as exc:
        result['metrics'] = []
        result['data_error'] = 'SNP data unavailable: %s' % exc
    return result


def load_campaign(directory, current_source=None):
    """Read public campaign metadata and row index; never read OA or cached metrics."""
    directory = Path(directory)
    with (directory / 'campaign.json').open(encoding='utf-8') as stream:
        meta = json.load(stream)
    with (directory / 'index.json').open(encoding='utf-8') as stream:
        rows = json.load(stream)
    if not isinstance(meta, dict) or meta.get('schema') != 'flux.emx.campaign.v1':
        raise ValueError('Unsupported or missing campaign schema.')
    identity = meta.get('identity'); design = meta.get('design'); config = meta.get('config')
    if not isinstance(identity, dict) or not isinstance(design, dict) or not isinstance(config, dict):
        raise ValueError('Campaign identity, design or configuration is missing.')
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('id') is None for row in rows):
        raise ValueError('Campaign result index must be a list of identified rows.')
    ids = [str(row['id']) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Campaign result index contains duplicate candidate IDs.')
    fields = []
    for axis in config.get('axes') or []:
        if not isinstance(axis, dict) or not isinstance(axis.get('name'), str):
            raise ValueError('Invalid campaign axis.')
        choices = axis.get('values') or []
        nonnull = [x for x in choices if x is not None]
        kind = 'bool' if nonnull and all(isinstance(x, bool) for x in nonnull) else 'string' if any(isinstance(x, str) for x in nonnull) else 'float'
        fields.append(dict(name=axis['name'], type=kind, optional=None in choices))
    source = dict(identity=copy.deepcopy(identity), design=copy.deepcopy(design), fields=fields, offline=bool(meta.get('synthetic_example')))
    current_source = current_source if isinstance(current_source, dict) else {}
    current_id = current_source.get('identity') or {}
    keys = ('workspace', 'cds_lib', 'library', 'technology', 'library_path')
    matches = all(identity.get(k) and identity.get(k) == current_id.get(k) for k in keys)
    matches = matches and design.get('model') == (current_source.get('design') or {}).get('model')
    if matches:
        source['runtime'] = copy.deepcopy(current_source.get('runtime') or {})
        source['definition'] = copy.deepcopy(current_source.get('definition') or {})
    result = copy.deepcopy(rows)
    for row in result:
        if row.get('snp_path') and not Path(row['snp_path']).is_absolute():
            row['snp_path'] = str((directory / row['snp_path']).resolve())
        row['metrics'] = []
        row['objective_values'] = []
        row['objectives'] = []
        row['feasible'] = None
        row['violation'] = None
        row.pop('data_error', None)
        row.pop('_data_pending', None)
    return dict(source=source, config=copy.deepcopy(config), rows=result, original_run_id=meta.get('run_id'))
