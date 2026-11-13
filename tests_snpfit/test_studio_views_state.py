"""Independent contracts for session identity and exact circuit/curve views."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from matplotlib.figure import Figure
from snpfit.studio_state import default_config, new_page, save_session, load_session, model_label, topology_label
from snpfit.studio_views import comparison_series, circuit_diff, circuit_layout, draw_circuit


class StudioStateTests(unittest.TestCase):
    def page(self, directory):
        source = Path(directory)/'measured.s2p'
        source.write_text('# GHz S RI R 50\n1 0 0 0 0 0 0 0 0\n',encoding='utf-8')
        page=new_page('First circuit')
        page['config']['source_path']=str(source)
        page['source']={'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
        page['source_valid']=True
        page['results']['current']={'id':'c1','passed':True,'native_verified':True,'curves':{'freq_hz':[1e9],'source':{'winding':{'l_h':[1e-9],'q':[2.],'r_ohm':[3.]}},'fitted':{}}}
        return page,source

    def test_roundtrip_every_config_and_multiple_independent_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            page,source=self.page(directory);page['config']['tune_seconds']='draft-invalid'
            page['view']={'axis_limits':[[1,2,3,4]],'legend_visibility':{'Source':False},'diagram_scale':'150%'}
            second=new_page('Second circuit');second['config']['fmin_ghz']='4';second['logs']=['Independent page']
            path=Path(directory)/'session.json'
            save_session(path,[page,second],second['id'],{'geometry':'1280x800'})
            loaded=load_session(path)
            self.assertEqual(loaded['active_id'],second['id']);self.assertEqual(loaded['pages'][0]['config'],page['config'])
            self.assertEqual(loaded['pages'][0]['view'],page['view']);self.assertTrue(loaded['pages'][0]['results']['current']['passed'])
            loaded['pages'][0]['config']['fmin_ghz']='9';self.assertEqual(loaded['pages'][1]['config']['fmin_ghz'],'4')
            self.assertEqual(loaded['window_state']['geometry'],'1280x800')

    def test_changed_and_missing_source_invalidates_acceptance_but_keeps_curves(self):
        with tempfile.TemporaryDirectory() as directory:
            page,source=self.page(directory);path=Path(directory)/'session.json';save_session(path,[page],page['id'])
            source.write_text('changed',encoding='utf-8');loaded=load_session(path);p=loaded['pages'][0]
            self.assertFalse(p['source_valid']);self.assertTrue(p['results_stale']);self.assertFalse(p['results']['current']['passed'])
            self.assertTrue(p['results']['current']['historical_passed']);self.assertTrue(p['results']['current']['curves'])
            source.unlink();self.assertEqual(load_session(path)['pages'][0]['source_validation']['status'],'missing')

    def test_backup_atomic_and_source_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            page,source=self.page(directory);path=Path(directory)/'session.json';save_session(path,[page],page['id'])
            old=path.read_bytes();result=save_session(path,[page],page['id']);self.assertEqual(Path(result['backup']).read_bytes(),old)
            original=source.read_bytes()
            with self.assertRaisesRegex(ValueError,'overwrite'):save_session(source,[page],page['id'])
            self.assertEqual(source.read_bytes(),original)

    def test_reject_invalid_json_and_nonfinite_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.json';path.write_text('{"schema":"snpfit-studio-session","version":99}')
            with self.assertRaisesRegex(ValueError,'version'):load_session(path)
            page=new_page();page['unexpected'] = float('nan')
            with self.assertRaisesRegex(ValueError,'non-finite'):save_session(path,[page],page['id'])
            page=new_page()
            with self.assertRaisesRegex(ValueError,'unique'):save_session(path,[page,copy.deepcopy(page)],page['id'])


class StudioDiagramTests(unittest.TestCase):
    def circuit(self):
        return {'name':'handwritten','ports':['P','N'],'reference':'REF','metadata':{'export_reference':True},'elements':[
            {'kind':'res','name':'rw0_0','n1':'P','n2':'w0_0_r','value':2.},
            {'kind':'ind','name':'lw0_0','n1':'w0_0_r','n2':'N','value':1e-9},
            {'kind':'cap','name':'c','n1':'P','n2':'REF','value':1e-13},
            {'kind':'res','name':'rref','n1':'N','n2':'REF','value':1000.}]}

    def test_exact_graph_draws_every_instance_and_shared_reference(self):
        circuit=self.circuit();fig=Figure();ax=fig.add_subplot(111);drawn=draw_circuit(ax,circuit)
        self.assertEqual(set(drawn['elements']),{e['name'] for e in circuit['elements']})
        self.assertEqual(set(drawn['nodes']),{'P','N','REF','w0_0_r'})
        self.assertEqual(circuit['elements'][0]['value'],2.)
        layout=circuit_layout(circuit);self.assertEqual(layout['nodes']['P'][1],layout['nodes']['N'][1])

    def test_diff_includes_added_removed_values_and_rewiring(self):
        before=self.circuit();after=copy.deepcopy(before)
        after['elements'][0]['value']=3.;after['elements'][1]['n2']='REF';after['elements'].pop(2)
        after['elements'].append({'kind':'cap','name':'newcap','n1':'P','n2':'N','value':2e-13})
        diff=circuit_diff(before,after)
        self.assertEqual(diff['added'],['newcap']);self.assertEqual(diff['removed'],['c'])
        self.assertEqual(diff['changed'],['lw0_0','rw0_0']);self.assertEqual(diff['rewired'],['lw0_0'])

    def test_three_versions_and_actual_sample_points_no_extra_quick_curve(self):
        curve={'freq_hz':[1e9,2e9],'source':{'winding':{'l_h':[1e-9,2e-9],'q':[3,4],'r_ohm':[2,3]}},'fitted':{'winding':{'l_h':[1.1e-9,1.9e-9],'q':[2.9,4.1],'r_ohm':[2.1,3.1]}}}
        snapshot={'curves':curve};page=new_page();page['results']={'quick':snapshot,'current':snapshot,'before_upgrade':snapshot,'upgraded':snapshot}
        result=comparison_series(page)
        self.assertEqual([s['label'] for s in result],['Source','Before upgrade','After upgrade'])
        self.assertEqual(result[0]['freq_hz'],[1e9,2e9]);self.assertEqual(len(result[0]['branches']['winding']['l_h']),2)
        self.assertIn('Differential',model_label('transformer_dm_dual_tap_gnd'));self.assertEqual(topology_label('distributed_3'),'Distributed: 3 sections')

if __name__=='__main__':unittest.main()
