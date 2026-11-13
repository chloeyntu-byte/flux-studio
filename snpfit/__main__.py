import argparse
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description='SNPFit: explicit RLC(K) model fitting')
    sub=parser.add_subparsers(dest='command')
    gui=sub.add_parser('gui', help='Open the English multi-page studio');gui.add_argument('--runtime');gui.add_argument('--request')
    integration=sub.add_parser('integration', help='Fit and deliver a JSON request');integration.add_argument('--request',required=True)
    action=sub.add_parser('studio-action', help='Run a staged studio action from a saved JSON configuration')
    action.add_argument('action', choices=('quick','tune','upgrade','native','symbol'))
    action.add_argument('--config', required=True)
    action.add_argument('--baseline', help='Previous immutable studio snapshot for tune, upgrade or native')
    action.add_argument('--runtime')
    sub.add_parser('doctor')
    sub.add_parser('models')
    fit=sub.add_parser('fit')
    fit.add_argument('snp');fit.add_argument('--model',required=True)
    fit.add_argument('--ports',required=True,help='1-based file ports in model role order, e.g. 1,2,3,4')
    fit.add_argument('--band-ghz',nargs=2,type=float,default=(1.,50.))
    fit.add_argument('--error',type=float,help='Explicit percentage for both L and Q, range 5..20')
    fit.add_argument('--l-error',type=float);fit.add_argument('--q-error',type=float)
    fit.add_argument('--runtime')
    fit.add_argument('--seconds',type=float,default=180)
    fit.add_argument('--short-unused',action='store_true')
    deliver=sub.add_parser('deliver')
    deliver.add_argument('result');deliver.add_argument('--workspace',required=True)
    deliver.add_argument('--library',required=True);deliver.add_argument('--cell',required=True)
    deliver.add_argument('--library-path')
    a=parser.parse_args()
    if a.command in (None,'gui'):
        from .studio_gui import main as gui_main
        try:
            gui_main(getattr(a,'runtime',None), getattr(a,'request',None))
        except Exception as exc:
            if type(exc).__module__ == '_tkinter':
                raise RuntimeError('Unable to initialize the graphical interface: '+str(exc)+'. Start from a graphical desktop with a valid DISPLAY, or run ./launch doctor for diagnostics.') from exc
            raise
        return 0
    if a.command=='integration':
        import signal, threading
        from .integration_api import run_request
        cancel = threading.Event()
        def request_cancel(signum, frame): cancel.set()
        signal.signal(signal.SIGTERM, request_cancel)
        signal.signal(signal.SIGINT, request_cancel)
        result=run_request(json.loads(Path(a.request).read_text(encoding='utf-8')), cancel_event=cancel, progress=lambda msg:print(str(msg),file=sys.stderr,flush=True))
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if result.get('passed') else 3
    if a.command=='studio-action':
        from .studio_engine import run_action
        config=json.loads(Path(a.config).read_text(encoding='utf-8'))
        baseline=json.loads(Path(a.baseline).read_text(encoding='utf-8')) if a.baseline else None
        result=run_action(a.action,config,baseline=baseline,runtime=a.runtime,
                          progress=lambda msg:print(str(msg),file=sys.stderr,flush=True))
        print(json.dumps({'id':result.get('id'),'stage':result.get('stage'),
                          'status':result.get('status'),'passed':result.get('passed'),
                          'native_verified':result.get('native_verified'),
                          'run_dir':result.get('run_dir')},ensure_ascii=False,indent=2))
        return 0 if a.action=='quick' or result.get('passed') else 3
    if a.command=='doctor':
        from .workflow import diagnose
        print(json.dumps(diagnose(),ensure_ascii=False,indent=2));return 0
    if a.command=='models':
        from .models import list_models
        print(json.dumps([m.as_dict() for m in list_models()],ensure_ascii=False,indent=2));return 0
    if a.command=='fit':
        l=a.l_error if a.l_error is not None else a.error
        q=a.q_error if a.q_error is not None else a.error
        if l is None or q is None:parser.error('Each fit requires --error or both --l-error and --q-error (5..20).')
        from .workflow import fit_file
        result=fit_file(snp=a.snp,model_id=a.model,port_order=[int(v)-1 for v in a.ports.split(',')],
                        fmin=a.band_ghz[0]*1e9,fmax=a.band_ghz[1]*1e9,l_pct=l,q_pct=q,
                        runtime=a.runtime,timeout_s=a.seconds,
                        short_unused=a.short_unused,progress=lambda m:print(m,file=sys.stderr,flush=True))
        from .plots import save_plot
        if result.get('metrics'):save_plot(result)
        print(json.dumps({'passed':result['passed'],'run_dir':result['run_dir'],'status':result['delivery_status'],'strategy':result['strategy']},ensure_ascii=False,indent=2))
        return 0 if result['passed'] else 3
    if a.command=='deliver':
        from .delivery import deliver as native_deliver
        result=native_deliver(json.loads(Path(a.result).read_text(encoding='utf-8')),
                              a.workspace,a.library,a.cell,existing_library_path=a.library_path,
                              progress=lambda m:print(m,file=sys.stderr,flush=True))
        print(json.dumps(result['native'],ensure_ascii=False,indent=2))
        return 0 if result['native_verified'] else 4


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,RuntimeError,OSError) as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(2)
