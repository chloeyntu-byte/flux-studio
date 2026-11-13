"""Geometry provenance, pin identity and electrical invariance of visual plans."""
import copy
import json
import math
from pathlib import Path
import unittest

from snpfit.models import get_model, list_models
from snpfit.winding_visual import build_visual, reference_catalog, REFERENCE_DIR, SCHEMATIC_SCALE, SYMBOL_SCALE


def circuit_for(model_id, skin=True):
    spec=get_model(model_id)
    circuit={'name':'visual_test','ports':list(spec.port_roles),'reference':'REF','elements':[],
             'metadata':{'model_id':model_id,'export_reference':spec.explicit_reference}}
    branch=0
    for wind,(first,last) in enumerate(spec.winding_pairs):
        tap='TAP' if spec.device=='inductor' else ('PTAP' if wind==0 else 'STAP')
        chain=[first,tap,last] if tap in spec.tap_roles else [first,last]
        for start,stop in zip(chain,chain[1:]):
            midpoint='node%d'%branch
            for section,(a,b) in enumerate([(start,midpoint),(midpoint,stop)]):
                suffix='w%d_%d'%(branch,section);rnode=suffix+'_r';lnode=suffix+'_l'
                circuit['elements'].append({'kind':'res','name':'r'+suffix,'n1':a,'n2':rnode,'value':1.+section})
                circuit['elements'].append({'kind':'ind','name':'l'+suffix,'n1':rnode,'n2':lnode if skin else b,'value':1e-9})
                if skin:
                    circuit['elements'].extend([
                        {'kind':'ind','name':'lskin'+suffix,'n1':lnode,'n2':b,'value':.2e-9},
                        {'kind':'res','name':'rskin'+suffix,'n1':lnode,'n2':b,'value':3.},
                    ])
            branch+=1
    circuit['elements'].append({'kind':'cap','name':'cg0','n1':first,'n2':'REF','value':1e-14})
    mains=[e['name'] for e in circuit['elements'] if e['kind']=='ind' and e['name'].startswith('lw')]
    if len(mains)>1:
        circuit['elements'].append({'kind':'mind','name':'k01','ind1':mains[0],'ind2':mains[1],'value':.1})
    return circuit


def reference(identity):
    return json.loads((REFERENCE_DIR/(identity.replace('/','__')+'.json')).read_text())


def rounded(point,scale):
    return [round(value*scale/.0625)*.0625 for value in point]


