"""Leg Pin manager mode on the Layout page, against the real offline backend.

The application runs with a synthetic offline technology
(tests/fixtures/generic_a.json) and its real background preview worker.  The
layout is the built-in differential inductor with its centre TAP on, so the
legs are L1, L2 and CTAP; the EMX pages are replaced by a stub so
these tests only exercise the Layout page.
"""
import copy
import json
import math
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import tkinter as tk
from tkinter import ttk

import app
import backend
import leg_pins
from pack_support import EXAMPLE_MODEL, PAIR_MODEL, install_example_pack, register_pair_transformer

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'generic_a.json'
LIBRARY = 'LegPinOffline'


class StubEMXPage(ttk.Frame):
    is_running = False

    def __init__(self, parent, _source, settings_parent=None):
        super().__init__(parent)

    def refresh_source(self):
        pass

    def request_close(self, callback):
        callback()


def install_library():
    tech = json.loads(FIXTURE.read_text(encoding='utf-8'))
    models = backend.models()
    backend.LIBRARIES[LIBRARY] = dict(
        name=LIBRARY, available=True, layers=tech['layers'], _tech=tech, warnings=[],
        model_default_layers={model['id']: backend.layer_defaults(tech, model['id']) for model in models})
    return dict(libraries=[backend.public_library(backend.LIBRARIES[LIBRARY])], models=models,
                defaults={}, presets=[])


class LegPinManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        backend.LIBRARIES.pop(LIBRARY, None)
        cls.root.destroy()

    def setUp(self):
        install_example_pack(self)
        register_pair_transformer(self)
        self.context = install_library()
        self.window = tk.Toplevel(self.root)
        with mock.patch('emx_page.EMXPage', StubEMXPage), mock.patch.object(self.window, 'after', return_value=None):
            self.app = app.TransformerApp(self.window)
        self.addCleanup(self._close)
        self.dialogs = {}
        for name, value in (('showinfo', None), ('showerror', None), ('showwarning', None), ('askyesno', True)):
            patcher = mock.patch.object(app.messagebox, name, return_value=value)
            self.dialogs[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.app._result(0, 'context', self.app.revision, True, copy.deepcopy(self.context))
        self.app.vars['center_tap'].set(True)
        self.window.update()

    def _close(self):
        # Cancel pending Tk callbacks so none fires after this interpreter is gone.
        self.app._cancel_3d_callbacks()
        for handle in (self.app.redraw_after, getattr(self.app, 'preview_after', None)):
            if handle:
                try:
                    self.window.after_cancel(handle)
                except tk.TclError:
                    pass
        self.app.redraw_after = self.app.preview_after = None
        self.app.closed = True
        self.app.tasks.put(None)
        self.window.destroy()

    # helpers --------------------------------------------------------------
    def settle(self):
        for _ in range(20):
            if not self.app.preview_busy and not self.app.leg_preview_pending:
                break
            try:
                item = self.app.results.get(timeout=60)
            except queue.Empty:
                self.fail('Preview worker did not answer.')
            self.app._result(*item)
        self.assertFalse(self.app.preview_busy)
        self.window.update()

    def applied(self, **parameters):
        for name, value in parameters.items():
            self.app.vars[name].set(value)
        self.app.apply_parameters()
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)

    def enter(self):
        self.applied()
        self.assertTrue(self.app.enter_leg_mode())

    def leg(self, leg_id):
        return next(leg for leg in self.app.leg_lines if leg['id'] == leg_id)

    def click_leg(self, leg_id, distance):
        x, y = self.app.xy(*app.leg_point(self.leg(leg_id), distance))
        return self.app.leg_canvas_click(x, y)

    def custom_ports(self):
        return {port['name']: port for port in self.app.geometry['ports'] if port.get('custom_leg_pin')}

    def codes(self, items):
        return [item.get('code') for item in items]

    # entry -----------------------------------------------------------------
    def test_entry_requires_applied_validated_unchanged_design(self):
        self.assertFalse(self.app.enter_leg_mode())
        self.assertIn('Apply Parameters first', self.dialogs['showinfo'].call_args[0][1])
        self.applied()
        self.app.vars['spacing'].set('9')
        self.assertTrue(self.app.dirty)
        self.assertFalse(self.app.enter_leg_mode())
        self.assertIn('not applied', self.dialogs['showinfo'].call_args[0][1])
        self.applied()
        self.app.generating = True
        self.assertIn('generation', self.app.leg_mode_block_reason())
        self.app.generating = False
        self.app.preview_busy = True
        self.assertIn('validation', self.app.leg_mode_block_reason())
        self.app.preview_busy = False
        self.assertTrue(self.app.enter_leg_mode())
        self.assertEqual(self.app.leg_panel.winfo_manager(), 'grid')
        self.assertEqual(self.app.notebook.winfo_manager(), '')
        self.assertEqual(str(self.app.apply_button.cget('state')), 'disabled')
        self.app.toggle_leg_mode()
        self.assertFalse(self.app.leg_mode)
        self.assertEqual(self.app.notebook.winfo_manager(), 'grid')
        self.assertEqual(self.app.leg_panel.winfo_manager(), '')

    def test_first_entry_initializes_default_layout_without_storing_it(self):
        self.enter()
        self.assertEqual([leg['id'] for leg in self.app.leg_lines], ['L1', 'L2', 'CTAP'])
        self.assertEqual(self.app.leg_layout, self.app.leg_default)
        self.assertTrue(all(len(pins) == 1 and pins[0]['name'] == leg and pins[0]['ratio'] == 0
                            for leg, pins in self.app.leg_layout.items()))
        self.assertNotIn('leg_pins', self.app.applied_design)
        self.assertEqual(self.app.leg_status.get(), 'Leg pins: default')
        self.assertEqual(set(self.app.leg_tree.get_children()), {'leg:L1', 'leg:L2', 'leg:CTAP'})

    # add / delete ----------------------------------------------------------------
    def test_add_uses_click_position_default_length_and_free_pin_name(self):
        self.enter()
        hit = self.click_leg('L1', 20.0)
        self.assertEqual(hit[:2], ('leg', 'L1'))
        self.assertAlmostEqual(self.app.leg_clicks['L1'], 20.0, delta=0.6)
        self.assertTrue(self.app.add_leg_pin())
        pin = self.app.leg_layout['L1'][-1]
        total = self.leg('L1')['length']
        self.assertEqual(pin['name'], 'pin_1')
        self.assertEqual(pin['length'], 1.0)
        self.assertAlmostEqual(pin['ratio'] * total, self.app.leg_clicks['L1'] - 0.5, places=6)
        self.assertEqual(self.app.leg_pin_selected, ('L1', 1))
        self.assertFalse(self.app.dirty)
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        port = self.custom_ports()['pin_1']
        self.assertEqual(port['leg'], 'L1')
        self.assertAlmostEqual(port['leg_distance'], pin['ratio'] * total, places=2)
        width = self.leg('L1')['width']
        box = port['bbox']
        self.assertAlmostEqual(min(box[1][0] - box[0][0], box[1][1] - box[0][1]), 1.0, places=3)
        self.assertAlmostEqual(max(box[1][0] - box[0][0], box[1][1] - box[0][1]), width, places=3)
        # No click on CTAP: the free end is taken by CTAP, so the pin goes to the first free gap.
        self.app.leg_selected = 'CTAP'
        self.assertTrue(self.app.add_leg_pin())
        added = self.app.leg_layout['CTAP'][-1]
        self.assertEqual(added['name'], 'pin_2')
        self.assertAlmostEqual(added['ratio'] * self.leg('CTAP')['length'], self.app.leg_default['CTAP'][0]['length'] + 1.0, places=6)
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        self.assertEqual(self.app.applied_design['leg_pins']['CTAP'][1]['name'], 'pin_2')
        self.assertEqual(self.app.collect_design()['leg_pins'], self.app.applied_design['leg_pins'])
        self.assertIn('Leg pins: custom · 5 pins', self.app.leg_status.get())
        self.assertIn('EMX targets must name pins', self.app.leg_status.get())

    def test_add_without_click_on_empty_leg_goes_to_free_end_and_delete_can_empty_a_leg(self):
        self.enter()
        self.app.leg_pin_selected = ('L2', 0)
        self.assertTrue(self.app.delete_leg_pin())
        self.assertEqual(self.app.leg_layout['L2'], [])
        self.assertEqual(self.app.applied_design['leg_pins'], {'L2': []})
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        self.assertNotIn('L2', [port['name'] for port in self.app.geometry['ports']])
        self.app.leg_selected = 'L2'
        self.assertTrue(self.app.add_leg_pin())
        self.assertEqual(self.app.leg_layout['L2'], [dict(name='pin_1', ratio=0.0, length=1.0)])
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        # Delete key on the canvas removes the selected pin.
        self.app.leg_pin_selected = ('L2', 0)
        self.app.canvas.focus_force()
        self.window.update()
        self.app.canvas.event_generate('<Delete>')
        self.window.update()
        self.assertEqual(self.app.leg_layout['L2'], [])

    def test_click_on_pin_selects_it_and_overlap_is_refused(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        self.app.redraw()
        region = next(r for r in self.app.leg_pin_regions() if r['name'] == 'pin_1')
        self.app.leg_pin_selected = None
        hit = self.app.leg_canvas_click(*region['point'])
        self.assertEqual(hit, ('pin', 'L1', 1))
        self.assertEqual(self.app.leg_pin_selected, ('L1', 1))
        self.assertEqual(self.app.leg_tree.selection(), ('pin:L1:1',))
        self.assertEqual(self.app.leg_name_var.get(), 'pin_1')
        self.click_leg('L1', 20.0)
        self.assertFalse(self.app.add_leg_pin())
        self.assertIn('overlap', self.dialogs['showerror'].call_args[0][1])
        self.assertEqual(len(self.app.leg_layout['L1']), 2)

    # rename ----------------------------------------------------------------
    def test_rename_rules_duplicate_prompt_and_reserved_names(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.click_leg('L2', 30.0)
        self.app.add_leg_pin()
        self.click_leg('CTAP', 30.0)
        self.app.add_leg_pin()
        self.settle()
        self.assertEqual([pin['name'] for pin in self.app.leg_layout['CTAP']], ['CTAP', 'pin_3'])
        index = next(i for i, pin in enumerate(self.app.leg_layout['L1']) if pin['name'] == 'pin_1')
        for bad, fragment in (('1bad', 'start with a letter'), ('GND_PGS', 'reserved'),
                              ('GND_RING', 'reserved'), ('CTAP', 'default name of leg CTAP'),
                              ('pin_3', 'cannot share a name')):
            self.dialogs['showerror'].reset_mock()
            self.assertFalse(self.app.rename_leg_pin('L1', index, bad))
            self.assertIn(fragment, self.dialogs['showerror'].call_args[0][1])
        self.assertEqual(self.app.leg_layout['L1'][index]['name'], 'pin_1')
        self.dialogs['askyesno'].return_value = False
        self.assertFalse(self.app.rename_leg_pin('L1', index, 'pin_2'))
        self.assertIn('one shorted terminal', self.dialogs['askyesno'].call_args[0][1])
        self.assertEqual(self.app.leg_layout['L1'][index]['name'], 'pin_1')
        self.dialogs['askyesno'].return_value = True
        self.assertTrue(self.app.rename_leg_pin('L1', index, 'pin_2'))
        self.settle()
        self.assertTrue(self.app.preview_ok)
        self.assertIn('leg_pin_duplicate_name', self.codes(self.app.messages.values()))
        self.assertTrue(any(row for row in self.app.leg_tree.get_children('leg:L1')
                            if 'shared' in self.app.leg_tree.item(row, 'text')))
        # A leg may keep its own default name on another pin.
        self.dialogs['askyesno'].reset_mock()
        self.assertTrue(self.app.rename_leg_pin('L1', index, 'L1'))
        self.dialogs['askyesno'].assert_called_once()
        with mock.patch.object(app.simpledialog, 'askstring', return_value='tap_p1'):
            self.app.leg_pin_selected = ('L1', index)
            self.assertTrue(self.app.rename_leg_pin_dialog())
        self.assertEqual(self.app.leg_layout['L1'][index]['name'], 'tap_p1')
        self.settle()
        self.assertIn('tap_p1', self.custom_ports())

    def test_pin_names_cannot_join_two_windings(self):
        # Needs two windings: a synthetic test model (P1/P2 and S1/S2), registered for this test only.
        self.app.switch_model(PAIR_MODEL)
        self.enter()
        self.assertEqual([leg['id'] for leg in self.app.leg_lines], ['P1', 'P2', 'S1', 'S2'])
        self.click_leg('P1', 20.0)
        self.app.add_leg_pin()
        self.click_leg('S1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        index = next(i for i, pin in enumerate(self.app.leg_layout['S1']) if pin['name'] == 'pin_2')
        self.assertFalse(self.app.rename_leg_pin('S1', index, 'pin_1'))
        self.assertIn('another winding', self.dialogs['showerror'].call_args[0][1])
        self.assertTrue(self.app.rename_leg_pin('S1', index, 'sec_a'))

    # geometry ----------------------------------------------------------------
    def test_position_and_length_edit_store_ratio_and_scale_with_leg_length(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        total = self.leg('L1')['length']
        index = self.app.leg_pin_selected[1]
        self.assertTrue(self.app.set_leg_pin_geometry('L1', index, '10', '2'))
        pin = next(pin for pin in self.app.leg_layout['L1'] if pin['name'] == 'pin_1')
        self.assertAlmostEqual(pin['ratio'], 10 / total, places=8)
        self.assertEqual(pin['length'], 2.0)
        self.assertFalse(self.app.set_leg_pin_geometry('L1', self.app.leg_pin_selected[1], total - 1, 2))
        self.assertIn('beyond leg L1', self.dialogs['showerror'].call_args[0][1])
        self.assertFalse(self.app.set_leg_pin_geometry('L1', self.app.leg_pin_selected[1], 'x', 2))
        self.assertFalse(self.app.set_leg_pin_geometry('L1', self.app.leg_pin_selected[1], 0, 1))
        self.assertIn('overlap', self.dialogs['showerror'].call_args[0][1])
        self.settle()
        self.assertAlmostEqual(self.custom_ports()['pin_1']['leg_distance'], 10.0, places=2)
        self.app.exit_leg_mode()
        old_extension = float(self.app.vars['leg_length'].get())
        self.applied(leg_length=str(old_extension + 30))
        self.assertIn('leg_pins', self.app.applied_design)
        new_total = self.leg('L1')['length']
        self.assertGreater(new_total, total + 20)
        self.assertAlmostEqual(self.custom_ports()['pin_1']['leg_distance'], 10 / total * new_total, delta=0.01)

    # restore -----------------------------------------------------------------
    def test_restore_single_leg_and_all_defaults(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.click_leg('CTAP', 20.0)
        self.app.add_leg_pin()
        self.assertEqual(set(self.app.applied_design['leg_pins']), {'L1', 'CTAP'})
        self.assertTrue(self.app.restore_leg_default('L1'))
        self.assertEqual(self.app.leg_layout['L1'], self.app.leg_default['L1'])
        self.assertEqual(set(self.app.applied_design['leg_pins']), {'CTAP'})
        self.assertTrue(self.app.restore_all_leg_defaults())
        self.assertNotIn('leg_pins', self.app.applied_design)
        self.assertNotIn('leg_pins', self.app.collect_design())
        self.settle()
        self.assertTrue(self.app.preview_ok)
        self.assertEqual(self.custom_ports(), {})
        self.assertEqual(self.app.leg_status.get(), 'Leg pins: default')

    # persistence -------------------------------------------------------------
    def test_export_import_and_preset_round_trip_keep_leg_pins(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        layout = copy.deepcopy(self.app.applied_design['leg_pins'])
        self.app.exit_leg_mode()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'legs.json'
            with mock.patch.object(app.filedialog, 'asksaveasfilename', return_value=str(target)):
                self.app.export_design()
            saved = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(saved['design']['leg_pins'], layout)
            self.app.restore_applied_leg_pins(confirm=False)
            self.settle()
            self.assertNotIn('leg_pins', self.app.collect_design())
            with mock.patch.object(app.filedialog, 'askopenfilename', return_value=str(target)):
                self.app.import_design()
        self.assertTrue(self.app.dirty)
        self.assertEqual(self.app.collect_design()['leg_pins'], layout)
        self.applied()
        self.assertEqual(self.app.applied_design['leg_pins'], layout)
        self.assertIn('pin_1', self.custom_ports())
        # Presets carry the same design.
        with mock.patch.object(self.app, 'submit', return_value=99) as submit:
            self.app.vars['preset_name'].set('legs')
            self.app.save_preset()
        self.assertEqual(submit.call_args[0][2]['leg_pins'], layout)
        self.app.preset_busy = False
        preset = dict(schema='tfgen.design.v2', design=backend.normalize_design(submit.call_args[0][2]))
        self.app.restore_applied_leg_pins(confirm=False)
        self.settle()
        self.app._result(5, 'load_preset', self.app.revision, True, preset)
        self.assertEqual(self.app.collect_design()['leg_pins'], layout)

    def test_leg_pins_stay_with_their_model_draft(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        layout = copy.deepcopy(self.app.applied_design['leg_pins'])
        self.app.switch_model(EXAMPLE_MODEL)
        self.assertFalse(self.app.leg_mode)
        self.assertNotIn('leg_pins', self.app.collect_design())
        self.applied()
        self.assertNotIn('leg_pins', self.app.applied_design)
        self.assertEqual([leg['id'] for leg in self.app.leg_lines][:2], ['L1', 'L2'])
        self.app.switch_model('differential_inductor')
        self.assertEqual(self.app.collect_design()['leg_pins'], layout)

    def pin_center(self, name):
        region = next(r for r in self.app.leg_pin_regions() if r['name'] == name)
        return region['point']

    def screen(self, leg_id, distance):
        return self.app.xy(*app.leg_point(self.leg(leg_id), distance))

    def test_dragging_a_pin_moves_it_along_its_leg_and_stores_the_ratio(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        name = self.app.leg_layout['L1'][-1]['name']
        start = self.pin_center(name)
        self.assertTrue(self.app.leg_drag_start(*start))
        target = self.screen('L1', 30.5)
        # Sideways jitter is ignored: the pin follows its own leg.
        self.assertTrue(self.app.leg_drag_move(target[0] + 40, target[1]))
        self.assertTrue(self.app.leg_drag_finish())
        self.settle()
        stored = next(p for p in self.app.applied_design['leg_pins']['L1'] if p['name'] == name)
        total = self.leg('L1')['length']
        self.assertAlmostEqual(stored['ratio'] * total, 30.0, delta=0.05)   # 1 um pin centred on 30.5
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        self.assertFalse(self.app.dirty)

    def test_mouse_events_drag_a_pin_but_still_pan_elsewhere(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        name = self.app.leg_layout['L1'][-1]['name']
        canvas = self.app.canvas
        x, y = (int(round(v)) for v in self.pin_center(name))
        tx, ty = (int(round(v)) for v in self.screen('L1', 35.5))
        view = list(self.app.view)
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<B1-Motion>', x=tx, y=ty)
        canvas.event_generate('<ButtonRelease-1>', x=tx, y=ty)
        self.settle()
        self.assertEqual(self.app.view, view)                       # dragging a pin does not pan
        stored = next(p for p in self.app.applied_design['leg_pins']['L1'] if p['name'] == name)
        self.assertAlmostEqual(stored['ratio'] * self.leg('L1')['length'], 35.0, delta=0.6)
        canvas.event_generate('<ButtonPress-1>', x=5, y=5)
        canvas.event_generate('<B1-Motion>', x=45, y=25)
        canvas.event_generate('<ButtonRelease-1>', x=45, y=25)
        self.assertNotEqual(self.app.view, view)                    # empty space still pans

    def test_tiny_drag_delete_during_drag_and_cancel_refresh_leave_no_stale_state(self):
        # Review T-0193.
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        before = copy.deepcopy(self.app.applied_design['leg_pins'])
        name = self.app.leg_layout['L1'][-1]['name']
        canvas = self.app.canvas
        x, y = (int(round(v)) for v in self.pin_center(name))
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<B1-Motion>', x=x, y=y - 2)
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y - 2)
        self.assertEqual(self.app._stored_layout(self.app.leg_layout), before)
        self.assertIsNone(self.app.pin_drag)
        # Delete while holding a pin: the drag ends, nothing raises.
        self.assertTrue(self.app.leg_drag_start(*self.pin_center(name)))
        self.app.leg_drag_move(*self.screen('L1', 40.0))
        self.app.leg_pin_selected = ('L1', len(self.app.leg_layout['L1']) - 1)
        self.assertTrue(self.app.delete_leg_pin())
        self.assertIsNone(self.app.pin_drag)
        self.assertFalse(self.app.leg_drag_move(*self.screen('L1', 30.0)))
        self.assertFalse(self.app.leg_drag_finish())
        self.assertFalse(self.app.leg_drag_cancel())
        self.assertNotIn(name, [p['name'] for p in self.app.leg_layout['L1']])
        self.settle()
        # Cancel refreshes the panel fields to the restored position.
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        name = self.app.leg_layout['L1'][-1]['name']
        self.assertTrue(self.app.leg_drag_start(*self.pin_center(name)))
        self.app.leg_drag_move(*self.screen('L1', 40.0))
        self.app._refresh_leg_panel()
        self.assertTrue(self.app.leg_drag_cancel())
        stored = next(p for p in self.app.leg_layout['L1'] if p['name'] == name)
        distance = stored['ratio'] * self.leg('L1')['length']
        self.assertAlmostEqual(float(self.app.leg_distance_var.get()), distance, delta=0.01)

    def test_drag_onto_another_pin_is_reverted_and_escape_cancels(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.click_leg('L1', 30.0)
        self.app.add_leg_pin()
        self.settle()
        before = copy.deepcopy(self.app.applied_design['leg_pins'])
        first, second = [p['name'] for p in self.app.leg_layout['L1'][-2:]]
        self.assertTrue(self.app.leg_drag_start(*self.pin_center(second)))
        self.app.leg_drag_move(*self.screen('L1', 20.0))
        self.assertFalse(self.app.leg_drag_finish())
        self.assertEqual(self.app.applied_design['leg_pins'], before)
        self.assertIn('not moved', self.app.status.get())
        self.assertTrue(self.app.leg_drag_start(*self.pin_center(second)))
        self.app.leg_drag_move(*self.screen('L1', 40.0))
        self.app.leg_drag_cancel()
        self.assertEqual(self.app._stored_layout(self.app.leg_layout), before)

    def test_restoring_the_applied_model_keeps_another_models_draft_pins(self):
        # Review T-0192: restore must touch only the applied model's pins.
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        layout = copy.deepcopy(self.app.applied_design['leg_pins'])
        self.app.switch_model(EXAMPLE_MODEL)
        self.app.leg_pins_draft = {'NOPE': [dict(name='pin_9', ratio=0.0, length=1.0)]}
        self.app.apply_parameters()
        self.settle()
        self.assertIn('leg_pin_unknown_leg', self.codes(self.app.preview_errors))
        self.app.switch_model('differential_inductor')
        self.assertEqual(self.app.collect_design()['leg_pins'], layout)
        self.assertTrue(self.app.restore_applied_leg_pins(confirm=False))
        self.settle()
        self.assertEqual(self.app.collect_design()['leg_pins'], layout)
        self.assertEqual(self.app.body_drafts['differential_inductor']['leg_pins'], layout)
        self.assertNotIn('leg_pins', self.app.applied_design)
        self.assertEqual(self.app.applied_design['model'], EXAMPLE_MODEL)

    def test_unknown_leg_is_reported_and_restorable(self):
        self.applied()
        self.app.leg_pins_draft = {'NOPE': [dict(name='pin_9', ratio=0.0, length=1.0)]}
        self.app.apply_parameters()
        self.settle()
        self.assertFalse(self.app.preview_ok)
        self.assertIn('leg_pin_unknown_leg', self.codes(self.app.preview_errors))
        self.assertIn('Restore default pins', self.app.status.get())
        self.assertEqual(self.app.leg_restore_button.winfo_manager(), 'pack')
        self.assertIn('leg pin issue', self.app.leg_status.get())
        self.assertTrue(self.app.enter_leg_mode())
        self.assertIn('missing', self.app.leg_tree.item('leg:NOPE', 'text'))
        self.assertTrue(self.app.restore_leg_default('NOPE'))
        self.assertNotIn('NOPE', self.app.leg_layout)
        self.settle()
        self.assertTrue(self.app.preview_ok, self.app.preview_errors)
        self.assertNotIn('leg_pins', self.app.applied_design)

    def test_edit_during_running_preview_is_revalidated_afterwards(self):
        self.enter()
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.assertTrue(self.app.preview_busy)
        self.click_leg('L1', 35.0)
        self.app.add_leg_pin()
        self.assertTrue(self.app.leg_preview_pending)
        self.settle()
        self.assertTrue(self.app.preview_ok)
        self.assertEqual(self.app.preview_revision, self.app.revision)
        self.assertEqual(set(self.custom_ports()) & {'pin_1', 'pin_2'}, {'pin_1', 'pin_2'})

    # canvas ------------------------------------------------------------------
    def test_canvas_dims_non_leg_geometry_and_highlights_pins_only_in_mode(self):
        self.applied()
        self.app.redraw()
        canvas = self.app.canvas
        self.assertFalse(canvas.find_withtag('dimmed'))
        normal_fill = {canvas.itemcget(item, 'fill') for item in canvas.find_withtag('geometry')}
        self.assertTrue(self.app.enter_leg_mode())
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        self.app.redraw()
        geometry = canvas.find_withtag('geometry')
        self.assertTrue(geometry)
        self.assertTrue(all('dimmed' in canvas.gettags(item) for item in geometry))
        dimmed_fill = {canvas.itemcget(item, 'fill') for item in geometry}
        self.assertFalse(dimmed_fill & normal_fill)
        self.assertEqual({app.dim_color(color) for color in normal_fill}, dimmed_fill)
        self.assertTrue(all('dimmed' in canvas.gettags(item) for item in canvas.find_withtag('via')))
        legs = canvas.find_withtag('leg')
        self.assertEqual(len(legs), 3)
        self.assertTrue(all(canvas.itemcget(item, 'fill') in normal_fill for item in legs))
        pins = canvas.find_withtag('leg_pin')
        self.assertEqual(len(pins), 4)
        self.assertTrue(all(canvas.itemcget(item, 'fill') == app.LEG_PIN_COLOR for item in pins))
        selected = canvas.find_withtag('leg_pin_selected')
        self.assertEqual(len(selected), 1)
        self.assertEqual(canvas.itemcget(selected[0], 'outline'), app.LEG_PIN_SELECTED)
        self.assertTrue(canvas.find_withtag('leg_selected'))
        self.assertTrue(canvas.find_withtag('leg_click'))
        labels = {canvas.itemcget(item, 'text') for item in canvas.find_withtag('leg_pin_label')
                  if canvas.type(item) == 'text'}
        self.assertEqual(labels, {'L1', 'L2', 'CTAP', 'pin_1'})
        self.app.exit_leg_mode()
        self.app.redraw()
        self.assertFalse(canvas.find_withtag('dimmed'))
        self.assertFalse(canvas.find_withtag('leg_pin'))
        self.assertTrue(canvas.find_withtag('port'))


if __name__ == '__main__':
    unittest.main()
