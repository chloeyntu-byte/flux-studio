"""English, multi-document Tk studio for independent passive SNP fitting.

Tk widgets are owned by the main thread. Every worker gets an immutable page
and request snapshot; results from obsolete requests cannot update another page.
"""
from __future__ import annotations

import fieldwork_tk as fw
from fieldwork_ui import bind_status
import copy
import datetime as dt
import json
import math
from pathlib import Path
import queue
import threading
import time
import traceback
import uuid
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

from .models import get_model, list_models
from .workflow import runtime_root
from . import studio_engine as engine
from . import studio_state as state
from .studio_views import ComparisonView, CircuitView

IDENTITY_KEYS = {'source_path', 'model_id', 'port_order', 'short_unused', 'fmin_ghz', 'fmax_ghz'}
TARGET_KEYS = {'l_pct', 'q_pct', 'k_pct', 'srf_pct', 'network_pct', 'component_limit'}
LEVEL_COLORS = {'info': fw.DIM, 'success': fw.OK, 'warning': fw.WARN, 'error': fw.ERR, 'working': fw.accent('flux')}



def inspect_ui_source(path):
    """The UI may preview a single real point; strict actions keep their gates."""
    from .network import read_touchstone
    from .sampled_fit import SampledNetwork
    source = read_touchstone(path, network_class=SampledNetwork)
    positive = source.freq[source.freq > 0]
    sampled_data = None
    if len(positive) == 1:
        index = next(i for i,v in enumerate(source.freq) if v > 0)
        sampled_data = {'frequency_hz':float(source.freq[index]), 's_real':source.s[index].real.tolist(), 's_imag':source.s[index].imag.tolist(), 'reference_ohm':source.z0.tolist()}
    return {'sampled_data':sampled_data, 'path':str(Path(path).expanduser().resolve()), 'sha256':source.metadata['sha256'],
            'nports':source.nports, 'point_count':len(source.freq), 'positive_point_count':len(positive),
            'fmin_hz':float(positive[0]) if len(positive) else None,
            'fmax_hz':float(positive[-1]) if len(positive) else None,
            'fmin_ghz':float(positive[0]/1e9) if len(positive) else None,
            'fmax_ghz':float(positive[-1]/1e9) if len(positive) else None,
            'port_names':list(source.port_names), 'reference_ohm':source.z0.tolist(),
            'metadata':copy.deepcopy(source.metadata)}

def model_label(spec):
    mode = 'Single-ended' if spec.mode == 'se' else 'Differential'
    kind = 'Inductor' if spec.device == 'inductor' else 'Transformer'
    if spec.device == 'inductor':
        tap = 'Tap' if spec.tap_roles else 'No tap'
    else:
        tap = ('Primary + secondary taps' if set(spec.tap_roles) == {'PTAP', 'STAP'} else
               'Primary tap' if 'PTAP' in spec.tap_roles else
               'Secondary tap' if 'STAP' in spec.tap_roles else 'No tap')
    return f"{mode} {kind} | {tap} | {'GND pin' if spec.explicit_reference else 'Internal reference'}"


def topology_label(candidate):
    labels = {'series_rl': 'Series RL', 'compact_pi': 'Compact RLC', 'coupled_rl': 'Coupled RL',
              'parasitic_rlck': 'Parasitic RLCK', 'substrate_skin': 'Substrate + skin effect',
              'distributed_2': 'Distributed: 2 sections', 'distributed_3': 'Distributed: 3 sections',
              'distributed_bridge': 'Distributed: cross-winding capacitance'}
    return labels.get(candidate.id, candidate.id.replace('_', ' ').title())


def _snapshots(page):
    return page.setdefault('results', {'quick': None, 'current': None, 'before_upgrade': None, 'upgraded': None})


def _current(page):
    return _snapshots(page).get('current')


def _fmt(value, suffix='', precision=4):
    try:
        number = float(value)
        return f'{number:.{precision}g}{suffix}' if math.isfinite(number) else 'N/A'
    except (TypeError, ValueError):
        return 'N/A'


class ScrollPanel(ttk.Frame):
    """A local wheel binding that does not replace other pages' bindings."""
    def __init__(self, parent, width=326):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=width, highlightthickness=0, borderwidth=0, background=fw.PANEL)
        self.scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.content = ttk.Frame(self.canvas, padding=(8, 8, 8, 8))
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self._size = None
        self._tag = 'StudioScroll_' + uuid.uuid4().hex
        self._commands = []
        self.content.bind('<Configure>', self._content_changed, add='+')
        self.canvas.bind('<Configure>', self._canvas_changed, add='+')
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            command = self.bind_class(self._tag, sequence, self._wheel, add='+')
            self._commands.append((sequence, command))
        self.bind('<Destroy>', self._destroy_bindings, add='+')

    def _content_changed(self, event):
        size = (event.width, event.height)
        if size != self._size:
            self._size = size
            self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _canvas_changed(self, event):
        width = max(50, event.width)
        if int(float(self.canvas.itemcget(self.window, 'width') or 0)) != width:
            self.canvas.itemconfigure(self.window, width=width)

    def install_wheel(self, widget=None):
        widget = self if widget is None else widget
        if not isinstance(widget, (tk.Text, ttk.Treeview, tk.Listbox)):
            tags = widget.bindtags()
            if self._tag not in tags:
                widget.bindtags((self._tag,) + tags)
        for child in widget.winfo_children():
            self.install_wheel(child)

    def _wheel(self, event):
        if getattr(event, 'num', None) == 4:
            direction = -1
        elif getattr(event, 'num', None) == 5:
            direction = 1
        else:
            delta = getattr(event, 'delta', 0)
            if not delta:
                return
            direction = -1 if delta > 0 else 1
        self.canvas.yview_scroll(direction * 3, 'units')
        return 'break'

    def _destroy_bindings(self, event):
        if event.widget is not self:
            return
        for sequence, command in self._commands:
            try:
                self.unbind_class(self._tag, sequence)
                if command:
                    self._root().deletecommand(command)
            except tk.TclError:
                pass
        self._commands.clear()


