"""Pin-assigned EMX goals: editor, text form, result columns and hydration."""
import copy
import hashlib
import json
import os
import queue
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk

from emx_page import EMXPage

PINS = ['pin_1', 'pin_2', 'pin_3', 'pin_4']
COUPLED = Path(__file__).resolve().parent / 'fixtures' / 'ideal_coupled.s4p'


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires private Tk display')
class GoalPinTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.geometry('1100x760')
        self.current = dict(design=dict(model='differential_inductor', library='work', cell='base'),
                            fields=[dict(name='width', label='Width', type='float', current=2, min=1, max=10)],
                            definition=dict(emx_metrics=['Lp', 'Ls', 'k', 'Qp', 'Qs']),
                            pins=list(PINS), custom_pins=True)
        self.page = EMXPage(self.root, lambda: copy.deepcopy(self.current)); self.page.pack(fill='both', expand=True)
        self.page.refresh_source(); self.root.update()
        self.temp = tempfile.mkdtemp(prefix='flux-goal-pins-')

    def tearDown(self):
        self.page.destroy(); self.root.destroy()
        shutil.rmtree(self.temp, ignore_errors=True)

    # helpers -----------------------------------------------------------
    def visible_pins(self):
        return [box for box in self.page.goal_pin_widgets if box.winfo_manager()]

    def add(self, metric, pins, kind='Objective', rule='maximize', low='', freq='1'):
        page = self.page
        page.goal_tree.selection_remove(*page.goal_tree.selection())
        page.goal_vars['kind'].set(kind); page._goal_kind_changed()
        page.goal_vars['metric'].set(metric)
        for var, value in zip(page.goal_pin_vars, list(pins) + [''] * 4):
            var.set(value)
        page.goal_vars['frequency'].set(freq); page.goal_vars['rule'].set(rule); page.goal_vars['low'].set(low)
        page._goal_apply(False)
        return page.goal_status.get()

    def tree_metrics(self):
        return [self.page.goal_tree.set(item, 'metric') for item in self.page.goal_tree.get_children()]

    def wait(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.02)
        self.fail('timed out waiting for the page')

    def snp_fixture(self):
        from emx_adapter import read_touchstone
        path = Path(self.temp) / 'cand.s4p'; shutil.copyfile(COUPLED, path)
        frequencies = [row['frequency_hz'] for row in read_touchstone(path, 4)['rows']]
        ports = dict(signals=list(PINS), grounds=[], protocol='fixed_ground_pins',
                     terminals=[dict(name=name, kind='signal') for name in PINS])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        network = dict(ports=ports, frequencies_hz=frequencies, snp_sha256=digest)
        return path, frequencies, network

    # editor ------------------------------------------------------------
    def test_pin_selector_count_follows_metric_and_custom_notice(self):
        page = self.page
        self.assertEqual(page.goal_pin_notice.winfo_manager(), 'grid')
        self.assertEqual(tuple(page.goal_widgets['metric'].cget('values')), ('L', 'Q', 'Lp', 'Ls', 'k', 'Qp', 'Qs'))
        page.goal_vars['metric'].set('Lp'); self.root.update()
        boxes = self.visible_pins()
        self.assertEqual(len(boxes), 2)
        self.assertEqual([label.cget('text') for label in page.goal_pin_labels[:2]], ['+', '−'])
        self.assertEqual(tuple(boxes[0].cget('values')), tuple(PINS))
        self.assertIn('required', page.goal_pin_hint.get())
        page.goal_vars['metric'].set('k'); self.root.update()
        self.assertEqual(len(self.visible_pins()), 4)
        self.assertEqual([label.cget('text') for label in page.goal_pin_labels],
                         ['Pair 1 +', 'Pair 1 −', 'Pair 2 +', 'Pair 2 −'])
        # Default pins: optional, blank allowed, notice hidden.
        self.current['custom_pins'] = False; page.refresh_source(); self.root.update()
        self.assertEqual(page.goal_pin_notice.winfo_manager(), '')
        self.assertEqual(tuple(page.goal_widgets['metric'].cget('values')), ('Lp', 'Ls', 'k', 'Qp', 'Qs'))
        page.goal_vars['metric'].set('Qp'); self.root.update()
        self.assertEqual(len(self.visible_pins()), 2)
        self.assertEqual(tuple(self.visible_pins()[0].cget('values')), ('',) + tuple(PINS))
        # A source without a pin list shows no selectors.
        self.current.pop('pins'); page.refresh_source(); self.root.update()
        self.assertEqual(self.visible_pins(), []); self.assertEqual(page.goal_pin_frame.winfo_manager(), '')

    def test_custom_layout_requires_pins_and_check_pins_errors(self):
        page = self.page
        self.assertIn('choose 2 pins', self.add('Lp', []))
        self.assertIn('Choose all 2 pins', self.add('Lp', ['pin_1']))
        self.assertIn('two different pins', self.add('Lp', ['pin_1', 'pin_1']))
        self.assertIn('each pair', self.add('k', ['pin_1', 'pin_1', 'pin_3', 'pin_4']))
        self.assertEqual(self.tree_metrics(), [])
        with self.assertRaisesRegex(ValueError, 'custom leg pins'):
            page._parse_goals('Lp, 1e9, maximize')
        with self.assertRaisesRegex(ValueError, 'not in the layout'):
            page._parse_goals('Lp[pin_1,pin_9], 1e9, maximize')
        with self.assertRaisesRegex(ValueError, 'needs 4 pins'):
            page._parse_goals('k[pin_1,pin_2], 1e9, maximize')
        # Default layouts: blank pins mean the default winding pair, mixing is rejected.
        self.current['custom_pins'] = False; page.refresh_source()
        self.assertIn('leave them all blank', self.add('Lp', ['', 'pin_2']))
        self.assertIn('Saved', self.add('Lp', []))
        self.assertIn('Saved', self.add('Lp', ['pin_1', 'pin_2']))
        self.assertEqual(self.tree_metrics(), ['Lp', 'Lp[pin_1,pin_2]'])
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            page._parse_goals('L, 1e9, maximize')
        self.assertEqual(page._parse_goals('L[pin_1,pin_2], 1e9, maximize')[0]['pins'], ['pin_1', 'pin_2'])

    def test_same_metric_with_different_pins_repeats_and_duplicates_are_rejected(self):
        page = self.page
        self.assertIn('Saved', self.add('Lp', ['pin_1', 'pin_2']))
        self.assertIn('Saved', self.add('Lp', ['pin_3', 'pin_4']))
        self.assertIn('Saved', self.add('k', ['pin_1', 'pin_2', 'pin_3', 'pin_4']))
        self.assertIn('already has Lp[pin_1,pin_2]', self.add('Lp', ['pin_1', 'pin_2'], rule='minimize'))
        self.assertIn('Saved', self.add('Lp', ['pin_1', 'pin_2'], freq='2'))
        self.assertIn('Saved', self.add('Lp', ['pin_1', 'pin_2'], kind='Constraint', rule='>=', low='1'))
        self.assertIn('between', self.add('Lp', ['pin_1', 'pin_2'], kind='Constraint', rule='<=', low='3'))
        self.assertEqual(self.tree_metrics(), ['Lp[pin_1,pin_2]', 'Lp[pin_3,pin_4]', 'k[pin_1,pin_2,pin_3,pin_4]',
                                               'Lp[pin_1,pin_2]', 'Lp[pin_1,pin_2]'])
        # Selecting a row restores its pins; updating keeps other rows intact.
        page.goal_tree.selection_set('o:2'); page._goal_selected(); self.root.update()
        self.assertEqual(page.goal_vars['metric'].get(), 'k')
        self.assertEqual([v.get() for v in page.goal_pin_vars], PINS)
        page.goal_tree.selection_set('o:1'); page._goal_selected(); self.root.update()
        self.assertEqual([v.get() for v in page.goal_pin_vars[:2]], ['pin_3', 'pin_4'])
        page.goal_pin_vars[1].set('pin_1'); page._goal_apply(True)
        self.assertEqual(self.tree_metrics()[1], 'Lp[pin_3,pin_1]')
        # Updating into an existing key is a duplicate too.
        page.goal_tree.selection_set('o:1'); page._goal_selected()
        page.goal_pin_vars[0].set('pin_1'); page.goal_pin_vars[1].set('pin_2'); page._goal_apply(True)
        self.assertIn('already has', page.goal_status.get())
        config_dupes = 'Lp[pin_1,pin_2], 1e9, maximize\nLp[pin_1, pin_2], 1e9, minimize'
        page.objectives.delete('1.0', 'end'); page.objectives.insert('1.0', config_dupes)
        page.constraints.delete('1.0', 'end')
        with self.assertRaisesRegex(ValueError, 'appears twice'):
            page.config()

    def test_text_form_round_trip(self):
        page = self.page
        text = ('Lp[pin_1,pin_2], 1e9, maximize\n'
                'k[pin_1, pin_2, pin_3, pin_4], 2e9, target, 0.5, 2\n'
                'Lp[pin_3,pin_4], 1e9, minimize')
        page.objectives.insert('1.0', text)
        page.constraints.insert('1.0', 'Qp[pin_1,pin_2], 1e9, between, 5:20')
        page._goal_text_changed()
        goals = page._parse_goals(page.objectives.get('1.0', 'end'))
        self.assertEqual([g.get('pins') for g in goals], [PINS[:2], PINS, PINS[2:]])
        self.assertEqual(goals[1]['target'], .5); self.assertEqual(goals[1]['scale'], 2.0)
        config = page.config()
        self.assertEqual(config['constraints'][0]['pins'], ['pin_1', 'pin_2'])
        self.assertEqual(config['constraints'][0]['limit'], [5.0, 20.0])
        # Exporting through the editor writes the key form and parses back identically.
        for index in range(3):
            page.goal_tree.selection_set('o:%s' % index); page._goal_selected(); page._goal_apply(True)
        lines = page._goal_lines(page.objectives)
        self.assertEqual(lines[0], 'Lp[pin_1,pin_2], 1000000000, maximize')
        self.assertTrue(lines[1].startswith('k[pin_1,pin_2,pin_3,pin_4], 2000000000, target, 0.5'))
        self.assertEqual(page._parse_goals('\n'.join(lines)), goals)
        self.assertEqual(self.tree_metrics()[:3], ['Lp[pin_1,pin_2]', 'k[pin_1,pin_2,pin_3,pin_4]', 'Lp[pin_3,pin_4]'])

    def test_stale_pins_are_marked_after_source_switch(self):
        page = self.page
        self.add('Lp', ['pin_1', 'pin_2']); self.add('Ls', ['pin_3', 'pin_4'])
        self.current['pins'] = ['pin_1', 'pin_2', 'pin_5', 'pin_6']; page.refresh_source(); self.root.update()
        tags = [page.goal_tree.item(item, 'tags') for item in page.goal_tree.get_children()]
        self.assertEqual(tags[0], ''); self.assertEqual(tuple(tags[1]), ('pin_error',))
        self.assertIn('pin_3, pin_4 are not in the synced layout', page.goal_status.get())
        with self.assertRaisesRegex(ValueError, 'not in the layout'):
            page.config()
        page.goal_tree.selection_set('o:1'); page._goal_selected()
        self.assertIn('choose pins', page.goal_status.get())
        self.assertEqual([v.get() for v in page.goal_pin_vars[:2]], ['pin_3', 'pin_4'])
        page._goal_apply(True); self.assertIn('not in the layout', page.goal_status.get())
        page.goal_pin_vars[0].set('pin_5'); page.goal_pin_vars[1].set('pin_6'); page._goal_apply(True)
        self.assertEqual(page.goal_tree.item('o:1', 'tags'), '')
        self.assertEqual(self.tree_metrics(), ['Lp[pin_1,pin_2]', 'Ls[pin_5,pin_6]'])
        # Switching to a custom layout marks goals without pins.
        self.current.update(custom_pins=False); page.refresh_source()
        page.objectives.insert('end', '\nLp, 1e9, minimize'); page._goal_text_changed()
        self.current.update(custom_pins=True); page.refresh_source()
        self.assertEqual(tuple(page.goal_tree.item('o:2', 'tags')), ('pin_error',))
        self.assertIn('need assigned pins', page.goal_status.get())

    # results -----------------------------------------------------------
    def test_result_columns_follow_config_sort_and_charts(self):
        page = self.page
        page._queue_hydration = lambda ident, signature: None
        page.objectives.insert('1.0', 'Lp[pin_1,pin_2], 1e9, maximize\nLp[pin_3,pin_4], 1e9, minimize')
        page.constraints.insert('1.0', 'k[pin_1,pin_2,pin_3,pin_4], 1e9, >=, 0.3')
        page._goal_text_changed()
        keys = ('Lp[pin_1,pin_2]', 'Lp[pin_3,pin_4]', 'k[pin_1,pin_2,pin_3,pin_4]')
        self.assertEqual(tuple(page.results.cget('columns'))[-3:], keys)
        self.assertEqual(tuple(page.results.cget('displaycolumns')), ('status',) + keys + ('values', 'objectives', 'constraint', 'snp'))
        self.assertEqual(page.results.heading(keys[0], 'text'), 'Lp[pin_1,pin_2] (nH)')
        self.assertEqual(page.results.heading(keys[2], 'text'), 'k[pin_1,pin_2,pin_3,pin_4]')
        rows = [dict(id='a', status='completed', metrics=[{'frequency_hz': 1e9, keys[0]: 2.0, keys[1]: 1.5, keys[2]: .5}]),
                dict(id='b', status='completed', metrics=[{'frequency_hz': 1e9, keys[0]: 1.0, keys[1]: 1.7, keys[2]: .6}]),
                dict(id='c', status='failed', metrics=[])]
        for row in rows:
            page._add_row(row)
            page.rows[row['id']] = copy.deepcopy(row)
        page._refresh_results()
        self.assertEqual(page.results.set('a', keys[0]), '2'); self.assertEqual(page.results.set('b', keys[2]), '0.6')
        self.assertEqual(page.results.set('c', keys[1]), '—')
        page._sort_results(keys[0]); self.assertEqual(page.results.get_children(), ('b', 'a', 'c'))
        page._sort_results(keys[0]); self.assertEqual(page.results.get_children(), ('a', 'b', 'c'))
        page.mode.set('optimize'); page._refresh_chart_choices()
        self.assertEqual(tuple(page.chart_combos['y'].cget('values')), keys)
        self.assertEqual(page.chart_vars['x'].get(), keys[0]); self.assertIn(page.chart_vars['y'].get(), keys)
        from emx_charts import axis_options, _label
        self.assertIn(keys[2], axis_options(*page._chart_context(), [])['metrics'])
        self.assertEqual(_label(keys[0], 1e9), 'Lp[pin_1,pin_2] (nH) @ 1000000000.0 Hz')
        # A different campaign rebuilds the columns and drops a stale sort.
        config = dict(mode='sweep', axes=[], objectives=[dict(metric='Qs', pins=['pin_3', 'pin_4'], frequency_hz=1e9, direction='maximize')], constraints=[])
        page.campaign = SimpleNamespace(source=copy.deepcopy(self.current), config=config, run_id='other', is_running=False, events=queue.Queue())
        page.rows = {}; page.results.delete(*page.results.get_children())
        page._refresh_results()
        self.assertEqual(tuple(page.results.cget('columns'))[-1:], ('Qs[pin_3,pin_4]',))
        self.assertNotIn(keys[0], page.results.cget('columns'))
        self.assertIsNone(page.sort_column)
        self.assertEqual(page.results.heading('Qp', 'text'), 'Qp')

    def test_hydration_uses_campaign_metric_specs(self):
        page = self.page
        path, frequencies, network = self.snp_fixture()
        objectives = [dict(metric='Lp', pins=['pin_1', 'pin_2'], frequency_hz=frequencies[0], direction='maximize', scale=1.0),
                      dict(metric='Lp', pins=['pin_3', 'pin_4'], frequency_hz=frequencies[0], direction='maximize', scale=1.0)]
        constraints = [dict(metric='k', pins=PINS, frequency_hz=frequencies[0], operator='>=', limit=.5, scale=1.0)]
        config = dict(mode='optimize', axes=[], objectives=objectives, constraints=constraints, emx=dict(frequencies_hz=frequencies))
        page.campaign = SimpleNamespace(source=copy.deepcopy(self.current), config=config, run_id='run', is_running=False, events=queue.Queue())
        page._result_revision += 1
        page._add_row(dict(id='one', status='completed', snp_path=str(path), network=network, values={}))
        self.wait(lambda: page.rows['one'].get('metrics') and not page.rows['one'].get('_data_pending'))
        row = page.rows['one']
        self.assertNotIn('data_error', row)
        sample = row['metrics'][0]
        self.assertAlmostEqual(sample['Lp[pin_1,pin_2]'], 2.0, delta=.05)
        self.assertAlmostEqual(sample['Lp[pin_3,pin_4]'], 1.5, delta=.05)
        self.assertAlmostEqual(sample['k[pin_1,pin_2,pin_3,pin_4]'], .55, delta=.02)
        self.assertNotIn('Lp', sample)
        self.assertTrue(row['feasible'])
        self.assertEqual(len(row['objective_values']), 2)
        self.assertAlmostEqual(float(page.results.set('one', 'Lp[pin_1,pin_2]')), sample['Lp[pin_1,pin_2]'], places=5)
        self.assertNotEqual(page.results.set('one', 'k[pin_1,pin_2,pin_3,pin_4]'), '—')

    def test_loaded_campaign_with_pins_rebuilds_columns_and_metrics(self):
        page = self.page
        path, frequencies, network = self.snp_fixture()
        objectives = [dict(metric='Ls', pins=['pin_3', 'pin_4'], frequency_hz=frequencies[0], direction='maximize', scale=1.0)]
        constraints = [dict(metric='Qp', pins=['pin_1', 'pin_2'], frequency_hz=frequencies[0], operator='>=', limit=1, scale=1.0)]
        meta = dict(schema='flux.emx.campaign.v1', run_id='old', identity=dict(workspace='/none'), design=dict(model='differential_inductor'),
                    config=dict(mode='sweep', axes=[], objectives=objectives, constraints=constraints, emx=dict(frequencies_hz=frequencies)))
        rows = [dict(id='old-1', status='completed', snp_path=str(path), network=network, values={}, metrics=[{'L': 99}])]
        directory = Path(self.temp) / 'campaign'; directory.mkdir()
        (directory / 'campaign.json').write_text(json.dumps(meta)); (directory / 'index.json').write_text(json.dumps(rows))
        page._load_results_from_dir(str(directory))
        self.wait(lambda: 'old-1' in page.rows and page.rows['old-1'].get('metrics') and not page.rows['old-1'].get('_data_pending'))
        self.root.update()
        columns = page.results.cget('displaycolumns')
        self.assertIn('Ls[pin_3,pin_4]', columns); self.assertIn('Qp[pin_1,pin_2]', columns)
        self.assertNotIn('Lp', columns)
        self.assertAlmostEqual(float(page.results.set('old-1', 'Ls[pin_3,pin_4]')), 1.5, delta=.05)
        self.assertTrue(page.rows['old-1']['feasible'])
        self.assertEqual(page._objective_label(0), 'Ls[pin_3,pin_4] (nH) · %.6g Hz · maximize' % frequencies[0])


if __name__ == '__main__':
    unittest.main()
