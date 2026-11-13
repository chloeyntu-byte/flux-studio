"""Controlled presentation fixtures, never presented as electromagnetic data."""
import copy
import itertools
import os
import tkinter as tk
import unittest

from emx_page import EMXPage


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires private Tk display')
class HighDimensionalCharts(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('1024x700')
        self.source=dict(design=dict(model='fixture',library='fixture',cell='fixture'),
                         identity={},definition=dict(emx_metrics=['Lp','Ls','Qp','Qs','k']),
                         fields=[dict(name=n,type='float',current=0,min=0,max=2) for n in 'abcd'])
        self.page=EMXPage(self.root,lambda:copy.deepcopy(self.source));self.page.pack(fill='both',expand=True)
        # These controlled chart fixtures already contain hydrated responses.
        # Actual SNP hydration is verified separately by test_flux_result_ui.
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
        self.page.refresh_source();self.root.update()

    def tearDown(self):
        self.page.destroy();self.root.destroy()

    def test_four_parameter_sweep_keeps_all_groups_as_curves(self):
        self.page.axes={n:dict(name=n,sampling='choices',values=[0,1]) for n in 'abcd'}
        for index,values in enumerate(itertools.product((0,1),repeat=4)):
            self.page._add_row(dict(id=str(index),status='completed',values=dict(zip('abcd',values)),
                                    metrics=[dict(frequency_hz=1e9,Lp=1+sum(values)/10)]))
        self.assertEqual(self.page.charts,[])
        self.page._refresh_chart_choices()
        for key,value in dict(x='a',y='Lp',y_frequency_hz='1000000000.0').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update()
        chart=self.page.charts[0]
        self.assertEqual(chart['data']['type'],'line')
        self.assertEqual(len(chart['data']['series']),8)
        self.assertEqual(len(chart['data']['points']),16)
        self.assertEqual({p['id'] for p in chart['data']['points']},set(self.page.rows))
        self.assertTrue(all(len(series['points'])==2 for series in chart['data']['series']))

    def test_three_objective_surface_uses_front_and_measured_vertices(self):
        self.page.mode.set('optimize')
        self.page.objectives.insert('1.0','\n'.join('%s, 1e9, minimize' % m for m in ('Lp','Ls','Qp')))
        for index,values in enumerate(((1,3,3),(2,1,3),(3,2,1),(4,4,4))):
            sample=dict(zip(('Lp','Ls','Qp'),values));sample['frequency_hz']=1e9
            self.page._add_row(dict(id=str(index),status='completed',values={},feasible=True,violation=0,objectives=list(values),objective_values=list(values),metrics=[sample]))
        self.page._refresh_chart_choices()
        for key,value in dict(x='Lp',y='Ls',z='Qp',x_frequency_hz='1000000000.0',y_frequency_hz='1000000000.0',z_frequency_hz='1000000000.0').items():self.page.chart_vars[key].set(value)
        self.page.add_chart();self.root.update();chart=self.page.charts[0]
        self.assertEqual(chart['data']['type'],'surface')
        self.assertEqual({p['id'] for p in chart['data']['points']},{'0','1','2'})
        self.assertEqual(len(chart['plot'].find_withtag('mesh')),1)
        self.assertEqual(len(chart['plot'].find_withtag('surface_point')),3)

    def test_four_objective_add_all_pairs_preserves_full_front(self):
        self.page.mode.set('optimize')
        self.page.objectives.insert('1.0','\n'.join('%s, 1e9, maximize' % m for m in ('Lp','Ls','Qp','Qs')))
        metrics=[(3,3,1,1),(1,1,3,3),(2,2,2,2),(.5,.5,.5,.5)]
        for index,values in enumerate(metrics):
            sample=dict(zip(('Lp','Ls','Qp','Qs'),values));sample.update(frequency_hz=1e9,k=.2+index/100)
            self.page._add_row(dict(id=str(index),status='completed',values={},feasible=True,violation=0,
                                    objectives=[-v for v in values],objective_values=list(values),metrics=[sample]))
        self.assertEqual(self.page.charts,[])
        self.page._refresh_chart_choices();self.page.add_all_pairs();self.root.update()
        self.assertEqual(len(self.page.charts),6)
        for chart in self.page.charts:
            self.assertEqual(chart['data']['type'],'scatter')
            self.assertEqual({p['id'] for p in chart['data']['points']},{'0','1','2'})
        self.assertEqual(len(self.page.rows),4)
        self.page.delete_chart();self.assertEqual(len(self.page.charts),5)
        self.assertEqual(len(self.page.rows),4)


if __name__=='__main__':unittest.main()
