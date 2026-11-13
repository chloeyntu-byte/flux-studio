import ast
import copy
import os
import queue
import tempfile
from pathlib import Path
import unittest
from unittest import mock
import tkinter as tk
from tkinter import ttk

from emx_page import EMXPage, axis_from_editor, frequency_list


class ParsingTests(unittest.TestCase):
    def test_frequency_count_and_step_never_add_off_grid_endpoint(self):
        self.assertEqual(frequency_list('1','3','count','3'),[1,2,3])
        self.assertEqual(frequency_list('1','3.5','step','1'),[1,2,3])
        for args in [('1','3','count','1'),('0','3','count','3'),('1','4','step','0')]:
            with self.assertRaises(ValueError):frequency_list(*args)

    def test_frequency_modes_reject_invalid_and_preserve_points(self):
        self.assertEqual(frequency_list('2e9','','single',''),[2e9])
        self.assertEqual(frequency_list('1e9 2e9,3e9','','list',''),[1e9,2e9,3e9])
        self.assertEqual(frequency_list('1e9','4e9','log','3'),[1e9,2e9,4e9])
        extreme=frequency_list('1e-300','1e300','log','3')
        self.assertEqual((extreme[0],extreme[-1]),(1e-300,1e300))
        points=frequency_list('1e9','1e11','log','3')
        for actual,expected in zip(points,(1e9,1e10,1e11)):self.assertAlmostEqual(actual/expected,1)
        self.assertEqual(frequency_list('1','1','count','1'),[1])
        for args in [('1 bad 3','','list',''),('nan','','single',''),('1','10','log','1'),('1','10','other','3'),(' '.join(['1']*257),'','list','')]:
            with self.assertRaises(ValueError):frequency_list(*args)

    def test_typed_axes_and_optional_choices(self):
        values=dict(sampling='choices',values='none,1,2',group='pair')
        self.assertEqual(axis_from_editor(dict(name='turns',type='int',optional=True),values)['values'],[None,1,2])
        self.assertEqual(axis_from_editor(dict(name='tap',type='bool'),dict(sampling='choices',values='false,true'))['values'],[False,True])
        self.assertEqual(axis_from_editor(dict(name='metal',type='enum',options=[{'id':'M1'},{'id':'M2'}]),dict(sampling='choices',values='M1,M2'))['values'],['M1','M2'])
        with self.assertRaises(ValueError):axis_from_editor(dict(name='turns',type='int'),dict(sampling='choices',values='1.5'))
        with self.assertRaises(ValueError):axis_from_editor(dict(name='tap',type='bool'),dict(sampling='choices',values='yes'))


