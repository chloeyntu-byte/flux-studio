"""Electrical crossing isolation, including device keepouts and real anchors."""
import copy
import unittest

from snpfit.native_crossings import (different_net_intersections, protect_wires,
                                     segment_intersection)
from snpfit.native_visual import _read_wire_receipt, validate_visual_plan
from snpfit.models import list_models
from snpfit.winding_visual import build_visual
from test_winding_visual import circuit_for


def route(name, net, points):
    return {'id':name, 'net':net, 'points':points,
            'start':{'net':net,'position':points[0]},
            'end':{'net':net,'position':points[-1]}}


class NativeCrossings(unittest.TestCase):
    def test_intersection_includes_vertex_touches_and_collinear_overlap(self):
        self.assertEqual(segment_intersection([-1,0],[1,0],[0,-1],[0,1]),([0.,0.],[0.,0.]))
        self.assertEqual(segment_intersection([-1,0],[1,0],[0,0],[0,1]),([0.,0.],[0.,0.]))
        self.assertEqual(segment_intersection([0,0],[2,0],[1,0],[3,0]),([1.,0.],[2.,0.]))
        self.assertIsNone(segment_intersection([0,0],[1,0],[0,1],[1,1]))

    def test_crossing_splits_one_wire_and_retains_all_original_anchors_and_nets(self):
        original=[route('a','H',[[-2,0],[2,0]]),route('z','V',[[0,-2],[0,2]])]
        before=copy.deepcopy(original)
        actual,summary=protect_wires({'elements':[]},original)
        self.assertEqual(original,before)
        self.assertEqual(len(actual),3)
        self.assertEqual(actual[0],original[0])
        self.assertEqual(actual[1]['start'],original[1]['start'])
        self.assertEqual(actual[2]['end'],original[1]['end'])
        self.assertEqual(actual[1]['end'],{'net':'V','position':[0.,-.375]})
        self.assertEqual(actual[2]['start'],{'net':'V','position':[0.,.375]})
        self.assertEqual({r['net'] for r in actual},{'H','V'})
        self.assertEqual(summary['different_net_crossings_remaining'],0)
        self.assertFalse(different_net_intersections(actual))
        self.assertEqual(protect_wires({'elements':[]},actual)[0],actual)
        reversed_routes,_=protect_wires({'elements':[]},list(reversed(original)))
        self.assertEqual(sorted(actual,key=lambda r:r['id']),sorted(reversed_routes,key=lambda r:r['id']))

    def test_same_net_junction_is_retained_without_a_gap(self):
        routes=[route('a','H',[[-2,0],[2,0]]),route('tap','H',[[0,-2],[0,0]])]
        actual,summary=protect_wires({'elements':[]},routes)
        self.assertEqual(actual,routes)
        self.assertEqual(summary['events'],[])

    def test_foreign_wire_break_preserves_an_existing_same_net_junction(self):
        routes=[route('a','H',[[-2,0],[2,0]]),route('z','H',[[0,-2],[0,2]]),
                route('b','V',[[-2,-2],[2,2]])]
        actual,_=protect_wires({'elements':[]},routes)
        self.assertIn(routes[0],actual)
        self.assertIn(routes[1],actual)
        self.assertFalse(different_net_intersections(actual))

    def test_endpoint_touch_breaks_the_other_wire_and_both_protected_anchors_fail(self):
        routes=[route('a','H',[[-2,0],[2,0]]),route('z','V',[[0,0],[0,2]])]
        actual,_=protect_wires({'elements':[]},routes)
        self.assertEqual(next(r for r in actual if r['id']=='z'),routes[1])
        self.assertFalse(different_net_intersections(actual))
        with self.assertRaisesRegex(ValueError,'protected anchors'):
            protect_wires({'elements':[]},[route('a','H',[[0,0],[2,0]]),route('z','V',[[0,0],[0,2]])])

    def test_multiple_diagonal_crossings_are_grid_aligned_and_merged(self):
        routes=[route('a','H',[[-2,0],[2,0]]),route('b','M',[[-2,.125],[2,.125]]),
                route('z','D',[[-2,-2],[2,2]])]
        actual,_=protect_wires({'elements':[]},routes)
        self.assertFalse(different_net_intersections(actual))
        for wire in actual:
            for p in wire['points']:
                self.assertTrue(all(abs(v/.0625-round(v/.0625))<1e-8 for v in p))

    def test_collinear_overlap_is_removed_as_an_interval(self):
        routes=[route('a','H',[[0,0],[1,0]]),route('z','V',[[-2,0],[3,0]])]
        actual,_=protect_wires({'elements':[]},routes)
        self.assertFalse(different_net_intersections(actual))
        self.assertEqual([r['points'] for r in actual if r['net']=='V'],
                         [[[-2.,0.],[-.375,0.]],[[1.375,0.],[3.,0.]]])

    def test_unrelated_device_body_and_terminals_have_clearance(self):
        circuit={'elements':[{'name':'R','kind':'res','n1':'A','n2':'B','value':1.}]}
        placements=[{'name':'R','orientation':'R0','center':[0,0]}]
        wires,summary=protect_wires(circuit,[route('wire','X',[[0,-2],[0,2]])],placements)
        self.assertEqual([p['points'] for p in wires],[[[0.,-2.],[0.,-.4375]],[[0.,.4375],[0.,2.]]])
        self.assertEqual(summary['events'][0]['kind'],'device_clearance')
        endpoint=route('own','A',[[-2,0],[0,0]])
        endpoint['end']={'component':'R','terminal':'PLUS'}
        self.assertEqual(protect_wires(circuit,[endpoint],placements)[0],[endpoint])
        with self.assertRaisesRegex(ValueError,'protected device/pin'):
            protect_wires(circuit,[route('collision','X',[[0,0],[0,2]])],placements)

    def test_all_model_plans_preserve_circuit_components_and_are_idempotent(self):
        for spec in list_models():
            with self.subTest(model=spec.id):
                circuit=circuit_for(spec.id)
                original=copy.deepcopy(circuit)
                plan=validate_visual_plan(circuit,build_visual(circuit))
                self.assertEqual(circuit,original)
                self.assertEqual({p['name'] for p in plan['placements']},{e['name'] for e in circuit['elements']})
                self.assertFalse(different_net_intersections(plan['winding_wires']))
                self.assertEqual(plan,validate_visual_plan(circuit,plan))

    def test_reopened_actual_wire_crossing_is_rejected_even_with_matching_receipt(self):
        wires=[route('a','H',[[-2,0],[2,0]]),route('z','V',[[0,-2],[0,2]])]
        plan={'winding_wires':wires,'port_order':['H','V'],'native_reference':'REF'}
        receipt='SCHEMATIC_WIRE 0 0 H -2 0 2 0\nSCHEMATIC_WIRE 1 0 V 0 -2 0 2\nSCHEMATIC_WIRES 2 2'
        with self.assertRaisesRegex(RuntimeError,'different nets intersect'):
            _read_wire_receipt(receipt,plan,{})


if __name__=='__main__':
    unittest.main()
