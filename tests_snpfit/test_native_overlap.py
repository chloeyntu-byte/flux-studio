"""Overlapping native profiles, finite placement and unchanged fit/UI data."""
import copy
import json
import math
import unittest

from snpfit.models import list_models, get_model
from snpfit.native_crossings import segment_intersection
from snpfit.native_outline import project, _segments, _distance
from snpfit.native_overlap import transformer_style, crossing_keepouts
from snpfit.native_visual import validate_visual_plan
from snpfit.winding_visual import build_visual
from test_winding_visual import circuit_for


def turns(plan, symbol=False):
    if symbol:
        paths=plan['symbol']['paths']
        return {name:next(p['points'] for p in paths if p.get('center_path')=='main_'+letter
                          and p.get('side')=='left')
                for name,letter in (('primary','P'),('secondary','S'))}
    return {p['winding']:p['points'] for p in plan['center_paths'] if p['role']=='winding'}


def arclength(point, path):
    for start, stop, a, b, tangent in _segments(path)[0]:
        distance=_distance(a,point)
        if abs(distance+_distance(point,b)-(stop-start))<1e-7:
            return start+distance
    raise AssertionError('Placement is outside its winding center path.')


class NativeOverlap(unittest.TestCase):
    def test_all_twentyfour_models_in_both_styles_preserve_graph_and_ui(self):
        self.assertEqual(len(list_models()),24)
        for spec in list_models():
            for style in ('coplanar','stacked'):
                for skin in (False,True):
                    with self.subTest(model=spec.id,style=style,skin=skin):
                        circuit=circuit_for(spec.id,skin=skin)
                        raw=build_visual(circuit);raw['native_transformer_style']=style
                        original=copy.deepcopy(circuit);oldraw=copy.deepcopy(raw)
                        plan=validate_visual_plan(circuit,raw)
                        self.assertEqual(circuit,original);self.assertEqual(raw,oldraw)
                        self.assertEqual(plan,validate_visual_plan(circuit,plan))
                        self.assertEqual({p['name'] for p in plan['placements']},
                                         {e['name'] for e in original['elements']})
                        self.assertEqual({p['name'] for p in plan['pins']},
                                         set(spec.port_roles)|({'REF'} if spec.explicit_reference else set()))
                        self.assertEqual(plan['native_report']['frequencies_hz'],[5e9,10e9,20e9])
                        self.assertFalse(any(p['role']=='coupling_guide' for p in plan['paths']))
                        for path in plan['center_paths']:
                            edges=[p for p in plan['paths'] if p.get('center_path')==path['id']]
                            self.assertEqual({p['side'] for p in edges},{'left','right'})
                        json.dumps(plan,allow_nan=False)

    def test_coplanar_alternates_half_radii_and_stacked_has_two_full_radii(self):
        circuit=circuit_for('transformer_dm_dual_tap_gnd')
        for style in ('coplanar','stacked'):
            raw=build_visual(circuit);raw['native_transformer_style']=style
            plan=project(circuit,raw)
            for symbol in (False,True):
                paths=turns(plan,symbol=symbol);p=paths['primary'];s=paths['secondary']
                self.assertLess(min(x for x,y in p),min(x for x,y in s))
                if style=='coplanar':
                    self.assertLess(max(x for x,y in p),max(x for x,y in s))
                else:
                    self.assertGreater(max(x for x,y in p),max(x for x,y in s))
                # Both windings surround the same origin and physically overlap
                # in projection; neither was translated into a separate loop.
                self.assertLess(min(x for x,y in p),0);self.assertGreater(max(x for x,y in p),0)
                self.assertLess(min(x for x,y in s),0);self.assertGreater(max(x for x,y in s),0)
                self.assertGreater(min(max(x for x,y in p),max(x for x,y in s)),
                                   max(min(x for x,y in p),min(x for x,y in s)))
            for key in ('pins',):
                for pins in (plan[key],plan['symbol'][key]):
                    byname={pin['name']:pin for pin in pins}
                    for name in ('P1','P2'):
                        self.assertGreater(byname[name]['position'][1],0)
                        self.assertEqual(byname[name]['direction'],'top')
                    for name in ('S1','S2'):
                        self.assertLess(byname[name]['position'][1],0)
                        self.assertEqual(byname[name]['direction'],'bottom')
                    self.assertEqual(byname['PTAP']['position'][0],0.)
                    self.assertEqual(byname['STAP']['position'][0],0.)
            self.assertEqual(plan['native_transform']['winding_center'],[0.,0.])

    def test_coplanar_has_radial_bridges_instead_of_nested_rings(self):
        circuit=circuit_for('transformer_dm_gnd')
        raw=build_visual(circuit);plan=project(circuit,raw)
        for winding,path in turns(plan).items():
            bridges=[(a,b) for a,b in zip(path,path[1:]) if a[0]<0<b[0]
                     and a[1]!=b[1]]
            self.assertEqual(len(bridges),1)
            a,b=bridges[0]
            self.assertAlmostEqual(abs(a[0]-b[0]),abs(a[1]-b[1]))
            self.assertTrue(all(y<0 for x,y in (a,b)) if winding=='primary'
                            else all(y>0 for x,y in (a,b)))

    def test_native_profile_selection_and_invalid_choice(self):
        self.assertEqual(transformer_style({}),'coplanar')
        self.assertEqual(transformer_style({'layout_metadata':{'geometry_kind':'stacked_transformer'}}),'stacked')
        self.assertEqual(transformer_style({'native_transformer_style':'coplanar',
                                           'layout_metadata':{'geometry_kind':'stacked_transformer'}}),'coplanar')
        with self.assertRaisesRegex(ValueError,'coplanar or stacked'):
            transformer_style({'native_transformer_style':'automatic'})

    def test_annotations_face_outside_outer_halves_and_inside_inner_halves(self):
        circuit=circuit_for('transformer_dm_dual_tap_gnd')
        for style in ('coplanar','stacked'):
            raw=build_visual(circuit);raw['native_transformer_style']=style;plan=project(circuit,raw)
            for item in plan['placements']:
                if item.get('group')!='winding':continue
                label=next(v for v in plan['labels'] if v['role']=='device_annotation' and v['text']==item['name'])
                # Name is .125 above the common name/value anchor.
                anchor=[label['position'][0],label['position'][1]-.125]
                x,y=item['center'];radial=sum((anchor[i]-item['center'][i])*item['center'][i] for i in (0,1))
                outer=(item['winding']=='primary' if style=='stacked' else
                       (item['winding']=='primary' and x<0) or (item['winding']=='secondary' and x>0))
                path=turns(plan)[item['winding']];segments,_=_segments(path)
                arc=arclength(item['center'],path)
                outer=outer or arc<segments[0][1] or arc>segments[-1][0]
                self.assertGreater(radial,0) if outer else self.assertLess(radial,0)

    def test_r_l_bodies_stay_outside_projected_crossovers(self):
        for spec in list_models():
            if spec.device!='transformer':continue
            for style in ('coplanar','stacked'):
                with self.subTest(model=spec.id,style=style):
                    circuit=circuit_for(spec.id);raw=build_visual(circuit)
                    raw['native_transformer_style']=style;plan=project(circuit,raw)
                    for placement in plan['placements']:
                        if placement.get('group')!='winding':continue
                        path=turns(plan)[placement['winding']]
                        arc=arclength(placement['center'],path)
                        blocks=crossing_keepouts(plan['center_paths'],placement['winding'],segment_intersection)
                        # Snapping a cardinal center can shift arclength by at
                        # most half a native grid.  Body margin stays >=.71875.
                        for start,stop in blocks:
                            self.assertFalse(start+.032<arc<stop-.032)

    def test_ordinary_and_tapped_inductor_paths_stay_unchanged(self):
        for spec in list_models():
            if spec.device!='inductor':continue
            circuit=circuit_for(spec.id);raw=build_visual(circuit)
            plan=project(circuit,raw);scale=plan['native_transform']['schematic_coordinate_scale']
            expected=[{**copy.deepcopy(p),'points':[[round(v*scale/.0625)*.0625 for v in point]
                         for point in p['points']]} for p in raw['paths'] if p['role']in('winding','tap')]
            self.assertEqual(plan['center_paths'],expected)

    def test_independent_transformer_ref_clears_main_labels_and_inductor_ref_stays_put(self):
        for model in ('transformer_se_gnd','transformer_dm_dual_tap_gnd','inductor_dm_gnd','inductor_dm_tap_gnd'):
            for style in ('coplanar','stacked'):
                with self.subTest(model=model,style=style):
                    circuit=circuit_for(model);raw=build_visual(circuit);raw['native_transformer_style']=style
                    plan=project(circuit,raw)
                    ref=next(p for p in plan['pins'] if p['name']=='REF' and p.get('position_kind')=='unified_style')
                    points=[v for p in plan['center_paths'] if p['role']=='winding' for v in p['points']]
                    self.assertEqual(ref['position'][0],max(v[0] for v in points)+1.)
                    if model.startswith('transformer_'):
                        self.assertEqual(ref['position'][1],max(v[1] for v in points)+.25)
                        for label in plan['labels']:
                            if label['role'] in ('device_annotation','segment_q'):
                                self.assertGreaterEqual(math.dist(ref['position'],label['position']),.5)
                    else:self.assertEqual(ref['position'][1],0.)

    def test_thirty_inductors_and_resistors_expand_finitely_for_both_tf_styles(self):
        spec=get_model('transformer_dm_dual_tap_gnd')
        circuit={'name':'dense','ports':list(spec.port_roles),'reference':'REF',
                 'metadata':{'model_id':spec.id,'export_reference':True},'elements':[]}
        branches=[('P1','PTAP'),('PTAP','P2'),('S1','STAP'),('STAP','S2')]
        index=0
        for branch,(first,last) in enumerate(branches):
            count=8 if branch<2 else 7
            for number in range(count):
                a=first if number==0 else 'n%d_%d'%(branch,number)
                b=last if number==count-1 else 'n%d_%d'%(branch,number+1)
                mid='rnode%d'%index
                circuit['elements'].extend([{'name':'rw%d_%d'%(branch,number),'kind':'res','n1':a,'n2':mid,'value':1.},
                    {'name':'lw%d_%d'%(branch,number),'kind':'ind','n1':mid,'n2':b,'value':1e-10}])
                index+=1
        for style in ('coplanar','stacked'):
            raw=build_visual(circuit);raw['native_transformer_style']=style;plan=project(circuit,raw)
            main=[p for p in plan['placements'] if p.get('group')=='winding']
            self.assertEqual(len(main),60)
            self.assertGreater(plan['native_transform']['schematic_coordinate_scale'],.25)
            self.assertLess(plan['native_transform']['schematic_coordinate_scale'],3.)
            for winding in ('primary','secondary'):
                placements=[p for p in main if p['winding']==winding]
                self.assertGreaterEqual(min(math.dist(a['center'],b['center']) for i,a in enumerate(placements)
                                             for b in placements[i+1:]),.7)


if __name__=='__main__':unittest.main()