class CrossLayerTests(unittest.TestCase):
    def test_discovery_deduplicates_and_invalidates_requests(self):
        class Value:
            def __init__(self,value=''):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        class Headless:
            _discover_paths=EMXPage._discover_paths
            _drain_discovery=EMXPage._drain_discovery
            _reset_auto_paths=EMXPage._reset_auto_paths
        page=Headless();page._discovery_queue=queue.Queue();page._discovery_generation=0
        page._discovery_request=None;page._auto_paths={}
        page.v={key:Value() for key in ('emx_executable','process_file')};page.discovery_text=Value()
        page.source={'identity':{'technology':'tech','workspace':'/project'},'runtime':{'profile':'a'}}
        workers=[]
        class Worker:
            def __init__(self,target,daemon):workers.append(target)
            def start(self):pass
        with mock.patch('emx_page.threading.Thread',Worker), mock.patch('emx_adapter.discover_config',return_value={'emx_executable':'/auto/emx','process_file':'/auto/proc'}) as detect:
            page._discover_paths(page.source);page._discover_paths(copy.deepcopy(page.source))
            self.assertEqual(len(workers),1)
            workers[0]();page._drain_discovery();page._discover_paths(page.source)
            self.assertEqual(len(workers),1);self.assertEqual(detect.call_count,1)
            page.v['emx_executable'].set('/manual/emx');page._discover_paths(page.source)
            self.assertEqual(len(workers),2)
            page.source['runtime']['profile']='b';page._discover_paths(page.source)
            self.assertEqual(len(workers),3)
            workers[1]();page._drain_discovery()
            self.assertEqual(page.v['emx_executable'].get(),'/manual/emx')
            page._reset_auto_paths();self.assertEqual(len(workers),4)
            workers[2]();page._drain_discovery()
            self.assertEqual(page.v['process_file'].get(),'')
            workers[3]();page._drain_discovery()
            self.assertEqual(page.v['process_file'].get(),'/auto/proc')

    @staticmethod
    def ui_algorithms():
        tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('emx_page.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            if not isinstance(node.func,ast.Attribute) or node.func.attr!='Combobox':continue
            keywords={item.arg:item.value for item in node.keywords}
            variable=keywords.get('textvariable')
            if isinstance(variable,ast.Subscript) and isinstance(variable.value,ast.Attribute) and variable.value.attr=='opt':
                key=ast.literal_eval(variable.slice.value if type(variable.slice).__name__ == 'Index' else variable.slice)
                if key=='algorithm':return ast.literal_eval(keywords['values'])
        raise AssertionError('Could not find algorithm Combobox')

    @staticmethod
    def ui_config(algorithm='nsga2',mode='optimize'):
        class Value:
            def __init__(self,value):self.value=value
            def get(self,*unused):return self.value
        class Headless:
            _parse_goals=EMXPage._parse_goals
        page=Headless()
        page.v={k:Value(v) for k,v in dict(edge='.5',thickness='.5',via='.5',freq_start='1e9',freq_stop='2e9',freq_mode='count',freq_amount='2',freq_values='',extra='--3d=-*',emx_executable='/tool/emx',process_file='/tech/process.proc',parallel='2',memory='1024',timeout='300',concurrency='2',output='/tmp').items()}
        page.opt={k:Value(v) for k,v in dict(algorithm=algorithm,population='4',generations='3',seed='1').items()}
        page.mode=Value(mode);page.three_d=Value(False);page.cleanup=Value(False)
        page.source=dict(definition=dict(emx_metrics=['L','Q']))
        page.axes={'width':dict(name='width',enabled=True,sampling='linear',minimum=1,maximum=30,count=30,group='')}
        page.objectives=Value('L, 1e9, maximize' if mode=='optimize' else '')
        page.constraints=Value('')
        return EMXPage.config(page)

    def test_each_ui_algorithm_config_reaches_second_real_search_batch(self):
        from emx_search import SearchSpace, Optimizer
        fields=[dict(name='width',type='float',current=1,min=1,max=30)]
        choices=self.ui_algorithms()
        self.assertEqual(choices,('nsga2','de','bayesian'))
        for algorithm in choices:
            with self.subTest(algorithm=algorithm):
                config=self.ui_config(algorithm)
                space=SearchSpace(fields,config['axes'])
                optimizer=Optimizer(space,**config['optimizer'])
                first=optimizer.ask();self.assertEqual(len(first),4)
                optimizer.tell([dict(values=values,objectives=[float(values['width'])],violation=0,ok=True) for values in first])
                self.assertTrue(optimizer.ask())

    def test_ui_configuration_is_accepted_by_real_campaign_and_sweep_needs_no_goal(self):
        import emx_campaign
        import emx_adapter
        source=dict(fields=[dict(name='width',type='float',current=1,min=1,max=30)],design=dict(model='inductor'),definition=dict(emx_metrics=['L','Q']),identity={},runtime={})
        config=self.ui_config(mode='sweep')
        self.assertEqual(config['objectives'],[])
        self.assertEqual(set(config),{'mode','axes','emx','objectives','constraints','optimizer','concurrency','output_dir','cleanup_intermediates'})
        self.assertEqual(set(config['emx']),{'edge_um','thickness_um','via_merge_um','three_d','frequencies_hz','extra_options','options_text','emx_executable','process_file','parallel','memory_mb','timeout_seconds'})
        effective=emx_adapter.resolve_settings(config['emx'])
        campaign=emx_campaign.Campaign(source,config)
        self.assertEqual(campaign.settings['edge_um'],effective['edge_um'])
        self.assertEqual(campaign.space.size,30)
        invalid=copy.deepcopy(config)
        invalid['objectives']=[dict(metric='Lp',frequency_hz=1e9,direction='maximize',scale=1)]
        with self.assertRaisesRegex(ValueError,'unavailable'):
            emx_campaign.Campaign(source,invalid)


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires private Tk display')
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('1024x700')
        self.current=dict(design=dict(model='ind',library='work',cell='base'),fields=[dict(name='width',label='Width',type='float',current=2,min=1,max=10),dict(name='layers.body',label='Metal',type='enum',current='M1',options=['M1','M2'])])
        self.page=EMXPage(self.root,lambda:copy.deepcopy(self.current));self.page.pack(fill='both',expand=True)
        # Legacy chart interaction fixtures represent already-hydrated responses;
        # real Touchstone validation is exercised in test_flux_result_ui.
        original_add=self.page._add_row
        self.page._queue_hydration=lambda ident,signature: None
        def add_fixture(row):
            original_add(row)
            self.page.rows[str(row['id'])]=copy.deepcopy(row)
            from emx_search import pareto
            try:self.page.pareto_ids=[str(r['id']) for r in pareto(list(self.page.rows.values()))]
            except (TypeError,ValueError):self.page.pareto_ids=[]
            self.page._refresh_results();self.page._schedule_plot()
        self.page._add_row=add_fixture
        self.root.update()

    def tearDown(self):
        self.page.destroy();self.root.destroy()

    def test_goals_do_not_extend_emx_sampling(self):
        self.current['definition']={'emx_metrics':['L','Q']}
        self.page.refresh_source()
        self.assertIn('must match an EMX Settings sample',self.page.goal_help.get())
        self.assertNotIn('added to EMX',self.page.goal_help.get())
        self.page.v['freq_mode'].set('list');self.page.v['freq_values'].set('1e9 2e9')
        self.page.objectives.insert('1.0','L, 1.5e9, maximize')
        with self.assertRaisesRegex(ValueError,'Change the goal or EMX Settings'):
            self.page.config()
        self.page.objectives.delete('1.0','end');self.page.objectives.insert('1.0','L, 2e9, maximize')
        self.page.chart_vars['y_frequency_hz'].set('3000000000')
        self.assertEqual(self.page.config()['emx']['frequencies_hz'],[1e9,2e9])

    def test_separate_settings_and_campaign_snapshot(self):
        class FakeCampaign:
            run_id='separate';is_running=False
            def __init__(self,source,config):
                self.source=source;self.config=config;self.events=queue.Queue()
            def start(self):self.is_running=True
        self.assertEqual([self.page.pages.tab(tab,'text') for tab in self.page.pages.tabs()],
                         ['EMX Settings','Sweep & Optimization'])
        self.page.pages.select(0);self.root.update()
        self.assertTrue(self.page.settings_scroll.winfo_ismapped())
        self.assertFalse(self.page.sidebar.winfo_ismapped())
        self.page.v['edge'].set('1.25');self.page.v['output'].set('/tmp')
        self.current['fields'][0]['current']=4
        self.page.pages.select(1);self.root.update()
        self.assertEqual(self.page.source['fields'][0]['current'],4)
        with mock.patch('emx_campaign.Campaign',FakeCampaign):self.page.start()
        campaign=self.page.campaign
        self.assertEqual(campaign.config['emx']['edge_um'],1.25)
        self.page.pages.select(0);self.page.v['edge'].set('2.5')
        self.page.mode.set('optimize');self.page.axes.clear()
        self.current['fields'][0]['current']=7;self.root.update()
        self.assertEqual(campaign.config['emx']['edge_um'],1.25)
        self.assertEqual(campaign.source['fields'][0]['current'],4)
        self.assertEqual(self.page._chart_context()[1]['mode'],'sweep')
        campaign.is_running=False;self.page.progress.stop()

    def test_external_settings_wheel_bindings_are_released(self):
        settings=ttk.Frame(self.root)
        page=EMXPage(self.root,lambda:copy.deepcopy(self.current),settings_parent=settings)
        scroll=page.settings_scroll;tag=scroll._wheel_tag;commands=list(scroll._wheel_commands)
        self.assertTrue(self.root.bind_class(tag,'<Button-5>'))
        page.destroy()
        self.assertFalse(self.root.bind_class(tag,'<Button-5>'))
        for command in commands:self.assertFalse(self.root.tk.call('info','commands',command))
        self.assertEqual(settings.winfo_children(),[])
        self.assertTrue(self.root.bind_class(self.page.settings_scroll._wheel_tag,'<Button-5>'))
        settings.destroy()

    def test_all_metric_columns_frequency_sort_status_and_selection(self):
        rows=[
            dict(id='a',status='completed',feasible=True,violation=0,objectives=[1],metrics=[dict(frequency_hz=1e9,Lp=3,Ls=4,k=.8,Qp=12,Qs=13),dict(frequency_hz=2e9,Lp=8,Qp=10)]),
            dict(id='b',status='completed',feasible=True,violation=0,objectives=[2],metrics=[dict(frequency_hz=1e9,Lp=1,Ls=2,k=.7,Qp=8,Qs=9)]),
            dict(id='c',status='rejected',feasible=False,metrics=[]),
            dict(id='e',status='completed',feasible=False,metrics=[]),
        ]
        for row in rows:self.page._add_row(row)
        self.page.select_row('a');self.root.update()
        self.assertEqual(self.page.sample_frequency.get(),'1000000000.0')
        self.assertEqual(self.page.results.set('a','Qs'),'13')
        self.assertEqual(self.page.results.item('a','tags')[0],'optimal')
        self.assertEqual(self.page.results.item('b','tags')[0],'feasible')
        self.assertEqual(self.page.results.item('c','tags')[0],'fail')
        self.assertEqual(self.page.results.item('e','tags')[0],'infeasible')
        self.page._sort_results('Lp')
        self.assertEqual(self.page.results.get_children(),('b','a','c','e'))
        self.page._sort_results('Lp')
        self.assertEqual(self.page.results.get_children(),('a','b','c','e'))
        self.assertEqual(self.page.results.selection(),('a',))
        self.page.sample_frequency.set('2000000000.0');self.page._refresh_results()
        self.assertEqual(self.page.results.set('a','Lp'),'8')
        self.assertEqual(self.page.results.set('b','Lp'),'—')
        self.assertEqual(self.page.results.get_children(),('a','b','c','e'))
        self.assertEqual(self.page.results.selection(),('a',))
        self.page._add_row(dict(id='d',status='completed',feasible=True,violation=0,objectives=[0],metrics=[dict(frequency_hz=2e9,Lp=20)]))
        self.assertEqual(self.page.results.get_children()[0],'d')
        self.assertEqual(self.page.results.item('a','tags')[0],'feasible')
        self.assertEqual(self.page.results.item('d','tags')[0],'optimal')

    def test_chart_management_and_view_independence(self):
        self.page.refresh_source()
        self.page.axes={'width':dict(name='width',sampling='linear',minimum=1,maximum=3,count=3)}
        self.page._add_row(dict(id='a',status='completed',values={'width':1},metrics=[dict(frequency_hz=1e9,L=2,Q=3)]))
        self.page.update_plot();self.assertEqual(self.page.charts,[])
        self.page._refresh_chart_choices();self.page.chart_vars['y'].set('L');self.page.chart_vars['y_frequency_hz'].set('1000000000.0');self.page.add_chart();self.root.update()
        first=self.page.charts[0];self.assertEqual(first['plot'].points[0][:3],(1,2,'a'))
        first['plot'].wheel(type('Event',(),dict(num=4,delta=0))());view=first['plot'].view[:]
        self.page.chart_vars['y'].set('Q');self.page.add_chart();self.root.update()
        second=self.page.charts[1];self.assertEqual(first['spec']['y'],'L');self.assertEqual(second['spec']['y'],'Q')
        self.page._add_row(dict(id='b',status='completed',values={'width':2},metrics=[dict(frequency_hz=1e9,L=4,Q=5)]));self.page.update_plot()
        self.assertEqual(first['plot'].view,view);self.assertEqual(len(first['plot'].points),2)
        self.page.chart_vars['y'].set('L');self.assertEqual(second['spec']['y'],'Q');self.page.update_chart();self.assertEqual(second['spec']['y'],'L')
        self.page.delete_chart();self.assertEqual(len(self.page.charts),1);self.assertIn('b',self.page.rows)
        first['plot'].reset();self.assertFalse(first['plot'].manual_view)

    def test_three_axis_slice_and_settings_round_trip(self):
        self.current['fields'].append(dict(name='turns',type='int',current=1))
        self.page.refresh_source();self.page.axes={
            'width':dict(name='width',sampling='linear',minimum=1,maximum=2,count=2),
            'layers.body':dict(name='layers.body',sampling='choices',values=['M1','M2']),
            'turns':dict(name='turns',sampling='choices',values=[1,2])}
        self.page._add_row(dict(id='one',status='completed',values={'width':1,'layers.body':'M1','turns':1},metrics=[dict(frequency_hz=1e9,L=2)]))
        self.page._add_row(dict(id='two',status='completed',values={'width':1,'layers.body':'M1','turns':2},metrics=[dict(frequency_hz=1e9,L=4)]))
        self.page._refresh_chart_choices()
        for key,value in dict(x='width',y='L',y_frequency_hz='1000000000.0',grid_y='layers.body',slice='turns',slice_value='2').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update();chart=self.page.charts[0]
        self.assertEqual(chart['plot'].chart_type,'heatmap');self.assertEqual(chart['plot'].top_margin,55);self.assertGreaterEqual(chart['plot'].winfo_height()-chart['plot'].top_margin-40,80);self.assertGreaterEqual(chart['plot'].xy(*chart['plot'].points[0][:2])[1],55);self.assertEqual([p[2] for p in chart['plot'].points],['two'])
        self.assertEqual(chart['spec']['slice_value'],2)
        plot=chart['plot'];self.assertEqual(plot.color_range,(4,4))
        x,y=plot.xy(*plot.points[0][:2]);plot.hover(type('Event',(),dict(x=x,y=y))())
        self.assertIn('= 4',plot.readout.get())
        self.assertEqual(str(self.page.chart_combos['grid_y'].cget('state')),'readonly')
        self.assertEqual(str(self.page.chart_combos['z'].cget('state')),'disabled')
        self.assertEqual(str(self.page.pairs_button.cget('state')),'disabled')
        with tempfile.TemporaryDirectory() as directory:
            export=str(Path(directory)/'heatmap.csv')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=export):self.page.export_csv()
            import csv
            with open(export,newline='') as file:record=next(csv.DictReader(file))
            self.assertEqual((record['candidate'],record['color_value'],record['slice_axis'],record['slice_value']),('two','4','turns','2'))
            self.assertIn('L (nH) @',record['color_metric'])
        chart['visible'].set(False)
        with tempfile.TemporaryDirectory() as directory:
            target=str(Path(directory)/'settings.json')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=target):self.page.save_settings()
            self.page.delete_chart();self.assertFalse(self.page.charts)
            with mock.patch('emx_page.filedialog.askopenfilename',return_value=target):self.page.load_settings()
            self.assertEqual(self.page.charts[0]['spec']['slice_value'],2)
            self.assertEqual(self.page.charts[0]['plot'].points[0][2],'two')
            data=__import__('json').loads(Path(target).read_text());data.pop('charts');Path(target).write_text(__import__('json').dumps(data))
            with mock.patch('emx_page.filedialog.askopenfilename',return_value=target):self.page.load_settings()
            self.assertEqual(self.page.charts,[])

    def test_optimization_shows_only_full_feasible_pareto(self):
        self.page.refresh_source();self.page.mode.set('optimize')
        self.page.objectives.insert('1.0','L, 1e9, minimize\nQ, 1e9, minimize')
        for ident,scores,feasible in [('front',[1,2],True),('dominated',[2,3],True),('invalid',[0,0],False)]:
            self.page._add_row(dict(id=ident,status='completed',feasible=feasible,violation=0,objectives=scores,metrics=[dict(frequency_hz=1e9,L=scores[0],Q=scores[1])]))
        self.page._refresh_chart_choices();self.page.chart_vars['x'].set('L');self.page.chart_vars['y'].set('Q')
        self.page.chart_vars['x_frequency_hz'].set('1000000000.0');self.page.chart_vars['y_frequency_hz'].set('1000000000.0')
        self.page.add_chart();self.root.update();chart=self.page.charts[0]
        self.assertEqual([p[2] for p in chart['plot'].points],['front'])
        self.assertEqual(chart['plot'].chart_type,'scatter')
        self.page.add_all_pairs();self.assertEqual(len(self.page.charts),2)

    def test_surface_page_selection_and_csv(self):
        import csv
        self.current['definition']=dict(emx_metrics=['L','Q','k'])
        self.page.refresh_source();self.page.mode.set('optimize')
        self.page.objectives.insert('1.0','L, 1e9, minimize\nQ, 1e9, minimize\nk, 1e9, minimize')
        for ident,x,y,z in [('a',1,3,3),('b',2,1,3),('c',3,2,1)]:
            self.page._add_row(dict(id=ident,status='completed',feasible=True,violation=0,objectives=[x,y,z],metrics=[dict(frequency_hz=1e9,L=x,Q=y,k=z)]))
        self.page._refresh_chart_choices()
        for key,value in dict(x='L',y='Q',z='k',x_frequency_hz='1000000000.0',y_frequency_hz='1000000000.0',z_frequency_hz='1000000000.0').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update();chart=self.page.charts[0];plot=chart['plot']
        self.assertEqual(chart['data']['type'],'surface');self.assertEqual(len(plot.find_withtag('mesh')),1)
        x,y,_=plot.surface_screen[0];plot.event_generate('<ButtonPress-1>',x=int(x),y=int(y));plot.event_generate('<ButtonRelease-1>',x=int(x),y=int(y));self.root.update()
        self.assertEqual(self.page.results.selection(),('a',));self.assertEqual(plot.highlight,'a')
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'plot.csv')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=path):self.page.export_csv()
            with open(path,encoding='utf8') as file: rows=list(csv.DictReader(file))
            self.assertEqual([r['z'] for r in rows],['3','3','1']);self.assertTrue(rows[0]['z_label'])
        with mock.patch('emx_charts.build_chart',side_effect=ValueError('invalid axes')):self.page.update_plot()
        self.assertEqual(chart['data'],{});self.assertFalse(plot.surface_points);self.assertFalse(plot.find_withtag('mesh'));self.assertIn('invalid axes',chart['notes'].get())

    def test_single_objective_can_project_other_metrics_and_add_pairs(self):
        self.current['definition']=dict(emx_metrics=['L','Q'])
        self.page.refresh_source();self.page.mode.set('optimize');self.page.objectives.insert('1.0','Q, 1e9, maximize')
        self.page._add_row(dict(id='best',status='completed',feasible=True,violation=0,objectives=[-9],metrics=[dict(frequency_hz=1e9,L=2,Q=9)]))
        self.page._add_row(dict(id='other',status='completed',feasible=True,violation=0,objectives=[-4],metrics=[dict(frequency_hz=1e9,L=3,Q=4)]))
        self.page._refresh_chart_choices();self.page.chart_vars['x'].set('L');self.page.chart_vars['y'].set('Q')
        self.page.chart_vars['x_frequency_hz'].set('1000000000.0');self.page.chart_vars['y_frequency_hz'].set('1000000000.0')
        self.page.add_chart();self.root.update();self.assertEqual([p[2] for p in self.page.charts[0]['plot'].points],['best'])
        self.page.add_all_pairs();self.assertEqual(len(self.page.charts),2)

    def test_highlight_does_not_reorder_line_segments(self):
        self.page.refresh_source();self.page.axes={'width':dict(name='width',sampling='linear',minimum=1,maximum=3,count=3)}
        for i in range(1,4):self.page._add_row(dict(id=str(i),status='completed',values={'width':i},metrics=[dict(frequency_hz=1e9,L=i)]))
        self.page._refresh_chart_choices();self.page.chart_vars['y_frequency_hz'].set('1000000000.0');self.page.add_chart();self.root.update()
        plot=self.page.charts[0]['plot'];self.page.select_row('2');self.root.update();plot.draw()
        lines=[plot.coords(item) for item in plot.find_all() if plot.type(item)=='line']
        self.assertEqual(len(lines),2)
        self.assertTrue(all(float(plot.itemcget(item,'width'))==3 for item in plot.find_all() if plot.type(item)=='line'))
        for line,start,end in zip(lines,(1,2),(2,3)):
            x0,y0=plot.xy(start,start);x1,y1=plot.xy(end,end)
            self.assertEqual(line,[x0,y0,x1,y1])

    def test_overlapping_heatmap_candidates_cycle_and_keep_notice(self):
        self.current['fields'].append(dict(name='turns',type='int',current=1));self.page.refresh_source()
        self.page.axes={'width':dict(name='width',sampling='linear',minimum=1,maximum=2,count=2),
                        'layers.body':dict(name='layers.body',sampling='choices',values=['M1','M2']),
                        'turns':dict(name='turns',sampling='choices',values=[1,2])}
        for ident,value in (('a',2),('b',4)):
            self.page._add_row(dict(id=ident,status='completed',values={'width':1,'layers.body':'M1','turns':1},metrics=[dict(frequency_hz=1e9,L=value)]))
        self.page._refresh_chart_choices()
        for key,value in dict(x='width',y='L',y_frequency_hz='1000000000.0',grid_y='layers.body',slice='turns',slice_value='1').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update();chart=self.page.charts[0];plot=chart['plot']
        self.assertIn('Multiple candidates',chart['notes'].get())
        x,y=plot.xy(*plot.points[0][:2]);event=type('Event',(),dict(x=x,y=y))()
        plot.hover(event);self.assertIn('2 overlapping',plot.readout.get());self.assertIn('Multiple candidates',chart['notes'].get())
        plot.release(event);self.root.update();first=self.page.results.selection()[0]
        plot.release(event);self.root.update();second=self.page.results.selection()[0]
        self.assertEqual({first,second},{'a','b'});plot.hover(event);self.assertIn(second,plot.readout.get())

    def test_four_frequency_goals_create_six_pairs_and_selector_reaches_all(self):
        self.current['definition']=dict(emx_metrics=['L','Q']);self.page.refresh_source();self.page.mode.set('optimize')
        self.page.objectives.insert('1.0','L, 1e9, minimize\nQ, 1e9, minimize\nL, 2e9, minimize\nQ, 2e9, minimize')
        self.page._add_row(dict(id='a',status='completed',feasible=True,violation=0,objectives=[1,1,1,1],metrics=[dict(frequency_hz=1e9,L=1,Q=1),dict(frequency_hz=2e9,L=2,Q=2)]))
        self.page._refresh_chart_choices();self.page.add_all_pairs();self.root.update()
        self.assertEqual(len(self.page.charts),6);self.assertEqual(len(self.page.chart_selector.cget('values')),6)
        pairs={((c['spec']['x'],c['spec']['x_frequency_hz']),(c['spec']['y'],c['spec']['y_frequency_hz'])) for c in self.page.charts}
        self.assertIn((('L',1e9),('L',2e9)),pairs)
        self.page.chart_selector.current(5);self.page._choose_chart();self.root.update()
        self.assertIs(self.page._current_chart(),self.page.charts[5])
        self.page.delete_chart();self.root.update();self.assertEqual(len(self.page.chart_selector.cget('values')),5)
        self.page._clear_charts();self.assertEqual(len(self.page.chart_selector.cget('values')),0)

    def test_optional_none_slice_survives_chart_selection_and_update(self):
        self.current['fields'].append(dict(name='turns',type='int',optional=True,current=None))
        self.page.refresh_source();self.page.axes={
            'width':dict(name='width',sampling='linear',minimum=1,maximum=2,count=2),
            'layers.body':dict(name='layers.body',sampling='choices',values=['M1','M2']),
            'turns':dict(name='turns',sampling='choices',values=[None,1])}
        self.page._add_row(dict(id='a',status='completed',values={'width':1,'layers.body':'M1','turns':None},metrics=[dict(frequency_hz=1e9,L=2)]))
        self.page._refresh_chart_choices()
        for key,value in dict(x='width',y='L',y_frequency_hz='1000000000.0',grid_y='layers.body',slice='turns',slice_value='None').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update();self.page._chart_selected()
        self.assertEqual(self.page.chart_vars['slice_value'].get(),'None');self.page.update_chart()
        self.assertIsNone(self.page.charts[0]['spec']['slice_value']);self.assertEqual(self.page.charts[0]['plot'].points[0][2],'a')

    def test_full_application_compact_heatmap_has_drawable_space(self):
        from app import TransformerApp
        window=tk.Toplevel(self.root)
        with mock.patch.object(window,'after',return_value=None):app=TransformerApp(window)
        window.geometry('1024x700');app.workspace_tabs.select(app.emx_page);self.root.update()
        page=app.emx_page;page.source={'fields':[]}
        page.campaign=type('Campaign',(),dict(is_running=False,config=dict(mode='optimize',objectives=[dict(metric=m,frequency_hz=1e9) for m in ('L','Q','k')],emx=dict(frequencies_hz=[1e9])),source={'fields':[]}))()
        page._new_chart(dict(mode='optimize',x='L',y='Q',color='k',x_frequency_hz=1e9,y_frequency_hz=1e9,color_frequency_hz=1e9));page.update_plot();self.root.update()
        plot=page.charts[0]['plot'];self.assertTrue(page.manager_open.get());self.assertGreaterEqual(plot.winfo_height()-plot.top_margin-40,80)
        for button in page.chart_toolbar_buttons+[page.chart_selector]:self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),page.winfo_rootx()+page.winfo_width())
        page.manager_open.set(False);page._toggle_manager();page.manager_open.set(True);page._toggle_manager();self.root.update();self.assertTrue(page.manager_open.get())
        page.campaign=None;window.destroy()

    def test_paths_pin_contract_and_stale_discovery(self):
        self.current['identity']=dict(technology='tech',workspace='/tmp')
        self.current['runtime']={}
        self.current['port_contract']=dict(terminals=[dict(name='TAP',kind='tap'),dict(name='GND',kind='ground')])
        with mock.patch('emx_adapter.discover_config',return_value=dict(emx_executable='/auto/emx',process_file='/auto/proc')):
            self.page.refresh_source()
            import time
            deadline=time.monotonic()+2
            while self.page._discovery_queue.empty() and time.monotonic()<deadline:time.sleep(.01)
            self.page.v['emx_executable'].set('/manual/emx')
            self.page._drain_discovery()
        self.assertEqual(self.page.v['emx_executable'].get(),'/manual/emx')
        self.assertEqual(self.page.v['process_file'].get(),'/auto/proc')
        self.assertIn('TAP (tap)',self.page.pin_text.get());self.assertIn('GND (ground)',self.page.pin_text.get())
        self.page._discovery_queue.put((self.page._discovery_generation-1,{},dict(emx_executable='/stale',process_file='/stale'),None))
        self.page._drain_discovery();self.assertEqual(self.page.v['process_file'].get(),'/auto/proc')
        config=self.page.config()['emx'];self.assertEqual(config['emx_executable'],'/manual/emx');self.assertEqual(config['process_file'],'/auto/proc')
        self.assertEqual(config['options_text'],'--3d=-*')

    def test_technology_switch_clears_only_old_automatic_paths_before_discovery(self):
        self.current['identity']=dict(technology='tech1',library='lib1',workspace='/tmp')
        self.page.refresh_source()
        self.page.v['emx_executable'].set('/auto/emx');self.page.v['process_file'].set('/auto/proc')
        self.page._auto_paths=dict(emx_executable='/auto/emx',process_file='/auto/proc')
        old_generation=self.page._discovery_generation
        self.page.v['emx_executable'].set('/manual/emx')
        self.current['identity'].update(technology='tech2',library='lib2')
        self.page.refresh_source()
        config=self.page.config()['emx']
        self.assertEqual(config['emx_executable'],'/manual/emx')
        self.assertEqual(config['process_file'],'')
        self.page._discovery_queue.put((old_generation,dict(emx_executable='/auto/emx',process_file='/auto/proc'),dict(emx_executable='/old/emx',process_file='/old/proc'),None))
        self.page._drain_discovery()
        self.assertEqual(self.page.v['emx_executable'].get(),'/manual/emx')
        self.assertEqual(self.page.v['process_file'].get(),'')

    def test_auto_paths_save_as_auto_and_old_missing_paths_do_not_become_manual(self):
        import json
        self.page.refresh_source()
        self.page._auto_paths=dict(emx_executable='/auto/emx',process_file='/auto/proc')
        self.page.v['emx_executable'].set('/auto/emx');self.page.v['process_file'].set('/auto/proc')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=str(path)):self.page.save_settings()
            data=json.loads(path.read_text())
            self.assertEqual(data['values']['emx_executable'],'');self.assertEqual(data['values']['process_file'],'')
            self.assertEqual(self.page.v['process_file'].get(),'/auto/proc')
            data['values'].pop('emx_executable');data['values'].pop('process_file');path.write_text(json.dumps(data))
            with mock.patch.object(self.page,'_discover_paths') as detect, mock.patch('emx_page.filedialog.askopenfilename',return_value=str(path)):self.page.load_settings()
            self.assertEqual(self.page.v['emx_executable'].get(),'');self.assertEqual(self.page.v['process_file'].get(),'')
            detect.assert_called_once()

    def test_manual_paths_are_saved_and_loaded_as_overrides(self):
        import json
        self.page.refresh_source();self.page._auto_paths=dict(emx_executable='/auto/emx',process_file='/auto/proc')
        self.page.v['emx_executable'].set('/manual/emx');self.page.v['process_file'].set('/manual/proc')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=str(path)):self.page.save_settings()
            data=json.loads(path.read_text());self.assertEqual(data['values']['process_file'],'/manual/proc')
            self.page.v['emx_executable'].set('/auto/emx');self.page.v['process_file'].set('/auto/proc')
            with mock.patch.object(self.page,'_discover_paths'), mock.patch('emx_page.filedialog.askopenfilename',return_value=str(path)):self.page.load_settings()
            self.assertEqual(self.page.v['emx_executable'].get(),'/manual/emx');self.assertEqual(self.page.v['process_file'].get(),'/manual/proc')
            self.assertEqual(self.page._auto_paths,{})

    def test_legacy_three_d_settings_migrate(self):
        self.page.refresh_source()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            import json
            path.write_text(json.dumps(dict(schema='flux.emx.ui.settings.v1',model='ind',values=dict(extra='--foo'),three_d=True)))
            with mock.patch('emx_page.filedialog.askopenfilename',return_value=str(path)):self.page.load_settings()
            self.assertEqual(self.page.v['extra'].get(),'--3d=* --foo')

    def test_constraint_column_is_unknown_before_completed_evaluation(self):
        for status in ('failed','rejected','cancelled'):
            self.page._add_row(dict(id=status,status=status,feasible=False))
            self.assertEqual(self.page.results.set(status,'constraint'),'—')
        self.page._add_row(dict(id='infeasible',status='completed',feasible=False))
        self.assertEqual(self.page.results.set('infeasible','constraint'),'No')

    def test_display_columns_follow_structure_and_legacy_all_frequencies(self):
        self.current['definition']=dict(emx_metrics=['L','Q'])
        self.page.refresh_source()
        self.assertEqual(self.page.results.cget('displaycolumns'),('status','L','Q','values','objectives','constraint','snp'))
        self.current['definition']=dict(emx_metrics=['Lp','Ls','k','Qp','Qs'])
        self.page.refresh_source()
        self.assertEqual(self.page.results.cget('displaycolumns'),('status','Lp','Ls','k','Qp','Qs','values','objectives','constraint','snp'))
        self.current.pop('definition');self.page.refresh_source()
        self.page._add_row(dict(id='legacy',status='completed',metrics=[dict(frequency_hz=1,L=1),dict(frequency_hz=2,Q=2)]))
        expected=('status','L','Q','values','objectives','constraint','snp')
        self.assertEqual(self.page.results.cget('displaycolumns'),expected)
        self.page.sample_frequency.set('2.0');self.page._refresh_results()
        self.assertEqual(self.page.results.cget('displaycolumns'),expected)
        self.page._add_row(dict(id='another',status='completed',metrics=[dict(frequency_hz=1,Lp=3)]))
        self.assertEqual(self.page.results.cget('displaycolumns'),('status','L','Q','Lp','values','objectives','constraint','snp'))
        self.assertEqual(tuple(self.page.results.cget('columns'))[-7:],('L','Q','Lp','Ls','k','Qp','Qs'))

    def test_multicurve_legend_and_stable_selection(self):
        self.page.refresh_source();self.page.axes={
            'width':dict(name='width',sampling='linear',minimum=1,maximum=2,count=2),
            'layers.body':dict(name='layers.body',sampling='choices',values=['M1','M2'])}
        for i,metal in enumerate(('M1','M2')):
            for width in (1,2):self.page._add_row(dict(id='%s-%s'%(metal,width),status='completed',values={'width':width,'layers.body':metal},metrics=[dict(frequency_hz=1e9,L=width+i)]))
        self.page._refresh_chart_choices();self.page.chart_vars['x'].set('width');self.page.chart_vars['y'].set('L');self.page.chart_vars['y_frequency_hz'].set('1000000000.0');self.page.add_chart();self.root.update()
        chart=self.page.charts[0];self.assertEqual(chart['data']['type'],'heatmap')
        chart['spec']['style']='line';self.page.update_plot();self.root.update()
        self.assertEqual(chart['listing'].size(),2);self.assertEqual(len(chart['plot'].points),4)
        chart['listing'].selection_set(1);chart['listing'].event_generate('<<ListboxSelect>>');self.root.update()
        self.assertEqual(self.page.results.selection(),('M2-1',))
        self.page.select_row('M1-2');self.root.update();self.assertEqual(chart['plot'].highlight,'M1-2')
        self.assertEqual(chart['listing'].curselection(),(0,))
        point=chart['plot'].points[-1];x,y=chart['plot'].xy(*point[:2]);chart['plot'].event_generate('<ButtonPress-1>',x=int(x),y=int(y));chart['plot'].event_generate('<ButtonRelease-1>',x=int(x),y=int(y));self.root.update()
        self.assertEqual(self.page.results.selection(),('M2-2',))

    def test_sync_model_isolation_and_same_model_keeps_settings(self):
        self.page.refresh_source()
        self.page.axes={'width':dict(name='width',sampling='linear',minimum=2,maximum=4,count=3)}
        self.current['fields'][0]['current']=3
        self.page.refresh_source()
        self.assertEqual(self.page.source['fields'][0]['current'],3)
        self.assertIn('width',self.page.axes)
        self.current['design']['model']='other'
        self.page.objectives.insert('1.0','L, 1e9, maximize')
        self.page.refresh_source()
        self.assertEqual(self.page.axes,{})
        self.assertEqual(self.page.objectives.get('1.0','end').strip(),'')

    def test_sync_failure_keeps_framework_usable(self):
        self.page.get_source=mock.Mock(side_effect=ValueError('Apply and validate first'))
        self.page.refresh_source()
        self.assertIn('Apply',self.page.source_text.get())
        self.assertIsNone(self.page.source)

    def test_candidate_arrival_keeps_charts_empty_and_preserves_log(self):
        self.page._add_row(dict(id='c1',status='completed',values={'width':2},metrics=[dict(frequency_hz=1e9,L=2.5)],log_path='/actual/emx.log'))
        self.page.select_row('c1');self.page.update_plot()
        self.assertFalse(self.page.charts);self.assertEqual(self.page.rows['c1']['log_path'],'/actual/emx.log')

    def test_scroll_and_resize_at_supported_sizes(self):
        from app import TransformerApp
        controller=TransformerApp.__new__(TransformerApp);controller.root=self.root;controller._styles()
        for size in ('1024x700','1280x900'):
            self.root.geometry(size);self.root.update()
            sidebar=self.page.sidebar_scroll
            sidebar.canvas.yview_moveto(1);self.root.update()
            self.assertGreaterEqual(sidebar.canvas.yview()[1],.99)
            for widget in self.page.chart_toolbar_buttons+[self.page.chart_selector,self.page.frequency_combo]+list(self.page.chart_combos.values()):
                self.assertGreaterEqual(widget.winfo_rootx(),sidebar.winfo_rootx())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),sidebar.winfo_rootx()+sidebar.winfo_width(),str(widget))
            self.assertTrue(self.page.frequency_combo.winfo_ismapped())
            sidebar.canvas.yview_moveto(0);self.root.update()
            for section in self.page.section_widgets.values():
                section['toggle'].invoke();self.root.update();section['toggle'].invoke();self.root.update()
            self.assertLessEqual(sidebar.canvas.yview()[0],.01)
        self.page.advanced_open.set(True);self.page._toggle_advanced();self.root.update()
        self.assertTrue(self.page.advanced.winfo_ismapped())

    def test_sidebar_wheel_does_not_capture_nested_scrollers_or_plot(self):
        sidebar=self.page.sidebar_scroll
        self.root.update()
        sidebar.canvas.yview_moveto(0);self.root.update()
        sidebar.canvas.event_generate('<Button-5>');self.root.update()
        self.assertGreater(sidebar.canvas.yview()[0],0)
        position=sidebar.canvas.yview()[0]
        for widget in (self.page.field_tree,self.page.objectives,self.page.constraints):
            widget.event_generate('<Button-5>');self.root.update()
            self.assertEqual(sidebar.canvas.yview()[0],position)
        self.assertNotIn(sidebar.canvas.bindtags()[0],self.page.field_tree.bindtags())
        self.assertNotIn(sidebar.canvas.bindtags()[0],self.page.objectives.bindtags())

    def test_sidebar_wheel_binding_lifecycle(self):
        from emx_page import ScrollSettings
        for factory in (lambda: ScrollSettings(self.root), lambda: EMXPage(self.root,lambda:copy.deepcopy(self.current))):
            for _ in range(3):
                widget=factory();sidebar=widget if isinstance(widget,ScrollSettings) else widget.sidebar_scroll
                sidebar.install_wheel_bindings();sidebar.install_wheel_bindings()
                tag=sidebar._wheel_tag;sequences=sidebar._wheel_sequences
                commands=list(sidebar._wheel_commands)
                self.assertEqual(len(commands),len(sequences))
                self.assertEqual(sidebar.canvas.bindtags().count(tag),1)
                for sequence in sequences:self.assertTrue(self.root.bind_class(tag,sequence))
                widget.destroy()
                for sequence in sequences:self.assertFalse(self.root.bind_class(tag,sequence))
                for command in commands:self.assertFalse(self.root.tk.call('info','commands',command))
        self.assertTrue(self.root.bind_class(self.page.sidebar_scroll._wheel_tag,'<Button-5>'))

    def test_start_stop_events_selection_plot_hover_export_and_log(self):
        class FakeCampaign:
            run_id='fixture';is_running=False
            def __init__(self,source,config):
                self.source=source;self.config=config;self.events=queue.Queue();self.cancelled=False
            def start(self):self.is_running=True
            def cancel(self):self.cancelled=True
        self.page.refresh_source();self.page.v['output'].set('/tmp')
        self.page.axes={'width':dict(name='width',sampling='linear',minimum=1,maximum=3,count=3)}
        with mock.patch('emx_campaign.Campaign',FakeCampaign):self.page.start()
        campaign=self.page.campaign
        self.assertTrue(campaign.is_running)
        self.assertEqual(str(self.page.progress['mode']),'indeterminate')
        self.current['fields'][0]['current']=9
        self.assertEqual(campaign.source['fields'][0]['current'],2)
        row=dict(id='fixture-1',status='completed',values={'width':2},source=dict(library='lib',cell='cell',view='layout'),metrics=[dict(frequency_hz=1e9,L=2.5),dict(frequency_hz=2e9,L=3)],objective_values=[2.5,3],snp_path='/tmp/fixture.s2p',log_path=None)
        campaign.events.put(dict(type='candidate',run_id='fixture',row=row))
        campaign.events.put(dict(type='progress',run_id='fixture',completed=1,total=2))
        self.page.after_cancel(self.page._after);self.page._after=None;self.page._poll();self.root.update();self.page.select_row('fixture-1');self.page.update_plot();self.root.update()
        self.assertFalse(self.page.charts)
        self.page._refresh_chart_choices();self.page.chart_vars['x'].set('width');self.page.chart_vars['y'].set('L');self.page.chart_vars['y_frequency_hz'].set('1000000000.0');self.page.add_chart();self.root.update()
        plot=self.page.charts[0]['plot'];point=plot.points[0];x,y=plot.xy(*point[:2])
        plot.event_generate('<Motion>',x=int(x),y=int(y));self.root.update();self.assertIn('fixture-1',plot.readout.get())
        before=plot.view[:];plot.event_generate('<Button-4>',x=int(x),y=int(y));self.root.update();self.assertNotEqual(before,plot.view)
        self.page.copy_layout_identity();self.assertEqual(self.root.clipboard_get(),'lib/cell/layout')
        self.page.copy_snp();self.assertEqual(self.root.clipboard_get(),'/tmp/fixture.s2p')
        with tempfile.TemporaryDirectory() as directory:
            out=str(Path(directory)/'plot.csv')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=out):self.page.export_csv()
            self.assertIn('fixture-1',Path(out).read_text())
            log=Path(directory)/'emx.log';log.write_text('fixture log')
            self.page.rows['fixture-1']['log_path']=str(log)
            with mock.patch.object(self.page,'_log_window') as show:self.page.open_candidate_log()
            self.assertIn('fixture log',show.call_args[0][1])
            save=str(Path(directory)/'settings.json')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=save):self.page.save_settings()
            self.assertTrue(Path(save).exists())
            with mock.patch('emx_page.filedialog.askopenfilename',return_value=save):self.page.load_settings()
        self.page.stop();self.assertTrue(campaign.cancelled);campaign.is_running=False

    def test_default_output_follows_workspace_but_explicit_path_is_preserved(self):
        self.current['identity']=dict(workspace='/tmp/project-a')
        self.page.refresh_source()
        suffix=Path(__import__('emx_page').__file__).resolve().parent.name+'_runtime'
        self.assertEqual(self.page.v['output'].get(),str(Path('/tmp/project-a')/suffix/'emx_campaigns'))
        self.current['identity']['workspace']='/tmp/project-b';self.page.refresh_source()
        self.assertEqual(self.page.v['output'].get(),str(Path('/tmp/project-b')/suffix/'emx_campaigns'))
        self.page.v['output'].set('/tmp/custom');self.current['identity']['workspace']='/tmp/project-c';self.page.refresh_source()
        self.assertEqual(self.page.v['output'].get(),'/tmp/custom')
        self.page.v['output'].set('');self.page.refresh_source()
        self.assertEqual(self.page.v['output'].get(),str(Path('/tmp/project-c')/suffix/'emx_campaigns'))
        with tempfile.TemporaryDirectory() as directory:
            settings=str(Path(directory)/'settings.json')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=settings):self.page.save_settings()
            self.current['identity']['workspace']='/tmp/project-d';self.page.refresh_source()
            with mock.patch('emx_page.filedialog.askopenfilename',return_value=settings):self.page.load_settings()
            self.assertEqual(self.page.v['output'].get(),str(Path('/tmp/project-d')/suffix/'emx_campaigns'))

    def test_pareto_labels_use_frozen_campaign_not_edited_goals(self):
        class Frozen:
            is_running=False
            config={'objectives':[dict(metric='Lp',frequency_hz=2e9,direction='maximize'),dict(metric='Qp',frequency_hz=3e9,direction='target',target=20)]}
        self.page.campaign=Frozen()
        self.page.objectives.insert('1.0','L, 5e9, minimize')
        self.assertEqual(self.page._objective_label(0),'Lp (nH) · 2e+09 Hz · maximize')
        self.assertEqual(self.page._objective_label(1),'Qp · 3e+09 Hz · target 20')
        self.assertEqual(self.page._objective_label(2),'Objective 3')
        self.page.campaign=None

    def test_real_algorithm_combobox_drives_real_optimizer(self):
        from tkinter import ttk
        from emx_search import SearchSpace, Optimizer
        self.page.refresh_source()
        self.page.axes={'width':dict(name='width',enabled=True,sampling='linear',minimum=1,maximum=10,count=10,group='')}
        self.page.mode.set('optimize');self.page.objectives.insert('1.0','L, 1e9, maximize')
        self.page.opt['population'].set('4');self.page.opt['generations'].set('3')
        combo=next(w for w in self.page.advanced.winfo_children() if isinstance(w,ttk.Combobox))
        self.assertEqual(combo.cget('values'),('nsga2','de','bayesian'))
        for algorithm in combo.cget('values'):
            with self.subTest(algorithm=algorithm):
                combo.set(algorithm)
                config=self.page.config()
                optimizer=Optimizer(SearchSpace(self.page.source['fields'],config['axes']),**config['optimizer'])
                first=optimizer.ask()
                optimizer.tell([dict(values=values,objectives=[float(values['width'])],violation=0,ok=True) for values in first])
                self.assertTrue(optimizer.ask())

    def test_parameter_edit_goal_metric_filter_and_log_window(self):
        self.current['definition']=dict(emx_metrics=['L','Q'])
        self.page.refresh_source()
        self.page.field_tree.selection_set('width');self.page._selected_field()
        self.page.edit['minimum'].set('2');self.page.edit['maximum'].set('4');self.page.edit['count'].set('3')
        self.page.set_axis();self.assertEqual(self.page.axes['width']['count'],3)
        self.page._refresh_chart_choices();self.assertEqual(self.page.chart_combos['y'].cget('values'),('L','Q'))
        with self.assertRaises(ValueError):self.page._parse_goals('Lp, 1e9, maximize')
        self.assertEqual(self.page._parse_goals('Q, 1e9, maximize')[0]['metric'],'Q')
        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'input.log';log.write_text('actual fixture log')
            self.page._add_row(dict(id='x',source={},log_path=str(log)));self.page.select_row('x')
            self.page.open_candidate_log();self.root.update()
            windows=[w for w in self.page.winfo_children() if isinstance(w,tk.Toplevel)]
            self.assertEqual(len(windows),1)
            text=next(w for w in windows[0].winfo_children() if isinstance(w,tk.Text))
            self.assertIn('actual fixture log',text.get('1.0','end'))
            target=str(Path(directory)/'saved.log')
            with mock.patch('emx_page.filedialog.asksaveasfilename',return_value=target):self.page._save_text(text.get('1.0','end-1c'))
            self.assertEqual(Path(target).read_text(),'actual fixture log')
            windows[0].destroy()

    def test_progress_stages_counts_and_cancel_do_not_invent_completion(self):
        class Fixture:
            run_id='run';is_running=False
        self.page.campaign=Fixture()
        self.page._event(dict(type='state',run_id='run',stage='exporting_layout',message='candidate-1 exporting layout'))
        self.assertIn('exporting layout',self.page.status.get())
        self.page._event(dict(type='progress',run_id='run',completed=0,total=10))
        self.assertEqual(str(self.page.progress['mode']),'determinate')
        self.page._event(dict(type='progress',run_id='run',completed=2,total=10))
        rows=[dict(id='a',status='completed'),dict(id='b',status='failed')]
        self.page._event(dict(type='complete',run_id='run',status='cancelled',rows=rows,pareto_ids=[],counts=dict(completed=1,failed=1,rejected=0,cancelled=0)))
        self.assertEqual(float(self.page.progress['value']),2)
        self.assertEqual(float(self.page.progress['maximum']),10)
        self.assertIn('1 completed, 1 failed',self.page.status.get())
        self.page._event(dict(type='complete',run_id='run',status='partial',rows=rows,pareto_ids=[],counts=dict(completed=1,failed=1,rejected=0,cancelled=0)))
        self.assertEqual(float(self.page.progress['value']),2)
        self.assertEqual(float(self.page.progress['maximum']),2)
        self.page.campaign=None

    def test_candidate_details_displays_warnings_and_public_data_only(self):
        row=dict(id='w',status='completed',warnings=[dict(message='spacing insufficient')],error='rejected',errors=[dict(part='coil',fields=['width'],code='too_small')],
                 source=dict(library='lib',cell='cell',view='layout',**{'secret': 'do not show'}),
                 design=dict(parameters=dict(width=2),layers=dict(body='M1'),structure='layout',**{'secret': 'do not show'}),
                 metrics=[dict(frequency_hz=1e9,L=2.5)])
        self.page._add_row(row);self.page.select_row('w')
        self.assertEqual(self.page.results.item('w','values')[0], 'completed (1 warnings)')
        self.assertNotIn('\\u26',self.page.results.item('w','values')[0])
        self.assertIn('warning',self.page.results.item('w','tags'))
        self.page.candidate_details();self.root.update()
        window=next(w for w in self.page.winfo_children() if isinstance(w,tk.Toplevel))
        text=next(w for w in window.winfo_children() if isinstance(w,tk.Text))
        content=text.get('1.0','end')
        for part in ('spacing insufficient','width','M1','nH','lib','too_small','coil','rejected'):self.assertIn(part,content)
        self.assertNotIn('do not show',content)
        self.assertEqual(str(text.cget('state')),'disabled')
        window.destroy()

    def test_close_waits_for_owned_campaign_and_drains_events(self):
        class Campaign:
            is_running=True
            run_id='mine'
            events=queue.Queue()
            def cancel(self):self.cancelled=True
        campaign=Campaign();self.page.campaign=campaign
        called=[];self.page.request_close(lambda:called.append(True))
        self.assertTrue(campaign.cancelled);self.assertFalse(called)
        campaign.events.put(dict(type='state',run_id='other',message='wrong'))
        campaign.events.put(dict(type='complete',run_id='mine',status='cancelled',rows=[],pareto_ids=[]))
        campaign.is_running=False;self.page.after_cancel(self.page._after);self.page._after=None;self.page._poll()
        self.assertEqual(called,[True]);self.assertNotIn('wrong',self.page.log_lines)