class AnalysisPage:
    def __init__(self, app, data):
        self.app, self.data = app, data
        self.id = data['id']
        self._populating = True
        self._revision = int(data.get('revision', 0))
        self._compact = False
        self._responsive_key = None
        self._metric_widgets = []
        self._comparison_caption = None
        self._comparison_caption_pack = None
        self._sidebar_pending = None
        self._sidebar_initialized = False
        self._sidebar_dragging = False
        try:
            preferred = float((data.get('view') or {}).get('sidebar_width', 326))
            self._sidebar_preferred = preferred if math.isfinite(preferred) and 100 <= preferred <= 10000 else 326
        except (TypeError, ValueError):
            self._sidebar_preferred = 326
        self.frame = ttk.Frame(app.notebook)
        self.vars = {}
        self.controls = {}
        self._editable = []
        self._topology_ids = ['auto']
        self._topology_labels = ['Automatic initial topology']
        self.status_var = tk.StringVar(value='Load an SNP to begin.')
        self.source_var = tk.StringVar(value='No source loaded.')
        self.role_var = tk.StringVar(value='Select one explicit model. Port order follows the roles shown here.')
        self.hint_var = tk.StringVar(value='Load a source, choose a model and confirm its ports.')
        self.strategy_var = tk.StringVar(value='L/Q priority is selected automatically from your targets.')
        self.elapsed_var = tk.StringVar(value='')
        self.metric_vars = {key: tk.StringVar(value='N/A') for key in ('L', 'Q', 'k', 'SRF', 'Elements')}
        self._build()
        self.populate(data)
        self._populating = False
        self.sidebar.install_wheel()
        self.refresh()

    def _group(self, parent, label):
        box = ttk.LabelFrame(parent, text=label, padding=8)
        box.pack(fill='x', pady=(0, 8))
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)
        return box

    def _entry(self, box, key, label, row, column=0, span=1, width=13, browse=None, state_value='normal'):
        frame = ttk.Frame(box)
        frame.grid(row=row, column=column, columnspan=span, sticky='ew', padx=(0, 4) if span == 1 and column == 0 else 0, pady=(0, 4))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=label).grid(row=0, column=0, columnspan=2, sticky='w')
        var = self.vars.setdefault(key, tk.StringVar())
        entry = ttk.Entry(frame, textvariable=var, width=width, state=state_value)
        entry.grid(row=1, column=0, sticky='ew')
        if browse:
            ttk.Button(frame, text='Browse...', command=browse, width=9).grid(row=1, column=1, padx=(4, 0))
        self.controls[key] = entry
        if state_value != 'readonly':
            self._editable.append(entry)
        return entry

    def _combo(self, box, key, label, row, values, span=2):
        frame = ttk.Frame(box)
        frame.grid(row=row, column=0, columnspan=span, sticky='ew', pady=(0, 4))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=label).grid(row=0, column=0, sticky='w')
        var = self.vars.setdefault(key, tk.StringVar())
        combo = ttk.Combobox(frame, textvariable=var, values=values, state='readonly')
        combo.grid(row=1, column=0, sticky='ew')
        self.controls[key] = combo
        self._editable.append(combo)
        return combo

    def _build(self):
        pane = ttk.PanedWindow(self.frame, orient='horizontal')
        pane.pack(fill='both', expand=True)
        self.sidebar = ScrollPanel(pane)
        right = ttk.Frame(pane, padding=(12, 8, 8, 8))
        self.right = right
        right.bind('<Configure>', self._right_layout_changed, add='+')
        pane.add(self.sidebar, weight=0)
        pane.add(right, weight=1)
        self.pane = pane
        pane.bind('<Map>', self._pane_mapped, add='+')
        pane.bind('<Configure>', self._pane_configured, add='+')
        pane.bind('<ButtonPress-1>', self._sash_pressed, add='+')
        pane.bind('<ButtonRelease-1>', self._sash_released, add='+')
        pane.bind('<Destroy>', self._pane_destroyed, add='+')
        content = self.sidebar.content

        source = self._group(content, '1. Source and model')
        self._entry(source, 'source_path', 'Touchstone SNP file', 0, span=2)
        self.controls['load'] = ttk.Button(source, text='Load SNP...', command=lambda: self.app.browse_snp(self.id))
        self.controls['load'].grid(row=1, column=0, sticky='ew', pady=(0, 4))
        self.controls['reload'] = ttk.Button(source, text='Reload', command=lambda: self.app.load_snp(self.vars['source_path'].get(), self.id))
        self.controls['reload'].grid(row=1, column=1, sticky='ew', padx=(4, 0), pady=(0, 4))
        ttk.Label(source, textvariable=self.source_var, wraplength=280, foreground=fw.DIM).grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 8))
        combo = self._combo(source, '_model_label', 'Model', 3, self.app.model_labels)
        combo.bind('<<ComboboxSelected>>', self._model_selected)
        self._entry(source, 'port_order', 'File port numbers (1-based)', 4, span=2)
        ttk.Label(source, textvariable=self.role_var, wraplength=280, foreground=fw.DIM).grid(row=5, column=0, columnspan=2, sticky='w', pady=(0, 4))
        self.vars['short_unused'] = tk.BooleanVar()
        short = ttk.Checkbutton(source, text='Short unselected ports\nto reference', variable=self.vars['short_unused'])
        short.grid(row=6, column=0, columnspan=2, sticky='w', pady=4)
        self._editable.append(short)
        self.controls['short_unused'] = short
        self.vars['ports_confirmed'] = tk.BooleanVar()
        confirm = ttk.Checkbutton(source, text='Confirm port mapping\nand terminations', variable=self.vars['ports_confirmed'])
        confirm.grid(row=7, column=0, columnspan=2, sticky='w', pady=4)
        self._editable.append(confirm)
        self.controls['ports_confirmed'] = confirm
        self.controls['preview'] = ttk.Button(source, text='Preview SNP curves', command=lambda: self.app.start_action('preview', self.id))
        self.controls['preview'].grid(row=8, column=0, columnspan=2, sticky='ew', pady=(4, 0))

        targets = self._group(content, '2. Frequency and accuracy')
        self._entry(targets, 'fmin_ghz', 'Start (GHz)', 0, 0)
        self._entry(targets, 'fmax_ghz', 'Stop (GHz)', 0, 1)
        self._entry(targets, 'l_pct', 'L error (%) *', 1, 0)
        self._entry(targets, 'q_pct', 'Q error (%) *', 1, 1)
        self._entry(targets, 'k_pct', 'k error (%)', 2, 0)
        self._entry(targets, 'srf_pct', 'SRF error (%)', 2, 1)
        self._entry(targets, 'network_pct', 'S absolute limit (%)', 3, 0)
        self._combo(targets, 'component_limit', 'Maximum of each R / L / C / K', 4, ['20', '30'])
        ttk.Label(targets, text='* L and Q: 5-20%. S=5% means max |dS|=0.05. k applies to transformers only.', wraplength=280, foreground=fw.DIM).grid(row=5, column=0, columnspan=2, sticky='w')
        ttk.Label(targets, textvariable=self.strategy_var, wraplength=280, foreground=fw.TEXT).grid(row=6, column=0, columnspan=2, sticky='w', pady=(4, 0))

        fitting = self._group(content, '3. Initialization and tuning')
        topo = self._combo(fitting, '_topology_label', 'Initial topology', 0, self._topology_labels)
        topo.bind('<<ComboboxSelected>>', self._topology_selected)
        self._entry(fitting, 'quick_seconds', 'Quick time target (s)', 1, 0, state_value='readonly')
        self._entry(fitting, 'tune_seconds', 'Tune budget (s)', 1, 1)
        self._entry(fitting, 'upgrade_seconds', 'Upgrade budget (s)', 2, 0)
        ttk.Label(fitting, text='Quick Initial creates an unverified starting circuit. Tune changes values only. Structure upgrades run only when you click Upgrade Structure.', wraplength=280, foreground=fw.DIM).grid(row=3, column=0, columnspan=2, sticky='w')

        visual = self._group(content, '4. Winding visualization')
        ttk.Label(visual,text='Unified single-turn winding style',wraplength=280).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,4))
        self._entry(visual,'q_annotation_ghz','Segment Q frequency (GHz; blank = Auto)',1,span=2)
        self.controls['q_annotation_ghz'].bind('<Return>',lambda e:self._visual_refresh())
        self.controls['apply_visual']=ttk.Button(visual,text='Apply drawing settings',command=self._visual_refresh)
        self.controls['apply_visual'].grid(row=2,column=0,columnspan=2,sticky='ew')
        ttk.Label(visual,text='Inductor legs: bottom. Transformer primary: top; secondary: bottom. One turn per winding, independent of the layout turn count. Drawing settings do not refit the circuit.',wraplength=280,foreground=fw.DIM).grid(row=3,column=0,columnspan=2,sticky='w',pady=(4,0))

        native = self._group(content, '5. Schematic and export')
        self._entry(native, 'workspace', 'Virtuoso workspace', 0, span=2, browse=lambda: self.app.choose_workspace(self.id))
        self._entry(native, 'library', 'Library', 1, 0)
        self._entry(native, 'cell', 'New cell', 1, 1)
        self.vars['_export_variant'] = tk.StringVar(value='Current result')
        box = self._combo(native, '_export_variant', 'Result to export', 2, ['Current result', 'Before upgrade', 'After upgrade'])
        box.bind('<<ComboboxSelected>>', lambda e: (self.render_summary(), self.refresh_buttons()))
        self.controls['native'] = ttk.Button(native, text='Generate Schematic', command=lambda: self.app.start_action('native', self.id))
        self.controls['native'].grid(row=3, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        self.controls['symbol'] = ttk.Button(native,text='Generate Symbol',command=lambda:self.app.start_action('symbol',self.id))
        self.controls['symbol'].grid(row=4,column=0,columnspan=2,sticky='ew',pady=(0,4))
        self.controls['integrated'] = ttk.Button(native, text='Create / overwrite schematic + symbol', command=lambda: self.app.start_integrated(self.id))
        self.controls['integrated'].grid(row=9, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.integration_target = tk.StringVar(value='Target: set workspace, library and cell above')
        ttk.Label(native, textvariable=self.integration_target, wraplength=280).grid(row=10,column=0,columnspan=2,sticky='w')
        self.controls['spice'] = ttk.Button(native, text='Export SPICE...', command=lambda: self.app.export_spice(self.id))
        self.controls['spice'].grid(row=5, column=0, columnspan=2, sticky='ew')
        self.symbol_hint=tk.StringVar(value='Generate and verify the schematic first, then add its symbol to the same cell.')
        ttk.Label(native,textvariable=self.symbol_hint,wraplength=280,foreground=fw.DIM).grid(row=6,column=0,columnspan=2,sticky='w',pady=(8,0))

        self._advanced_shown = False
        self.advanced_native = ttk.Frame(native)
        self.advanced_native.columnconfigure(0, weight=1)
        self.advanced_native.columnconfigure(1, weight=1)
        self.controls['advanced_native'] = ttk.Button(native, text='Advanced native settings...', command=self.toggle_advanced_native)
        self.controls['advanced_native'].grid(row=7, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.advanced_native.grid(row=8, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        for row_number, (key, label) in enumerate([
            ('env_script', 'Environment script (blank = Auto)'),
            ('virtuoso', 'Virtuoso executable (blank = Auto)'),
            ('ocean', 'Ocean executable (blank = Auto)'),
            ('spectre', 'Spectre executable (blank = Auto)'),
            ('native_user', 'Native user (blank = Auto)'),
            ('existing_library_path', 'Existing library path (optional)'),
        ]):
            browse = None if key == 'native_user' else lambda k=key: self.app.browse_native_setting(self.id, k)
            self._entry(self.advanced_native, key, label, row_number, span=2, browse=browse)
        self.advanced_native.grid_remove()

        right.columnconfigure(0, weight=1)
        right.rowconfigure(5, weight=1)
        self.decorative_title = ttk.Label(right, text='SNP response and equivalent circuit', style='Title.TLabel')
        self.decorative_title.grid(row=0, column=0, sticky='w')
        self.actions = ttk.Frame(right)
        self.actions.grid(row=1, column=0, sticky='ew', pady=(8, 4))
        labels = [('quick', 'Quick Initial (~5 s)'), ('tune', 'Fit'), ('upgrade', 'Upgrade Structure')]
        for column, (action, label) in enumerate(labels):
            button = ttk.Button(self.actions, text=label, style='Primary.TButton' if action == 'tune' else 'TButton', command=lambda a=action: self.app.start_action(a, self.id))
            button.grid(row=0, column=column, sticky='ew', padx=(0, 4))
            self.controls[action] = button
        self.controls['cancel'] = ttk.Button(self.actions, text='Cancel', command=self.app.cancel_job)
        self.controls['cancel'].grid(row=0, column=3, sticky='ew')
        self.actions.columnconfigure(4, weight=1)
        self.banner = ttk.Label(right, textvariable=self.status_var, wraplength=850, foreground=LEVEL_COLORS['info'])
        self.banner.grid(row=2, column=0, sticky='ew', pady=(0, 4))
        progress = ttk.Frame(right)
        self.progress_row = progress
        progress.grid(row=3, column=0, sticky='ew', pady=(0, 4))
        progress.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress, mode='indeterminate')
        self.progress.grid(row=0, column=0, sticky='ew')
        ttk.Label(progress, textvariable=self.elapsed_var, width=10, anchor='e').grid(row=0, column=1, padx=(8, 0))
        metrics = ttk.Frame(right)
        self.metric_row = metrics
        metrics.grid(row=4, column=0, sticky='ew', pady=(0, 8))
        for i, key in enumerate(self.metric_vars):
            box = ttk.Frame(metrics, padding=(8, 4))
            box.grid(row=0, column=i, sticky='ew', padx=(0, 4))
            metrics.columnconfigure(i, weight=1, uniform='cards')
            name_label = ttk.Label(box, text=('Worst ' + key + ' error') if key != 'Elements' else 'Element counts', foreground=fw.DIM)
            name_label.pack(anchor='w')
            value_label = ttk.Label(box, textvariable=self.metric_vars[key], style='Metric.TLabel')
            value_label.pack(anchor='w')
            self._metric_widgets.append((key, box, name_label, value_label))

        self.views = ttk.Notebook(right)
        self.views.grid(row=5, column=0, sticky='nsew')
        plots = ttk.Frame(self.views)
        circuits = ttk.Frame(self.views)
        results = ttk.Frame(self.views, padding=8)
        logs = ttk.Frame(self.views, padding=8)
        self.views.add(plots, text='Frequency comparisons')
        self.views.add(circuits, text='Circuit and parameters')
        self.views.add(results, text='Accuracy and native result')
        self.views.add(logs, text='Log')
        plot_options = ttk.Frame(plots, padding=(4, 4))
        plot_options.pack(fill='x')
        ttk.Label(plot_options, text='Frequency axis:').pack(side='left')
        self.plot_xscale = tk.StringVar(value='Log')
        axis_box = ttk.Combobox(plot_options, textvariable=self.plot_xscale, values=['Log', 'Linear'], state='readonly', width=8)
        axis_box.pack(side='left', padx=(4, 12))
        axis_box.bind('<<ComboboxSelected>>', self._plot_options_changed)
        self.plot_markers = tk.BooleanVar(value=True)
        self.plot_srf = tk.BooleanVar(value=True)
        self.plot_tolerance = tk.BooleanVar(value=True)
        for text, variable in [('Samples', self.plot_markers), ('SRF bands', self.plot_srf), ('Target bands', self.plot_tolerance)]:
            ttk.Checkbutton(plot_options, text=text, variable=variable, command=self._plot_options_changed).pack(side='left', padx=(0, 8))
        self.comparison = ComparisonView(plots)
        self.comparison.pack(fill='both', expand=True)
        for child in self.comparison.winfo_children():
            if isinstance(child, ttk.Label) and str(child.cget('textvariable')) == str(self.comparison.message):
                self._comparison_caption = child
                self._comparison_caption_pack = child.pack_info()
                break
        self.circuits = CircuitView(circuits)
        self.circuits.pack(fill='both', expand=True)
        self.views.bind('<<NotebookTabChanged>>', self._view_selected)
        exportbar = ttk.Frame(right)
        self.exportbar = exportbar
        exportbar.grid(row=6, column=0, sticky='ew', pady=(4, 0))
        exportbar.columnconfigure(0, weight=1)
        self.hint_label = ttk.Label(exportbar, textvariable=self.hint_var, wraplength=700, foreground=fw.DIM)
        self.hint_label.grid(row=0, column=0, columnspan=4, sticky='ew', pady=(0, 4))
        ttk.Button(exportbar, text='Save circuit...', command=lambda: self.app.export_circuit(self.id)).grid(row=1, column=1, padx=(0, 4), sticky='e')
        ttk.Button(exportbar, text='Export CSV...', command=lambda: self.app.export_csv(self.id)).grid(row=1, column=2, padx=(0, 4), sticky='e')
        ttk.Button(exportbar, text='Save plot...', command=lambda: self.app.export_plot(self.id)).grid(row=1, column=3, sticky='e')

        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        ttk.Label(results, text='Acceptance is separate from initialization and native verification.', wraplength=800).grid(row=0, column=0, sticky='w', pady=(0, 8))
        self.summary = ScrolledText(results, height=10, wrap='word', font=(self.app.mono_font_family, 10), state='disabled')
        self.summary.grid(row=1, column=0, sticky='nsew')
        logbar = ttk.Frame(logs)
        logbar.pack(fill='x', pady=(0, 8))
        ttk.Label(logbar, text='Show:').pack(side='left')
        self.log_filter = tk.StringVar(value='All')
        filterbox = ttk.Combobox(logbar, textvariable=self.log_filter, values=['All', 'Warnings', 'Errors'], state='readonly', width=12)
        filterbox.pack(side='left', padx=4)
        filterbox.bind('<<ComboboxSelected>>', lambda e: self.render_log())
        ttk.Button(logbar, text='Copy log', command=self.copy_log).pack(side='right')
        ttk.Button(logbar, text='Save log...', command=lambda: self.app.export_log(self.id)).pack(side='right', padx=4)
        self.log = ScrolledText(logs, height=10, wrap='word', font=(self.app.mono_font_family, 9), state='disabled')
        self.log.pack(fill='both', expand=True)
        for key, var in self.vars.items():
            if key not in ('_model_label', '_topology_label', '_export_variant'):
                var.trace_add('write', lambda *a, k=key: self._config_changed(k))
        self.frame.bind('<Configure>', self._layout_changed, add='+')

    def _sidebar_bounds(self):
        width = self.pane.winfo_width()
        # Ignore unmapped/tiny panes; the application minsize supports both sides.
        if width < 820:
            return None
        return 300, max(300, width - 505)

    def _schedule_sidebar_restore(self):
        if self._sidebar_pending is not None or self._sidebar_dragging:
            return
        try:
            if not self.pane.winfo_exists() or not self.pane.winfo_ismapped():
                return
            self._sidebar_pending = self.pane.after_idle(self._restore_sidebar)
        except tk.TclError:
            pass

    def _restore_sidebar(self):
        self._sidebar_pending = None
        try:
            if not self.pane.winfo_exists() or not self.pane.winfo_ismapped() or self._sidebar_dragging:
                return
            bounds = self._sidebar_bounds()
            if bounds is None:
                return
            lower, upper = bounds
            target = int(round(min(max(self._sidebar_preferred, lower), upper)))
            current = self.pane.sashpos(0)
            if abs(current - target) > 1:
                self.pane.sashpos(0, target)
            self._sidebar_initialized = True
        except tk.TclError:
            pass

    def _pane_mapped(self, event):
        if event.widget is self.pane:
            self._schedule_sidebar_restore()

    def _pane_configured(self, event):
        if event.widget is self.pane:
            self._schedule_sidebar_restore()

    def _sash_pressed(self, event):
        if event.widget is not self.pane:
            return
        try:
            hit = self.pane.identify(event.x, event.y)
            self._sidebar_dragging = hit is not None and hit != ''
        except tk.TclError:
            self._sidebar_dragging = False

    def _sash_released(self, event):
        if event.widget is not self.pane or not self._sidebar_dragging:
            return
        self._sidebar_dragging = False
        self.remember_sidebar_width()
        self._schedule_sidebar_restore()

    def remember_sidebar_width(self):
        # Manual positions are stored; automatic resize clamps keep the preference.
        try:
            bounds = self._sidebar_bounds()
            if bounds is None or not self.pane.winfo_ismapped():
                return
            lower, upper = bounds
            current = int(self.pane.sashpos(0))
            self._sidebar_preferred = min(max(current, lower), upper)
            self.data.setdefault('view', {})['sidebar_width'] = self._sidebar_preferred
            self.app.dirty = True
        except tk.TclError:
            pass

    def _pane_destroyed(self, event):
        if event.widget is self.pane and self._sidebar_pending is not None:
            try:
                self.pane.after_cancel(self._sidebar_pending)
            except tk.TclError:
                pass
            self._sidebar_pending = None

    def _layout_changed(self, event):
        if event.widget is self.frame and hasattr(self, 'comparison'):
            self._apply_responsive_layout()

    def _right_layout_changed(self, event):
        if event.widget is self.right and hasattr(self, 'comparison'):
            self._apply_responsive_layout()
            # Cached figures can request new geometry without resizing the root.
            # Only restore an invalid split here, never fight a user's drag.
            try:
                bounds = self._sidebar_bounds()
                if bounds and not self._sidebar_dragging:
                    current = self.pane.sashpos(0)
                    if not self._sidebar_initialized or current < bounds[0] or current > bounds[1]:
                        self._schedule_sidebar_restore()
            except tk.TclError:
                pass

    def _apply_responsive_layout(self):
        width = max(250, self.right.winfo_width() - 26)
        height = self.right.winfo_height()
        if height < 20:
            return
        compact = self.app.root.winfo_height() <= 800 or height < 710
        key = (width, height, compact)
        if key == self._responsive_key:
            return
        self._responsive_key = key
        self.banner.configure(wraplength=width)
        self.hint_label.configure(wraplength=width)
        for owner, variable_name in [(self.comparison, 'message'), (self.comparison, 'readout'),
                                      (self.circuits, 'summary'), (self.circuits, 'detail')]:
            variable = getattr(owner, variable_name, None)
            if variable is None:
                continue
            for child in self._all_children(owner):
                if isinstance(child, ttk.Label) and str(child.cget('textvariable')) == str(variable):
                    child.configure(wraplength=max(200, width - 14))
        if compact != self._compact:
            self._compact = compact
            if compact:
                self.decorative_title.grid_remove()
                self.hint_label.grid_remove()
                self.actions.grid_configure(pady=(0, 4))
                self.metric_row.grid_configure(pady=(0, 4))
                self.exportbar.grid_configure(pady=(4, 0))
                if self._comparison_caption is not None:
                    self._comparison_caption.pack_forget()
                if self.comparison.readout.get().startswith('Hover over a sampled point'):
                    self.comparison.readout.set('Hover for exact samples; click a legend to toggle a curve.')
            else:
                self.decorative_title.grid()
                self.hint_label.grid()
                self.actions.grid_configure(pady=(8, 4))
                self.metric_row.grid_configure(pady=(0, 8))
                self.exportbar.grid_configure(pady=(4, 0))
                if self._comparison_caption is not None and self._comparison_caption_pack:
                    options = dict(self._comparison_caption_pack)
                    others = self.comparison.pack_slaves()
                    if others:
                        options['before'] = others[0]
                    self._comparison_caption.pack(**options)
            for metric, box, name_label, value_label in self._metric_widgets:
                name_label.pack_forget(); value_label.pack_forget()
                if compact:
                    box.configure(padding=(4, 0))
                    name_label.configure(text='R/L/C/K' if metric == 'Elements' else metric + ' max', font=(self.app.font_family, 9))
                    value_label.configure(font=(self.app.font_family, 9, 'bold'))
                    name_label.pack(side='left', padx=(0, 4))
                    value_label.pack(side='left')
                else:
                    box.configure(padding=(8, 4))
                    name_label.configure(text='Element counts' if metric == 'Elements' else 'Worst ' + metric + ' error', font=(self.app.font_family, 10))
                    value_label.configure(font=(self.app.font_family, 12, 'bold'))
                    name_label.pack(anchor='w');value_label.pack(anchor='w')
            self.render_summary()
        self.hint_var.set(self._hint())

    @staticmethod
    def _all_children(widget):
        for child in widget.winfo_children():
            yield child
            yield from AnalysisPage._all_children(child)

    def populate(self, data):
        self._populating = True
        self.data = data
        config = dict(state.default_config())
        config.update(data.get('config') or {})
        self.data['config'] = config
        _snapshots(self.data)
        self.data.setdefault('logs', [])
        self.data.setdefault('view', {})
        self.data.setdefault('results_stale', False)
        self.data.setdefault('identity_stale', False)
        for key, var in self.vars.items():
            if key.startswith('_'):
                continue
            value = config.get(key, False if isinstance(var, tk.BooleanVar) else '')
            if key == 'port_order' and isinstance(value, (list, tuple)):
                value = ', '.join(str(i) for i in value)
            var.set(value)
        model_id = config.get('model_id', '')
        label = self.app.id_to_label.get(model_id, '')
        self.vars['_model_label'].set(label)
        self._update_model_details(reset_ports=False)
        view = data.get('view') or {}
        self.vars['_export_variant'].set(view.get('export_variant', 'Current result'))
        self._advanced_shown = bool(view.get('advanced_native', False))
        if self._advanced_shown:
            self.advanced_native.grid()
        else:
            self.advanced_native.grid_remove()
        self.log_filter.set(view.get('log_filter', 'All'))
        self.plot_xscale.set('Linear' if view.get('xscale') == 'linear' else 'Log')
        self.plot_markers.set(bool(view.get('show_markers', True)))
        self.plot_srf.set(bool(view.get('show_srf', True)))
        self.plot_tolerance.set(bool(view.get('show_tolerance', True)))
        try:
            self.views.select(int(view.get('active_panel', 0)))
        except (tk.TclError, ValueError, TypeError):
            self.views.select(0)
        self._populating = False
        self.integration_target.set('Target: %s / %s  (%s)' % (self.vars['library'].get(), self.vars['cell'].get(), self.vars['workspace'].get()))

    def sync(self):
        config = self.data['config']
        for key, var in self.vars.items():
            if not key.startswith('_'):
                config[key] = var.get()
        label = self.vars['_model_label'].get()
        config['model_id'] = self.app.label_to_id.get(label, config.get('model_id', ''))
        topo = self.vars['_topology_label'].get()
        config['topology_id'] = self._topology_ids[self._topology_labels.index(topo)] if topo in self._topology_labels else 'auto'
        self.data['view'].update(export_variant=self.vars['_export_variant'].get(), log_filter=self.log_filter.get(), active_panel=self.views.index(self.views.select()), advanced_native=self._advanced_shown, xscale='linear' if self.plot_xscale.get() == 'Linear' else 'log', show_markers=self.plot_markers.get(), show_srf=self.plot_srf.get(), show_tolerance=self.plot_tolerance.get())
        self.data['view']['sidebar_width'] = int(round(self._sidebar_preferred))
        self.data['revision'] = self._revision
        return config

    def capture_views(self):
        if getattr(self.comparison, '_series', None):
            captured = self.comparison.capture_view_state()
            # Keep automatic limits automatic; only persist explicit user zoom.
            automatic = getattr(self.comparison, '_auto_limits', None)
            actual = captured.get('axis_limits')
            changed = False
            if automatic and actual and len(automatic) == len(actual):
                changed = any(any(abs(float(a)-float(b)) > 1e-8*max(1., abs(float(b))) for a, b in zip(values, normal)) for values, normal in zip(actual, automatic))
            if not changed:
                captured.pop('axis_limits', None)
                self.data['view'].pop('axis_limits', None)
            self.data['view'].update(captured)
        self.data['view'].update(self.circuits.capture_view_state())

    def _plot_options_changed(self, event=None):
        if self._populating:
            return
        old_scale = self.data.setdefault('view', {}).get('xscale', 'log')
        new_scale = 'linear' if self.plot_xscale.get() == 'Linear' else 'log'
        if old_scale != new_scale:
            self.data['view'].pop('axis_limits', None)
            self.comparison._auto_limits = None
        self.sync()
        self.comparison.set_data(self.data)
        self.app.dirty = True

    def _config_changed(self, key):
        if self._populating:
            return
        value = self.vars[key].get()
        old = self.data['config'].get(key)
        if str(old) == str(value):
            return
        self.data['config'][key] = value
        self._revision += 1
        self.app.dirty = True
        if key in IDENTITY_KEYS:
            self.data.setdefault('view', {}).pop('axis_limits', None)
            self.data['identity_stale'] = bool(_current(self.data))
            self.data['results_stale'] = bool(_current(self.data))
            if key == 'source_path':
                self.data['source_valid'] = False
            if key in ('port_order', 'short_unused'):
                self._populating = True
                self.vars['ports_confirmed'].set(False)
                self.data['config']['ports_confirmed'] = False
                self._populating = False
            if _current(self.data):
                self.set_status('Inputs changed. Run Quick Initial again; cached results are historical.', 'warning')
        elif key in TARGET_KEYS:
            self.data['results_stale'] = bool(_current(self.data))
            if _current(self.data):
                self.set_status('Targets changed. Fit to re-evaluate this circuit before native export.', 'warning')
        self._update_strategy()
        if hasattr(self, 'integration_target'):
            self.integration_target.set('Target: %s / %s  (%s)' % (self.vars['library'].get(), self.vars['cell'].get(), self.vars['workspace'].get()))
        self.refresh_buttons()

    def _visual_error(self):
        try:
            raw=self.vars['q_annotation_ghz'].get().strip()
            if not raw:return None
            value=float(raw);lo=float(self.vars['fmin_ghz'].get());hi=float(self.vars['fmax_ghz'].get())
            if not math.isfinite(value) or not 0<lo<=value<=hi:
                return 'Segment Q annotation frequency must be within the fitting band.'
        except (ValueError,TypeError):return 'Enter a positive segment Q frequency in GHz, or leave it blank for Auto.'
        return None

    def _visual_refresh(self):
        error=self._visual_error()
        if error:
            self.set_status(error,'error');return False
        self.sync()
        self.data.setdefault('view',{})['circuit_panel']=self.circuits.tabs.index(self.circuits.tabs.select())
        self.circuits.set_data(self.data);self.app.dirty=True
        self.set_status('Drawing and local Q updated. The fitted circuit is unchanged.','info')
        self.refresh_buttons()
        return True

    def _model_selected(self, event=None):
        if self._populating:
            return
        old = self.data['config'].get('model_id')
        value = self.app.label_to_id.get(self.vars['_model_label'].get(), '')
        self.data['config']['model_id'] = value
        if old != value:
            self._revision += 1
            self.data['identity_stale'] = bool(_current(self.data))
            self.data['results_stale'] = bool(_current(self.data))
            self._update_model_details(reset_ports=True)
            self.app.dirty = True
            if _current(self.data):
                self.set_status('Model changed. Generate a new initial circuit before tuning.', 'warning')
            else:self.circuits.set_data(self.data)
        self.refresh_buttons()

    def _topology_selected(self, event=None):
        if self._populating:
            return
        self.sync()
        self.app.dirty = True
        self.set_status('Initial topology selection applies to the next Quick Initial action.', 'info')

    def _update_model_details(self, reset_ports=False):
        model_id = self.data['config'].get('model_id')
        try:
            spec = get_model(model_id)
        except (KeyError, ValueError, TypeError):
            self.role_var.set('Select one explicit model. Port order follows the roles shown here.')
            return
        self._topology_ids = ['auto'] + [c.id for c in spec.candidates]
        self._topology_labels = ['Automatic initial topology'] + [topology_label(c) for c in spec.candidates]
        self.controls['_topology_label'].configure(values=self._topology_labels)
        selected = self.data['config'].get('topology_id', 'auto')
        if selected not in self._topology_ids:
            selected = 'auto'
        self.vars['_topology_label'].set(self._topology_labels[self._topology_ids.index(selected)])
        self.data['config']['topology_id'] = selected
        if reset_ports:
            self._populating = True
            self.vars['port_order'].set(', '.join(str(i + 1) for i in range(spec.nports)))
            self.vars['ports_confirmed'].set(False)
            self.data['config']['port_order'] = self.vars['port_order'].get()
            self.data['config']['ports_confirmed'] = False
            self._populating = False
        reference = 'exported GND pin' if spec.explicit_reference else 'internal reference (no GND pin)'
        self.role_var.set('Roles: ' + ' -> '.join(spec.port_roles) + '\n' + reference + '; taps open; ' + ('common mode matched.' if spec.mode == 'dm' else 'selected single-ended return.'))
        self.controls['k_pct'].configure(state='normal' if spec.device == 'transformer' and not self._owner_busy() else 'disabled')

    def _update_strategy(self):
        try:
            l, q = float(self.vars['l_pct'].get()), float(self.vars['q_pct'].get())
            if not 5 <= l <= 20 or not 5 <= q <= 20:
                raise ValueError()
            text = 'L priority (automatic)' if l < q else 'Joint L/Q priority (automatic)'
        except ValueError:
            text = 'L/Q priority is selected automatically from your targets.'
        self.strategy_var.set(text)

    def toggle_advanced_native(self):
        self._advanced_shown = not self._advanced_shown
        if self._advanced_shown:
            self.advanced_native.grid()
        else:
            self.advanced_native.grid_remove()
        self.data.setdefault('view', {})['advanced_native'] = self._advanced_shown
        self.app.dirty = True

    def _view_selected(self, event=None):
        if self._populating:
            return
        self.data.setdefault('view', {})['active_panel'] = self.views.index(self.views.select())

    def _owner_busy(self):
        return bool(self.app.job and (self.app.job.get('page_id') == self.id or self.app.job.get('action') == 'load_session'))

    def selected_snapshot(self):
        result = _snapshots(self.data)
        variant = self.vars['_export_variant'].get()
        key = {'Before upgrade': 'before_upgrade', 'After upgrade': 'upgraded'}.get(variant, 'current')
        return result.get(key)

    def _input_error(self, require_targets=True):
        config = self.sync()
        if not config.get('source_path'):
            return 'Load an SNP file first.'
        if not self.data.get('source_valid'):
            return 'Load or reload the source file to validate its identity.'
        if not config.get('model_id'):
            return 'Choose one explicit model.'
        if not config.get('ports_confirmed'):
            return 'Confirm port mapping and terminations.'
        try:
            spec = get_model(config['model_id'])
            order = config['port_order']
            ports = [int(x.strip()) for x in order.split(',')] if isinstance(order, str) else [int(x) for x in order]
            n = self.data.get('source', {}).get('nports', self.data.get('source', {}).get('num_ports'))
            if len(ports) != spec.nports or len(set(ports)) != len(ports) or any(i < 1 for i in ports):
                return f'Enter {spec.nports} unique file port numbers in role order.'
            if n and any(i > int(n) for i in ports):
                return 'A mapped port exceeds the SNP port count.'
            fmin, fmax = float(config['fmin_ghz']), float(config['fmax_ghz'])
            if not math.isfinite(fmin + fmax) or not 0 < fmin < fmax:
                return 'Use positive frequency limits with Start < Stop.'
            if require_targets:
                for key, label in [('l_pct', 'L'), ('q_pct', 'Q')]:
                    v = float(config[key])
                    if not math.isfinite(v) or not 5 <= v <= 20:
                        return f'{label} error must be in 5-20%.'
                for key, label in [('srf_pct', 'SRF'), ('network_pct', 'S')]+([('k_pct', 'k')] if spec.device == 'transformer' else []):
                    v = float(config[key])
                    if not math.isfinite(v) or not 0 < v <= 100:
                        return f'{label} error must be greater than 0 and at most 100%.'
                if int(config['component_limit']) not in (20, 30):
                    return 'Choose a per-kind element limit of 20 or 30.'
                for key in ('tune_seconds', 'upgrade_seconds'):
                    v = float(config[key])
                    if not math.isfinite(v) or not 0 < v <= 600:
                        return 'Tuning and upgrade budgets must be positive and at most 600 seconds.'
        except (ValueError, TypeError, KeyError):
            return 'Complete all required numeric inputs and check the port numbers.'
        return None

    def _hint(self):
        if self.app.job:
            owner = self.app.pages.get(self.app.job.get('page_id'))
            name = owner.data.get('title', 'another page') if owner else 'the session'
            return f'Working in {name}. Switch pages to inspect completed results; Cancel stops the active job.'
        error = self._input_error(require_targets=True)
        if error:
            return error
        if self.data.get('identity_stale'):
            return 'Source/model/ports/band changed: generate a new initial circuit.'
        if self.data.get('results_stale'):
            return 'Targets changed: tune again before native generation.'
        if not _current(self.data):
            return 'Ready for Quick Initial. It is a starting circuit, not a qualified model.'
        if _current(self.data).get('passed'):
            return 'Current numerical targets passed. Generate Schematic performs native verification.'
        return 'Current targets are not met. Tune values, or click Upgrade Structure to try a richer circuit.'

    def refresh_buttons(self):
        global_busy = self.app.job is not None
        count = (self.data.get('source') or {}).get('positive_point_count', 0)
        self.controls['integrated'].configure(state='normal' if count >= 2 and self.data.get('source_valid') and not global_busy else 'disabled')
        target_text = 'Target: %s / %s  (%s)' % (self.vars['library'].get(), self.vars['cell'].get(), self.vars['workspace'].get())
        if self.data.get('source_valid') and count < 2:
            target_text += '\nFitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.'
        self.integration_target.set(target_text)
        owner_busy = self._owner_busy()
        for widget in self._editable:
            try:
                if owner_busy:
                    widget.configure(state='disabled')
                else:
                    widget.configure(state='readonly' if isinstance(widget, ttk.Combobox) else 'normal')
            except tk.TclError:
                pass
        self._update_model_details(reset_ports=False)
        valid = self._input_error(require_targets=True) is None
        preview_ok = self._input_error(require_targets=False) is None
        existing = bool(_current(self.data) and _current(self.data).get('circuit'))
        same_identity = not self.data.get('identity_stale', False)
        for key, enabled in [('quick', valid), ('tune', valid and existing and same_identity), ('upgrade', valid and existing and same_identity), ('preview', preview_ok)]:
            self.controls[key].configure(state='normal' if enabled and not global_busy else 'disabled')
        self.controls['load'].configure(state='disabled' if global_busy else 'normal')
        self.controls['reload'].configure(state='disabled' if global_busy or not self.vars['source_path'].get() else 'normal')
        selected = self.selected_snapshot()
        native_ok = bool(selected and selected.get('passed') and selected.get('circuit') and self.data.get('source_valid') and not self.data.get('results_stale') and same_identity and self.vars['ports_confirmed'].get())
        self.controls['native'].configure(state='normal' if native_ok and not global_busy else 'disabled')
        visual_error=self._visual_error()
        if visual_error:self.controls['native'].configure(state='disabled')
        native=(selected or {}).get('native') or {}
        symbol_ok=bool(native_ok and selected.get('native_verified') and selected.get('visual_plan') and not native.get('symbol_ports') and not native.get('symbol'))
        self.controls['symbol'].configure(state='normal' if symbol_ok and not visual_error and not global_busy else 'disabled')
        self.controls['apply_visual'].configure(state='disabled' if owner_busy else 'normal')
        if native.get('symbol_ports') or native.get('symbol'):
            self.symbol_hint.set('A symbol already exists for the selected native cell. Existing views are never overwritten.')
        elif symbol_ok:
            self.symbol_hint.set('Generate Symbol adds the matching shape to %s/%s.'%(native.get('library'),native.get('cell')))
        else:self.symbol_hint.set('Generate and verify the schematic first, then add its symbol to the same cell.')
        self.controls['spice'].configure(state='normal' if selected and selected.get('circuit') else 'disabled')
        self.controls['cancel'].configure(state='normal' if global_busy else 'disabled')
        self.hint_var.set(self._hint())
        if owner_busy:
            self.progress_row.grid()
            self.progress.start(70)
        else:
            self.progress.stop()
            self.progress_row.grid_remove()
            self.elapsed_var.set('')

    def set_status(self, message, level='info'):
        self.status_var.set(str(message))
        self.banner.configure(foreground=LEVEL_COLORS.get(level, LEVEL_COLORS['info']))
        self.data['status'] = {'level': level, 'message': str(message)}
        self.app.global_status.set(str(message))

    def refresh(self):
        self._update_strategy()
        source = self.data.get('source') or {}
        if source:
            n = source.get('nports', source.get('num_ports', '?'))
            count = source.get('point_count', source.get('points', source.get('sample_count', '?')))
            low = source.get('fmin_ghz')
            high = source.get('fmax_ghz')
            if low is None:
                value = source.get('fmin_hz', source.get('frequency_min_hz'))
                low = float(value) / 1e9 if value is not None else None
            if high is None:
                value = source.get('fmax_hz', source.get('frequency_max_hz'))
                high = float(value) / 1e9 if value is not None else None
            digest = source.get('sha256', source.get('source_sha256', ''))
            self.source_var.set(f'{n} ports | {count} samples | {_fmt(low)}-{_fmt(high)} GHz\nSHA256: {digest[:16]}'+(' | source unavailable/changed' if not self.data.get('source_valid') else ''))
        else:
            self.source_var.set('No source loaded.')
        status = self.data.get('status')
        if isinstance(status, dict):
            self.set_status(status.get('message', 'Ready.'), status.get('level', 'info'))
        elif status:
            self.set_status(status, 'warning' if self.data.get('results_stale') else 'info')
        self.comparison.set_data(self.data)
        self.circuits.set_data(self.data)
        self.render_summary()
        self.render_log()
        self.refresh_buttons()

    def render_summary(self):
        snapshot = self.selected_snapshot() or _current(self.data) or {}
        metrics = snapshot.get('metrics') or {}
        lq = metrics.get('lq', metrics)
        branches = lq.get('branches') or {}
        def worst(key):
            vals = [v.get(key, {}).get('max_relative_pct') for v in branches.values()]
            vals = [float(x) for x in vals if x is not None]
            return max(vals) if vals else None
        self.metric_vars['L'].set(_fmt(worst('l'), '%'))
        self.metric_vars['Q'].set(_fmt(worst('q'), '%'))
        km = metrics.get('k') or {}
        self.metric_vars['k'].set(_fmt(km.get('max_relative_pct'), '%') if km.get('applicable', False) else 'N/A')
        srf = metrics.get('srf') or {}
        self.metric_vars['SRF'].set(_fmt(srf.get('max_error_upper_pct'), '%'))
        circuit = snapshot.get('circuit') or {}
        counts = {}
        for e in circuit.get('elements', []):
            counts[e.get('kind')] = counts.get(e.get('kind'), 0) + 1
        separator = '/' if self._compact else ' / '
        self.metric_vars['Elements'].set(separator.join(str(counts.get(key, 0)) for key in ('res', 'ind', 'cap', 'mind')) if circuit else 'N/A')
        lines = []
        if not snapshot:
            lines = ['No result yet.', '', '1. Load an SNP and choose its explicit model.', '2. Confirm port mapping and set targets.', '3. Generate Quick Initial, then Fit.', '4. Upgrade Structure runs only after you click it.', '', 'All numerical plots preserve the original SNP sample points.']
        else:
            lines = [f"Status: {snapshot.get('status', 'Unknown')}",
                     f"Stage: {snapshot.get('stage', 'Unknown')}",
                     'Numerical acceptance: ' + ('PASS' if snapshot.get('passed') and not self.data.get('results_stale') else 'STALE' if self.data.get('results_stale') else 'NOT MET / NOT VERIFIED'),
                     'Native verification: ' + ('PASS' if snapshot.get('native_verified') else 'Not verified'),
                     f"Result: {snapshot.get('id', '')}", f"Run directory: {snapshot.get('run_dir', '')}",
                     'Elements R / L / C / K: ' + self.metric_vars['Elements'].get(), '']
            for name, errors in branches.items():
                lines.append(name.title() + ':')
                for key in ('l', 'q'):
                    value = errors.get(key) or {}
                    frequency = value.get('worst_frequency_hz')
                    at = ' at '+_fmt(float(frequency)/1e9)+' GHz' if frequency is not None else ''
                    lines.append('  '+key.upper()+': '+_fmt(value.get('max_relative_pct'), '%')+at+' | '+('PASS' if value.get('pass') else 'NOT MET'))
            net = lq.get('network') or {}
            if net:
                lines.append('Complete S: '+_fmt(net.get('max_s_absolute_error'))+' absolute | '+('PASS' if net.get('pass') else 'NOT MET'))
            if km:
                lines.append('Effective k: '+('N/A (single winding)' if not km.get('applicable') else _fmt(km.get('max_relative_pct'), '%')+' | '+('PASS' if km.get('passed') else 'NOT MET / UNDEFINED')))
            if srf:
                lines.append('First SRF conservative error: '+_fmt(srf.get('max_error_upper_pct'), '%')+' | '+('PASS' if srf.get('passed') else 'NOT MET / REFERENCE UNRESOLVED'))
            local = metrics.get('local_s', metrics.get('local_network')) or {}
            if local:
                lines.append('Near-SRF complete S: '+_fmt(local.get('max_s_absolute_error'))+' absolute | '+('PASS' if local.get('passed') else 'NOT MET / UNDEFINED'))
            for warning in snapshot.get('warnings', []):
                lines.append('Warning: '+str(warning))
            native = snapshot.get('native') or {}
            if native:
                lines.extend(['', 'Native output:', json.dumps(native, ensure_ascii=False, indent=2)])
        self.summary.configure(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('end', '\n'.join(lines))
        self.summary.configure(state='disabled')

    def append_log(self, message, level='info'):
        if isinstance(message, dict):
            level = message.get('level', level)
            message = message.get('message', str(message))
        entry = {'time': dt.datetime.now().isoformat(timespec='seconds'), 'level': level, 'message': str(message)}
        self.data.setdefault('logs', []).append(entry)
        try:
            path = self.app.runtime / 'ui-logs' / (self.id + '.log')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(self._log_line(entry)+'\n')
        except OSError:
            pass
        if self.log_filter.get() == 'All' or (self.log_filter.get() == 'Warnings' and level in ('warning', 'error')) or (self.log_filter.get() == 'Errors' and level == 'error'):
            bottom = self.log.yview()[1] >= .98
            self.log.configure(state='normal')
            self.log.insert('end', self._log_line(entry)+'\n')
            self.log.configure(state='disabled')
            if bottom:
                self.log.see('end')

    @staticmethod
    def _log_line(entry):
        if isinstance(entry, str):
            return entry
        return f"{entry.get('time', '')}  {entry.get('level', 'info').upper()}  {entry.get('message', '')}"

    def render_log(self):
        lines = []
        for entry in self.data.get('logs', []):
            level = entry.get('level', 'info') if isinstance(entry, dict) else 'info'
            if self.log_filter.get() == 'Warnings' and level not in ('warning', 'error'):
                continue
            if self.log_filter.get() == 'Errors' and level != 'error':
                continue
            lines.append(self._log_line(entry))
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.insert('end', '\n'.join(lines)+ ('\n' if lines else ''))
        self.log.configure(state='disabled')
        self.log.see('end')

    def copy_log(self):
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append('\n'.join(self._log_line(e) for e in self.data.get('logs', [])))


class App:
    """Public methods intentionally remain usable by real Tk integration tests."""
    def __init__(self, root, runtime=None):
        self.root = root
        self.runtime = runtime_root(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.pages = {}
        self.job = None
        self.thread = None
        self.cancel = threading.Event()
        self._token = 0
        self._closing = False
        self._poll_id = None
        self.session_path = None
        self.integration_context = {}
        self.dirty = False
        self.models = list(list_models())
        if self.models and isinstance(self.models[0], str):
            self.models = [get_model(m) for m in self.models]
        self.model_labels = [model_label(m) for m in self.models]
        self.id_to_label = {m.id: label for m, label in zip(self.models, self.model_labels)}
        self.label_to_id = {label: m.id for m, label in zip(self.models, self.model_labels)}
        self.global_status = tk.StringVar(value='Ready. Each page keeps its own source, targets and results.')
        self._configure_style()
        root.title('SNPFit Studio — Untitled')
        root.geometry('1440x940')
        root.minsize(1280, 800)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self._build_toolbar()
        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=1, column=0, sticky='nsew', padx=8)
        self.notebook.bind('<<NotebookTabChanged>>', self._page_selected)
        statusbar = ttk.Frame(root, padding=(12, 4))
        statusbar.grid(row=2, column=0, sticky='ew')
        statusbar.columnconfigure(0, weight=1)
        self.status_chip = fw.StatusChip(statusbar, bg=fw.BG)
        self.status_chip.grid(row=0, column=0, sticky='w')
        bind_status(self.global_status, self.status_chip, lambda: self.job is not None)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Control-n>', lambda e: self.new_page())
        root.bind('<Control-o>', lambda e: self.open_session_dialog())
        root.bind('<Control-s>', lambda e: self.save_session_dialog())
        root.bind('<Configure>', self._root_layout_changed, add='+')
        self._toolbar_compact = False
        self.new_page()
        self.dirty = False
        self._poll_id = root.after(80, self.poll)

    def _configure_style(self):
        style = fw.apply(self.root, product='flux')
        fw.apply_matplotlib()
        self.font_family = tkfont.nametofont('TkDefaultFont', root=self.root).actual('family')
        self.mono_font_family = tkfont.nametofont('TkFixedFont', root=self.root).actual('family')
        style.configure('Metric.TLabel', font=(self.font_family, 12, 'bold'))

    def _build_toolbar(self):
        def actions(box):
            self.toolbar_buttons = {}
            buttons = [('example', 'Load example', self.load_example), ('new', 'New Page', self.new_page),
                       ('open', 'Open', self.open_session_dialog), ('save', 'Save', self.save_session_dialog),
                       ('rename', 'Rename', self.rename_page), ('close_page', 'Close', self.close_page),
                       ('doctor', 'Diagnostics', self.diagnostics), ('help', 'Help', self.show_help)]
            for key, label, command in buttons:
                button = ttk.Button(box, text=label, command=command, style='Ghost.TButton' if key in ('doctor', 'help', 'rename') else 'Danger.TButton' if key == 'close_page' else 'TButton')
                button.pack(side='left', padx=4)
                self.toolbar_buttons[key] = button
        toolbar = fw.header(self.root, 'SNPFit Studio', 'RLC(K) fitting', right=actions)
        toolbar.grid(row=0, column=0, sticky='ew')
        # Retain the responsive interface used by existing embedding clients.
        self.toolbar_title = ttk.Label(toolbar)

    def load_example(self):
        page = self.current_page
        if page is None or self.job:
            return
        path = Path(__file__).resolve().parents[1]/'examples'/'ideal_rlc.s2p'
        page.vars['_model_label'].set(self.id_to_label['inductor_dm_gnd'])
        page._model_selected()
        for key, value in [('port_order', '1,2'), ('l_pct', '5'), ('q_pct', '20'), ('fmin_ghz', '1'), ('fmax_ghz', '10')]:
            page.vars[key].set(value)
        self.load_snp(path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)
        self.root.title('SNPFit Studio — '+path.name)

    def _root_layout_changed(self, event):
        if event.widget is not self.root:
            return
        compact = self.root.winfo_height() <= 800
        if compact != self._toolbar_compact:
            self._toolbar_compact = compact
            if compact:
                self.toolbar_title.grid_remove()
            else:
                pass  # Header always remains visible.
        page = self.current_page
        if page is not None:
            page._apply_responsive_layout()

    @property
    def current_page(self):
        selected = self.notebook.select()
        for page in self.pages.values():
            if str(page.frame) == selected:
                return page
        return None

    def page(self, page_id=None):
        return self.pages.get(page_id) if page_id else self.current_page

    def new_page(self, title=None, data=None):
        if len(self.pages) >= state.MAX_PAGES:
            self.global_status.set(f'Page limit ({state.MAX_PAGES}) reached. Close a page before adding another.')
            return None
        if data is None:
            data = state.new_page(title or f'Page {len(self.pages)+1}')
        data = copy.deepcopy(data)
        data.setdefault('id', uuid.uuid4().hex)
        if data['id'] in self.pages:
            data['id'] = uuid.uuid4().hex
        data.setdefault('title', title or f'Page {len(self.pages)+1}')
        page = AnalysisPage(self, data)
        self.pages[page.id] = page
        self.notebook.add(page.frame, text=data['title'])
        self.notebook.select(page.frame)
        self.dirty = True
        self._refresh_all_buttons()
        return page

    add_page = new_page

    def rename_page(self, title=None, page_id=None):
        page = self.page(page_id)
        if not page:
            return
        if title is None:
            title = simpledialog.askstring('Rename page', 'Page name:', initialvalue=page.data['title'], parent=self.root)
        if title and title.strip():
            page.data['title'] = title.strip()
            self.notebook.tab(page.frame, text=title.strip())
            self.dirty = True

    def close_page(self, page_id=None, confirm=True):
        page = self.page(page_id)
        if not page:
            return False
        if self.job and self.job.get('page_id') == page.id:
            self.global_status.set('Cancel the active job and wait for it to stop before closing its page.')
            return False
        if confirm and (_current(page.data) or page.data.get('source')):
            if not messagebox.askyesno('Close page', 'Close this page? Save the session first to keep its state.', parent=self.root):
                return False
        self.notebook.forget(page.frame)
        del self.pages[page.id]
        page.frame.destroy()
        if not self.pages:
            self.new_page()
        self.dirty = True
        self._refresh_all_buttons()
        return True

    def _page_selected(self, event=None):
        page = self.current_page
        if page:
            page.refresh_buttons()
            page._schedule_sidebar_restore()
            if not self.job:
                self.global_status.set(f"Active page: {page.data['title']}")

    def browse_snp(self, page_id=None):
        path = filedialog.askopenfilename(parent=self.root, title='Load Touchstone SNP', filetypes=[('Touchstone SNP', '*.s*p *.S*P *.ts'), ('All files', '*')])
        if path:
            self.load_snp(path, page_id)

    def choose_workspace(self, page_id=None):
        page = self.page(page_id)
        if not page:
            return
        path = filedialog.askdirectory(parent=self.root, title='Select a Virtuoso workspace containing cds.lib')
        if path:
            page.vars['workspace'].set(path)

    def browse_native_setting(self, page_id, key):
        page = self.page(page_id)
        if not page:
            return
        if key == 'existing_library_path':
            path = filedialog.askdirectory(parent=self.root, title='Select an existing OA library directory')
        else:
            path = filedialog.askopenfilename(parent=self.root, title='Select '+key.replace('_', ' '))
        if path:
            page.vars[key].set(path)

    def load_snp(self, path, page_id=None):
        page = self.page(page_id)
        if not page or self.job:
            return False
        if not str(path).strip():
            page.set_status('Choose an SNP file to load.', 'warning')
            return False
        path = str(Path(path).expanduser())
        page._populating = True
        page.vars['source_path'].set(path)
        page.vars['ports_confirmed'].set(False)
        page.data['config']['source_path'] = path
        page.data['config']['ports_confirmed'] = False
        page._populating = False
        page.data['source_valid'] = False
        page._revision += 1
        page.data['identity_stale'] = bool(_current(page.data))
        page.data['results_stale'] = bool(_current(page.data))
        page.set_status('Loading and validating SNP data...', 'working')
        return self._dispatch('load', page.id, lambda cancel, progress: inspect_ui_source(path), {'source_path': path}, None)

    def start_integrated(self, page_id=None):
        page = self.page(page_id)
        if not page or self.job: return False
        config = copy.deepcopy(page.sync())
        source = page.data.get('source') or {}
        if source.get('positive_point_count', 0) < 2:
            page.set_status('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.', 'error'); return False
        if not page.data.get('source_valid') or not source.get('sha256'):
            page.set_status('Load an SNP before creating the target views.', 'error'); return False
        config.update(source_sha256=source['sha256'], runtime=str(self.runtime), overwrite=True)
        # The sampled quick profile covers supplied samples; strict Studio band fields remain unchanged.
        config.pop('fmin_ghz', None); config.pop('fmax_ghz', None)
        config['cds_lib'] = self.integration_context.get('cds_lib') or str(Path(config['workspace'])/'cds.lib')
        config['flux_native_runtime'] = self.integration_context.get('flux_native_runtime')
        try:
            config['port_order'] = [int(v.strip()) for v in str(config['port_order']).split(',') if v.strip()]
        except ValueError:
            page.set_status('Port mapping must contain comma-separated integers.', 'error'); return False
        target = '%s / %s (%s)' % (config.get('library'), config.get('cell'), config.get('workspace'))
        if not messagebox.askyesno('Create / overwrite target views', 'Create or replace schematic and symbol at:\n'+target+'\nExisting views will be backed up.', parent=self.root): return False
        from .integration_api import run_request
        return self._dispatch('integrated', page.id, lambda cancel, progress: run_request(config, progress=progress, cancel_event=cancel), {k:v for k,v in config.items() if k != 'flux_native_runtime'}, None)

    def start_action(self, action, page_id=None):
        page = self.page(page_id)
        if not page or self.job:
            return False
        error = page._input_error(require_targets=action != 'preview')
        if error:
            page.set_status(error, 'error')
            page.append_log(error, 'error')
            return False
        config = copy.deepcopy(page.sync())
        baseline = None
        if action in ('tune', 'upgrade'):
            baseline = _current(page.data)
            if not baseline or not baseline.get('circuit'):
                page.set_status('Generate an initial circuit first.', 'warning')
                return False
            if page.data.get('identity_stale'):
                page.set_status('Source/model/ports/band changed. Generate a new initial circuit.', 'warning')
                return False
        elif action in ('native','symbol'):
            baseline = page.selected_snapshot()
            if not baseline or not baseline.get('passed') or page.data.get('results_stale') or page.data.get('identity_stale'):
                page.set_status('Native generation requires an accepted snapshot under the current inputs and targets.', 'warning')
                return False
            if not config.get('workspace') or not Path(config['workspace'], 'cds.lib').is_file():
                page.set_status('Select a Virtuoso workspace containing cds.lib.', 'error')
                return False
            if not config.get('library') or not config.get('cell'):
                page.set_status('Enter a library and a new cell name.', 'error')
                return False
            if not page._visual_refresh():return False
            config=copy.deepcopy(page.sync())
            if action=='symbol' and not baseline.get('native_verified'):
                page.set_status('Generate and verify the schematic before adding its symbol.','warning');return False
        if action == 'upgrade':
            page.append_log('Manual structure upgrade requested. The before-upgrade circuit is preserved.')
        names = {'quick': 'Creating quick initial circuit', 'tune': 'Tuning parameters with fixed topology', 'upgrade': 'Trying a manual structure upgrade', 'native': 'Generating and verifying a new native schematic', 'symbol':'Adding the matching symbol to the verified schematic cell', 'preview': 'Preparing original SNP curves'}
        page.set_status(names.get(action, action)+'...', 'working')
        baseline = copy.deepcopy(baseline)
        return self._dispatch(action, page.id,
                              lambda cancel, progress: engine.run_action(action, config, baseline=baseline, runtime=self.runtime, cancel=cancel, progress=progress),
                              config, baseline)

    def _dispatch(self, action, page_id, fn, config, baseline):
        if self.job:
            return False
        self._token += 1
        token = self._token
        self.cancel = threading.Event()
        self.job = {'token': token, 'page_id': page_id, 'action': action, 'config': copy.deepcopy(config), 'baseline': copy.deepcopy(baseline), 'started': time.monotonic(), 'revision': self.pages[page_id]._revision if page_id in self.pages else None, 'baseline_stale': bool(self.pages[page_id].data.get('results_stale')) if page_id in self.pages else False}
        self._refresh_all_buttons()
        def run():
            try:
                value = fn(self.cancel, lambda msg: self.events.put((token, page_id, 'progress', msg)))
                self.events.put((token, page_id, 'done', value))
            except BaseException as exc:
                self.events.put((token, page_id, 'error', {'message': str(exc), 'traceback': traceback.format_exc()}))
        self.thread = threading.Thread(target=run, name='SNPFit-'+action, daemon=False)
        self.thread.start()
        self.global_status.set(f"{action.replace('_', ' ').title()} started"+(f" in {self.pages[page_id].data['title']}." if page_id in self.pages else '.'))
        return True

    def cancel_job(self):
        if not self.job:
            return
        self.cancel.set()
        page = self.pages.get(self.job.get('page_id'))
        if page:
            page.set_status('Cancellation requested. Waiting for the current operation to stop...', 'warning')
            page.append_log('Cancellation requested. Completed results remain available.', 'warning')
        self.global_status.set('Stopping the active operation. The window remains open until it has stopped.')

    def _refresh_all_buttons(self):
        for page in self.pages.values():
            page.refresh_buttons()
        for key in ('open', 'doctor'):
            if key in self.toolbar_buttons:
                self.toolbar_buttons[key].configure(state='disabled' if self.job else 'normal')
        if 'new' in self.toolbar_buttons:
            loading = self.job and self.job.get('action') == 'load_session'
            self.toolbar_buttons['new'].configure(state='disabled' if loading else 'normal')
        if 'close_page' in self.toolbar_buttons:
            owner = self.job and self.current_page and self.job.get('page_id') == self.current_page.id
            self.toolbar_buttons['close_page'].configure(state='disabled' if owner else 'normal')

    def poll(self):
        try:
            while True:
                token, page_id, kind, value = self.events.get_nowait()
                if not self.job or token != self.job['token'] or page_id != self.job['page_id']:
                    continue
                page = self.pages.get(page_id)
                if kind == 'progress':
                    if page:
                        text = value.get('message', str(value)) if isinstance(value, dict) else str(value)
                        page.append_log(value)
                        if not self.cancel.is_set():
                            page.set_status(text, 'working')
                    continue
                job = self.job
                self.job = None
                if self.thread and not self.thread.is_alive():
                    self.thread.join(timeout=0)
                    self.thread = None
                obsolete = bool(page and job.get('revision') != page._revision)
                if kind == 'error':
                    target_page = page or self.current_page
                    if target_page:
                        target_page.set_status('Operation failed: '+value['message'], 'error')
                        target_page.append_log(value['traceback'], 'error')
                    self.global_status.set('Operation failed: '+value['message'])
                elif self.cancel.is_set():
                    if page:
                        page.set_status('Cancelled. Previous completed results remain available.', 'warning')
                        page.append_log('Operation cancelled; the returned partial result was not adopted.', 'warning')
                    self.global_status.set('Cancelled.')
                elif obsolete:
                    if page:
                        page.append_log('Ignored a result for an obsolete input revision.', 'warning')
                        page.set_status('Inputs changed during processing. The obsolete result was not adopted.', 'warning')
                else:
                    try:
                        self._apply_result(job, page, value)
                    except Exception as exc:
                        target_page = page or self.current_page
                        if target_page:
                            target_page.set_status('Result display failed: '+str(exc)+'. Completed numerical files remain in the run directory.', 'error')
                            target_page.append_log(traceback.format_exc(), 'error')
                        self.global_status.set('Result display failed: '+str(exc))
                self._refresh_all_buttons()
                if self._closing:
                    self._finish_close()
                    return
        except queue.Empty:
            pass
        if self.job:
            elapsed = time.monotonic()-self.job['started']
            page = self.pages.get(self.job.get('page_id'))
            if page:
                page.elapsed_var.set(f'{elapsed:.1f} s')
        self._poll_id = self.root.after(80, self.poll)

    def _apply_result(self, job, page, value):
        action = job['action']
        if action == 'save_session':
            self.session_path = job['config']['path']
            self.dirty = False
            self.global_status.set('Session saved: '+self.session_path)
            return
        if action == 'load_session':
            self._apply_session(value)
            self.session_path = job['config']['path']
            return
        if action == 'diagnostics':
            if page:
                page.append_log(json.dumps(value, ensure_ascii=False, indent=2))
                page.views.select(3)
                page.set_status('Diagnostics completed. See the Log tab.', 'info')
            return
        if page is None:
            return
        if action == 'integrated':
            page.append_log(json.dumps(value, ensure_ascii=False, indent=2))
            page.set_status(('Schematic and symbol verified: ' if value.get('passed') else 'Generation incomplete: ')+value.get('status', ''), 'success' if value.get('passed') else 'warning')
            page.views.select(3)
            return
        if action == 'load':
            old = _snapshots(page.data)
            if any(old.values()):
                page.data.setdefault('history', []).append(copy.deepcopy(old))
            page.data['results'] = {'quick': None, 'current': None, 'before_upgrade': None, 'upgraded': None}
            page.data.setdefault('view', {}).pop('axis_limits', None)
            page.data['source'] = value
            positive_points = value.get('positive_point_count', value.get('point_count', 2))
            page.data['source_valid'] = int(positive_points) >= 1
            page.data['results_stale'] = False
            page.data['identity_stale'] = False
            source_name = Path(job['config']['source_path']).name
            if page.data['title'].startswith('Page '):
                page.data['title'] = source_name
                self.notebook.tab(page.frame, text=source_name)
            if page.data['source_valid']:
                page.set_status('SNP loaded. Choose a model, confirm ports, and set this page\'s targets.', 'info')
            else:
                page.set_status('The SNP has no positive-frequency samples.', 'warning')
            page.append_log('Loaded source: '+job['config']['source_path'])
            if value.get('sampled_data'):
                page.append_log('Original single-point S data (real/imag): '+json.dumps(value['sampled_data'], ensure_ascii=False))
                page.set_status('Fitting needs at least two actual simulated frequencies. Add frequencies in EMX Settings and rerun.', 'warning')
                page.views.select(3)
            page._update_model_details(reset_ports=False)
        elif action == 'preview':
            if page.data.get('identity_stale'):
                page.data.setdefault('history', []).append(copy.deepcopy(_snapshots(page.data)))
                page.data['results'] = {'quick': None, 'current': value, 'before_upgrade': None, 'upgraded': None, 'preview': value}
                page.data['identity_stale'] = False
                page.data['results_stale'] = False
            else:
                _snapshots(page.data)['preview'] = value
                if not _current(page.data):
                    _snapshots(page.data)['current'] = value
            page.set_status('Original SNP curves loaded. Quick Initial will create the first circuit.', 'info')
        else:
            results = _snapshots(page.data)
            if action == 'quick':
                if results.get('current'):
                    page.data.setdefault('history', []).append(copy.deepcopy(results))
                page.data['results'] = {'quick': value, 'current': value, 'before_upgrade': None, 'upgraded': None}
                page.data['results_stale'] = False
                page.data['identity_stale'] = False
                page.views.select(0)
            elif action == 'tune':
                results['current'] = value
                if results.get('upgraded') and job.get('baseline', {}).get('id') == results['upgraded'].get('id'):
                    results['upgraded'] = value
                page.data['results_stale'] = False
            elif action == 'upgrade':
                if results.get('before_upgrade') or results.get('upgraded'):
                    page.data.setdefault('history', []).append({'before_upgrade': copy.deepcopy(results.get('before_upgrade')), 'upgraded': copy.deepcopy(results.get('upgraded'))})
                results['before_upgrade'] = copy.deepcopy(job['baseline'])
                results['upgraded'] = value
                not_selected = value.get('accepted_new') is False or value.get('selected') is False or str(value.get('status', '')).lower() in ('no_improvement', 'no improvement', 'upgrade_not_selected')
                results['current'] = job['baseline'] if not_selected else value
                page.data['results_stale'] = bool(job.get('baseline_stale')) if not_selected else False
                page.views.select(1)
            elif action in ('native','symbol'):
                original_id = (job.get('baseline') or {}).get('id')
                for key, snapshot in list(results.items()):
                    if isinstance(snapshot, dict) and snapshot.get('id') == original_id:
                        results[key] = value
                page.views.select(1 if action=='symbol' else 2)
                if action=='symbol':page.data.setdefault('view',{})['circuit_panel']=1
            status = value.get('status', '')
            warnings = value.get('warnings', [])
            if action == 'quick':
                message = 'Initial circuit ready. It is unverified; Fit for final targets.'
                level = 'warning'
            elif action in ('native','symbol'):
                message = 'Native schematic generated and verified.' if value.get('native_verified') else 'Native generation finished without a verified result. See details.'
                level = 'success' if value.get('native_verified') else 'warning'
                if action=='symbol':message='Symbol generated. The verified schematic and fitted response are unchanged.'
            elif value.get('passed'):
                message = 'Current numerical targets passed. Native schematic generation is available.'
                level = 'success'
            else:
                message = 'Targets not met. Best completed result retained; review the error details.'
                level = 'warning'
            if action == 'upgrade' and (value.get('accepted_new') is False or value.get('selected') is False):
                message = 'Upgrade attempt retained for comparison; the previous circuit remains current.'
                level = 'warning'
            page.set_status(message, level)
            page.append_log(f'{action.title()} completed: {status or message}', level)
            for warning in warnings:
                page.append_log(warning, 'warning')
        self.dirty = True
        page.refresh()
        self.global_status.set(f"{page.data['title']}: {page.status_var.get()}")

    def _page_list(self):
        order = [str(x) for x in self.notebook.tabs()]
        ordered = sorted(self.pages.values(), key=lambda p: order.index(str(p.frame)))
        for page in ordered:
            page.sync()
            page.capture_views()
        return copy.deepcopy([p.data for p in ordered])

    def save_session_dialog(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='Save complete session', defaultextension='.json', initialfile=Path(self.session_path).name if self.session_path else 'snpfit-session.json', filetypes=[('SNPFit session', '*.json')])
        if path:
            self.save_session(path)

    def save_session(self, path):
        if self.job:
            # Saving a completed-state snapshot while fitting is safe; no live
            # worker object is serialized and the request is not resumed later.
            try:
                result = state.save_session(path, self._page_list(), self.current_page.id if self.current_page else None, {'geometry': self.root.geometry()})
                self.session_path = str(path)
                self.global_status.set('Completed state saved while the active job continues: '+str(path))
                return result
            except Exception as exc:
                self.global_status.set('Session save failed: '+str(exc))
                return None
        pages = self._page_list()
        active = self.current_page.id if self.current_page else None
        geometry = self.root.geometry()
        return self._dispatch('save_session', None, lambda cancel, progress: state.save_session(path, pages, active, {'geometry': geometry}), {'path': str(path)}, None)

    def open_session_dialog(self):
        if self.job:
            return
        path = filedialog.askopenfilename(parent=self.root, title='Load complete session', filetypes=[('SNPFit session', '*.json'), ('All files', '*')])
        if path:
            self.open_session(path)

    def open_session(self, path):
        if self.job:
            return False
        previous = self._page_list() if self.dirty else None
        previous_active = self.current_page.id if self.current_page else None
        geometry = self.root.geometry()
        def load(cancel, progress):
            session = state.load_session(path)
            if previous and not cancel.is_set():
                recovery = self.runtime / 'sessions' / ('before-open-'+uuid.uuid4().hex[:8]+'.json')
                recovery.parent.mkdir(parents=True, exist_ok=True)
                state.save_session(recovery, previous, previous_active, {'geometry': geometry})
                session['replaced_session_recovery'] = str(recovery)
            return session
        return self._dispatch('load_session', None, load, {'path': str(path)}, None)

    load_session = open_session

    def _apply_session(self, session):
        for page in list(self.pages.values()):
            self.notebook.forget(page.frame)
            page.frame.destroy()
        self.pages.clear()
        for data in session.get('pages', []):
            self.new_page(data=data)
        if not self.pages:
            self.new_page()
        active = self.pages.get(session.get('active_id'))
        if active:
            self.notebook.select(active.frame)
        geometry = (session.get('window_state') or {}).get('geometry')
        if geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass
        warnings = session.get('warnings', [])
        self.dirty = False
        for page in self.pages.values():
            if page.data.get('results_stale'):
                page.set_status('Restored cached results. The source is missing or changed; reload it before fitting or export.', 'warning')
            page.refresh()
        self.global_status.set('Session restored'+(f' with {len(warnings)} warning(s). See page logs.' if warnings else '.'))
        if warnings and self.current_page:
            for warning in warnings:
                self.current_page.append_log(warning, 'warning')
        if session.get('replaced_session_recovery') and self.current_page:
            self.current_page.append_log('Previous unsaved pages were preserved in: '+session['replaced_session_recovery'])

    def export_spice(self, page_id=None, path=None):
        page = self.page(page_id)
        snapshot = page.selected_snapshot() if page else None
        if not snapshot or not snapshot.get('circuit'):
            if page:
                page.set_status('No circuit is available for SPICE export.', 'warning')
            return False
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title='Export selected circuit as SPICE', defaultextension='.sp', initialfile='initial.sp' if snapshot.get('stage') == 'quick' else 'fitted.sp')
        if not path:
            return False
        try:
            from .circuit import write_spice
            write_spice(snapshot['circuit'], path)
            if not snapshot.get('passed'):
                p = Path(path)
                p.write_text('* INITIAL / UNACCEPTED CIRCUIT - NOT A VERIFIED MODEL\n'+p.read_text(encoding='utf-8'), encoding='utf-8')
            page.set_status('SPICE exported: '+str(path), 'info')
            return True
        except Exception as exc:
            page.set_status('SPICE export failed: '+str(exc), 'error')
            return False

    def export_plot(self, page_id=None, path=None):
        page = self.page(page_id)
        if page is None:
            return False
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title='Save comparison plot', defaultextension='.png', filetypes=[('PNG image', '*.png'), ('PDF document', '*.pdf'), ('SVG image', '*.svg')])
        if path:
            try:
                page.comparison.export_plot(path)
                page.set_status('Comparison plot saved: '+str(path), 'info')
                return True
            except Exception as exc:
                page.set_status('Plot export failed: '+str(exc), 'error')
        return False

    def export_csv(self, page_id=None, path=None):
        page = self.page(page_id)
        if page is None:
            return False
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title='Export original comparison samples', defaultextension='.csv', filetypes=[('CSV table', '*.csv')])
        if path:
            try:
                page.comparison.export_csv(path)
                page.set_status('Comparison samples exported: '+str(path), 'info')
                return True
            except Exception as exc:
                page.set_status('CSV export failed: '+str(exc), 'error')
        return False

    def export_circuit(self, page_id=None, path=None):
        page = self.page(page_id)
        if page is None:
            return False
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title='Save circuit topology comparison', defaultextension='.png', filetypes=[('PNG image', '*.png'), ('PDF document', '*.pdf'), ('SVG image', '*.svg')])
        if path:
            try:
                page.circuits.export_figure(path)
                page.set_status('Circuit figure saved: '+str(path), 'info')
                return True
            except Exception as exc:
                page.set_status('Circuit export failed: '+str(exc), 'error')
        return False

    def export_log(self, page_id=None, path=None):
        page = self.page(page_id)
        if page is None:
            return False
        if path is None:
            path = filedialog.asksaveasfilename(parent=self.root, title='Save complete page log', defaultextension='.log')
        if path:
            try:
                Path(path).write_text('\n'.join(page._log_line(e) for e in page.data.get('logs', []))+'\n', encoding='utf-8')
                page.set_status('Log saved: '+str(path), 'info')
                return True
            except Exception as exc:
                page.set_status('Log export failed: '+str(exc), 'error')
        return False

    def diagnostics(self):
        if self.job:
            return
        from .workflow import diagnose
        page = self.current_page
        self._dispatch('diagnostics', page.id if page else None, lambda cancel, progress: diagnose(), {}, None)

    def show_help(self):
        messagebox.showinfo('SNPFit Studio workflow',
            '1. Add a page and Load SNP. Each page has independent inputs and results.\n'
            '2. Choose an explicit model. Map file ports to the listed roles and confirm the terminations.\n'
            '3. Enter the frequency band and required L/Q targets; set k, SRF, S and element limits.\n'
            '4. Quick Initial aims to create an editable starting circuit in about 5 seconds. Longer runs continue normally; 5 seconds is not a timeout or an error threshold.\n'
            '5. Fit changes device values while keeping the circuit topology fixed.\n'
            '6. Upgrade Structure runs only on your click. Before/after circuits and three response curves are preserved.\n'
            '7. Generate Schematic creates a new analogLib cell and verifies its native response; existing cells are never overwritten.\n\n'
            '8. Generate Symbol adds the unified single-turn symbol to that verified cell. Existing symbols are never overwritten.\n\n'
            '9. Winding drawings use inductor legs at the bottom, transformer primary at the top and secondary at the bottom. Segment Q uses the selected annotation frequency and excludes mutual coupling and shunt loading; the full winding Q remains in the comparison plots.\n\n'
            'Initial/unaccepted circuits may be exported as SPICE. Undefined k or an unresolved first-SRF reference is shown explicitly.\n'
            'The quick time target excludes OA/tool startup and native simulation. Actual elapsed time is shown; exceeding the target is not a warning or an error. Save Session preserves every page and completed result; it does not resume active jobs.', parent=self.root)

    def close(self):
        if self.job:
            if not self._closing and not messagebox.askyesno('Stop and close', 'Cancel the active operation and close after it has stopped?', parent=self.root):
                return
            self._closing = True
            self.cancel_job()
            return
        if self.dirty:
            answer = messagebox.askyesnocancel('Save session', 'Save this session before closing?', parent=self.root)
            if answer is None:
                return
            if answer:
                path = filedialog.asksaveasfilename(parent=self.root, title='Save complete session before closing', defaultextension='.json', initialfile='snpfit-session.json')
                if not path:
                    return
                try:
                    state.save_session(path, self._page_list(), self.current_page.id if self.current_page else None, {'geometry': self.root.geometry()})
                except Exception as exc:
                    messagebox.showerror('Session save failed', str(exc), parent=self.root)
                    return
        self._finish_close()

    def _finish_close(self):
        if self.job:
            return
        try:
            # A private recovery state never overwrites a user's named session.
            recovery = self.runtime / 'sessions' / ('recovery-'+uuid.uuid4().hex[:8]+'.json')
            recovery.parent.mkdir(parents=True, exist_ok=True)
            state.save_session(recovery, self._page_list(), self.current_page.id if self.current_page else None, {'geometry': self.root.geometry()})
        except Exception:
            pass
        if self._poll_id:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.root.destroy()


def main(runtime=None, request_path=None):
    initial = json.loads(Path(request_path).read_text(encoding='utf-8')) if request_path else {}
    root = tk.Tk()
    app = App(root, runtime=runtime or initial.get('runtime'))
    if initial:
        app.integration_context = {k: initial.get(k) for k in ('cds_lib', 'flux_native_runtime')}
        page = app.current_page
        config = page.data['config']
        config.update({k:v for k,v in initial.items() if k in config})
        config.update(l_pct=5, q_pct=20)
        page.populate(page.data)
        if initial.get('source_path'):
            app.load_snp(initial['source_path'], page.id)
    root.mainloop()
