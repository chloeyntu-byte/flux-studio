"""One immutable request per run. No previous tolerance is reused."""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import hashlib
import json
import math
import shutil
import traceback
import uuid

from .network import read_touchstone, write_touchstone


def json_write(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                               allow_nan=False, default=_json_default) + '\n', encoding='utf-8')
    temp.replace(path)


def _json_default(value):
    if hasattr(value, 'tolist'):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def runtime_root(override=None):
    if override:
        return Path(override).expanduser().resolve()
    install = Path(__file__).resolve().parents[1]
    return install.parent / (install.name + '_runtime')


def fit_file(*, snp, model_id, port_order, fmin, fmax, l_pct, q_pct,
             runtime=None, short_unused=False,
             timeout_s=180, cancel=None, progress=None):
    from .fitting import fit
    from .models import get_model
    from .lpriority import select_strategy
    if l_pct is None or q_pct is None or not all(
            math.isfinite(float(x)) and 5 <= float(x) <= 20 for x in (l_pct, q_pct)):
        raise ValueError('本次必须重新填写 5–20% 之间的 L、Q 误差。')
    model = get_model(model_id)
    source = read_touchstone(snp)
    selected = source.band(float(fmin), float(fmax)).mapped(list(port_order), short_unused=short_unused)
    root = runtime_root(runtime)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    run = root / 'runs' / (stamp + '-' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    config = {'schema': 3, 'fitting_engine': 'standalone_numpy',
              'strategy': select_strategy(l_pct, q_pct), 'source': source.metadata, 'model_id': model_id,
              'port_order_1based': [i+1 for i in port_order], 'short_unused': bool(short_unused),
              'fmin_hz': float(fmin), 'fmax_hz': float(fmax),
              'l_pct': float(l_pct), 'q_pct': float(q_pct),
              'tolerance_source': 'explicit_this_request', 'timeout_s': float(timeout_s),
              'sample_count': len(selected.freq), 'measurement_data': selected.metadata}
    json_write(run / 'request.json', config)
    shutil.copy2(snp, run / ('source' + Path(snp).suffix))
    write_touchstone(selected, run / f'selected.s{selected.nports}p')
    lines = []

    def log(message):
        line = str(message)
        lines.append(line)
        with (run / 'run.log').open('a', encoding='utf-8') as out:
            out.write(line + '\n')
        if progress:
            progress(line)

    try:
        log('读取完成：%d 个原始频点；按指定模型拟合。' % len(selected.freq))
        log('自动策略：' + ('L优先（L误差要求比Q严格）' if config['strategy']=='l_priority' else '联合拟合'))
        result = fit(selected, model_id,
                     {'l_pct': float(l_pct), 'q_pct': float(q_pct)},
                     run / 'fit', timeout_s=float(timeout_s), cancel=cancel, progress=log)
        data = result.as_dict()
        data.update({'run_dir': str(run), 'request': config,
                     'native_verified': False,
                     'delivery_status': 'NUMERIC_PASS' if result.passed else 'NOT_ACCEPTED'})
        if cancel is not None and cancel.is_set():
            data.update(passed=False,status='cancelled',delivery_status='CANCELLED')
        json_write(run / 'result.json', data)
        if result.circuit:
            json_write(run / 'circuit.json', result.circuit)
        write_summary(run, data)
        return data
    except Exception as exc:
        log(type(exc).__name__ + ': ' + str(exc))
        json_write(run / 'failure.json', {'status': 'CANCELLED' if cancel and cancel.is_set() else 'FAILED',
                                         'error': str(exc), 'traceback': traceback.format_exc()})
        raise


def write_summary(run, data):
    request = data['request']
    lines = ['# SNPFit 本次结果', '',
             '状态：' + data['delivery_status'],
             '', f"模型：`{request['model_id']}`", '',
             '原始 SNP SHA256：`' + request['source']['sha256'] + '`', '',
             f"频段：{request['fmin_hz']:.9g}–{request['fmax_hz']:.9g} Hz；原始验点 {request['sample_count']} 个。", '',
             f"本次门限：L {request['l_pct']:g}%，Q {request['q_pct']:g}%。", '',
             '自动策略：' + ('L优先' if data.get('strategy', request.get('strategy'))=='l_priority' else '联合拟合'), '',
             '端口映射（文件端口编号）：' + str(request['port_order_1based']), '',
             '数值拟合和原生交付分别记录。只有原生 schematic 重新网表化及回放通过后，交付状态才是 VERIFIED。', '',
             '```json', json.dumps(data.get('metrics', {}), ensure_ascii=False, indent=2,
                                  default=_json_default), '```', '']
    Path(run, 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


def diagnose():
    import importlib.util
    import os
    import platform
    import sys
    result = {'python': sys.executable, 'version': platform.python_version(),
              'numpy': None, 'tkinter_available': importlib.util.find_spec('tkinter') is not None,
              'display_configured': bool(os.environ.get('DISPLAY'))}
    import numpy as np
    result['numpy'] = np.__version__
    result.update({'fitting_engine': 'standalone_numpy',
                   'external_fitting_program_required': False,
                   'schematic_export': 'optional existing Virtuoso/Ocean/Spectre tools',
                   'native_export_checked': False})
    return result
