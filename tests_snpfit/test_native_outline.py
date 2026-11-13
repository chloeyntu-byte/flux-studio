"""Native winding geometry, honest wires and unchanged shared UI plans."""
import copy
import json
import math
import unittest

from snpfit.integration_api import _aliased_native
from snpfit.models import get_model, list_models
from snpfit.native_visual import validate_visual_plan, visual_skill_header, _read_wire_receipt
from snpfit.winding_visual import build_visual
from test_winding_visual import circuit_for


def on_cardinal_edge(point,path):
    x,y=point
    return any((abs(a[0]-b[0])<1e-8 and abs(x-a[0])<1e-8 and min(a[1],b[1])-1e-8<=y<=max(a[1],b[1])+1e-8)
               or(abs(a[1]-b[1])<1e-8 and abs(y-a[1])<1e-8 and min(a[0],b[0])-1e-8<=x<=max(a[0],b[0])+1e-8)
               for a,b in zip(path,path[1:]))


class NativeOutline(unittest.TestCase):
    def test_all_models_are_double_edged_and_main_devices_on_flats(self):
        for spec in list_models():
            with self.subTest(model=spec.id):
                circuit=circuit_for(spec.id);original=copy.deepcopy(circuit)
                raw=build_visual(circuit);oldraw=copy.deepcopy(raw)
                plan=validate_visual_plan(circuit,raw)
                self.assertEqual(circuit,original);self.assertEqual(raw,oldraw)
                self.assertEqual(plan,validate_visual_plan(circuit,plan))
                self.assertEqual({p['name'] for p in plan['placements']},{e['name'] for e in circuit['elements']})
                self.assertEqual(plan['port_order'],list(circuit['ports'])+(['REF'] if spec.explicit_reference else []))
                for path in plan['center_paths']:
                    edges=[p for p in plan['paths'] if p.get('center_path')==path['id']]
                    self.assertEqual({e['side'] for e in edges},{'left','right'})
                    self.assertTrue(all(len(e['points'])==len(path['points']) for e in edges))
                    self.assertNotEqual(edges[0]['points'][0],edges[1]['points'][0])
                for placement in plan['placements']:
                    if placement.get('group')!='winding':continue
                    path=next(p for p in plan['center_paths'] if p['role']=='winding' and p['winding']==placement['winding'])
                    self.assertTrue(on_cardinal_edge(placement['center'],path['points']))
                    self.assertIn(placement['orientation'],('R0','R90','R180','R270'))
                drawn=[point for p in plan['paths'] for point in p['points']]+[p['position'] for p in plan['labels']]
                drawn.extend(point for p in plan['symbol']['paths'] for point in p['points'])
                self.assertTrue(all(abs(value/.03125-round(value/.03125))<1e-8 for point in drawn for value in point))
                json.dumps(plan,allow_nan=False)

    def test_transformer_coils_overlap_and_taps_follow_their_windings(self):
        circuit=circuit_for('transformer_dm_dual_tap_gnd')
        for style in ('coplanar','stacked'):
            with self.subTest(style=style):
                raw=build_visual(circuit);raw['native_transformer_style']=style
                plan=validate_visual_plan(circuit,raw)
                p,s=[next(p for p in plan['center_paths'] if p['role']=='winding' and p['winding']==name) for name in ('primary','secondary')]
                self.assertLess(min(a[0] for a in p['points']),max(a[0] for a in s['points']))
                self.assertLess(min(a[0] for a in s['points']),max(a[0] for a in p['points']))
                centers=[(min(a[0] for a in path['points'])+max(a[0] for a in path['points']))/2 for path in (p,s)]
                self.assertAlmostEqual(centers[0]+centers[1],0.)
                if style=='stacked':self.assertAlmostEqual(centers[0],centers[1])
                pins={p['name']:p for p in plan['pins']}
                self.assertGreater(pins['P1']['position'][1],0)
                self.assertLess(pins['S1']['position'][1],0)
                for role in ('PTAP','STAP'):
                    tap=next(p for p in plan['center_paths'] if p['id']=='tap_'+role)
                    self.assertEqual(pins[role]['position'],tap['points'][-1])
                    self.assertNotEqual(tap['points'][0],tap['points'][-1])
                self.assertNotEqual(pins['PTAP']['position'],pins['STAP']['position'])
                self.assertEqual(sum(p.get('role')=='conductor_edge' for p in plan['symbol']['paths']),8)

    def test_wires_use_real_matching_nets_and_do_not_short_skin_loss(self):
        circuit=circuit_for('inductor_dm_tap_gnd')
        plan=validate_visual_plan(circuit,build_visual(circuit));elements={e['name']:e for e in circuit['elements']}
        def node(anchor):
            if 'component' in anchor:
                return elements[anchor['component']]['n1' if anchor['terminal']=='PLUS' else 'n2']
            return anchor.get('pin',anchor.get('net'))
        for route in plan['winding_wires']:
            self.assertEqual(node(route['start']),route['net']);self.assertEqual(node(route['end']),route['net'])
        self.assertTrue(any(r['net']=='TAP' for r in plan['winding_wires']))
        for route in plan['winding_wires']:
            endpoints={route['start'].get('component'),route['end'].get('component')}
            self.assertNotEqual(endpoints,{'lw0_0','rw0_1'})
        header='\n'.join(visual_skill_header(plan,'/tmp/receipt'))
        self.assertIn('snpfitVisualWires=list(',header);self.assertIn('"NET"',header)

    def test_cross_net_route_injection_rejected_before_native_work(self):
        circuit=circuit_for('inductor_dm_gnd');plan=validate_visual_plan(circuit,build_visual(circuit))
        changed=copy.deepcopy(plan);changed['winding_wires'][0]['net']='OTHER'
        with self.assertRaisesRegex(ValueError,'short distinct'):validate_visual_plan(circuit,changed)
        changed=copy.deepcopy(plan);changed['winding_wires'][0]['end']={'component':'lw0_0','terminal':'MINUS'}
        with self.assertRaisesRegex(ValueError,'short distinct'):validate_visual_plan(circuit,changed)

    def test_report_rows_frequency_columns_and_secondary_q_are_separate(self):
        circuit=circuit_for('transformer_se_gnd');raw=build_visual(circuit)
        raw['native_report_context']={'fit_band_hz':[1e9,10e9]}
        plan=validate_visual_plan(circuit,raw)
        headers=[x['text'] for x in plan['labels'] if x['role']=='metric_header']
        self.assertIn('5 GHz',headers);self.assertIn('10 GHz',headers);self.assertIn('20 GHz*',headers)
        self.assertEqual(len(plan['native_report']['rows']),5)
        self.assertEqual(len([x for x in plan['labels'] if x['role']=='metric_value']),15)
        self.assertEqual(len([x for x in plan['labels'] if x['role']=='segment_q']),4)
        self.assertGreater(plan['metric_table_bounds'][0][1],plan['body_bounds'][1][1])
        self.assertIn('k',headers);self.assertNotIn('k (signed)',headers)
        self.assertEqual(next(row['label'] for row in plan['native_report']['rows'] if row['key']=='k'),'k (signed)')

    def test_main_names_values_and_small_q_have_separate_lines(self):
        orientations=set()
        for identity in ('transformer_se_gnd','inductor_dm_gnd','inductor_dm_tap_gnd'):
            circuit=circuit_for(identity);plan=validate_visual_plan(circuit,build_visual(circuit))
            main=[p for p in plan['placements'] if p.get('group')=='winding']
            annotations=[v for v in plan['labels'] if v['role']=='device_annotation']
            self.assertEqual(len(annotations),2*len(main))
            for placement in main:
                orientations.add(placement['orientation'])
                name=next(v for v in annotations if v['text']==placement['name'])
                value=next(v for v in annotations if v['position']==[name['position'][0],name['position'][1]-.25])
                self.assertNotIn('=',value['text'])
                self.assertEqual(name['justification'],value['justification'])
                if placement['kind']!='ind':continue
                row=next(r for r in plan['segments'] if r['id']==placement['segment'])
                q=next(v for v in plan['labels'] if v['role']=='segment_q' and v['text'].startswith(row['display_tag']+'  Q='))
                if placement['orientation'] in ('R90','R270'):
                    self.assertGreaterEqual(value['position'][1]-q['position'][1],.375)
                else:
                    self.assertGreater(abs(q['position'][1]-placement['center'][1]),
                                       max(abs(v['position'][1]-placement['center'][1]) for v in (name,value))+.25)
        self.assertTrue(orientations.intersection(('R0','R180')))
        self.assertTrue(orientations.intersection(('R90','R270')))

    def test_aliases_keep_geometry_term_identity_and_wire_nodes(self):
        for identity in ('transformer_se_dual_tap_gnd','transformer_dm_dual_tap_gnd'):
            circuit=circuit_for(identity);raw=build_visual(circuit)
            aliases={role:'PORT'+str(index) for index,role in enumerate(circuit['ports'],1)}
            aliased,aplan=_aliased_native(circuit,raw,aliases)
            normal=validate_visual_plan(circuit,raw);actual=validate_visual_plan(aliased,aplan)
            for pin in normal['pins']:
                matching=next(p for p in actual['pins'] if p['name']==aliases.get(pin['name'],pin['name']) and p['position']==pin['position'])
                self.assertEqual(matching['direction'],pin['direction'])
            self.assertEqual(actual['port_order'],list(aliases.values())+['REF'])
            for route in actual['winding_wires']:
                for end in ('start','end'):
                    if 'pin' in route[end]:self.assertIn(route[end]['pin'],actual['port_order'])
            self.assertEqual(normal['center_paths'],actual['center_paths'])

    def test_dense_finite_model_expands_without_overlapping_main_glyphs(self):
        circuit={'name':'dense','ports':['P','N'],'reference':'REF',
                 'metadata':{'model_id':'inductor_dm_gnd','export_reference':True},'elements':[]}
        for index in range(30):
            a='P' if index==0 else 'node%d'%index;b='N' if index==29 else 'node%d'%(index+1);mid='r%d'%index
            circuit['elements'].extend([{'name':'rw0_%d'%index,'kind':'res','n1':a,'n2':mid,'value':1.},
                                       {'name':'lw0_%d'%index,'kind':'ind','n1':mid,'n2':b,'value':1e-10}])
        plan=validate_visual_plan(circuit,build_visual(circuit))
        main=[p for p in plan['placements'] if p.get('group')=='winding']
        self.assertEqual(len(main),60);self.assertGreater(plan['native_transform']['schematic_coordinate_scale'],.25)
        self.assertLess(plan['native_transform']['schematic_coordinate_scale'],2)
        self.assertGreaterEqual(min(math.dist(a['center'],b['center']) for index,a in enumerate(main) for b in main[index+1:]),.7)

    def test_saved_wires_need_actual_anchors_geometry_and_exact_net(self):
        circuit=circuit_for('transformer_se',skin=False);plan=validate_visual_plan(circuit,build_visual(circuit))
        terminals={}
        for placement in plan['placements']:
            if placement['kind']=='mind':continue
            angle=math.radians(int(placement['orientation'][1:]));direction=[math.cos(angle),math.sin(angle)]
            for term,sign in (('PLUS',-1),('MINUS',1)):
                terminals[(placement['name'],term)]=[placement['center'][i]+sign*.1875*direction[i] for i in (0,1)]
        def anchor(value):return terminals[(value['component'],value['terminal'])] if 'component' in value else value['position']
        lines=[];count=0
        for index,route in enumerate(plan['winding_wires']):
            points=copy.deepcopy(route['points']);points[0]=anchor(route['start']);points[-1]=anchor(route['end'])
            net='gnd!' if route['net']=='REF' else route['net']
            for number,(a,b) in enumerate(zip(points,points[1:])):
                # Deliberately reverse OA segment order: wires are undirected.
                lines.append('SCHEMATIC_WIRE %d %d %s %.12g %.12g %.12g %.12g'%(index,number,net,b[0],b[1],a[0],a[1]));count+=1
        lines.append('SCHEMATIC_WIRES %d %d'%(len(plan['winding_wires']),count));text='\n'.join(lines)
        proof=_read_wire_receipt(text,plan,terminals)
        self.assertTrue(proof['geometry_and_nets_verified_after_reopen'])
        with self.assertRaisesRegex(RuntimeError,'geometry or net'):_read_wire_receipt(text.replace(' gnd! ',' OTHER '),plan,terminals)
        with self.assertRaisesRegex(RuntimeError,'count'):_read_wire_receipt('\n'.join(lines[1:]),plan,terminals)


if __name__=='__main__':unittest.main()
