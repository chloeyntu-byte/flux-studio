"""Native schematic delivery and independent source-vs-OA replay acceptance."""
from pathlib import Path
import json
import os
import re
import numpy as np

from .network import read_touchstone
from .workflow import json_write, write_summary


def library_mappings(workspace, cds_lib=None):
    """Bounded literal cds.lib reader. Does not evaluate SKILL or edit mappings."""
    mappings={};seen=set()
    def read(path,depth=0):
        path=Path(path).resolve()
        if depth>8 or len(seen)>64 or path in seen or not path.is_file():return
        seen.add(path)
        for raw in path.read_text(encoding='utf-8',errors='replace').splitlines():
            line=raw.strip()
            if line.startswith(('#','--',';')):continue
            match=re.match(r'^(SOFTINCLUDE|INCLUDE)\s+(.+)$',line,re.I)
            if match:
                name=os.path.expandvars(match[2].strip().strip('"'))
                if '$' not in name:read(Path(name) if Path(name).is_absolute() else path.parent/name,depth+1)
                continue
            match=re.match(r'^DEFINE\s+(\S+)\s+(.+)$',line,re.I)
            if match:
                value=os.path.expandvars(match[2].strip().strip('"'))
                if '$' not in value:
                    mappings[match[1]]=str((Path(value) if Path(value).is_absolute() else path.parent/value).resolve())
    read(Path(cds_lib) if cds_lib else Path(workspace)/'cds.lib')
    return mappings


def deliver(result, workspace, library, cell, *, cancel=None, progress=None,
            existing_library_path=None):
    if (str(result.get('engine', '')).startswith('studio_')
            or result.get('stage') in ('quick', 'tune', 'upgrade', 'native', 'symbol')
            or isinstance(result.get('metrics'), dict) and 'lq' in result['metrics']):
        raise ValueError('Studio snapshots require studio-action native or Generate Schematic in the Studio UI; legacy deliver cannot verify all Studio targets.')
    from .native import build_native, replay_native
    from .fitting import evaluate_network
    if not result.get('passed') or not result.get('circuit'):
        raise ValueError('仅允许生成本次数值验收已经达标的候选。')
    run = Path(result['run_dir'])
    circuit=result['circuit']
    source=read_touchstone(next(run.glob('selected.s*p')))
    if not np.all(source.z0 == source.z0[0]):
        raise ValueError('原生回放目前要求共同实数参考阻抗。')
    log=progress or (lambda message: None)
    if existing_library_path is None:
        existing_library_path=library_mappings(workspace).get(library)
    log('创建新 schematic / symbol，验证实际连通并重新网表化。')
    native = build_native(circuit, workspace, run/'delivery',library,cell,
                          existing_library_path=existing_library_path,
                          cancel=cancel.is_set if cancel else None)
    log('使用原生 OA 网表回放，与原 SNP 同频点比较。')
    replay=replay_native(native,source.freq.tolist(),float(source.z0[0]),
                         cancel=cancel.is_set if cancel else None)
    candidate=read_touchstone(replay['response'])
    if len(candidate.freq)!=len(source.freq) or not np.allclose(candidate.freq,source.freq,rtol=5e-7,atol=0):
        raise ValueError('回放频点与本次原始验点不一致。')
    # Preserve requested source frequencies after checking textual roundoff.
    candidate.freq=source.freq.copy()
    metrics=evaluate_network(candidate,source,result['model_id'],
                             {'l_pct':result['request']['l_pct'],'q_pct':result['request']['q_pct']})
    native['replay']=replay
    native['metrics']=metrics
    result={**result,'native':native,'native_verified':bool(metrics['passed']),
            'delivery_status':'VERIFIED' if metrics['passed'] else 'NATIVE_NOT_ACCEPTED'}
    json_write(run/'result.json',result)
    write_summary(run,result)
    if not metrics['passed']:
        log('原生回放未达标：保留证据，不标记为已验收模型。')
    else:
        log('原生回放验收通过。库映射：'+native['mapping_line'])
    return result
