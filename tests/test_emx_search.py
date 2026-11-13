import unittest
from unittest import mock
import emx_search

from emx_search import SearchSpace, Optimizer, apply_values, evaluate_response, pareto


class SearchTests(unittest.TestCase):
    def test_failed_generation_and_mixed_generation_do_not_stop_search(self):
        space = SearchSpace([{'name':'x'}], [{'name':'x','sampling':'step','minimum':0,'maximum':11,'step':1}])
        for algorithm in ('nsga2', 'de', 'bayesian'):
            with self.subTest(algorithm=algorithm):
                opt = Optimizer(space, algorithm, population=4, generations=3, seed=1)
                first = opt.ask()
                opt.tell([{'values':v, 'objectives':[], 'violation':None, 'ok':False} for v in first])
                self.assertEqual(opt.history, [])
                second = opt.ask()
                opt.tell([{'values':v, 'objectives':[float(i)] if i else [],
                           'violation':0 if i else None, 'ok':bool(i)} for i,v in enumerate(second)])
                self.assertEqual(len(opt.history), 3)
                third = opt.ask()
                self.assertEqual(len(third), 4)
                self.assertEqual(len(opt.seen), 12)

    def setUp(self):
        self.fields = [{'name': 'x', 'min': 0, 'max': 10, 'value_step': 1},
                       {'name': 'y', 'min': 0, 'max': 10, 'value_step': 1},
                       {'name': 'shape', 'options': ['square', 'circle']},
                       {'name': 'flag', 'type': 'boolean'},
                       {'name': 'optional', 'optional': True},
                       {'name': 'layers.body', 'type': 'enum', 'options': ['M1', 'M2']},
                       {'name': 'structure', 'type': 'enum', 'options': ['compact', 'wide']}]

    def test_sweep_pairs_and_independent(self):
        axes = [{'name': name, 'sampling': 'step', 'minimum': 0, 'maximum': 2.5, 'step': 1, 'group': 'pair'} for name in ('x', 'y')]
        axes.append({'name': 'shape', 'sampling': 'choices', 'values': ['square', 'circle']})
        space = SearchSpace(self.fields, axes)
        self.assertEqual(space.size, 6)
        rows = list(space.iter_sweep())
        self.assertEqual([(r['x'], r['y']) for r in rows], [(0,0),(0,0),(1,1),(1,1),(2,2),(2,2)])
        axes[1]['maximum'] = 3
        with self.assertRaises(ValueError): SearchSpace(self.fields, axes)

    def test_validation_and_empty(self):
        self.assertEqual(list(SearchSpace(self.fields, []).iter_sweep()), [{}])
        for spec in [dict(name='x', sampling='log', minimum=0, maximum=10, count=3),
                     dict(name='x', sampling='linear', minimum=0, maximum=3, count=3),
                     dict(name='flag', sampling='choices', values=[True, 1]),
                     dict(name='x', sampling='step', minimum=0, maximum=10, step=0)]:
            with self.assertRaises(ValueError): SearchSpace(self.fields, [spec])
        self.assertEqual(list(SearchSpace(self.fields, [dict(name='optional', sampling='choices', values=[None, 1])]).iter_sweep()), [{'optional':None},{'optional':1.0}])

    def test_apply_does_not_mutate(self):
        original = {'parameters': {'x': 1}, 'layers': {'body': 'M1'}, 'structure':'compact'}
        result = apply_values(original, {'x':2, 'layers.body':'M2', 'structure':'wide'})
        self.assertEqual(result, {'parameters': {'x': 2}, 'layers': {'body': 'M2'}, 'structure':'wide'})
        self.assertEqual(original['parameters']['x'], 1)

    def test_objectives_constraints_and_full_pareto(self):
        response = [{'frequency_hz': 1e9, 'L': 2, 'Q': 4}]
        metrics = evaluate_response(response, [{'metric':'Q','frequency_hz':1e9,'direction':'maximize'}, {'metric':'L','frequency_hz':1e9,'direction':'target','target':3}], [{'metric':'L','frequency_hz':1e9,'operator':'between','limit':[1, 3]}])
        self.assertEqual(metrics['objectives'], [-4, 1])
        self.assertTrue(metrics['feasible'])
        with self.assertRaises(ValueError): evaluate_response(response, [{'metric':'L','frequency_hz':1.1e9,'direction':'minimize'}], [])
        rows = [{'ok': True, 'violation':0, 'objectives':v} for v in ([1,3,3],[2,2,2],[3,1,3],[3,3,4])]
        self.assertEqual(pareto(rows), rows[:3])

    def test_algorithms_deterministic_and_exhaust(self):
        space = SearchSpace(self.fields, [{'name':'x','sampling':'step','minimum':0,'maximum':5,'step':1}])
        def run(algorithm):
            opt = Optimizer(space, algorithm, population=3, generations=10, seed=42)
            all_values = []
            for _ in range(10):
                batch = opt.ask()
                if not batch: break
                all_values.extend(v['x'] for v in batch)
                opt.tell([{'values':v,'objectives':[(v['x']-2)**2],'violation':0,'ok':True} for v in batch])
            return all_values
        for name in ('nsga2','differential_evolution','bayesian_optimization'):
            first = run(name)
            self.assertEqual(first, run(name))
            self.assertEqual(len(set(first)), 6)
            self.assertEqual(len(first), 6)

    def test_single_objective_algorithm_rejects_multi(self):
        space = SearchSpace(self.fields, [{'name':'x','sampling':'step','minimum':0,'maximum':2,'step':1}])
        for name in ('differential_evolution','bayesian_optimization'):
            opt = Optimizer(space, name, population=2)
            batch = opt.ask()
            with self.assertRaises(ValueError): opt.tell([{'values':v,'objectives':[1,2],'violation':0,'ok':True} for v in batch])


    def test_real_metadata_grid_and_continuous_log(self):
        fields = [{'name':'width', 'min':0.1, 'max':10, 'step':0.1},
                  {'name':'turns', 'min':1, 'max':8, 'value_step':0.5},
                  {'name':'n', 'integer':True, 'min':1, 'max':8}]
        values = list(SearchSpace(fields, [{'name':'width', 'sampling':'log', 'minimum':0.1, 'maximum':10, 'count':4}]).iter_sweep())
        self.assertEqual(len(values), 4)
        with self.assertRaises(ValueError): SearchSpace(fields, [{'name':'turns','sampling':'choices','values':[1,1.25]}])
        with self.assertRaises(ValueError): SearchSpace(fields, [{'name':'n','sampling':'choices','values':[1,1.5]}])
        valid = SearchSpace(fields, [{'name':'turns','sampling':'choices','values':[1,1.5]}])
        self.assertEqual(valid.size, 2)

    def test_ranking_crowding_and_feasibility_precedence(self):
        rows = [{'objectives': v, 'violation':0} for v in ([0,10], [5,5], [10,0], [8,8])]
        bad = {'objectives':[-100,-100], 'violation':0.1}
        fronts = emx_search._fronts(rows + [bad])
        self.assertEqual(fronts[0], rows[:3])
        self.assertTrue(emx_search._dominates(rows[0], bad))
        self.assertEqual(fronts[1], [rows[3]])
        selected = emx_search._select(rows[:3], 2)
        self.assertEqual(selected, [rows[0], rows[2]])
        self.assertEqual(pareto([dict(r, ok=True) for r in rows + [bad]]), [dict(r, ok=True) for r in rows[:3]])

    def test_de_uses_parent_differences_and_compares_target(self):
        space = SearchSpace([{'name':'x'}], [{'name':'x','sampling':'step','minimum':0,'maximum':30,'step':1}])
        opt = Optimizer(space, 'differential_evolution', population=4, generations=3, seed=7)
        batch = opt.ask()
        opt.tell([{'values':v,'objectives':[abs(v['x']-15)],'violation':0,'ok':True} for v in batch])
        with mock.patch.object(opt, '_random', side_effect=AssertionError('DE fell back to random')):
            trial = opt.ask()
        self.assertTrue(opt._targets)
        previous = list(opt.parents)
        opt.tell([{'values':v,'objectives':[1000],'violation':0,'ok':True} for v in trial])
        self.assertEqual(opt.parents, previous)

    def test_bayesian_fits_gaussian_process(self):
        space = SearchSpace([{'name':'x'}], [{'name':'x','sampling':'step','minimum':0,'maximum':20,'step':1}])
        opt = Optimizer(space, 'bayesian_optimization', population=3, generations=3, seed=4)
        batch = opt.ask()
        opt.tell([{'values':v,'objectives':[(v['x']-10)**2],'violation':0,'ok':True} for v in batch])
        with mock.patch('emx_search._solve', wraps=emx_search._solve) as solve:
            batch = opt.ask()
        self.assertGreater(solve.call_count, 1)
        self.assertEqual(len(batch), 3)

    def test_multiple_frequencies_and_missing_or_invalid(self):
        response = [{'frequency_hz':1e9, 'L':1, 'Q':4}, {'frequency_hz':2e9, 'L':2, 'Q':3}]
        goals = [{'metric':'L','frequency_hz':1e9,'direction':'minimize'}, {'metric':'L','frequency_hz':2e9,'direction':'maximize'}]
        limits = [{'metric':'Q','frequency_hz':2e9,'operator':'>=','limit':5,'scale':2}]
        result = evaluate_response(response, goals, limits)
        self.assertEqual(result['objectives'], [1, -2])
        self.assertEqual(result['violation'], 1)
        self.assertFalse(result['feasible'])
        for frequency, metric in [(1.01e9,'L'), (1e9,'k')]:
            with self.assertRaises(ValueError): evaluate_response(response, [{'metric':metric,'frequency_hz':frequency,'direction':'minimize'}], [])
        with self.assertRaises(ValueError): evaluate_response([{'frequency_hz':1e9,'L':float('nan')}], goals[:1], [])

    def test_large_sparse_space_does_not_enumerate(self):
        fields = [{'name':'x'}, {'name':'y'}]
        axes = [{'name':name,'sampling':'step','minimum':0,'maximum':999,'step':1} for name in ('x','y')]
        space = SearchSpace(fields, axes)
        self.assertEqual(space.size, 1000000)
        opt = Optimizer(space, 'nsga2', population=12, seed=1)
        with mock.patch('emx_search.itertools.product', side_effect=AssertionError('enumerated full space')):
            self.assertEqual(len(opt.ask()), 12)


    def test_ui_algorithm_names_two_real_generations(self):
        space = SearchSpace([{'name':'x'}], [{'name':'x','sampling':'step','minimum':0,'maximum':8,'step':1}])
        for name, canonical in [('de','differential_evolution'),('bayesian','bayesian_optimization')]:
            opt = Optimizer(space, name, population=4, generations=2, seed=1)
            self.assertEqual(opt.algorithm, canonical)
            first = opt.ask()
            opt.tell([{'values':v,'objectives':[(v['x']-4)**2],'violation':0,'ok':True} for v in first])
            second = opt.ask()
            self.assertEqual(len(second), 4)
            self.assertFalse({v['x'] for v in first} & {v['x'] for v in second})
            opt.tell([{'values':v,'objectives':[(v['x']-4)**2],'violation':0,'ok':True} for v in second])
            self.assertEqual(opt.ask(), [])


    def test_huge_factorized_space_is_lazy_for_all_algorithms(self):
        fields = [{'name':'x%d' % i} for i in range(5)]
        axes = [{'name':f['name'], 'sampling':'step','minimum':0,'maximum':9999,'step':1} for f in fields]
        space = SearchSpace(fields, axes)
        self.assertEqual(space.size, 10000**5)
        self.assertGreater(space.size, 2**63)
        first = iter(space.iter_sweep())
        self.assertEqual(next(first), dict.fromkeys([f['name'] for f in fields],0.0))
        self.assertEqual(next(first)['x4'],1.0)
        for algorithm in ('nsga2','de','bayesian'):
            opt = Optimizer(space,algorithm,population=4,generations=2,seed=5)
            with mock.patch('emx_search.itertools.product', side_effect=AssertionError('optimizer enumerated space')):
                first = opt.ask()
                opt.tell([{'values':v,'objectives':[sum(v.values())],'violation':0,'ok':True} for v in first])
                second = opt.ask()
            self.assertEqual(len(first)+len(second),8)
            self.assertEqual(len({tuple(sorted(v.items())) for v in first+second}),8)

    def test_axis_limit_applies_to_all_sampling(self):
        field = [{'name':'x'}]
        for axis in [{'name':'x','sampling':'step','minimum':0,'maximum':10000,'step':1},
                     {'name':'x','sampling':'linear','minimum':0,'maximum':1,'count':10001},
                     {'name':'x','sampling':'choices','values':list(range(10001))}]:
            with self.assertRaises(ValueError): SearchSpace(field,[axis])


    def test_collision_fallback_never_enumerates_and_exhausts(self):
        space = SearchSpace([{'name':'x'}], [{'name':'x','sampling':'step','minimum':0,'maximum':4,'step':1}])
        opt = Optimizer(space, population=4, generations=3, seed=2)
        with mock.patch.object(opt, '_random', return_value=(0,)), mock.patch('emx_search.itertools.product', side_effect=AssertionError('enumerated')):
            first = opt.ask()
            opt.tell([{'values':v,'objectives':[v['x']],'violation':0,'ok':True} for v in first])
            second = opt.ask()
            opt.tell([{'values':v,'objectives':[v['x']],'violation':0,'ok':True} for v in second])
            self.assertEqual(opt.ask(),[])
        self.assertEqual(len({v['x'] for v in first+second}), 5)


if __name__ == '__main__': unittest.main()
