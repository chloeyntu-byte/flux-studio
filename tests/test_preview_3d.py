"""Optional 3D preview: off by default; with a 3D engine it follows the applied
geometry, sits beside the 2D editor in Leg Pins and tracks the selected pin.

A fake engine stands in for Layout 3D Viewer, so these tests need no OpenGL.
"""
import copy
import os
from pathlib import Path
import tkinter as tk
import unittest
from unittest import mock

import app
import test_leg_pin_manager as base


class FakePreview3D(object):
    instances = []

    def __init__(self, master, style='studio', theme='dark', on_pin_click=None, **options):
        self.frame = tk.Frame(master)
        self.ok, self.reason = True, ''
        self.style, self.scenes, self.highlights = style, [], []
        self.on_pin_click = on_pin_click
        FakePreview3D.instances.append(self)

    def grid(self, *args, **kwargs):
        return self.frame.grid(*args, **kwargs)

    def set_style(self, name):
        self.style = name

    def set_scene(self, scene, fit=None, immediate=False):
        self.scenes.append(scene)

    def highlight_pin(self, name, focus=False):
        self.highlights.append(name)
        return True


class FakeEmbed(object):
    def __init__(self, ok=True, reason=''):
        self._available = (ok, reason)
        self.Preview3D = FakePreview3D

    def available(self):
        return self._available

    @staticmethod
    def flux_scene(result, technology):
        return dict(pins=[port['name'] for port in result['geometry']['ports']],
                    layers=[layer['name'] for layer in technology['layers']])


class ConfiguredButBrokenTests(unittest.TestCase):
    def test_a_configured_root_that_cannot_load_is_explained_at_start(self):
        import subprocess, sys, tempfile
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, FLUX_VL3D_ROOT=empty)
            code = ("import app, tkinter; r = tkinter.Tk(); r.withdraw(); "
                    "print(repr(app.embed), app.EMBED_NOTE)")
            out = subprocess.run([sys.executable, '-B', '-c', code], cwd=str(Path(app.__file__).parent),
                                 env=env, capture_output=True, text=True, timeout=60)
        self.assertIn('None', out.stdout)
        self.assertIn('could not be loaded', out.stdout)

    def test_toolbar_shows_the_reason_when_3d_is_configured_but_missing(self):
        with mock.patch.dict(os.environ, {'FLUX_VL3D_ROOT': '/nonexistent-vl3d'}), \
                mock.patch.object(app, 'embed', None), mock.patch.object(app, 'EMBED_NOTE', 'FLUX_VL3D_ROOT is not a folder'):
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch('emx_page.EMXPage', base.StubEMXPage):
                    controller = app.TransformerApp(root)
                self.assertFalse(hasattr(controller, 'view_combo'))
                self.assertIn('not a folder', controller.view_note.cget('text'))
                controller.closed = True
                controller.tasks.put(None)
            finally:
                root.destroy()


class DefaultOffTests(unittest.TestCase):
    def test_without_flux_vl3d_root_there_is_no_3d_at_all(self):
        if os.environ.get('FLUX_VL3D_ROOT'):
            self.skipTest('FLUX_VL3D_ROOT is set in this environment')
        self.assertIsNone(app.embed)
        self.assertIn('FLUX_VL3D_ROOT', app.EMBED_NOTE)