if __name__=='__main__':unittest.main()

class SurfacePlotTests(unittest.TestCase):
    def test_heatmap_complete_grid_leaves_holes_and_picks_tile(self):
        from emx_page import Plot
        root=tk.Tk();root.geometry('600x500');selected=[]
        plot=Plot(root,selected.append);plot.pack(fill='both',expand=True);plot.chart_type='heatmap';plot.x_grid=[0,1,2];plot.y_grid=[0,1,2]
        plot.set_points([(0,0,'a',0,'#ff0000'),(2,0,'b',0,'#00ff00')],'X','Y');root.update()
        first=plot._cell_bounds(0,0);missing=plot._cell_bounds(1,0)
        self.assertAlmostEqual(first[2],missing[0]);self.assertLess(first[2]-first[0],plot.winfo_width()/2)
        event=type('E',(),dict(x=first[0]+5,y=first[1]+5))();plot.press(event);plot.release(event);self.assertEqual(selected,['a'])
        hole=type('E',(),dict(x=(missing[0]+missing[2])/2,y=(missing[1]+missing[3])/2))();self.assertIsNone(plot.nearest(hole))
        plot.view=[-.25,.25,-.25,.25];plot.draw()
        for item in plot.find_all():
            if plot.type(item)=='rectangle' and plot.itemcget(item,'fill')=='#ff0000':
                x0,y0,x1,y1=plot.coords(item);self.assertGreaterEqual(x0,65);self.assertLessEqual(x1,plot.winfo_width()-20);self.assertGreaterEqual(y0,plot.top_margin);self.assertLessEqual(y1,plot.winfo_height()-40)
        root.destroy()

    def test_heatmap_labels_measured_values_and_first_duplicate_only(self):
        from emx_page import Plot
        root=tk.Tk();root.geometry('600x500');plot=Plot(root,lambda ident:None)
        plot.pack(fill='both',expand=True);plot.chart_type='heatmap';plot.x_grid=[0,1,2];plot.y_grid=[0,1]
        plot.color_values={'first':.289487,'duplicate':999.,'other':-0.44573}
        plot.set_points([(0,0,'first',0,'#40aadd'),(0,0,'duplicate',0,'#ff8833'),(2,0,'other',0,'#ff9933')],'X','Y');root.update()
        labels=plot.find_withtag('heatmap_value');self.assertEqual(len(labels),2)
        self.assertEqual(plot.itemcget(plot.find_withtag('heatmap_value:first')[0],'text'),'0.2895')
        self.assertEqual(plot.itemcget(plot.find_withtag('heatmap_value:other')[0],'text'),'-0.4457')
        self.assertFalse(plot.find_withtag('heatmap_value:duplicate'))
        plot.highlight='duplicate';plot.draw()
        self.assertEqual(plot.itemcget(plot.find_withtag('heatmap_value:duplicate')[0],'text'),'999')
        self.assertFalse(plot.find_withtag('heatmap_value:first'))
        self.assertIsNone(plot.nearest(type('E',(),dict(x=sum(plot._cell_bounds(1,0)[::2])/2,y=sum(plot._cell_bounds(1,0)[1::2])/2))()))
        root.destroy()

    def test_surface_rotation_zoom_selection_and_cloud(self):
        from emx_page import Plot
        root=tk.Tk();root.geometry('800x600');selected=[]
        plot=Plot(root,selected.append);plot.pack(fill='both',expand=True)
        plot.chart_type='surface';plot.zlabel='Z (H)';plot.surface_points=[dict(id='a',x=1.,y=1.,z=2.),dict(id='b',x=2.,y=1.,z=3.),dict(id='c',x=1.,y=2.,z=4.)];plot.triangles=[(0,1,2)]
        plot.set_points([(p['x'],p['y'],p['id'],0,'#ffffff') for p in plot.surface_points],'X','Y');root.update()
        self.assertEqual(len(plot.find_withtag('mesh')),1);self.assertEqual(len(plot.find_withtag('surface_point')),3)
        x,y,_=plot.surface_screen[0];event=type('E',(),dict(x=x,y=y))();plot.press(event);plot.release(event)
        self.assertEqual(selected,['a']);plot.highlight='a';plot.draw()
        start=type('E',(),dict(x=200,y=200))();end=type('E',(),dict(x=250,y=230))();plot.press(start);plot.pan(end);plot.release(end)
        self.assertNotEqual(plot.azimuth,-.7);self.assertEqual(selected,['a'])
        plot.wheel(type('E',(),dict(num=4,delta=0))());self.assertGreater(plot.surface_zoom,1.)
        plot.reset();self.assertEqual((plot.azimuth,plot.elevation,plot.surface_zoom),(-.7,.5,1.))
        plot.triangles=[];plot.draw();self.assertFalse(plot.find_withtag('mesh'));self.assertEqual(len(plot.find_withtag('surface_point')),3)
        root.destroy()
