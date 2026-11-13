"""Controller invariants independent of a display server; real Tk is tested separately."""
import copy
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from snpfit.studio_gui import App, AnalysisPage, model_label
from snpfit.models import list_models, get_model
from snpfit import studio_state


class Value:
    def __init__(self, value=None): self.value=value
    def get(self): return self.value
    def set(self, value): self.value=value


class FakePage:
    def __init__(self, ident='one'):
        self.id=ident;self._revision=1
        self.data={'id':ident,'title':ident,'config':{},'source_valid':True,'results_stale':False,'identity_stale':False,
                   'results':{'quick':None,'current':None,'before_upgrade':None,'upgraded':None},'logs':[]}
        self.views=Mock();self.refresh=Mock();self.status_var=Value('')
    def set_status(self, message, level='info'):
        self.status_var.set(message);self.data['status']={'message':message,'level':level}
    def append_log(self, message, level='info'):
        self.data['logs'].append({'message':message,'level':level})


def snapshot(ident, passed=False):
    return {'id':ident,'passed':passed,'circuit':{'ports':['P','N'],'elements':[{'kind':'ind','value':1e-9}]},
            'stage':'tune','status':'ACCEPTED' if passed else 'TARGETS_NOT_MET','metrics':{},'warnings':[]}


def controller():
    app=App.__new__(App)
    app.root=Mock();app.events=queue.Queue();app.thread=None;app.cancel=threading.Event();app._closing=False
    app.pages={'one':FakePage('one'),'two':FakePage('two')};app.global_status=Value('');app.dirty=False
    app._refresh_all_buttons=Mock();app.job=None;app._poll_id=None
    return app


class StudioControllerTests(unittest.TestCase):
    def test_all_model_labels_are_explicit_unique_english(self):
        models=[get_model(m) if isinstance(m,str) else m for m in list_models()]
        labels=[model_label(m) for m in models]
        self.assertEqual(len(labels),24);self.assertEqual(len(set(labels)),24)
        self.assertTrue(all(all(ord(c)<128 for c in x) for x in labels))
        self.assertTrue(all('Single-ended' in x or 'Differential' in x for x in labels))

    def test_numeric_inputs_match_engine_target_and_budget_boundaries(self):
        config = studio_state.default_config()
        config.update(source_path='/tmp/valid.s2p', model_id='transformer_se_gnd',
                      port_order='1,2', ports_confirmed=True, l_pct='5', q_pct='20')
        page = AnalysisPage.__new__(AnalysisPage)
        page.data = {'source_valid':True, 'source':{'nports':2}}
        page.sync = lambda:config
        self.assertIsNone(page._input_error())
        for key in ('k_pct','srf_pct','network_pct'):
            previous=config[key]
            config[key]='100'
            self.assertIsNone(page._input_error(),key)
            config[key]='100.1'
            self.assertIn('at most 100%',page._input_error(),key)
            config[key]=previous
        for key in ('tune_seconds','upgrade_seconds'):
            previous=config[key]
            config[key]='600'
            self.assertIsNone(page._input_error(),key)
            config[key]='601'
            self.assertIn('at most 600 seconds',page._input_error(),key)
            config[key]=previous

    def test_page_limit_rejects_before_constructing_any_widgets(self):
        app=App.__new__(App)
        app.pages={str(i):object() for i in range(studio_state.MAX_PAGES)}
        app.global_status=Value('')
        with patch('snpfit.studio_gui.AnalysisPage') as widget, patch('snpfit.studio_gui.state.new_page') as factory:
            self.assertIsNone(App.new_page(app))
            self.assertIsNone(App.add_page(app))
            widget.assert_not_called();factory.assert_not_called()
        self.assertIn(str(studio_state.MAX_PAGES),app.global_status.get())
        self.assertIn('Close a page',app.global_status.get())

    def test_worker_result_is_owned_by_page_and_token(self):
        app=controller();applied=[]
        app._apply_result=lambda job,page,value:applied.append((page.id,value))
        app.job={'token':7,'page_id':'one','revision':1,'started':0,'action':'quick'}
        app.events.put((6,'one','done',{'id':'old'}))
        app.events.put((7,'two','done',{'id':'wrong-page'}))
        app.events.put((7,'one','done',{'id':'correct'}))
        App.poll(app)
        self.assertEqual(applied,[('one',{'id':'correct'})]);self.assertIsNone(app.job)

    def test_changed_request_revision_cannot_adopt_result(self):
        app=controller();app._apply_result=Mock()
        app.job={'token':7,'page_id':'one','revision':0,'started':0,'action':'quick'}
        app.events.put((7,'one','done',snapshot('obsolete',True)))
        App.poll(app)
        app._apply_result.assert_not_called()
        self.assertIn('obsolete',app.pages['one'].status_var.get())

    def test_cancelled_worker_does_not_replace_completed_result(self):
        app=controller();old=snapshot('kept',True);app.pages['one'].data['results']['current']=old
        app._apply_result=Mock();app.cancel.set()
        app.job={'token':3,'page_id':'one','revision':1,'started':0,'action':'tune'}
        app.events.put((3,'one','done',snapshot('partial',True)))
        App.poll(app)
        app._apply_result.assert_not_called();self.assertIs(app.pages['one'].data['results']['current'],old)

    def test_manual_upgrade_preserves_before_and_attempt_and_keeps_best(self):
        app=controller();page=app.pages['one'];old=snapshot('before',True);page.data['results']['current']=old
        frozen=copy.deepcopy(old);attempt=snapshot('attempt',False);attempt['accepted_new']=False
        App._apply_result(app,{'action':'upgrade','baseline':frozen,'baseline_stale':False},page,attempt)
        self.assertEqual(page.data['results']['before_upgrade'],frozen)
        self.assertEqual(page.data['results']['upgraded'],attempt)
        self.assertEqual(page.data['results']['current'],frozen)
        self.assertEqual(old,frozen)
        page.views.select.assert_called_with(1)

    def test_unselected_upgrade_cannot_restore_stale_acceptance(self):
        app=controller();page=app.pages['one'];old=snapshot('old-targets',True)
        page.data['results']['current']=old;page.data['results_stale']=True
        attempt=snapshot('new-targets-attempt',False);attempt['accepted_new']=False
        App._apply_result(app,{'action':'upgrade','baseline':copy.deepcopy(old),'baseline_stale':True},page,attempt)
        self.assertTrue(page.data['results_stale'])
        self.assertEqual(page.data['results']['current']['id'],'old-targets')

    def test_display_error_is_visible_and_does_not_stop_polling(self):
        app=controller();app._apply_result=Mock(side_effect=ValueError('Malformed display data'))
        app.job={'token':7,'page_id':'one','revision':1,'started':0,'action':'quick'}
        app.events.put((7,'one','done',snapshot('complete')))
        App.poll(app)
        self.assertIn('Result display failed',app.global_status.get())
        self.assertEqual(app.pages['one'].data['logs'][-1]['level'],'error')
        app.root.after.assert_called()

    def test_tune_completion_never_dispatches_structure_upgrade(self):
        app=controller();page=app.pages['one'];page.data['results']['current']=snapshot('initial')
        app._dispatch=Mock()
        tuned=snapshot('fixed-tune',False)
        App._apply_result(app,{'action':'tune','baseline':snapshot('initial')},page,tuned)
        app._dispatch.assert_not_called()
        self.assertIsNone(page.data['results']['before_upgrade'])
        self.assertIsNone(page.data['results']['upgraded'])
        self.assertEqual(page.data['results']['current']['id'],'fixed-tune')


if __name__=='__main__': unittest.main()