class Preview3DTests(base.LegPinManagerTests):
    """Reuses the Leg Pins fixture (offline library, real worker) with a fake engine."""

    def setUp(self):
        FakePreview3D.instances = []
        patcher = mock.patch.object(app, 'embed', FakeEmbed())
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def three(self):
        return FakePreview3D.instances[-1]

    def test_selector_exists_only_with_an_engine_and_3d_follows_the_geometry(self):
        self.assertTrue(hasattr(self.app, 'view_combo'))
        self.applied()
        self.assertIsNone(self.app.preview3d)                          # created on first use only
        self.assertTrue(self.app.switch_view('3D Studio'))
        view = self.three()
        self.window.update()
        self.assertEqual(view.style, 'studio')
        self.assertEqual(self.app.canvas.winfo_manager(), '')         # 3D replaces the 2D canvas
        self.assertEqual(view.frame.grid_info()['column'], 0)
        self.assertIn('L1', view.scenes[-1]['pins'])
        count = len(view.scenes)
        self.app.vars['leg_length'].set(60)
        self.applied()
        self.assertGreater(len(view.scenes), count)                    # every applied preview reaches 3D
        self.app.switch_view('3D Metal')
        self.assertEqual(view.style, 'metal')
        self.assertEqual(len(FakePreview3D.instances), 1)              # style change keeps the widget
        self.app.switch_view('2D')
        self.assertEqual(self.app.canvas.winfo_manager(), 'grid')
        self.assertEqual(view.frame.winfo_manager(), '')

    def test_leg_pins_put_2d_and_3d_side_by_side_and_mark_the_selected_pin(self):
        self.applied()
        self.app.switch_view('3D Studio')
        view = self.three()
        self.assertTrue(self.app.enter_leg_mode())
        self.window.update()
        self.assertEqual(self.app.canvas.grid_info()['column'], 0)
        self.assertEqual(view.frame.grid_info()['column'], 1)
        self.click_leg('L1', 20.0)
        self.app.add_leg_pin()
        self.settle()
        name = self.app.leg_layout['L1'][self.app.leg_pin_selected[1]]['name']
        self.assertIn(name, view.scenes[-1]['pins'])                   # leg pin edits refresh 3D
        self.app.sync_3d_pin()
        self.assertEqual(view.highlights[-1], name)
        # Clicking a pin in 3D selects it in the editor.
        self.app.leg_pin_selected = None
        view.on_pin_click('L2')
        self.assertEqual(self.app.leg_pin_selected, ('L2', 0))
        self.assertEqual(view.highlights[-1], 'L2')
        self.app.exit_leg_mode()
        self.window.update()
        self.assertEqual(self.app.canvas.winfo_manager(), '')
        self.assertEqual(view.frame.grid_info()['column'], 0)
        self.assertIsNone(view.highlights[-1])

    def test_same_named_pins_clicked_in_3d_step_through_their_instances(self):
        self.applied()
        self.app.switch_view('3D Studio')
        view = self.three()
        self.app.enter_leg_mode()
        self.app.leg_layout['L1'][0]['name'] = 'shared'
        self.app.leg_layout['L2'][0]['name'] = 'shared'
        self.app.leg_pin_selected = None
        view.on_pin_click('shared')
        first = self.app.leg_pin_selected
        view.on_pin_click('shared')
        second = self.app.leg_pin_selected
        self.assertEqual({first, second}, {('L1', 0), ('L2', 0)})

    def test_late_gl_failure_returns_to_2d_and_callbacks_are_cancelled_on_close(self):
        self.applied()
        self.app.switch_view('3D Studio')
        view = self.three()
        self.assertIsNotNone(self.app._check3d_after)
        view.ok, view.reason = False, 'GLX context failed'      # what Preview3D does on a failed first map
        self.app._check_3d()
        self.assertEqual(self.app.view_mode.get(), '2D')
        self.assertEqual(self.app.canvas.winfo_manager(), 'grid')
        self.assertIn('GLX context failed', self.app.status.get())
        view.ok = True
        self.app.switch_view('3D Studio')
        self.app.push_3d()
        self.assertIsNotNone(self.app._pin3d_after)
        pending = [self.app._pin3d_after, self.app._check3d_after]
        self.app._cancel_3d_callbacks()
        self.assertIsNone(self.app._pin3d_after)
        self.assertIsNone(self.app._check3d_after)
        scheduled = self.window.tk.call('after', 'info')
        self.assertFalse(set(pending) & set(scheduled))

    def test_2d_mode_in_leg_pins_keeps_the_single_canvas(self):
        self.enter()
        self.assertEqual(self.app.canvas.grid_info()['column'], 0)
        self.assertIsNone(self.app.preview3d)

    def test_unavailable_engine_reports_why_and_stays_in_2d(self):
        with mock.patch.object(app, 'embed', FakeEmbed(False, 'PyOpenGL is missing')):
            self.applied()
            self.assertFalse(self.app.switch_view('3D Studio'))
        self.assertEqual(self.app.view_mode.get(), '2D')
        self.assertIn('PyOpenGL is missing', self.app.status.get())
        self.assertEqual(self.app.canvas.winfo_manager(), 'grid')
        self.assertIsNone(self.app.preview3d)


# The inherited Leg Pins tests also run under the fake engine; keep only the 3D ones here.
for _name in [n for n in dir(base.LegPinManagerTests) if n.startswith('test_')]:
    if not hasattr(Preview3DTests, _name) or getattr(Preview3DTests, _name) is getattr(base.LegPinManagerTests, _name):
        setattr(Preview3DTests, _name, None)

if __name__ == '__main__':
    unittest.main()
