import unittest

from emx_charts import axis_options, build_chart

F = 1e9
SOURCE = {'fields': [{'name': n} for n in ('a', 'b', 'c', 'd')] + [{'name':'layer','type':'enum'}, {'name':'flag','type':'bool'}]}

def axis(name, values=(0, 1, 2), group=None):
    return dict(name=name, sampling='choices', values=list(values), group=group)

def row(ident, values, l=1, q=2, status='completed', **extra):
    return dict(id=ident, values=values, status=status, metrics=[dict(frequency_hz=F,L=l,Q=q,Lp=l+1,Qs=q+1), dict(frequency_hz=2*F,L=l+10,Q=q+10)], **extra)

class ChartsTest(unittest.TestCase):
    def test_one_and_two_dimensions_and_missing_gap(self):
        config = dict(mode='sweep', axes=[axis('a')])
        rows = [row('0',{'a':0}), row('1',{'a':1},status='rejected'), row('2',{'a':2})]
        spec = dict(mode='sweep',x='a',y='L',y_frequency_hz=F)
        result = build_chart(spec,SOURCE,config,rows)
        self.assertEqual([p['id'] for p in result['points']], ['0','2'])
        self.assertTrue(result['points'][1]['break_before'])
        config['axes'].append(axis('b',(0,1)))
        result = build_chart(spec,SOURCE,config,[row('a',{'a':0,'b':0}),row('b',{'a':1,'b':1})])
        self.assertEqual(result['type'],'heatmap')
        self.assertEqual(len(result['points']),2)
        spec['style'] = 'line'
        self.assertEqual(len(build_chart(spec,SOURCE,config,[row('a',{'a':0,'b':0}),row('b',{'a':1,'b':1})])['series']),2)

    def test_three_dimensional_heatmap_conflicts_and_slice(self):
        config = dict(mode='sweep',axes=[axis('a'),axis('layer',('M1','M2')),axis('flag',(False,True))])
        spec = dict(x='a',y='Q',y_frequency_hz=F,grid_y='layer',slice='flag',slice_value=False)
        rows = [row('a',{'a':0,'layer':'M1','flag':False}),row('b',{'a':0,'layer':'M1','flag':False},q=9),row('c',{'a':1,'layer':'M2','flag':True})]
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual(out['type'],'heatmap')
        self.assertEqual(out['y_ticks'],[(0,'M1'),(1,'M2')])
        self.assertEqual(out['x_grid'],[0,1,2])
        self.assertEqual(out['y_grid'],[0,1])
        self.assertEqual([p['color'] for p in out['points']],[2,9])
        self.assertEqual(out['conflicts'][0]['ids'],['a','b'])

    def test_four_and_paired_dimensions(self):
        config = dict(mode='sweep',axes=[axis('a',group='pair'),axis('b',group='pair'),axis('c'),axis('d'),axis('layer',('M1','M2'))])
        rows = [row('a',{'a':0,'b':0,'c':0,'d':0,'layer':'M1'}),row('b',{'a':1,'b':1,'c':0,'d':0,'layer':'M1'})]
        opts = axis_options(SOURCE,config,rows)
        self.assertEqual(len(opts['dimensions']),4)
        out = build_chart(dict(x='b',y='L',y_frequency_hz=F),SOURCE,config,rows)
        self.assertEqual(out['type'],'line')
        self.assertEqual(len(out['series']),1)
        self.assertFalse(out['points'][1]['break_before'])

    def test_full_front_three_objectives_and_exact_frequency(self):
        objectives = [dict(metric='L',frequency_hz=F,direction='target',target=1),dict(metric='Q',frequency_hz=F,direction='maximize'),dict(metric='L',frequency_hz=2*F,direction='minimize')]
        config = dict(mode='optimize',objectives=objectives)
        def scored(i,scores,**kw):
            return row(i,{},feasible=True,violation=0,objectives=scores,**kw)
        rows = [scored('a',[0,2,3]),scored('b',[1,1,2]),scored('dominated',[1,3,4])]
        spec = dict(mode='optimize',x='L',x_frequency_hz=F,y='Q',y_frequency_hz=F,color='L',color_frequency_hz=2*F)
        out = build_chart(spec,SOURCE,config,rows,['dominated'])
        self.assertEqual([p['id'] for p in out['points']],['a','b'])
        self.assertEqual(out['type'],'surface')
        self.assertEqual(out['points'][0]['z'],11)
        self.assertEqual(out['triangles'], [])
        self.assertTrue(out['notes'])
        spec['color_frequency_hz'] = 2*F+1
        with self.assertRaises(ValueError): build_chart(spec,SOURCE,config,rows)

    def test_surface_measured_vertices_duplicates_and_degeneracy(self):
        config = dict(mode='optimize', objectives=[dict(metric=m,frequency_hz=F) for m in ('L','Q','Lp')])
        spec = dict(x='L',x_frequency_hz=F,y='Q',y_frequency_hz=F,z='Lp',z_frequency_hz=F)
        def make(i,x,y,z):
            r = row(i,{},l=x,q=y,feasible=True,violation=0,objectives=[0,0,0])
            r['metrics'][0]['Lp'] = z
            return r
        rows = [make('a',0,0,5),make('b',1,0,6),make('c',0,1,7),make('d',1,1,8),make('dup',0,0,5)]
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual(len(out['points']),5)
        self.assertEqual(len(out['triangles']),2)
        self.assertIn('Facets connect measured Pareto vertices',out['notes'][0])
        self.assertTrue(all(len(t)==3 and all(0 <= i < 5 for i in t) for t in out['triangles']))
        self.assertEqual(out['z_label'], 'Lp (nH) @ 1000000000.0 Hz')
        rows.append(make('multivalued',0,0,9))
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual(out['triangles'],[])
        self.assertIn('Multiple heights',out['notes'][0])
        for sample in ([make('a',0,0,1),make('b',1,1,2)],
                       [make('a',0,0,1),make('b',1,1,2),make('c',2,2,3)]):
            out = build_chart(spec,SOURCE,config,sample)
            self.assertEqual(out['triangles'],[])
            self.assertTrue(out['notes'])

    def test_surface_filters_and_metric_frequency_identity(self):
        config = dict(mode='optimize', objectives=[dict(metric=m,frequency_hz=F) for m in ('L','Q','Lp')])
        spec = dict(x='L',x_frequency_hz=F,y='L',y_frequency_hz=2*F,z='Q',z_frequency_hz=F)
        rows = [row('ok',{},feasible=True,violation=0,objectives=[0,0,0]),
                row('failed',{},status='failed',feasible=True,violation=0,objectives=[-1,-1,-1]),
                row('infeasible',{},feasible=False,violation=1,objectives=[-1,-1,-1]),
                row('nan',{},l=float('nan'),feasible=True,violation=0,objectives=[0,0,0])]
        self.assertEqual([p['id'] for p in build_chart(spec,SOURCE,config,rows)['points']],['ok'])
        spec['z'] = 'L'; spec['z_frequency_hz'] = F
        with self.assertRaises(ValueError):
            build_chart(spec,SOURCE,config,rows)

    def test_two_dimensional_heatmap_conflicts(self):
        config = dict(mode='sweep',axes=[axis('a'),axis('b')])
        spec = dict(x='a',y='Q',y_frequency_hz=F,grid_y='b')
        rows = [row('a',{'a':0,'b':1},q=2),row('b',{'a':0,'b':1},q=8)]
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual(out['type'],'heatmap')
        self.assertEqual([p['color'] for p in out['points']],[2,8])
        self.assertEqual(out['conflicts'][0]['ids'],['a','b'])
        self.assertEqual(out['x_grid'],[0,1,2])
        self.assertEqual(out['y_grid'],[0,1,2])
        self.assertNotIn(1,[p['x'] for p in out['points']])

    def test_four_objectives_projection_full_front_missing_and_failed(self):
        objectives = [dict(metric=m,frequency_hz=F,direction='maximize') for m in ('L','Q','Lp','Qs')]
        config = dict(mode='optimize',objectives=objectives)
        rows = [row('a',{},feasible=True,violation=0,objectives=[0,0,2,2]),row('b',{},feasible=True,violation=0,objectives=[1,1,0,0]),row('bad',{},feasible=True,violation=0,objectives=[2,2,3,3]),row('failed',{},status='failed',feasible=True,violation=0,objectives=[-1,-1,-1,-1])]
        spec = dict(x='L',x_frequency_hz=F,y='Q',y_frequency_hz=F)
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual(out['type'],'scatter')
        self.assertEqual([p['id'] for p in out['points']],['a','b'])
        del rows[0]['metrics'][0]['L']
        self.assertEqual([p['id'] for p in build_chart(spec,SOURCE,config,rows)['points']],['b'])

    def test_pareto_projection_can_use_nonobjective_metrics(self):
        objectives = [dict(metric=m,frequency_hz=F,direction='maximize') for m in ('L','Q','Lp')]
        config = dict(mode='optimize',objectives=objectives)
        rows = [row('a',{},feasible=True,violation=0,objectives=[0,2,1]), row('b',{},feasible=True,violation=0,objectives=[1,0,1])]
        spec = dict(x='Qs',x_frequency_hz=F,y='L',y_frequency_hz=2*F,color='Q',color_frequency_hz=F)
        out = build_chart(spec,SOURCE,config,rows)
        self.assertEqual([p['id'] for p in out['points']], ['a','b'])
        self.assertEqual(out['points'][0]['x'], 3)

    def test_single_objective_front_projects_any_metrics(self):
        config = dict(mode='optimize', objectives=[dict(metric='Q', frequency_hz=F, direction='maximize')])
        rows = [row('best', {}, l=3, q=7, feasible=True, violation=0, objectives=[-7]),
                row('other', {}, l=4, q=6, feasible=True, violation=0, objectives=[-6])]
        spec = dict(x='L', x_frequency_hz=F, y='Q', y_frequency_hz=F)
        out = build_chart(spec, SOURCE, config, rows)
        self.assertEqual(out['type'], 'scatter')
        self.assertEqual([(p['id'],p['x'],p['y']) for p in out['points']], [('best',3,7)])

    def test_search_space_values_are_identical_for_log_and_step(self):
        from emx_search import SearchSpace
        for axis_spec in [dict(name='a',sampling='log',minimum=0.1,maximum=100,count=8),
                          dict(name='a',sampling='step',minimum=0.1,maximum=0.9,step=0.1)]:
            config = dict(mode='sweep', axes=[axis_spec])
            values = list(SearchSpace(SOURCE['fields'], config['axes']).iter_sweep())
            rows = [row(str(i),v,l=i) for i,v in enumerate(values)]
            out = build_chart(dict(x='a',y='L',y_frequency_hz=F), SOURCE, config, rows)
            self.assertEqual([p['x'] for p in out['points']], [v['a'] for v in values])
            self.assertTrue(all(not p['break_before'] for p in out['points'][1:]))
            self.assertEqual(axis_options(SOURCE,config,rows)['dimensions'][0]['values'], [v['a'] for v in values])

    def test_invalid_sampling_is_not_silently_truncated(self):
        for axis_spec in [dict(name='a',sampling='step',minimum=0,maximum=10001,step=1),
                          dict(name='a',sampling='linear',minimum=1,maximum=1,count=1.0)]:
            config = dict(mode='sweep',axes=[axis_spec])
            with self.assertRaises(ValueError): axis_options(SOURCE,config,[])
        config = dict(mode='sweep',axes=[dict(name='a',sampling='linear',minimum=1,maximum=1,count=1)])
        self.assertEqual(axis_options(SOURCE,config,[])['dimensions'][0]['values'],[1.0])

    def test_labels_show_model_units_and_measurement_frequency(self):
        source = {'fields': [dict(name='a',label='Inner opening diameter',unit='um'),
                             dict(name='layer',label='Metal layer',type='enum'),
                             dict(name='flag',label='Enabled',type='bool')]}
        config = dict(mode='sweep',axes=[axis('a'),axis('layer',('M1','M2')),axis('flag',(False,True))])
        spec = dict(x='a',y='Lp',y_frequency_hz=F,grid_y='layer',slice='flag',slice_value=False)
        out = build_chart(spec,source,config,[row('a',{'a':0,'layer':'M1','flag':False})])
        self.assertEqual(out['x_label'],'Inner opening diameter (µm)')
        self.assertEqual(out['y_label'],'Metal layer')
        self.assertEqual(out['color_label'],'Lp (nH) @ 1000000000.0 Hz')
        self.assertEqual(out['series'][0]['label'],'Enabled = False')
        config = dict(mode='optimize',objectives=[dict(metric='Q',frequency_hz=F,direction='maximize')])
        scored = row('b',{},feasible=True,violation=0,objectives=[-2])
        out = build_chart(dict(x='L',x_frequency_hz=F,y='Q',y_frequency_hz=F),source,config,[scored])
        self.assertIn('(nH)',out['x_label'])
        self.assertIn('(dimensionless)',out['y_label'])

    def test_unavailable_and_ambiguous_measurement_never_zero(self):
        config = dict(mode='sweep', axes=[axis('a')])
        spec = dict(x='a',y='L',y_frequency_hz=F)
        a = row('a',{'a':0}); del a['metrics'][0]['L']
        b = row('b',{'a':1}); b['metrics'].append(dict(frequency_hz=F,L=99))
        self.assertEqual(build_chart(spec,SOURCE,config,[a,b])['points'],[])

if __name__ == '__main__': unittest.main()