class WindingVisual(unittest.TestCase):
    def test_twelve_synthetic_references_preserve_paths_pin_faces_and_provenance(self):
        catalog=reference_catalog()
        self.assertEqual(len(catalog),12)
        self.assertEqual({r['pdk'] for r in catalog},{'generic_a','generic_d','generic_b','generic_c'})
        self.assertEqual(sum(len(reference(r['id'])['pins']) for r in catalog),60)
        for item in catalog:
            r=reference(item['id']);origin=r['normalization']['origin_um'];scale=r['normalization']['scale_um']
            self.assertEqual(len(r['provenance']['generator_sha256']),64)
            self.assertEqual(len(r['provenance']['geometry_sha256']),64)
            self.assertTrue(all(len(h)==64 for h in r['source_snp_sha256']))
            for pin in r['pins']:
                recovered=[x*scale+o for x,o in zip(pin['point'],origin)]
                self.assertLess(math.dist(recovered,pin['point_um']),1e-6)
            by_index={p['file_port']:p for p in r['pins'] if p['file_port']}
            for winding in r['windings']:
                self.assertEqual(winding['path'][0],by_index[winding['positive_file_port']]['point'])
                self.assertEqual(winding['path'][-1],by_index[winding['negative_file_port']]['point'])
            for tap in r['tap_paths']:
                self.assertAlmostEqual(tap['attachment_fraction'],.5,places=8)

    def test_all_twentyfour_models_cover_every_element_and_only_exported_terms(self):
        for spec in list_models():
            with self.subTest(model=spec.id):
                circuit=circuit_for(spec.id);before=copy.deepcopy(circuit)
                plan=build_visual(circuit,q_frequency_hz=3e9)
                self.assertEqual(circuit,before)
                names=[p['name'] for p in plan['placements']]
                self.assertEqual(len(names),len(set(names)))
                self.assertEqual(set(names),{e['name'] for e in circuit['elements']})
                expected=set(spec.port_roles)|({'REF'} if spec.explicit_reference else set())
                self.assertEqual({p['name'] for p in plan['pins']},expected)
                self.assertEqual({p['name'] for p in plan['symbol']['pins']},expected)
                for placement in plan['placements']:
                    self.assertTrue(all(abs(value/.0625-round(value/.0625))<1e-9 for value in placement['center']))
                    if placement['name'].startswith(('rw','lw')):
                        self.assertEqual(placement['group'],'winding')
                    if placement['name'].startswith(('lskin','rskin')):
                        self.assertEqual(placement['group'],'loss_auxiliary')
                q=[r['q'] for r in plan['top10'] if r['q'] is not None]
                self.assertEqual(q,sorted(q))
                self.assertLessEqual(len(plan['top10']),10)
                json.dumps(plan,allow_nan=False)

    def test_source_reference_and_port_swap_do_not_change_standard_style(self):
        r=reference('generic_b/inductor');sha=r['source_snp_sha256'][0]
        c=circuit_for('inductor_dm_gnd')
        p=build_visual(c,source_sha256=sha,port_order=[2,1],q_frequency_hz=1e9)
        baseline=build_visual(c,reference_id='template',port_order=[1,2],q_frequency_hz=1e9)
        self.assertEqual(p['mapping_kind'],'unified_single_turn')
        self.assertEqual([(x['name'],x['position']) for x in p['pins']],[(x['name'],x['position']) for x in baseline['pins']])
        self.assertEqual(p['paths'],baseline['paths'])
        self.assertEqual(next(x for x in p['pins'] if x['name']=='P')['file_port'],2)
        self.assertEqual(p['provenance']['source_port_order'],[2,1])
        self.assertTrue(all(pin['position'][1]<0 for pin in p['pins'] if pin['name']in('P','N')))

    def test_transformer_se_returns_share_ref_in_standard_top_bottom_slots(self):
        r=reference('generic_a/transformer');c=circuit_for('transformer_se_gnd')
        p=build_visual(c,source_sha256=r['source_snp_sha256'][0],port_order=[1,3],short_unused=True)
        refs=[pin for pin in p['pins'] if pin['name']=='REF']
        self.assertEqual(len(refs),3)
        self.assertTrue(any(pin.get('model_return')=='P' and pin['position'][1]>0 for pin in refs))
        self.assertTrue(any(pin.get('model_return')=='S' and pin['position'][1]<0 for pin in refs))
        self.assertTrue(any(pin['position'][0]>0 and pin['position'][1]==0 for pin in refs))
        self.assertGreater(next(pin for pin in p['pins'] if pin['name']=='P')['position'][1],0)
        self.assertLess(next(pin for pin in p['pins'] if pin['name']=='S')['position'][1],0)
        hidden=circuit_for('transformer_se')
        p=build_visual(hidden,source_sha256=r['source_snp_sha256'][0],port_order=[1,3],short_unused=True)
        self.assertNotIn('REF',{pin['name'] for pin in p['pins']})
        self.assertEqual(len(p['internal_returns']),2)
        self.assertTrue(any(path['role']=='internal_reference' for path in p['symbol']['paths']))

    def test_tap_branch_allocation_uses_fixed_top_center_not_source_offset(self):
        r=reference('generic_c/tapped_inductor');c=circuit_for('inductor_dm_tap_gnd')
        p=build_visual(c,source_sha256=r['source_snp_sha256'][0],port_order=[1,2,3])
        ranges=p['segment_ranges']
        self.assertEqual(ranges['lw0_0']['start_fraction'],0.)
        self.assertAlmostEqual(ranges['lw0_1']['stop_fraction'],.5)
        self.assertAlmostEqual(ranges['lw1_0']['start_fraction'],.5)
        self.assertEqual(ranges['lw1_1']['stop_fraction'],1.)
        actual=next(x for x in r['pins'] if x['name']=='CTAP')
        self.assertGreater(actual['point_um'][0],4.)
        self.assertEqual(next(x for x in p['pins'] if x['name']=='TAP')['position'],[0.,19.])

    def test_tapped_transformer_keeps_unified_single_turn_and_fixed_taps(self):
        c=circuit_for('transformer_dm_dual_tap_gnd')
        p=build_visual(c,source_sha256='unknown',port_order=[1,2,3,4,5,6])
        self.assertEqual(p['mapping_kind'],'unified_single_turn')
        self.assertEqual(p['provenance']['winding_style'],'single_turn')
        self.assertEqual({pin['name'] for pin in p['pins']},{'P1','P2','S1','S2','PTAP','STAP','REF'})
        self.assertEqual(len([path for path in p['paths'] if path['role']=='tap']),2)
        self.assertEqual(next(pin for pin in p['pins'] if pin['name']=='PTAP')['position'],[0.,19.])
        self.assertEqual(next(pin for pin in p['pins'] if pin['name']=='STAP')['position'],[0.,-19.])

    def test_four_generic_reference_choices_have_identical_style_coordinates(self):
        for model in ('inductor_dm_gnd','inductor_dm_tap_gnd','transformer_dm_gnd'):
            c=circuit_for(model);base=build_visual(c,reference_id='unified')
            for choice in reference_catalog(model):
                other=build_visual(c,reference_id=choice['id'],source_sha256=choice['source_snp_sha256'][0])
                for field in ('paths','placements','pins','symbol','segment_ranges','plot_bounds'):
                    self.assertEqual(base[field],other[field],(model,choice['id'],field))

    def test_external_loss_cards_and_segment_paths_remain_associated(self):
        c=circuit_for('transformer_dm_gnd');p=build_visual(c)
        cards={line['segment']:line for line in p['paths'] if line['role']=='annotation_box'}
        associations={line['segment']:line for line in p['paths'] if line['role']=='association'}
        segments={line['segment']:line for line in p['paths'] if line['role']=='segment'}
        self.assertEqual(set(cards),set(associations))
        self.assertEqual(set(cards),set(segments))
        for item in p['placements']:
            if item['group']!='loss_auxiliary':continue
            points=cards[item['segment']]['points'];x,y=item['center']
            self.assertGreaterEqual(x,min(q[0] for q in points));self.assertLessEqual(x,max(q[0] for q in points))
            self.assertGreaterEqual(y,min(q[1] for q in points));self.assertLessEqual(y,max(q[1] for q in points))
        self.assertGreater(p['bounds'][2],p['plot_bounds'][2])

    def test_saved_circuit_model_wins_over_stale_form_model(self):
        c=circuit_for('inductor_dm_gnd')
        p=build_visual(c,model_id='transformer_dm_gnd')
        self.assertEqual(p['model_id'],'inductor_dm_gnd')
        self.assertEqual(len([path for path in p['paths'] if path['role']=='winding']),1)

    def test_invalid_visual_inputs_fail_without_mutating_electrical_model(self):
        c=circuit_for('inductor_dm_gnd');before=copy.deepcopy(c)
        for value in (0,-1,float('nan'),float('inf')):
            with self.subTest(frequency=value),self.assertRaisesRegex(ValueError,'frequency'):
                build_visual(c,q_frequency_hz=value)
        with self.assertRaisesRegex(ValueError,'Unknown layout reference'):
            build_visual(c,reference_id='unknown-reference')
        r=reference('generic_b/inductor')
        with self.assertRaisesRegex(ValueError,'port count'):
            build_visual(c,port_order=[1])
        self.assertEqual(c,before)


if __name__=='__main__':
    unittest.main()
