#!/usr/bin/env python3
"""Offline geometry CLI. No Cadence installation is used by these commands."""
import argparse
import json
from pathlib import Path
import backend

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('models')
    sub.add_parser('packs', help='List structure packs found via FLUX_PACKS and packs/')
    preview = sub.add_parser('preview')
    preview.add_argument('--technology', required=True)
    preview.add_argument('--design', required=True)
    preview.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'models':
        print(json.dumps(backend.models(), indent=2)); return 0
    if args.command == 'packs':
        packs = backend.structure_pack_report()
        print(json.dumps(packs, indent=2))
        return 1 if any(pack['error'] for pack in packs) else 0
    if args.command != 'preview':
        parser.error('Choose models, packs or preview')
    try:
        context = backend.load_file_context(args.technology)
        source = json.loads(Path(args.design).read_text(encoding='utf-8'))
        design = source.get('design', source)
        design['library'] = context['selected_library']
        result = backend.preview(design)
        Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        print('Layout validation: '+('PASS' if result['ok'] else 'FAIL'))
        return 0 if result['ok'] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, 'Preview failed: '+str(exc)+'\n')

if __name__ == '__main__':
    raise SystemExit(main())
