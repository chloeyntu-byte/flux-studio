import copy
import os
import queue
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from emx_page import EMXPage

class Widget:
    def __init__(self): self.state = None
    def configure(self, **kw): self.state = kw.get('state', self.state)
class Variable:
    def set(self, value): self.value = value
    def get(self): return self.value
class Results:
    def selection(self): return ('a',)
class ResultActionTests(unittest.TestCase):
    def test_frozen_row_source_and_cancel_contract(self):
        with tempfile.NamedTemporaryFile() as snp:
            class Page:
                _start_result_action=EMXPage._start_result_action
                _result_buttons_update=EMXPage._result_buttons_update
                _fit_frequency_count=staticmethod(EMXPage._fit_frequency_count)
                _snp_signature=staticmethod(lambda row: None)
                _queue_hydration=lambda self, ident, signature: None
                is_running=False
            page=Page();page.results=Results();page.rows={'a':{'id':'a','status':'completed','snp_path':snp.name,'source':{'library':'lib','cell':'cell'}}}
            page.campaign=SimpleNamespace(source={'design':{'model':'frozen'}},config={'output_dir':'/campaign'},run_id='run')
            page._result_revision=2;page._result_worker=None;page._loading=False;page.load_results_btn=Widget();page._result_queue=queue.Queue();page.log_lines=[];page.status=Variable();page.result_target=Variable();page.result_buttons=[Widget()];page.result_cancel_button=Widget()
            called=[]; workers=[]
            class Worker:
                def __init__(self,target,daemon):workers.append(target)
                def start(self):pass
            service=SimpleNamespace(regenerate_layout=lambda row,source,**kw: called.append((row,source,kw)) or {})
            with mock.patch('emx_page.threading.Thread',Worker), mock.patch.dict('sys.modules',{'flux_results':service}):
                page._start_result_action('regenerate_layout')
                page.rows['a']['source']['cell']='changed';page.campaign.source['design']['model']='changed'
                workers[0]()
            self.assertEqual(called[0][0]['source']['cell'],'cell')
            self.assertEqual(called[0][1]['design']['model'],'frozen')
            self.assertEqual(called[0][2]['output_dir'],'/campaign')
            self.assertIs(called[0][2]['cancel_event'],page._result_cancel)
            self.assertEqual(page._result_queue.get_nowait()[:4],(2,'run','a','regenerate_layout'))
    def test_one_point_can_open_but_cannot_fit(self):
        class Page:
            _result_buttons_update=EMXPage._result_buttons_update
            _fit_frequency_count=staticmethod(EMXPage._fit_frequency_count)
            is_running=False
        with tempfile.NamedTemporaryFile() as snp:
            p=Page();p.results=Results();p.rows={'a':{'id':'a','status':'completed','snp_path':snp.name,'metrics':[{'frequency_hz':1e9,'reported_frequency_hz':1e9}]}}
            p.campaign=SimpleNamespace(source={'x':1});p._result_worker=None;p._loading=False;p.load_results_btn=Widget();p.result_buttons=[Widget(),Widget(),Widget()];p.result_cancel_button=Widget();p.result_target=Variable()
            p._result_buttons_update()
            self.assertEqual([b.state for b in p.result_buttons],['normal','disabled','normal'])
            self.assertIn('at least 2 distinct measured',p.result_target.get())
            p.rows['a']['metrics'].append({'frequency_hz':2e9,'reported_frequency_hz':1e9})
            p._result_buttons_update();self.assertEqual(p.result_buttons[1].state,'disabled')
            p.rows['a']['metrics'][1]['reported_frequency_hz']=2e9
            p._result_buttons_update();self.assertEqual(p.result_buttons[1].state,'normal')

    def test_failed_or_missing_snp_disables_actions(self):
        class Page:
            _result_buttons_update=EMXPage._result_buttons_update
            _fit_frequency_count=staticmethod(EMXPage._fit_frequency_count)
            is_running=False
        p=Page();p.results=Results();p.rows={'a':{'id':'a','status':'failed','snp_path':'/missing'}};p.campaign=SimpleNamespace(source={'x':1});p._result_worker=None;p._loading=False;p.load_results_btn=Widget();p.result_buttons=[Widget()];p.result_cancel_button=Widget();p.result_target=Variable()
        p._result_buttons_update();self.assertEqual(p.result_buttons[0].state,'disabled')
if __name__=='__main__':unittest.main()

