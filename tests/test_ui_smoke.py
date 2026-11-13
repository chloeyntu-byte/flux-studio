"""Real application construction on a private display, without EDA processes."""
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
import fieldwork_tk as fw

@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires a private X display')
class ThemeSmoke(unittest.TestCase):
    def test_flux_window_uses_theme_and_starts_offline(self):
        from app import TransformerApp
        root = tk.Tk()
        with patch('backend.load_context', side_effect=AssertionError('no native discovery on first launch')):
            app = TransformerApp(root)
            root.update()
            self.assertEqual(root._fw_accent, fw.accent('flux'))
            self.assertIsNone(app.context)
            self.assertEqual(str(app.generate_button.cget('style')), 'Primary.TButton')
            self.assertNotEqual(str(app.apply_button.cget('style')), 'Primary.TButton')
            app.custom_library()
            root.update()
            dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(dialogs), 1)
            self.assertEqual(dialogs[0]._fw_accent, fw.accent('flux'))
            dialogs[0].destroy()
            app.destroy()

    def test_independent_snpfit_window_uses_theme(self):
        from snpfit.studio_gui import App
        root = tk.Tk()
        with tempfile.TemporaryDirectory() as runtime:
            app = App(root, runtime=runtime)
            root.update()
            self.assertEqual(root._fw_accent, fw.accent('flux'))
            self.assertEqual(str(app.current_page.controls['tune'].cget('style')), 'Primary.TButton')
            if app._poll_id:
                root.after_cancel(app._poll_id)
            root.destroy()
