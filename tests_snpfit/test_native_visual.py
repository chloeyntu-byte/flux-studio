"""Visual handoff, exact read-back and missing-symbol ownership boundaries.

Native process calls are replaced at their boundary; these are not OA tests.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from snpfit.native import build_native, build_symbol

import shutil as _shutil
_REAL_WHICH = _shutil.which
_WHICH_PATCH = mock.patch('snpfit.native.shutil.which',
                          side_effect=lambda name, *a, **k: '/opt/native-stub/' + name if name in ('virtuoso', 'ocean') else _REAL_WHICH(name, *a, **k))


def setUpModule():
    # _run_native is mocked in every test; only the executable lookup needs a stand-in
    # so the contract runs on hosts without a Cadence installation.
    _WHICH_PATCH.start()


def tearDownModule():
    _WHICH_PATCH.stop()
from snpfit.native_visual import (circuit_fingerprint, file_sha256, json_hash,
                                 read_visual_receipt, validate_visual_plan, visual_skill_header)


def circuit():
    return {'name': 'test_winding', 'ports': ['P', 'N'], 'reference': 'REF',
            'metadata': {'export_reference': True}, 'elements': [
                {'kind': 'res', 'name': 'R1', 'n1': 'P', 'n2': 'x', 'value': 2.0},
                {'kind': 'ind', 'name': 'L1', 'n1': 'x', 'n2': 'N', 'value': 1e-9},
                {'kind': 'cap', 'name': 'C1', 'n1': 'N', 'n2': 'REF', 'value': 2e-14}]}


def plan():
    pins = [{'name': 'P', 'position': [-5, -5], 'direction': 'bottom', 'physical_names': ['P1']},
            {'name': 'N', 'position': [5, -5], 'direction': 'bottom', 'physical_names': ['P2']},
            {'name': 'REF', 'position': [0, -8], 'direction': 'bottom', 'physical_names': ['GND']},
            {'name': 'REF', 'position': [0, 8], 'direction': 'top', 'physical_names': ['PGS']}]
    return {'schema': 1, 'bounds': [-10, -10, 10, 10],
            'paths': [{'id': 'main', 'winding': 'primary', 'points': [[-5,-5],[-5,5],[5,5],[5,-5]]}],
            'pins': pins, 'placements': [
                {'name': 'R1', 'center': [-2,0], 'orientation': 'R0'},
                {'name': 'L1', 'center': [2,0], 'orientation': 'R90'},
                {'name': 'C1', 'center': [4,-4], 'orientation': 'R270'}],
            'labels': [{'text': 'Q=31.42 @10GHz', 'position': [0,2], 'role': 'segment_q'}],
            'q_frequency_hz': 1e10,
            'symbol': {'bounds': [-10,-10,10,10], 'paths': [{'points': [[-5,-5],[-5,5],[5,5],[5,-5]]}],
                       'pins': copy.deepcopy(pins)}}


def receipt_text(plan, section):
    geometry = plan if section == 'SCHEMATIC' else plan['symbol']
    lines = [f"{section}_PIN {p['name']} {p['position'][0]} {p['position'][1]}" for p in geometry['pins']]
    lines.append(section+'_ORDER ('+' '.join(json.dumps(v) for v in geometry['port_order'])+')')
    lines.append(f"{section}_GRAPHICS {len(geometry['paths'])} {len(geometry['labels'])}")
    if section == 'SCHEMATIC':
        lines += [f"SCHEMATIC_COMPONENT {p['name']} {p['center'][0]} {p['center'][1]} R90 {p['orientation']}" for p in plan['placements']]
    return '\n'.join(lines)+'\n'


class NativeVisualContract(unittest.TestCase):
    def test_plan_preserves_graph_and_multiple_anchors_keep_one_logical_port(self):
        c=circuit(); p=plan(); old_c=copy.deepcopy(c); old_p=copy.deepcopy(p)
        normalized=validate_visual_plan(c,p)
        self.assertEqual(normalized['port_order'], ['P','N','REF'])
        self.assertEqual(sum(x['name']=='REF' for x in normalized['pins']),2)
        self.assertEqual(c,old_c);self.assertEqual(p,old_p)
        changed=copy.deepcopy(c);changed['metadata']['only_visual']='other'
        self.assertEqual(circuit_fingerprint(changed),circuit_fingerprint(c))
        changed['elements'][0]['value']=3
        self.assertNotEqual(circuit_fingerprint(changed),circuit_fingerprint(c))

    def test_invalid_graph_placement_and_pin_aliases_rejected(self):
        for mutate in (lambda p:p['placements'].pop(),
                       lambda p:p['placements'].append(copy.deepcopy(p['placements'][0])),
                       lambda p:p['pins'][0].update(name='OTHER'),
                       lambda p:p['pins'][1].update(position=p['pins'][0]['position']),
                       lambda p:p['placements'][0].update(orientation='R45')):
            p=plan();mutate(p)
            with self.subTest(plan=p),self.assertRaises(ValueError):validate_visual_plan(circuit(),p)

    def test_header_has_only_data_and_no_component_value_rewrite(self):
        p=plan();p['labels'][0]['text']='Q "quoted"\\n test'
        p=validate_visual_plan(circuit(),p)
        text='\n'.join(visual_skill_header(p,'/private/receipt.txt'))
        self.assertIn('snpfitVisualPlacements=list(',text)
        self.assertIn('Q \\"quoted\\"\\\\n test',text)
        self.assertNotIn('snpfitElements',text)

    def test_actual_position_and_axis_receipts_are_required(self):
        p=validate_visual_plan(circuit(),plan())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'receipt';path.write_text(receipt_text(p,'SCHEMATIC'))
            r=read_visual_receipt(path,p,'SCHEMATIC');self.assertTrue(r['verified']);self.assertEqual(len(r['pins']),4)
            path.write_text(receipt_text(p,'SCHEMATIC').replace(' R90 R90',' R90 R0'))
            with self.assertRaisesRegex(RuntimeError,'axis'):read_visual_receipt(path,p,'SCHEMATIC')
            path.write_text(receipt_text(p,'SYMBOL').replace('SYMBOL_PIN P -5.0 -5.0','SYMBOL_PIN P -4.0 -5.0'))
            with self.assertRaisesRegex(RuntimeError,'position'):read_visual_receipt(path,p,'SYMBOL')

    def test_native_scale_keeps_common_plan_symbol_and_circuit_unchanged(self):
        c=circuit();p=plan();p['placements'][0].update(group='winding',winding='primary')
        p['placements'][1].update(group='winding',winding='secondary')
        original_c=copy.deepcopy(c);original_p=copy.deepcopy(p)
        n=validate_visual_plan(c,p)
        self.assertEqual(n['placements'][0]['center'],[-0.5,0.0])
        self.assertEqual(n['pins'][0]['position'],[-1.25,-1.25])
        self.assertEqual(n['symbol']['pins'][0]['position'],[-5.,-5.])
        labels=[v for v in n['labels'] if v['role']=='device_annotation']
        self.assertEqual({v['text'] for v in labels},{'R1=2Ohm','L1=1nH'})
        self.assertEqual(validate_visual_plan(c,n),n)
        self.assertEqual(c,original_c);self.assertEqual(p,original_p)

    def test_quarter_scale_parasitic_row_keeps_unrelated_stubs_apart(self):
        # The actual IC618 R/L/C endpoints span 0.375 native units.  The common
        # parasitic grid has a 3-unit pitch, or 0.75 after the visual handoff.
        p=plan();p['placements'][0].update(center=[-3,0],orientation='R0')
        p['placements'][1].update(center=[0,0],orientation='R0')
        n=validate_visual_plan(circuit(),p);length=n['native_transform']['visual_terminal_stub_length']
        first_right=n['placements'][0]['center'][0]+0.375/2+length
        second_left=n['placements'][1]['center'][0]-0.375/2-length
        self.assertLess(first_right,second_left)
        self.assertAlmostEqual(second_left-first_right,0.125)

    def test_vertical_main_value_text_faces_interior_not_external_card(self):
        p=plan();p['placements'][0].update(group='winding',winding='primary',orientation='R90')
        n=validate_visual_plan(circuit(),p)
        value=next(v for v in n['labels'] if v['role']=='device_annotation')
        self.assertGreater(value['position'][0],n['placements'][0]['center'][0])
        self.assertEqual(value['justification'],'centerLeft')

    def _build(self,folder,make_symbol=False,visual=True,use_default=False):
        root=Path(folder);workspace=root/'workspace';workspace.mkdir();(workspace/'cds.lib').write_text('')
        c=circuit();normalized=validate_visual_plan(c,plan()) if visual else None
        def native(executable,script,work,*args,**kwargs):
            lib=work/'library'/'visual_cell';(lib/'schematic').mkdir(parents=True)
            (lib/'schematic'/'sch.oa').write_bytes(b'unchanged schematic')
            (work/'connectivity.txt').write_text('saved connections')
            if normalized:
                (work/'visual-receipt.txt').write_text(receipt_text(normalized,'SCHEMATIC')+(receipt_text(normalized,'SYMBOL') if make_symbol else ''))
            if make_symbol:
                (lib/'symbol').mkdir();(lib/'symbol'/'symbol.oa').write_bytes(b'symbol')
            return {'status':'completed','returncode':0},'SNPFIT_NATIVE_CREATED test visual_cell\n'+('SNPFIT_SYMBOL_PORTS ("P" "N" "REF")\n' if make_symbol else '')
        with mock.patch('snpfit.native._run_native',side_effect=native) as run:
            options={} if use_default else {'make_symbol':make_symbol}
            result=build_native(c,workspace,root/'out','test','visual_cell',netlist=False,
                                visual_plan=plan() if visual else None,**options)
        self.assertEqual(run.call_count,1)
        return c,result,normalized

    def _verified(self,folder):
        c,native,p=self._build(folder)
        work=Path(native['native_work']);netlist=work/'component-netlist';netlist.write_text('R1 (P x) resistor r=2\n')
        response=work/'response.s2p';response.write_text('actual replay placeholder for boundary test')
        native.update(circuit_netlist=str(netlist),netlist_sha256=file_sha256(netlist),
                      metrics={'joint':{'passed':True}},replay={'response':str(response)})
        provenance=Path(native['provenance_file']);d=json.loads(provenance.read_text());d['netlist_sha256']=native['netlist_sha256'];provenance.write_text(json.dumps(d))
        return c,native,p

    def test_native_schematic_only_does_not_expect_symbol(self):
        with tempfile.TemporaryDirectory() as folder:
            c,native,p=self._build(folder)
            self.assertFalse(native['symbol_created']);self.assertNotIn('symbol_ports',native)
            self.assertTrue(native['visual_schematic']['verified'])
            self.assertEqual(native['circuit_fingerprint'],circuit_fingerprint(c))
            self.assertIn('snpfitMakeSymbolEnabled=nil',Path(native['native_work'],'build.il').read_text())

    def test_legacy_default_still_creates_and_checks_symbol(self):
        with tempfile.TemporaryDirectory() as folder:
            _,native,_=self._build(folder,make_symbol=True,visual=False,use_default=True)
            self.assertEqual(native['symbol_ports'],['P','N','REF'])
            self.assertTrue(native['symbol_created'])
            self.assertNotIn('visual_schematic',native)

    def test_symbol_creation_keeps_exact_schematic_and_netlist(self):
        with tempfile.TemporaryDirectory() as folder:
            c,native,p=self._verified(folder)
            before=file_sha256(Path(native['library_path'])/native['cell']/'schematic'/'sch.oa')
            def symbol(executable,script,work,*args,**kwargs):
                self.assertIn('snpfitAppendSymbol()',script.read_text())
                target=Path(native['library_path'])/native['cell']/'symbol';target.mkdir();(target/'symbol.oa').write_bytes(b'physical shape')
                (work/'visual-receipt.txt').write_text(receipt_text(p,'SYMBOL'))
                return {'status':'completed','returncode':0},'SNPFIT_SYMBOL_CREATED test visual_cell\n'
            with mock.patch('snpfit.native._run_native',side_effect=symbol) as run:
                result=build_symbol(native,c,plan())
            self.assertEqual(run.call_count,1);self.assertTrue(result['schematic_and_netlist_unchanged'])
            self.assertEqual(result['schematic_sha256_after'],before)
            self.assertEqual(result['visual_symbol']['port_order'],['P','N','REF'])
            with mock.patch('snpfit.native._run_native') as run,self.assertRaisesRegex(ValueError,'already exists'):
                build_symbol(native,c,plan(),out_dir=Path(folder)/'again')
            run.assert_not_called()

    def test_symbol_rejects_stale_or_unverified_schematic_before_native_work(self):
        with tempfile.TemporaryDirectory() as folder:
            c,native,p=self._verified(folder)
            invalid=copy.deepcopy(native);invalid['metrics']['joint']['passed']=False
            with mock.patch('snpfit.native._run_native') as run,self.assertRaisesRegex(ValueError,'verify'):
                build_symbol(invalid,c,plan())
            run.assert_not_called()

    def test_symbol_pre_cancel_never_starts_native_or_creates_target(self):
        with tempfile.TemporaryDirectory() as folder:
            c,native,p=self._verified(folder)
            output=Path(folder)/'cancelled-symbol';event=threading.Event();event.set()
            with mock.patch('snpfit.native._run_native') as run,self.assertRaisesRegex(RuntimeError,'cancelled before'):
                build_symbol(native,c,plan(),out_dir=output,cancel=event)
            run.assert_not_called();self.assertFalse(output.exists())
            with mock.patch('snpfit.native._run_native') as run,self.assertRaisesRegex(RuntimeError,'cancelled before'):
                build_symbol(native,c,plan(),out_dir=output,cancel=lambda:True)
            run.assert_not_called();self.assertFalse(output.exists())
            oa=Path(native['library_path'])/native['cell']/'schematic'/'sch.oa';oa.write_bytes(b'user edited schematic')
            with mock.patch('snpfit.native._run_native') as run,self.assertRaisesRegex(ValueError,'changed'):
                build_symbol(native,c,plan())
            run.assert_not_called()

    def test_symbol_cannot_pass_if_native_step_changes_schematic(self):
        with tempfile.TemporaryDirectory() as folder:
            c,native,p=self._verified(folder)
            def symbol(executable,script,work,*args,**kwargs):
                (work/'visual-receipt.txt').write_text(receipt_text(p,'SYMBOL'))
                (Path(native['library_path'])/native['cell']/'schematic'/'sch.oa').write_bytes(b'changed')
                return {'status':'completed','returncode':0},'SNPFIT_SYMBOL_CREATED test visual_cell\n'
            with mock.patch('snpfit.native._run_native',side_effect=symbol),self.assertRaisesRegex(RuntimeError,'changed schematic'):
                build_symbol(native,c,plan())


if __name__=='__main__':unittest.main()