class HydrateTests(unittest.TestCase):
    def test_snp_is_source_and_hash_enforced(self):
        import hashlib
        from result_data import hydrate_row
        with tempfile.TemporaryDirectory() as temp:
            path=os.path.join(temp,'result.s2p')
            with open(path,'w') as stream:stream.write('# Hz S RI R 50\n1e9 0.1 0 0 0 0 0 0.1 0\n')
            with open(path,'rb') as stream:digest=hashlib.sha256(stream.read()).hexdigest()
            ports={'signals':['p','n'],'grounds':[],'protocol':'fixed_ground_pins','terminals':[{'name':'p','winding':'L','endpoint':'positive'},{'name':'n','winding':'L','endpoint':'negative'}]}
            row={'status':'completed','snp_path':path,'metrics':[{'L':999}],'network':{'ports':ports,'frequencies_hz':[1e9],'snp_sha256':digest}}
            actual=hydrate_row(row)
            self.assertEqual(actual['metrics'][0]['frequency_hz'],1e9)
            self.assertNotEqual(actual['metrics'][0].get('L'),999)
            self.assertEqual(row['metrics'][0]['L'],999)
            with open(path,'a') as stream:stream.write('! changed\n')
            actual=hydrate_row(row);self.assertEqual(actual['metrics'],[]);self.assertIn('SHA-256',actual['data_error']);self.assertEqual(actual['status'],'completed')
            os.unlink(path);actual=hydrate_row(row);self.assertEqual(actual['metrics'],[]);self.assertIn('data_error',actual)
    def test_legacy_metadata_from_job(self):
        import json
        from result_data import hydrate_row
        with tempfile.TemporaryDirectory() as temp:
            path=os.path.join(temp,'result.s2p')
            with open(path,'w') as stream:stream.write('# Hz S RI R 50\n1e9 0.1 0 0 0 0 0 0.1 0\n')
            ports={'signals':['p','n'],'grounds':[],'protocol':'fixed_ground_pins','terminals':[{'name':'p','winding':'L','endpoint':'positive'},{'name':'n','winding':'L','endpoint':'negative'}]}
            with open(os.path.join(temp,'job.json'),'w') as stream:json.dump({'ports':ports,'frequencies_hz':[1e9]},stream)
            result=hydrate_row({'status':'completed','snp_path':path,'job_dir':temp})
            self.assertEqual(len(result['metrics']),1);self.assertNotIn('data_error',result)

class RevisionTests(unittest.TestCase):
    def test_stale_hydration_does_not_replace_new_campaign(self):
        class Page:
            _drain_hydrated=EMXPage._drain_hydrated
        page=Page();page._hydrate_done=queue.Queue();page._result_revision=2
        page._hydrate_state={'a':('new',2)};page.rows={'a':{'metrics':[{'L':7}]}}
        page._hydrate_done.put((1,'a',1,'old',{'metrics':[{'L':99}]}))
        page._drain_hydrated()
        self.assertEqual(page.rows['a']['metrics'],[{'L':7}])

class HistoryTests(unittest.TestCase):
    def test_public_snapshot_without_oa_and_no_cached_metrics(self):
        import json
        from result_data import load_campaign
        with tempfile.TemporaryDirectory() as temp:
            identity={'workspace':'/no/oa','cds_lib':'/no/cds.lib','library':'lib','technology':'tech','library_path':'/no/library'}
            meta={'schema':'flux.emx.campaign.v1','run_id':'old','identity':identity,'design':{'model':'ind'},'config':{'axes':[{'name':'width','sampling':'choices','values':[1,2]}]}}
            rows=[{'id':'one','status':'completed','metrics':[{'L':999}],'objective_values':[999],'feasible':True,'snp_path':'/no/oa/a.s2p'}]
            for name,content in [('campaign.json',meta),('index.json',rows)]:
                with open(os.path.join(temp,name),'w') as stream:json.dump(content,stream)
            source={'identity':dict(identity,library='other'),'design':{'model':'ind'},'runtime':{'secret':'different'}}
            result=load_campaign(temp,source)
            self.assertEqual(result['rows'][0]['metrics'],[]);self.assertEqual(result['rows'][0]['objective_values'],[])
            self.assertIsNone(result['rows'][0]['feasible']);self.assertNotIn('runtime',result['source'])
            self.assertEqual(result['source']['identity'],identity)
            from emx_charts import axis_options
            self.assertEqual(axis_options(result['source'],result['config'],[])['parameters'],['width'])
            source['identity']=identity;source['runtime']={'user':'native'}
            self.assertEqual(load_campaign(temp,source)['source']['runtime'],{'user':'native'})
            rows.append(rows[0]);
            with open(os.path.join(temp,'index.json'),'w') as stream:json.dump(rows,stream)
            with self.assertRaisesRegex(ValueError,'duplicate'):load_campaign(temp,source)
