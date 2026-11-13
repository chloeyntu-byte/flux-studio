"""Real cached-session geometry regression on a caller-selected private display.

Set SNPFIT_GUI_TESTS=1 and SNPFIT_GUI_SESSION to an existing two-page session.
No fitting, native generation or external tool is invoked.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest

from snpfit.studio_gui import App

SESSION = os.environ.get('SNPFIT_GUI_SESSION', '')


@unittest.skipUnless(os.environ.get('SNPFIT_GUI_TESTS') == '1' and SESSION and Path(SESSION).is_file(),
                     'requires an explicit private-display cached session')
class StudioSessionGeometryTests(unittest.TestCase):
    def test_async_two_page_restore_switch_resize_repeat_and_user_sash(self):
        checks=[]
        with tempfile.TemporaryDirectory(prefix='snpfit-real-session-geometry-') as runtime:
            root=tk.Tk();app=App(root,runtime=runtime);errors=[]
            root.report_callback_exception=lambda *args:errors.append(str(args))
            def settle():
                for _ in range(5):root.update();time.sleep(.02)
            def wait():
                deadline=time.monotonic()+30
                while app.job is not None and time.monotonic()<deadline:
                    root.update();time.sleep(.01)
                self.assertIsNone(app.job,'session worker did not finish')
                settle()
            def inspect_pages(geometry):
                root.geometry(geometry);settle()
                for page in list(app.pages.values()):
                    app.notebook.select(page.frame);page.views.select(0);settle()
                    item={'geometry':geometry,'page':page.data['title'],
                          'sidebar_width':page.sidebar.winfo_width(),'right_width':page.right.winfo_width(),
                          'plot_width':page.comparison.canvas.get_tk_widget().winfo_width(),
                          'sash':page.pane.sashpos(0),'font_family':app.font_family}
                    checks.append(item)
                    self.assertGreaterEqual(item['sidebar_width'],300,item)
                    self.assertGreaterEqual(item['right_width'],500,item)
                    self.assertGreaterEqual(item['plot_width'],500,item)
                    self.assertTrue(page.controls['load'].winfo_ismapped(),item)
            try:
                root.geometry('1280x800');settle()
                self.assertTrue(app.open_session(SESSION));wait()
                self.assertEqual(len(app.pages),2)
                inspect_pages('1024x720');inspect_pages('1280x800')
                page=app.current_page
                page.pane.sashpos(0,400);page.remember_sidebar_width();settle()
                self.assertAlmostEqual(page.sidebar.winfo_width(),400,delta=2)
                remembered_id=page.id
                inspect_pages('1024x720');inspect_pages('1280x800')
                self.assertAlmostEqual(app.pages[remembered_id].sidebar.winfo_width(),400,delta=2)
                saved=Path(runtime)/'remembered-session.json'
                self.assertTrue(app.save_session(saved));wait()
                self.assertTrue(app.open_session(saved));wait()
                self.assertAlmostEqual(app.pages[remembered_id].sidebar.winfo_width(),400,delta=2)
                inspect_pages('1024x720')
                self.assertTrue(app.open_session(SESSION));wait()
                inspect_pages('1280x800');inspect_pages('1024x720')
                self.assertFalse(errors,errors)
                output=os.environ.get('SNPFIT_GUI_GEOMETRY_RESULT')
                if output:
                    Path(output).write_text(json.dumps({'checks':checks,'callback_errors':errors,
                                                       'source_session':SESSION,'fits_or_native_runs':0},indent=2))
            finally:
                if app._poll_id:
                    root.after_cancel(app._poll_id)
                root.destroy()


if __name__=='__main__':unittest.main()
