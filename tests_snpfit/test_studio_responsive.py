"""Opt-in geometry regression. Run with SNPFIT_GUI_TESTS=1 and a private DISPLAY."""
import os
import tempfile
import time
import unittest
import tkinter as tk

from snpfit import studio_state
from snpfit.studio_gui import App


@unittest.skipUnless(os.environ.get('SNPFIT_GUI_TESTS') == '1', 'private-display GUI test is opt-in')
class StudioResponsiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='snpfit-responsive-test-')
        self.root = tk.Tk()
        self.app = App(self.root, runtime=self.directory.name)
        data = studio_state.new_page('Known sampled response')
        data['config'].update(model_id='inductor_se_gnd', port_order='1,2', ports_confirmed=True,
                              source_path='/tmp/known-response.s2p', l_pct='5', q_pct='20')
        data['source'] = {'path':'/tmp/known-response.s2p','sha256':'known-source','nports':2,
                          'point_count':4,'positive_point_count':4,'fmin_ghz':1.,'fmax_ghz':50.}
        data['source_valid'] = True
        values = {'l_h':[1e-9,1.01e-9,1.1e-9,1.2e-9], 'q':[2.,4.,7.,5.], 'r_ohm':[3.,4.,7.,15.]}
        snap = {'id':'sampled-circuit','stage':'tune','status':'ACCEPTED','passed':True,
                'native_verified':False,'metrics':{},'warnings':[],
                'circuit':{'name':'handwritten','ports':['P','N'],'reference':'REF',
                           'elements':[{'kind':'res','name':'r1','n1':'P','n2':'mid','value':2.},
                                       {'kind':'ind','name':'l1','n1':'mid','n2':'N','value':1e-9},
                                       {'kind':'cap','name':'c1','n1':'P','n2':'N','value':1e-13}]},
                'curves':{'freq_hz':[1e9,5e9,20e9,50e9], 'source':{'winding':values},
                          'fitted':{'winding':values}, 'source_k':None,'fitted_k':None}}
        data['results']['current']=snap
        self.page = self.app.new_page(data=data)
        self.root.geometry('1280x800')
        self.settle()

    def settle(self):
        for _ in range(4):
            self.root.update();time.sleep(.015)
        self.page.comparison.canvas.draw();self.root.update()

    def tearDown(self):
        self.app.job=None
        if self.app._poll_id:
            self.root.after_cancel(self.app._poll_id)
        self.root.destroy();self.directory.cleanup()

    def test_four_actual_axes_remain_readable_at_1280(self):
        self.assertEqual(len(self.page.comparison.axes), 4)
        self.assertGreaterEqual(self.page.comparison.canvas.get_tk_widget().winfo_height(),330)
        self.assertGreaterEqual(min(ax.get_window_extent().height for ax in self.page.comparison.axes),80)
        self.assertFalse(self.page.progress_row.winfo_ismapped())
        self.assertFalse(self.page.decorative_title.winfo_ismapped())
        self.assertFalse(self.page.hint_label.winfo_ismapped())
        for key in ('short_unused','ports_confirmed'):
            control=self.page.controls[key]
            self.assertIn('\n', control.cget('text'))
            self.assertLessEqual(control.winfo_reqwidth(),self.page.sidebar.content.winfo_width()-20)

    def test_resize_restores_decorations_and_progress_has_a_job_owner(self):
        self.root.geometry('1440x940');self.settle()
        self.assertTrue(self.page.decorative_title.winfo_ismapped())
        self.assertTrue(self.page.hint_label.winfo_ismapped())
        self.app.job={'token':99,'page_id':self.page.id,'action':'tune','started':time.monotonic()}
        self.page.refresh_buttons();self.root.update()
        self.assertTrue(self.page.progress_row.winfo_ismapped())
        self.app.job=None;self.page.refresh_buttons();self.root.update()
        self.assertFalse(self.page.progress_row.winfo_ismapped())
        self.root.geometry('1280x800');self.settle()
        self.assertGreaterEqual(min(ax.get_window_extent().height for ax in self.page.comparison.axes),80)
        self.assertIn('~5 s',self.page.controls['quick'].cget('text'))


if __name__=='__main__':unittest.main()
