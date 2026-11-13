"""Representative geometry and native-via contracts from four synthetic fixtures."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import geometry
from passive_symmetric import generate_differential


def profile(name):
    return json.loads((ROOT/'tests'/'fixtures'/(name+'.json')).read_text())


def select(tech):
    m=sorted(tech['layers'],key=lambda rule:rule['order'])
    return dict(body=m[-2]['name'],crossing=m[-3]['name'],tap=m[-4]['name'],
                pgs=m[0]['name'],ring=m[-1]['name'],ground_route=m[1]['name'])


class SymmetricInductorTest(unittest.TestCase):
    def test_four_generic_stacks_odd_even_turns_and_optional_midpoint(self):
        for name in ('generic_a','generic_d','generic_b','generic_c'):
            tech=profile(name); layers=select(tech)
            valid_vias={v['name'] for v in tech['vias']}
            for turns in (1,2,3,4,8):
                for tap in (False,True):
                    with self.subTest(pdk=name,turns=turns,tap=tap):
                        result=generate_differential(dict(turns=turns,center_tap=tap,ring_extent=250),layers,tech)
                        self.assertTrue(result['ok'],result['errors'])
                        g=result['geometry']; m=g['metrics']
                        self.assertEqual(m['winding_components'],{'L':1})
                        self.assertEqual(m['crossover_count'],turns-1)
                        arcs=[p for p in g['polygons'] if p.get('feature')=='symmetric_half_turn']
                        self.assertEqual({(p['turn_index'],p['side']) for p in arcs},
                            {(i,side) for i in range(turns) for side in ('left','right')})
                        ports={p['name']:p for p in g['ports']}
                        self.assertEqual({x for x in ports if x in ('L1','L2','CTAP')}, {'L1','L2'}|({'CTAP'} if tap else set()))
                        self.assertEqual(ports['L1']['access_direction'],'bottom')
                        self.assertEqual(ports['L2']['endpoint'],'negative')
                        self.assertEqual(m['half_path_lengths']['L1_to_midpoint'],m['half_path_lengths']['L2_to_midpoint'])
                        join=[p for p in g['polygons'] if p.get('feature')=='center_join']
                        self.assertTrue(any(geometry._point_inside(m['tap_midpoint'],p['points']) for p in join))
                        self.assertEqual(m['center_join_side'],'top' if turns%2 else 'bottom')
                        self.assertTrue(all(v['via_def'] in valid_vias for v in g['vias']))
                        if tap:
                            self.assertEqual(ports['CTAP']['layer'],layers['tap'])
                            self.assertEqual(ports['CTAP']['access_direction'],'top')
                            self.assertTrue(any(p.get('feature')=='tap_route' for p in g['polygons']))
                        else:
                            self.assertFalse(any(p.get('feature')=='tap_route' for p in g['polygons']))
                        if turns==1 and not tap: self.assertFalse(g['vias'])

    def test_bad_dimensions_layer_conflict_and_unusable_vias_fail_closed(self):
        t=profile('generic_d'); l=select(t)
        for overrides,layer_change,expected in [
            ({'turns':2.5},{},'parameter_range'),
            ({'center_tap':True},{'tap':l['body']},'same_tap_layer'),
            ({'turns':2,'width':.2},{},'via_width'),
            ({'turns':2,'terminal_gap':180},{},'crossover_room'),
            ({'turns':2,'spacing':.1,'rule_policy':'strict'},{},'turn_spacing')]:
            with self.subTest(expected=expected):
                r=generate_differential(overrides,dict(l,**layer_change),t)
                self.assertFalse(r['ok'])
                self.assertIn(expected,{e['code'] for e in r['errors']})
        broken=dict(t,vias=[])
        r=generate_differential({'turns':2},l,broken)
        self.assertFalse(r['ok'])
        self.assertIn('no_native_via_chain',{e['code'] for e in r['errors']})

    def test_wide_gap_either_passes_or_reports_room_not_short(self):
        t=profile('generic_a'); l=select(t)
        for gap in (8,24,48,70):
            r=generate_differential({'turns':4,'terminal_gap':gap,'ring_extent':220},l,t)
            if r['ok']:
                self.assertEqual(r['geometry']['metrics']['winding_components'],{'L':1})
            else:
                self.assertTrue(any(e['code'] in ('crossover_room','same_net_shortcut','endpoint_shortcut') for e in r['errors']),r['errors'])


if __name__=='__main__': unittest.main()
