#!/usr/bin/env python3
"""Native Tk UI. The backend alone computes and writes layout geometry."""
import datetime
import argparse
import copy
import colorsys
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import traceback

# Optional 3D preview (Layout 3D Viewer).  Off unless FLUX_VL3D_ROOT names a VL3D
# checkout; FLUX_VL3D_PYTHONPATH may add a private folder holding PyOpenGL and
# pyopengltk.  prepare_runtime() must run before tkinter is imported.
embed = None
EMBED_NOTE = 'FLUX_VL3D_ROOT is not set'


def _load_embed():
    global embed, EMBED_NOTE
    root = os.environ.get('FLUX_VL3D_ROOT', '').strip()
    if not root:
        return
    if not os.path.isdir(root):
        EMBED_NOTE = 'FLUX_VL3D_ROOT is not a folder: ' + root
        return
    for path in [root] + [p for p in os.environ.get('FLUX_VL3D_PYTHONPATH', '').split(os.pathsep) if p.strip()]:
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from native import embed as module
        module.prepare_runtime()
        embed, EMBED_NOTE = module, ''
    except Exception as exc:  # 3D stays off; Flux keeps its 2D preview.
        embed, EMBED_NOTE = None, '3D preview could not be loaded: {}'.format(exc)


_load_embed()
VIEW_MODES = ('2D', '3D Studio', '3D Metal')
import tkinter as tk
import fieldwork_tk as fw
from fieldwork_ui import bind_status
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

from geometry import shielding_parameter_active, shielding_role_active, shielding_state
import leg_pins

ROOT = Path(__file__).resolve().parent
PIN_LABEL_FONT = ('liberation sans', -8)  # Fixed pixels: discreet at every Tk DPI.

TITLE = 'Flux Studio — Untitled'
COLORS = dict(bg=fw.BG, panel=fw.PANEL, raised=fw.RAISED, border=fw.LINE,
              text=fw.TEXT, muted=fw.DIM, cyan=fw.accent('flux'), gold=fw.WARN,
              violet=fw.SERIES[4], green=fw.OK, red=fw.ERR, input=fw.RAISED)

PARTS = {'body': 'Winding', 'crossing': 'Crossover', 'top_crossing': 'Top crossover',
         'bottom_crossing': 'Bottom crossover', 'legs': 'Leads', 'pgs': 'PGS',
         'ring': 'GND ring', 'via': 'Via', 'library': 'Library', 'cell': 'Cell name',
         'parameters': 'Parameters', 'input': 'Parameters', 'system': 'System',
         'technology': 'PDK', 'pdk': 'PDK', 'ground_route': 'Ground route', 'secondary': 'Secondary',
         'tap': 'Center TAP'}
ROLES = ['body', 'secondary', 'crossing', 'tap', 'pgs', 'ring', 'ground_route']
DEFAULTS = dict(inner_diameter=100, width=8, spacing=8, turns=2,
                leg_length=45, terminal_gap=12, center_tap=False, rule_policy='warn',
                pgs_extent=95, pgs_width=.4, pgs_spacing=4,
                pgs_spine_width=None, pgs_shape='octagon', ring_extent=103, ring_width=5,
                ring_shape='square', pgs_connect_ring=False, ground_route_width=1, pgs_enabled=True, ring_enabled=True)
SHARED_KEYS = ['pgs_enabled', 'ring_enabled', 'pgs_extent', 'pgs_width', 'pgs_spacing', 'pgs_spine_width', 'pgs_shape',
               'ring_extent', 'ring_width', 'ring_shape', 'pgs_connect_ring', 'ground_route_width']
SHARED_ROLES = ['pgs', 'ring', 'ground_route']
from structures import MODEL_DEFINITIONS as BUILTIN_MODELS
DEFAULT_MODEL = 'differential_inductor'
# Until the backend context arrives, show the built-in model with its real schema.
FALLBACK_MODELS = {item['id']: dict(copy.deepcopy(item), parameters=[f['name'] for f in item['parameter_schema']])
                   for item in BUILTIN_MODELS if item['id'] == DEFAULT_MODEL}
LABELS = dict(inner_diameter='Inner opening diameter', width='Winding width', spacing='Turn spacing',
              turns='Turns', leg_length='Lead extension', terminal_gap='Terminal edge gap',
              pgs_extent='PGS radius / half-size', pgs_width='Finger width', pgs_spacing='Finger gap',
              pgs_spine_width='Spine width', ring_extent='Ring inner half-size', ring_width='Ring width',
              ground_route_width='Ground route width', center_tap='Include center TAP',
              rule_policy='PDK rule checking')
# Leg Pin manager: everything except legs is dimmed; pins use one loud color.
CANVAS_BG = fw.PLOT_BG
LEG_PIN_COLOR = '#ff5fd2'
LEG_PIN_SELECTED = '#ffffff'
LEG_SELECTED_COLOR = '#ffe27a'
LEG_CLICK_COLOR = '#7dfcff'
RESERVED_PIN_NAMES = ('GND_PGS', 'GND_RING')
LEG_PIN_MIN_PIXELS = 7


def dim_color(color, keep=0.3, background=CANVAS_BG):
    """Blend a #rrggbb color toward the canvas background."""
    try:
        source = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        target = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    except (TypeError, ValueError, IndexError):
        return color
    return '#{:02x}{:02x}{:02x}'.format(*(round(t + (s - t) * keep) for s, t in zip(source, target)))


def preview_with_legs(design):
    """Background preview that also reports the terminal legs.

    Custom leg pins replace the default pins that identify legs, so the legs
    are taken from the same design previewed without its leg-pin layout.
    """
    import backend
    from technology import drawing_grid
    result = backend.preview(design)
    if not isinstance(result, dict):
        return result
    try:
        entry = backend.LIBRARIES.get(design.get('library')) or {}
        grid = entry.get('grid') or (drawing_grid(entry['_tech']) if entry.get('_tech') else 0.001)
        base = result
        if design.get('leg_pins'):
            stripped = dict(design)
            stripped.pop('leg_pins', None)
            base = backend.preview(stripped)
        geometry = base.get('geometry') if isinstance(base, dict) else None
        if isinstance(geometry, dict) and geometry.get('ports'):
            lines = leg_pins.legs(geometry, grid)
            ids = {leg['id'] for leg in lines}
            result.update(leg_lines=lines, leg_default=leg_pins.default_layout(geometry, grid), leg_grid=grid,
                          leg_reserved=sorted({port['name'] for port in geometry['ports'] if port.get('name') not in ids}))
    except Exception as exc:  # Leg discovery must never hide the real preview.
        result['leg_lines_error'] = str(exc)
    return result


def leg_segments(centerline):
    """Centerline segments walked from the free end: (start, end, a, b)."""
    points = centerline[::-1]
    rows, walked = [], 0.0
    for a, b in zip(points, points[1:]):
        span = math.hypot(b[0] - a[0], b[1] - a[1])
        if span > 1e-12:
            rows.append((walked, walked + span, a, b))
        walked += span
    return rows


def leg_project(leg, x, y):
    """(distance to centerline, distance from free end, segment) of the nearest point."""
    best = None
    for s0, s1, a, b in leg_segments(leg['centerline']):
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = s1 - s0
        t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (span * span)))
        px, py = a[0] + dx * t, a[1] + dy * t
        distance = math.hypot(x - px, y - py)
        if best is None or distance < best[0]:
            best = (distance, s0 + t * span, (s0, s1))
    return best


def leg_point(leg, distance):
    """World point at ``distance`` from the free end along the centerline."""
    segments = leg_segments(leg['centerline'])
    for s0, s1, a, b in segments:
        if distance <= s1 or (s0, s1, a, b) == segments[-1]:
            t = max(0.0, min(1.0, (distance - s0) / (s1 - s0)))
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    return tuple(leg['centerline'][-1])


def same_pins(pins, reference):
    return len(pins) == len(reference) and all(
        pin['name'] == ref['name'] and abs(pin['ratio'] - ref['ratio']) <= 1e-9 and
        abs(pin['length'] - ref['length']) <= 1e-6 for pin, ref in zip(pins, reference))


def pin_annotations(geometry, layer_visible, xy, width, height):
    """Keep each label close to its visible, actual pin rectangle."""
    result = []
    for port in (geometry or {}).get('ports', []):
        layer, box = port.get('layer', ''), port.get('bbox')
        if not layer_visible.get(layer, True) or not TransformerApp._bounds_valid(box):
            continue
        x0, y0 = xy(*box[0])
        x1, y1 = xy(*box[1])
        x, y = (x0 + x1) / 2, (y0 + y1) / 2
        region = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if region[2] <= 0 or region[0] >= width or region[3] <= 0 or region[1] >= height:
            continue
        result.append(dict(name=str(port.get('name', '')), layer=layer,
                           kind=port.get('kind', 'signal'), region=region, point=(x, y)))
    return result


def pin_layer_colors(geometry):
    """Assign distinct colors using the complete layer set, independent of visibility."""
    layers = sorted({item.get('layer') for group in ('polygons', 'ports')
                     for item in (geometry or {}).get(group, []) if item.get('layer')})
    colors = {}
    for index, layer in enumerate(layers):
        rgb = colorsys.hsv_to_rgb((0.52 + index / max(1, len(layers))) % 1, .62, .98)
        colors[layer] = '#{:02x}{:02x}{:02x}'.format(*(round(v * 255) for v in rgb))
    return colors


def pin_label_offset(pin, text_box, regions, label_boxes, width, height):
    """Place a label locally using Tk's measured bounds, with a transparent fallback."""
    x0, y0, x1, y1 = text_box
    left, top, right, bottom = x0 - 1, y0 - 1, x1 + 1, y1 + 1
    rx0, ry0, rx1, ry1 = pin['region']
    w, h = right - left, bottom - top
    # Keep clear of title and footer where practical on small canvases.
    margin_y = 25 if height >= 100 else 4
    bounds = (4, margin_y, width - 4, height - margin_y)
    center_x, center_y = (rx0 + rx1) / 2, (ry0 + ry1) / 2
    for step in range(3):
        gap_x, gap_y = 3 + step * 16, 3 + step * (h + 3)
        candidates = [(center_x - w / 2, ry0 - gap_y - h),
                      (center_x - w / 2, ry1 + gap_y),
                      (rx0 - gap_x - w, center_y - h / 2),
                      (rx1 + gap_x, center_y - h / 2),
                      (rx0 - gap_x - w, ry0 - 3 - h),
                      (rx1 + gap_x, ry0 - 3 - h),
                      (rx0 - gap_x - w, ry1 + 3),
                      (rx1 + gap_x, ry1 + 3)]
        for bx, by in candidates:
            # Small edge corrections prevent an otherwise valid local candidate
            # from falling just outside the title/footer safe area.
            if bounds[2] - bounds[0] >= w and bounds[3] - bounds[1] >= h:
                clamped_x = max(bounds[0], min(bounds[2] - w, bx))
                clamped_y = max(bounds[1], min(bounds[3] - h, by))
                if abs(clamped_x - bx) <= 16 and abs(clamped_y - by) <= 16:
                    bx, by = clamped_x, clamped_y
            box = (bx, by, bx + w, by + h)
            if box[0] < bounds[0] or box[1] < bounds[1] or box[2] > bounds[2] or box[3] > bounds[3]:
                continue
            if any(box[0] < r[2] + 2 and box[2] > r[0] - 2 and
                   box[1] < r[3] + 2 and box[3] > r[1] - 2 for r in regions):
                continue
            if any(box[0] < r[2] and box[2] > r[0] and box[1] < r[3] and box[3] > r[1]
                   for r in label_boxes):
                continue
            return bx - left, by - top, box, False
    # The visible part of a huge or crowded pin can have no room around it.
    # Keep its name on that pin without an opaque rectangle hiding the fill.
    cx = (max(0, rx0) + min(width, rx1)) / 2
    cy = (max(0, ry0) + min(height, ry1)) / 2
    if width >= w:
        cx = max(bounds[0] + w / 2, min(bounds[2] - w / 2, cx))
    if bounds[3] - bounds[1] >= h:
        cy = max(bounds[1] + h / 2, min(bounds[3] - h / 2, cy))
    dx, dy = cx - (left + right) / 2, cy - (top + bottom) / 2
    box = (left + dx, top + dy, right + dx, bottom + dy)
    return dx, dy, box, True


def reject_constant(value):
    raise ValueError('Parameters cannot contain NaN or Infinity.')


def boolean_value(value):
    """Decode checkbox/StringVar values without treating the text 'false' as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return {'1': True, 'true': True, 'on': True, 'yes': True,
                '0': False, 'false': False, 'off': False, 'no': False}.get(value.strip().lower())
    return None


def normalize_messages(messages, part='parameters'):
    result = []
    for item in (messages if isinstance(messages, list) else [])[:100]:
        if isinstance(item, str):
            result.append(dict(part=part, message=item, fields=[]))
        elif isinstance(item, dict):
            result.append(dict(part=str(item.get('part', part)),
                               code=str(item.get('code') or ''),
                               message=str(item.get('message') or item.get('code') or 'Validation failed'),
                               fields=[v for v in item.get('fields', []) if isinstance(v, str)][:30]))
    return result


def library_provenance(library):
    """Keep warning/draft identity separate for equal names in other workspaces."""
    library = library or {}
    technology = library.get('technology')
    if isinstance(technology, dict):
        technology = technology.get('name') or technology.get('library')
    return tuple(str(library.get(key) or '') for key in
                 ('name', 'workspace', 'cds_lib', 'library_path', 'technology_path')) + (str(technology or ''),)


def merge_messages(*groups):
    merged, seen = [], set()
    for group in groups:
        for message in normalize_messages(group):
            key = (message['part'], message.get('code', ''), message['message'], tuple(message['fields']))
            if key not in seen:
                merged.append(message)
                seen.add(key)
    return merged


class ScrollPage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style='Panel.TFrame')
        self.canvas = tk.Canvas(self, bg=COLORS['panel'], bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.content = ttk.Frame(self.canvas, padding=(12, 12), style='Panel.TFrame')
        self.window = self.canvas.create_window(0, 0, anchor='nw', window=self.content)
        self.content.bind('<Configure>', lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))


class TransformerApp:
    def __init__(self, root, launch_context=None, technology_path=None):
        self.root = root
        root.title(TITLE)
        root.geometry('1280x900')
        root.minsize(1280, 800)
        root.configure(bg=COLORS['bg'])
        self.context = None
        self.source_context = copy.deepcopy(launch_context)
        self.models = copy.deepcopy(FALLBACK_MODELS)
        self.current_model = DEFAULT_MODEL
        self.body_drafts = {}
        self.applied_design = None
        self.preview_library = None
        self.dirty = True
        self.parameter_controls = []
        self.library = None
        self.warned_library_provenances = set()
        self.geometry = None
        self.geometry_design = None
        self.revision = 0
        self.preview_revision = -1
        self.preview_ok = False
        self.suspended = True
        self.context_busy = False
        self.preview_busy = False
        self.generating = False
        self.close_pending = False
        self.closed = False
        self.preview_after = None
        self.redraw_after = None
        self.preview_task = None
        self.tasks = queue.Queue()
        self.results = queue.Queue()
        self.task_id = 0
        self.field_widgets = {}
        self.field_tabs = {}
        self.layer_visible = {}
        self.layer_flags = {}
        self.messages = {}
        self.zoom = 1.0
        self.view = None
        self.pan_start = None
        self.last_job = ''
        self.generated_target = None
        self.preset_busy = False
        self.preview_errors = []
        self.leg_mode = False
        self.leg_lines = []
        self.leg_default = {}
        self.leg_grid = 0.001
        self.leg_reserved = []
        self.leg_layout = {}
        self.leg_selected = None
        self.leg_pin_selected = None
        self.leg_clicks = {}
        self.leg_pins_draft = None
        self.leg_preview_pending = False
        self.leg_tree_syncing = False
        self.press_at = None
        self.leg_status = tk.StringVar(value='Leg pins: default')
        self.leg_info = tk.StringVar(value='')
        self.leg_name_var = tk.StringVar()
        self.leg_distance_var = tk.StringVar()
        self.leg_length_var = tk.StringVar()
        self.vars = {name: (tk.BooleanVar(value=value) if isinstance(value, bool) else
                           tk.StringVar(value='' if value is None else str(value))) for name, value in DEFAULTS.items()}
        self.spine_follow = tk.BooleanVar(value=True)
        self.vars.update({role: tk.StringVar(value=value) for role, value in
                          dict.fromkeys(ROLES, '').items()})
        self.vars.update(library=tk.StringVar(), cell=tk.StringVar(value='transformer_generated'),
                         structure=tk.StringVar(value='layout'), preset=tk.StringVar(),
                         preset_name=tk.StringVar(), model=tk.StringVar(value=self.current_model))
        self.model_selection = tk.StringVar(value=self.models[self.current_model]['title'])
        self.draft_status = tk.StringVar(value='Draft not applied')
        self.connection = tk.StringVar(value='Identifying libraries and attached PDKs...')
        self.library_info = tk.StringVar(value='Metal layers and native via rules come from the attached PDK.')
        self.runtime_info = tk.StringVar(value='Runtime: identifying the current workspace and Cadence installation...')
        self.preview_state = tk.StringVar(value='Load example to begin')
        self.metrics = tk.StringVar(value='Size —    Ring gap —    Via cuts —    Ports —')
        initial = 'Load example for an offline layout, or choose a technology JSON.'
        if os.environ.get('FLUX_VL3D_ROOT', '').strip() and EMBED_NOTE:
            initial = '3D preview off: ' + EMBED_NOTE  # configured but not loadable
        self.status = tk.StringVar(value=initial)
        self._styles()
        self._build()
        self.compact = None
        root.bind('<Configure>', self._responsive)
        root.report_callback_exception = self._callback_error
        for name in list(DEFAULTS) + ROLES + ['cell', 'structure']:
            self.vars[name].trace_add('write', self._changed)
        self.suspended = False
        threading.Thread(target=self._worker, name='TransformerLayoutWorker', daemon=True).start()
        self._poll_after = self.root.after(60, self._poll)
        if technology_path:
            self.root.after(120, lambda: self.load_technology_file(technology_path))
        elif launch_context:
            self.root.after(120, self.load_context)
        else:
            self.connection.set('Offline workspace')
            self.runtime_info.set('Choose a technology JSON or connect your Cadence workspace.')
            self.preview_state.set('Load example to begin')
            self.status.set('Load example for an offline layout, or load your own technology JSON.')
        root.protocol('WM_DELETE_WINDOW', self.close)

    def _styles(self):
        style = fw.apply(self.root, product='flux')
        fw.apply_matplotlib()
        for name, values in {
            'Raised.TFrame': dict(background=fw.RAISED),
            'Bg.TLabel': dict(background=fw.BG),
            'Muted.TLabel': dict(foreground=fw.DIM),
            'Note.TLabel': dict(foreground=fw.DIM),
            'Eyebrow.TLabel': dict(foreground=fw.DIM),
            'Invalid.TEntry': dict(bordercolor=fw.ERR),
            'Invalid.TCombobox': dict(bordercolor=fw.ERR),
            'Mode.TButton': dict(foreground=fw.WARN),
            'ModeActive.TButton': dict(background=fw.WARN, foreground='#2b1d08'),
        }.items():
            style.configure(name, **values)
        style.map('Mode.TButton', foreground=[('disabled', fw.DIM), ('!disabled', fw.WARN)])
        style.map('ModeActive.TButton', background=[('disabled', fw.RAISED), ('active', '#f5c862')],
                  foreground=[('disabled', fw.DIM), ('!disabled', '#2b1d08')])

    def _global_actions(self, parent):
        ttk.Button(parent, text='Load example', command=self.load_example).pack(side='left', padx=4)
        ttk.Button(parent, text='Technology JSON', command=self.choose_technology).pack(side='left', padx=4)
        ttk.Button(parent, text='SNPFit Studio', style='Ghost.TButton', command=self.open_snpfit).pack(side='left', padx=4)

    def open_snpfit(self):
        import subprocess, sys
        subprocess.Popen([sys.executable, '-B', str(ROOT/'snpfit_launcher'/'launch.py'), 'gui'])

    def choose_technology(self):
        path = filedialog.askopenfilename(parent=self.root, title='Load technology JSON', filetypes=[('Technology JSON', '*.json')])
        if path:
            self.load_technology_file(path)

    def load_example(self):
        self.load_technology_file(ROOT/'examples'/'generic_technology.json', example=True)

    def load_technology_file(self, path, example=False):
        if self.context_busy or self.generating or self.preview_busy:
            self.status.set('Wait for the current layout task to finish before loading technology.')
            return
        import backend
        try:
            payload = backend.load_file_context(path)
            self.context = payload
            self._install_models(payload)
            self.library_combo.configure(values=[lib['name'] for lib in payload['libraries']])
            self.configure_library(payload['selected_library'], apply_defaults=True)
            self.connection.set('Offline technology')
            self.preview_state.set('Apply parameters to preview')
            self.status.set('Technology loaded. Generate Layout exports JSON; connect Cadence for native OA output.')
            self.root.title('Flux Studio — '+Path(path).name)
            self.invalidate()
            if example:
                self.apply_parameters()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.set('Technology load failed: '+str(exc)+'. Choose a valid technology JSON.')
        self.update_controls()

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.workspace_tabs = ttk.Notebook(self.root)
        fw.header(self.root, 'Flux Studio', 'RF passive layout & EMX', right=self._global_actions).grid(row=0, column=0, sticky='ew')
        self.workspace_tabs.grid(row=1, column=0, sticky='nsew')
        self.layout_page = ttk.Frame(self.workspace_tabs)
        self.layout_page.columnconfigure(0, weight=1)
        self.layout_page.rowconfigure(2, weight=1)
        self.workspace_tabs.add(self.layout_page, text='Layout')
        header = ttk.Frame(self.layout_page, padding=(16, 12))
        self.header_frame = header
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(1, weight=1)
        self.target_toggle = ttk.Button(header, text='Show library / cell', command=self.toggle_target)
        self.target_toggle.grid(row=0, column=2, padx=(0, 8))
        self.export_button = ttk.Button(header, text='Export JSON', command=self.export_design)
        self.export_button.grid(row=0, column=3, padx=(0, 8))
        self.import_button = ttk.Button(header, text='Load JSON', command=self.import_design)
        self.import_button.grid(row=0, column=4, padx=(0, 8))
        ttk.Button(header, text='Environment Check', command=self.check_environment).grid(row=0, column=5)

        target = ttk.Frame(self.layout_page, style='Panel.TFrame', padding=(16, 12))
        self.target_frame = target
        target.grid(row=1, column=0, sticky='ew', padx=16, pady=(0, 12))
        target.grid_remove()
        target.columnconfigure(1, weight=2)
        target.columnconfigure(4, weight=2)
        ttk.Label(target, text='Library').grid(row=0, column=0, padx=(0, 8))
        self.library_combo = ttk.Combobox(target, textvariable=self.vars['library'], state='readonly', width=23)
        self.library_combo.grid(row=0, column=1, sticky='ew')
        self.library_combo.bind('<<ComboboxSelected>>', self._library_changed)
        self.field_widgets['library'] = self.library_combo
        self.context_button = ttk.Button(target, text='↻', width=3, command=self.load_context)
        self.context_button.grid(row=0, column=2, padx=(4, 16))
        ttk.Label(target, text='New cell').grid(row=0, column=3, padx=(0, 8))
        self.cell_entry = ttk.Entry(target, textvariable=self.vars['cell'], width=27)
        self.cell_entry.grid(row=0, column=4, sticky='ew')
        self.field_widgets['cell'] = self.cell_entry
        self.custom_button = ttk.Button(target, text='Load library', command=self.custom_library)
        self.custom_button.grid(row=0, column=5, padx=(8, 0))
        self.library_detail_label = ttk.Label(target, textvariable=self.library_info, style='Muted.TLabel',
                                               wraplength=1160, justify='left')
        self.library_detail_label.grid(row=1, column=0, columnspan=6, sticky='w', pady=(8, 8))
        self.structure_label = ttk.Label(target, text='Lead mode')
        self.structure_label.grid(row=2, column=0, sticky='w')
        structures = ttk.Frame(target, style='Panel.TFrame')
        structures.grid(row=2, column=1, columnspan=4, sticky='w')
        self.structure_frame = structures
        self.structure_buttons = []
        ttk.Label(target, textvariable=self.connection, style='Muted.TLabel').grid(row=2, column=5, sticky='e')
        self.runtime_detail_label = ttk.Label(target, textvariable=self.runtime_info, style='Muted.TLabel',
                                               wraplength=1160, justify='left')
        self.runtime_detail_label.grid(row=3, column=0, columnspan=6, sticky='w', pady=(4, 0))

        body = ttk.Frame(self.layout_page)
        body.grid(row=2, column=0, sticky='nsew', padx=16)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        left = ttk.Frame(body, style='Panel.TFrame', width=352)
        self.left_panel = left
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        self.param_eyebrow = ttk.Label(left, text='Design parameters / μm', style='Eyebrow.TLabel')
        self.param_eyebrow.grid(row=0, column=0, sticky='w', padx=16, pady=(16, 8))
        self.notebook = ttk.Notebook(left)
        self.notebook.grid(row=1, column=0, sticky='nsew')
        self.pages = {}
        for key, title in [('body', 'Body'), ('pgs', 'PGS'), ('ring', 'GND ring')]:
            page = ScrollPage(self.notebook)
            self.notebook.add(page, text=title)
            self.pages[key] = page
        self._body_fields()
        self._pgs_fields()
        self._ring_fields()
        apply_panel = ttk.Frame(left, style='Raised.TFrame', padding=(12, 8))
        self.apply_panel = apply_panel
        apply_panel.grid(row=2, column=0, sticky='ew')
        ttk.Label(apply_panel, textvariable=self.draft_status, background=COLORS['raised'],
                  foreground=COLORS['gold'], font=('liberation sans', 10)).pack(anchor='w', pady=(0, 8))
        self.apply_button = ttk.Button(apply_panel, text='Apply Parameters',
                                       command=self.apply_parameters)
        self.apply_button.pack(fill='x')
        presets = ttk.Frame(left, style='Panel.TFrame', padding=(12, 8, 12, 12))
        self.presets_panel = presets
        presets.grid(row=3, column=0, sticky='ew')
        presets.columnconfigure(0, weight=1)
        ttk.Label(presets, text='Saved presets', style='Muted.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 8))
        self.preset_combo = ttk.Combobox(presets, textvariable=self.vars['preset'], state='readonly')
        self.preset_combo.grid(row=1, column=0, sticky='ew', padx=(0, 8))
        self.preset_load = ttk.Button(presets, text='Load', command=self.load_preset, width=5)
        self.preset_load.grid(row=1, column=1)
        self.preset_entry = ttk.Entry(presets, textvariable=self.vars['preset_name'])
        self.preset_entry.grid(row=2, column=0, sticky='ew', pady=(8, 0), padx=(0, 8))
        self.preset_save = ttk.Button(presets, text='Save', command=self.save_preset, width=5)
        self.preset_save.grid(row=2, column=1, pady=(8, 0))
        self.leg_panel = self._build_leg_panel(left)
        self.leg_panel.grid(row=1, column=0, rowspan=3, sticky='nsew')
        self.leg_panel.grid_remove()

        right = ttk.Frame(body, style='Panel.TFrame')
        right.grid(row=0, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        tools = ttk.Frame(right, style='Panel.TFrame', padding=(12, 8))
        self.preview_tools = tools
        tools.grid(row=0, column=0, sticky='ew')
        tools.columnconfigure(1, weight=1)
        ttk.Label(tools, text='Applied layout preview', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(tools, textvariable=self.preview_state, style='Muted.TLabel').grid(row=0, column=1, sticky='w', padx=12)
        ttk.Button(tools, text='−', width=3, command=lambda: self.zoom_view(.8)).grid(row=0, column=2, padx=4)
        self.zoom_label = ttk.Label(tools, text='100%', style='Muted.TLabel', width=5, anchor='center')
        self.zoom_label.grid(row=0, column=3)
        ttk.Button(tools, text='+', width=3, command=lambda: self.zoom_view(1.25)).grid(row=0, column=4, padx=4)
        ttk.Button(tools, text='Fit', command=self.fit_view).grid(row=0, column=5, padx=(8, 0))
        self.leg_button = ttk.Button(tools, text='Leg Pins', style='Mode.TButton', command=self.toggle_leg_mode)
        self.leg_button.grid(row=0, column=6, padx=(7, 0))
        self.view_mode = tk.StringVar(master=self.root, value='2D')
        self.preview3d = None
        self._pin3d_after = None
        if embed is not None:
            self.view_combo = ttk.Combobox(tools, textvariable=self.view_mode, values=VIEW_MODES, state='readonly', width=10)
            self.view_combo.grid(row=0, column=7, padx=(7, 0))
            self.view_combo.bind('<<ComboboxSelected>>', lambda _e: self.switch_view())
        elif os.environ.get('FLUX_VL3D_ROOT', '').strip():
            # 3D was requested but cannot load: say why instead of silently hiding it.
            note = '3D unavailable: ' + EMBED_NOTE
            self.view_note = ttk.Label(tools, text=note if len(note) <= 70 else note[:67] + '...', style='Muted.TLabel')
            self.view_note.grid(row=0, column=7, padx=(7, 0))
        leg_row = ttk.Frame(tools, style='Panel.TFrame')
        leg_row.grid(row=1, column=0, columnspan=8, sticky='ew', pady=(5, 0))
        self.leg_status_label = ttk.Label(leg_row, textvariable=self.leg_status, style='Muted.TLabel')
        self.leg_status_label.pack(side='left')
        self.leg_restore_button = ttk.Button(leg_row, text='Restore default pins', command=self.restore_applied_leg_pins)
        self.leg_restore_button.pack(side='right')
        self.leg_restore_button.pack_forget()
        self.legend = ttk.Frame(right, style='Panel.TFrame', padding=(12, 0, 12, 8))
        self.legend.grid(row=1, column=0, sticky='ew')
        area = ttk.Frame(right, style='Panel.TFrame')
        area.grid(row=2, column=0, sticky='nsew', padx=12)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        area.grid_propagate(False)  # the 2D/3D swap must not resize the preview area
        self.preview_area = area
        self.canvas = tk.Canvas(area, bg=fw.tk_plot_colors()['bg'], highlightbackground=COLORS['border'], highlightthickness=1, bd=0, cursor='fleur')
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.canvas.bind('<Configure>', self._canvas_resize)
        self.canvas.bind('<ButtonPress-1>', self._pan_press)
        self.canvas.bind('<B1-Motion>', self._pan_move)
        self.canvas.bind('<ButtonRelease-1>', self._pan_release)
        self.canvas.bind('<Double-Button-1>', self._canvas_double)
        self.canvas.bind('<Escape>', lambda _e: self.leg_drag_cancel())
        self.canvas.bind('<Delete>', lambda _e: self.delete_leg_pin() if self.leg_mode else None)
        self.canvas.bind('<BackSpace>', lambda _e: self.delete_leg_pin() if self.leg_mode else None)
        self.metrics_label = ttk.Label(right, textvariable=self.metrics, style='Muted.TLabel')
        self.metrics_label.grid(row=3, column=0, sticky='w', padx=12, pady=(8, 8))
        diagnostics = ttk.Frame(right, style='Panel.TFrame', padding=(12, 0, 12, 12))
        self.diagnostics_frame = diagnostics
        diagnostics.grid(row=4, column=0, sticky='ew')
        diagnostics.columnconfigure(0, weight=1)
        diagnostic_header = ttk.Frame(diagnostics, style='Panel.TFrame')
        diagnostic_header.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        diagnostic_header.columnconfigure(0, weight=1)
        self.validation_label = ttk.Label(diagnostic_header, text='Apply parameters to validate geometry and layers.', style='Muted.TLabel')
        self.validation_label.grid(row=0, column=0, sticky='w')
        self.refresh_button = ttk.Button(diagnostic_header, text='Recheck applied', command=self.recheck_applied)
        self.refresh_button.grid(row=0, column=1)
        self.error_tree = ttk.Treeview(diagnostics, columns=('part', 'message'), show='headings', height=3)
        self.error_tree.heading('part', text='Part')
        self.error_tree.heading('message', text='Validation results · select to locate input')
        self.error_tree.column('part', width=95, minwidth=85, stretch=False)
        self.error_tree.column('message', width=450, minwidth=160)
        self.error_tree.grid(row=1, column=0, sticky='ew')
        scrollbar = ttk.Scrollbar(diagnostics, orient='vertical', command=self.error_tree.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        self.error_tree.configure(yscrollcommand=scrollbar.set)
        self.error_tree.tag_configure('error', foreground=COLORS['red'])
        self.error_tree.tag_configure('warning', foreground=COLORS['gold'])
        self.error_tree.bind('<<TreeviewSelect>>', self._focus_error)
        self.root.bind_all('<MouseWheel>', self._wheel)
        self.root.bind_all('<Button-4>', self._wheel)
        self.root.bind_all('<Button-5>', self._wheel)

        bottom = ttk.Frame(self.layout_page, padding=(16, 12, 16, 12))
        self.bottom_frame = bottom
        bottom.grid(row=3, column=0, sticky='ew')
        bottom.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(bottom, textvariable=self.status, style='Bg.TLabel', foreground=COLORS['muted'], wraplength=850)
        self.status_chip = fw.StatusChip(bottom, bg=fw.BG)
        self.status_chip.grid(row=0, column=0, sticky='w', padx=(0, 16))
        bind_status(self.status, self.status_chip, lambda: self.generating or self.context_busy or self.preview_busy)
        self.generate_button = ttk.Button(bottom, text='Generate Layout', style='Primary.TButton', command=self.generate)
        self.generate_button.grid(row=0, column=1, rowspan=2, sticky='e')
        self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=180)
        self.progress.grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.progress.grid_remove()
        self.update_controls()
        self.redraw()
        from emx_page import EMXPage
        self.emx_settings_page = ttk.Frame(self.workspace_tabs, style='Panel.TFrame')
        self.workspace_tabs.add(self.emx_settings_page, text='EMX Settings')
        self.emx_page = EMXPage(self.workspace_tabs, self.emx_source, settings_parent=self.emx_settings_page)
        self.workspace_tabs.add(self.emx_page, text='Sweep & Optimization')
        self.workspace_tabs.bind('<<NotebookTabChanged>>', self._workspace_selected)

    def check_environment(self):
        window = tk.Toplevel(self.root)
        window.title('Flux Studio — Environment check')
        fw.apply(window, product='flux')
        fw.header(window, 'Flux Studio', 'Environment check').pack(fill='x')
        window.geometry('780x390')
        window.transient(self.root)
        panel = ttk.Frame(window, padding=12)
        panel.pack(fill='both', expand=True)
        report = tk.Text(panel, wrap='word', padx=8, pady=8, bg=COLORS['panel'],
                         fg=COLORS['text'], relief='flat')
        scroll = ttk.Scrollbar(panel, command=report.yview)
        scroll.pack(side='right', fill='y')
        report.configure(yscrollcommand=scroll.set)
        report.pack(fill='both', expand=True)
        report.insert('1.0', 'Checking local capabilities…')
        report.configure(state='disabled')

        tk_version = str(window.tk.call('package', 'provide', 'Tk'))
        try:
            photo = tk.PhotoImage(master=window, data=b'P6\n1 1\n255\n\x00\x00\x00', format='ppm')
            tk_photo = (photo.width(), photo.height()) == (1, 1)
        except tk.TclError:
            tk_photo = False
        library = self.library or {}
        if library.get('available'):
            warnings = self._library_warnings()
            layout_status = '{} [Layout] Current PDK: {}'.format(
                'WARN' if warnings else 'PASS', self.library_info.get())
            if warnings:
                layout_status += '\n' + '\n'.join('  ' + entry['message'] for entry in warnings)
        else:
            layout_status = 'NOT TESTED [Layout] Current PDK: Select a readable design library.'
        result = queue.Queue()

        def work():
            try:
                from flux_doctor import check_environment, format_report
                result.put(format_report(check_environment(tk_patchlevel=tk_version, tk_photo=tk_photo)))
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                result.put('Environment check failed. {}'.format(exc))

        def poll():
            if not window.winfo_exists():
                return
            try:
                message = result.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            report.configure(state='normal')
            report.delete('1.0', 'end')
            report.insert('1.0', message + '\n\n' + layout_status)
            report.configure(state='disabled')

        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, poll)

    def emx_source(self):
        """Only an applied, validated layout can seed a frozen EMX campaign."""
        if self.context_busy or self.preview_busy or self.generating:
            raise ValueError('Wait for layout validation or generation to finish.')
        if self.dirty or not self.applied_design or not self.preview_ok or self.preview_revision != self.revision:
            raise ValueError('Apply Parameters on the Layout page and wait for successful validation first.')
        import backend
        source = backend.capture_generation_context(copy.deepcopy(self.applied_design),
                                                     expected_library=self.preview_library or self.library)
        from emx_adapter import port_contract
        source['port_contract'] = port_contract(self.geometry)
        source['layout_revision'] = self.preview_revision
        return source

    def toggle_target(self):
        self.set_target_visible(not self.target_frame.winfo_manager())

    def set_target_visible(self, visible):
        if visible:
            self.target_frame.grid()
        else:
            self.target_frame.grid_remove()
        self.target_toggle.configure(text='Hide library / cell' if visible else 'Show library / cell')

    def _workspace_selected(self, _event=None):
        if (hasattr(self, 'emx_page') and self.workspace_tabs.select() in (str(self.emx_page), str(self.emx_settings_page))
                and not self.emx_page.is_running):
            self.emx_page.refresh_source()

    def _responsive(self, event):
        if event.widget != self.root:
            return
        compact = event.width <= 1100 or event.height <= 790
        if compact == self.compact:
            return
        self.compact = compact
        self.header_frame.configure(padding=(16, 8) if compact else (20, 13))
        self.target_frame.configure(padding=(12, 8) if compact else (18, 12))
        if self.target_frame.winfo_manager():
            self.target_frame.grid_configure(pady=(0, 8) if compact else (0, 12))
        self.library_detail_label.grid_configure(pady=(4, 4) if compact else (8, 9))
        self.bottom_frame.configure(padding=(16, 4, 16, 8) if compact else (20, 13, 20, 14))
        self.preview_tools.configure(padding=(12, 0) if compact else (13, 9))
        self.metrics_label.grid_configure(pady=(4, 4) if compact else (7, 8))
        self.diagnostics_frame.configure(padding=(12, 0, 12, 8) if compact else (12, 0, 12, 12))
        self.error_tree.configure(height=2 if compact else 3)

    def _note(self, tab, text):
        content = self.pages[tab].content
        ttk.Label(content, text=text, style='Note.TLabel', wraplength=294, justify='left').pack(anchor='w', fill='x', pady=(0, 12))

    def _fields(self, tab, specifications):
        frame = ttk.Frame(self.pages[tab].content, style='Panel.TFrame')
        frame.pack(fill='x')
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        for index, (key, label) in enumerate(specifications):
            box = ttk.Frame(frame, style='Panel.TFrame')
            box.grid(row=index // 2, column=index % 2, sticky='ew', padx=(0, 8) if index % 2 == 0 else (0, 0), pady=(0, 12))
            ttk.Label(box, text=label, style='Muted.TLabel').pack(anchor='w', pady=(0, 4))
            entry = ttk.Entry(box, textvariable=self.vars[key], width=12)
            entry.pack(fill='x')
            self.field_widgets[key] = entry
            self.field_tabs[key] = tab

    def _metal(self, tab, role, title):
        frame = ttk.Frame(self.pages[tab].content, style='Panel.TFrame')
        frame.pack(fill='x', pady=(4, 12))
        ttk.Label(frame, text=title, style='Muted.TLabel').pack(anchor='w', pady=(0, 4))
        combo = ttk.Combobox(frame, textvariable=self.vars[role], state='readonly')
        combo.pack(fill='x')
        self.field_widgets[role] = combo
        self.field_tabs[role] = tab

    def _body_fields(self):
        content = self.pages['body'].content
        ttk.Label(content, text='Device model', style='Section.TLabel').pack(anchor='w', pady=(0, 8))
        self.model_combo = ttk.Combobox(content, textvariable=self.model_selection, state='readonly',
                                       values=[model['title'] for model in self.models.values()])
        self.model_combo.pack(fill='x', pady=(0, 12))
        self.model_combo.bind('<<ComboboxSelected>>', self._model_selected)
        self.field_widgets['model'] = self.model_combo
        self.field_tabs['model'] = 'body'
        self.body_content = ttk.Frame(content, style='Panel.TFrame')
        self.body_content.pack(fill='x')
        self._render_body_fields()

    def schema_for(self, model=None):
        definition = self.models.get(model or self.current_model, {})
        raw = definition.get('parameter_schema')
        if isinstance(raw, dict):
            schema = {name: dict(entry, name=name) for name, entry in raw.items()}
        elif isinstance(raw, list):
            schema = {entry['name']: dict(entry) for entry in raw if isinstance(entry, dict) and isinstance(entry.get('name'), str)}
        else:
            schema = {}
        keys = definition.get('parameters', FALLBACK_MODELS[DEFAULT_MODEL]['parameters']) + SHARED_KEYS
        for key in keys:
            if key in schema:
                continue
            if raw and key not in SHARED_KEYS:
                continue
            part = 'pgs' if key.startswith('pgs') else 'ring' if key.startswith('ring') else 'ground_route' if key.startswith('ground_route') else 'body'
            entry = dict(name=key, label=LABELS.get(key, key.replace('_', ' ').title()), part=part,
                         type='float', min=0.000001, max=5000, default=DEFAULTS.get(key))
            if key in ('pgs_shape', 'ring_shape'):
                entry.update(type='enum', options=['square', 'octagon', 'circle'] if key == 'pgs_shape' else ['square', 'octagon'])
            if key in ('pgs_connect_ring', 'pgs_enabled', 'ring_enabled'):
                entry.update(type='bool')
            if key == 'pgs_spine_width':
                entry.update(optional=True)
            schema[key] = entry
        for name, entry in schema.items():
            kind = entry.get('type') or ('int' if entry.get('integer') else 'enum' if entry.get('options') else 'bool' if isinstance(entry.get('default'), bool) else 'float')
            entry['type'] = {'boolean': 'bool', 'integer': 'int', 'number': 'float'}.get(kind, kind)
            entry['label'] = LABELS.get(name, entry.get('label', name.replace('_', ' ').title()))
        return schema

    def model_roles(self, model=None):
        roles = self.models.get(model or self.current_model, {}).get('layer_roles', ['body', 'crossing'])
        return list(dict.fromkeys(list(roles) + SHARED_ROLES))

    def active_model_roles(self, model=None, parameters=None):
        """Keep optional draft selections while requiring only generated metals."""
        if parameters is None:
            parameters = {}
            for name, default in (('center_tap', False), ('pgs_enabled', True),
                                  ('ring_enabled', True), ('pgs_connect_ring', False)):
                value = self.vars.get(name)
                parameters[name] = boolean_value(value.get()) if value is not None else default
        return [role for role in self.model_roles(model)
                if (role != 'tap' or parameters.get('center_tap') is True)
                and shielding_role_active(role, parameters)]

    def ensure_variable(self, name, default=None, kind='float'):
        if name not in self.vars:
            self.vars[name] = (tk.BooleanVar(master=self.root, value=boolean_value(default) is True)
                               if kind == 'bool' else
                               tk.StringVar(master=self.root, value='' if default is None else str(default)))
            self.vars[name].trace_add('write', self._changed)

    def _render_body_fields(self):
        for key in list(self.field_tabs):
            if self.field_tabs[key] == 'body' and key != 'model':
                self.field_tabs.pop(key, None)
                self.field_widgets.pop(key, None)
        for child in self.body_content.winfo_children():
            child.destroy()
        definition = self.models[self.current_model]
        ttk.Label(self.body_content, text=definition.get('description', ''), style='Note.TLabel',
                  wraplength=294, justify='left').pack(anchor='w', fill='x', pady=(0, 12))
        grid = ttk.Frame(self.body_content, style='Panel.TFrame')
        grid.pack(fill='x')
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        defaults = definition.get('defaults', {})
        body_entries = [(name, entry) for name, entry in self.schema_for().items() if name not in SHARED_KEYS]
        for index, (name, entry) in enumerate(body_entries):
            kind = entry.get('type', 'float')
            self.ensure_variable(name, defaults.get(name, entry.get('default')), kind)
            box = ttk.Frame(grid, style='Panel.TFrame')
            box.grid(row=index // 2, column=index % 2, sticky='ew', padx=(0, 8) if index % 2 == 0 else 0, pady=(0, 12))
            caption = ttk.Frame(box, style='Panel.TFrame', height=36)
            caption.pack(fill='x', pady=(0, 4))
            caption.pack_propagate(False)
            ttk.Label(caption, text=entry.get('label', LABELS.get(name, name)), style='Muted.TLabel',
                      wraplength=142, anchor='sw').pack(fill='both', expand=True)
            if kind == 'enum':
                widget = ttk.Combobox(box, textvariable=self.vars[name], state='readonly', values=entry.get('options', []), width=12)
            elif kind == 'bool':
                widget = ttk.Checkbutton(box, variable=self.vars[name], text='Include TAP' if name == 'center_tap' else 'Enabled')
            else:
                widget = ttk.Entry(box, textvariable=self.vars[name], width=12)
            widget.pack(fill='x')
            self.field_widgets[name] = widget
            self.field_tabs[name] = 'body'
        role_labels = {'body': 'Winding metal', 'secondary': 'Secondary winding metal',
                       'crossing': 'Underpass metal', 'tap': 'TAP routing metal'}
        role_labels.update({key: str(value) for key, value in (definition.get('role_labels') or {}).items()})
        for role in self.model_roles():
            if role in SHARED_ROLES:
                continue
            self.ensure_variable(role, '', 'enum')
            frame = ttk.Frame(self.body_content, style='Panel.TFrame')
            frame.pack(fill='x', pady=(4, 12))
            ttk.Label(frame, text=role_labels.get(role, role.replace('_', ' ').title()), style='Muted.TLabel').pack(anchor='w', pady=(0, 4))
            widget = ttk.Combobox(frame, textvariable=self.vars[role], state='readonly')
            widget.pack(fill='x')
            self.field_widgets[role] = widget
            self.field_tabs[role] = 'body'
        if 'center_tap' in self.schema_for():
            ttk.Label(self.body_content, text='TAP is a separate winding terminal, not ground. Its routing metal is used only when TAP is enabled.',
                      style='Note.TLabel', wraplength=294, justify='left').pack(anchor='w', fill='x', pady=(0, 12))
        if 'rule_policy' in self.schema_for():
            ttk.Label(self.body_content, text='Warn retains dimensions with PDK rule warnings. Strict requires the known rules. Shorts, broken windings and invalid native vias always block generation.',
                      style='Note.TLabel', wraplength=294, justify='left').pack(anchor='w', fill='x', pady=(0, 12))
        self._render_structure_buttons(definition)
        self.model_selection.set(definition.get('title', self.current_model))
        self.pages['body'].canvas.yview_moveto(0)

    def model_structures(self, model=None):
        """Routing variants declared by a (pack) model, as [(id, title)]."""
        entries = self.models.get(model or self.current_model, {}).get('structures') or []
        result = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get('id'), str):
                result.append((entry['id'], str(entry.get('title', entry['id']))))
            elif isinstance(entry, str):
                result.append((entry, entry))
        return result

    def _render_structure_buttons(self, definition=None):
        for button in self.structure_buttons:
            button.destroy()
        self.structure_buttons = []
        variants = self.model_structures()
        for structure, title in variants:
            button = ttk.Radiobutton(self.structure_frame, text=title, value=structure, variable=self.vars['structure'])
            button.pack(side='left', padx=(0, 12))
            self.structure_buttons.append(button)
        self.structure_label.grid() if variants else self.structure_label.grid_remove()

    def _structure_value(self, model=None, saved=None):
        variants = [key for key, _title in self.model_structures(model)]
        if not variants:
            return 'layout'
        return saved if saved in variants else variants[0]

    def _save_body_draft(self):
        self.body_drafts[self.current_model] = dict(
            parameters={key: self.vars[key].get() for key in self.schema_for() if key not in SHARED_KEYS and key in self.vars},
            layers={role: self.vars[role].get() for role in self.model_roles() if role not in SHARED_ROLES},
            library=self.vars['library'].get(), provenance=library_provenance(self.library),
            structure=self.vars['structure'].get(), leg_pins=copy.deepcopy(getattr(self, 'leg_pins_draft', None)))

    def _model_selected(self, _event=None):
        selected = next((key for key, definition in self.models.items() if definition.get('title') == self.model_selection.get()), None)
        if selected:
            self.switch_model(selected)

    def switch_model(self, model, mark_dirty=True):
        if model not in self.models or model == self.current_model:
            return
        self.exit_leg_mode()
        self._save_body_draft()
        was_suspended = self.suspended
        self.suspended = True
        try:
            self.current_model = model
            self.vars['model'].set(model)
            self._render_body_fields()
            saved = self.body_drafts.get(model)
            # Leg pins belong to one model's draft; never carry them across models.
            self.leg_pins_draft = copy.deepcopy(saved.get('leg_pins')) if saved else None
            defaults = self.models[model].get('defaults', {})
            for name, entry in self.schema_for().items():
                if name in SHARED_KEYS:
                    continue
                value = saved['parameters'].get(name) if saved else defaults.get(name, entry.get('default'))
                self.vars[name].set('' if value is None else value)
            self.vars['structure'].set(self._structure_value(model, saved.get('structure') if saved else None))
            same_library = saved and saved.get('provenance') == library_provenance(self.library)
            for role in self.model_roles():
                if role in SHARED_ROLES:
                    continue
                desired = saved.get('layers', {}).get(role) if same_library else None
                self.vars[role].set(self._preferred_layer(role, desired, prefer_defaults=True))
            self._configure_layer_choices()
        finally:
            self.suspended = was_suspended
        if mark_dirty:
            self.invalidate()

    def _pgs_fields(self):
        self.pgs_checkbox = ttk.Checkbutton(self.pages['pgs'].content, text='Include PGS', variable=self.vars['pgs_enabled'])
        self.pgs_checkbox.pack(anchor='w', pady=(0, 8))
        self._note('pgs', 'A centered 2 × 2 μm hub with four diagonal spines and evenly spaced fingers.')
        self._fields('pgs', [('pgs_extent', 'Radius / half-size'), ('pgs_width', 'Finger width'), ('pgs_spacing', 'Finger gap'), ('pgs_spine_width', 'Spine width')])
        self.spine_checkbox = ttk.Checkbutton(self.pages['pgs'].content, text='Spine width follows fingers', variable=self.spine_follow, command=self._spine_changed)
        self.spine_checkbox.pack(anchor='w', pady=(0, 12))
        frame = ttk.Frame(self.pages['pgs'].content, style='Panel.TFrame')
        frame.pack(fill='x', pady=(4, 12))
        ttk.Label(frame, text='PGS outline', style='Muted.TLabel').pack(anchor='w', pady=(0, 4))
        self.pgs_shape_buttons = []
        for shape, name in [('octagon', 'Octagon'), ('square', 'Square'), ('circle', 'Circle')]:
            button = ttk.Radiobutton(frame, text=name, variable=self.vars['pgs_shape'], value=shape)
            button.pack(side='left', padx=(0, 16))
            self.pgs_shape_buttons.append(button)
        self._metal('pgs', 'pgs', 'PGS metal')
        self._note('pgs', 'Clear the follow option for an independent spine width. A blank value also follows finger width. PGS must be below winding and crossover metals. Finger endpoints use a 0.1 μm grid.')

    def _spine_changed(self):
        if self.spine_follow.get():
            self.vars['pgs_spine_width'].set('')
        elif not self.vars['pgs_spine_width'].get():
            self.vars['pgs_spine_width'].set(self.vars['pgs_width'].get())
        self.invalidate()
        self.update_controls()

    def _ring_fields(self):
        self.ring_checkbox = ttk.Checkbutton(self.pages['ring'].content, text='Include GND ring', variable=self.vars['ring_enabled'])
        self.ring_checkbox.pack(anchor='w', pady=(0, 8))
        self._note('ring', 'Centered ground ring. Geometry is validated for collisions and layer constraints.')
        self._fields('ring', [('ring_extent', 'Ring inner half-size'), ('ring_width', 'Ring width')])
        outline = ttk.Frame(self.pages['ring'].content, style='Panel.TFrame')
        outline.pack(fill='x', pady=(0, 8))
        ttk.Label(outline, text='Ring outline', style='Muted.TLabel').pack(anchor='w', pady=(0, 4))
        self.ring_shape_buttons = []
        for value, title in [('square', 'Square'), ('octagon', 'Octagon')]:
            button = ttk.Radiobutton(outline, text=title, variable=self.vars['ring_shape'], value=value)
            button.pack(side='left', padx=(0, 16))
            self.ring_shape_buttons.append(button)
        self._metal('ring', 'ring', 'GND ring metal')
        self.ground_checkbox = ttk.Checkbutton(self.pages['ring'].content, text='Connect PGS to GND ring', variable=self.vars['pgs_connect_ring'])
        self.ground_checkbox.pack(anchor='w', pady=(0, 12))
        self._fields('ring', [('ground_route_width', 'Ground route width')])
        self._metal('ring', 'ground_route', 'Ground routing metal')
        self._note('ring', 'PGS and ring have separate ground ports. An optional routed connection can join them using native vias.')

    def _worker(self):
        while True:
            task = self.tasks.get()
            if task is None:
                return
            task_id, kind, revision, args = task
            if kind == 'preview' and revision != self.revision:
                self.results.put((task_id, kind, revision, True, {'skipped': True}))
                continue
            try:
                import backend
                calls = {'context': backend.load_context, 'library': backend.register_library,
                         'preview': preview_with_legs, 'generate': backend.generate,
                         'save_preset': backend.save_preset, 'load_preset': backend.load_preset,
                         'presets': backend.preset_list}
                payload = calls[kind](*args)
                self.results.put((task_id, kind, revision, True, payload))
            except Exception as exc:
                self.results.put((task_id, kind, revision, False, str(exc)))

    def submit(self, kind, *args):
        self.task_id += 1
        self.tasks.put((self.task_id, kind, self.revision, args))
        return self.task_id

    def _poll(self):
        if self.closed:
            return
        emx_page = getattr(self, 'emx_page', None)
        campaign_running = bool(emx_page and emx_page.is_running)
        if campaign_running != getattr(self, '_last_campaign_running', False):
            self._last_campaign_running = campaign_running
            self.update_controls()
        try:
            for _ in range(20):
                try:
                    task_id, kind, revision, ok, payload = self.results.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._result(task_id, kind, revision, ok, payload)
                except Exception as exc:
                    self._callback_error(type(exc), exc, exc.__traceback__)
        finally:
            if not self.closed:
                self._poll_after = self.root.after(60, self._poll)

    def _install_models(self, context):
        definitions = context.get('models', [])
        if definitions:
            self.models = {entry['id']: entry for entry in definitions[:20]
                           if isinstance(entry, dict) and isinstance(entry.get('id'), str)}
        if self.current_model not in self.models:
            self.current_model = next(iter(self.models))
            self.vars['model'].set(self.current_model)
        was_suspended = self.suspended
        self.suspended = True
        try:
            for name, entry in self.schema_for().items():
                self.ensure_variable(name, entry.get('default'), entry.get('type', 'float'))
            self.model_combo.configure(values=[definition.get('title', model) for model, definition in self.models.items()])
            self._render_body_fields()
        finally:
            self.suspended = was_suspended

    def _result(self, task_id, kind, revision, ok, payload):
        if kind == 'preview' and (revision != self.revision or task_id != self.preview_task):
            if task_id == self.preview_task:
                self.preview_busy = False
                self._flush_leg_preview()
                self.update_controls()
            return
        if kind in ('context', 'library'):
            self.context_busy = False
            if not ok:
                self.connection.set('Library identification incomplete')
                self.status.set('Library identification failed: ' + str(payload))
                self.runtime_info.set('Runtime: automatic identification failed. Check flux_config.sh if a path override is needed.')
                self.render_messages([dict(part='library', message=str(payload), fields=[])])
            else:
                if kind == 'context':
                    if not isinstance(payload, dict) or not isinstance(payload.get('libraries'), list):
                        raise ValueError('Incompatible library context response.')
                    previous = self.vars['library'].get()
                    self.context = payload
                    self._show_runtime(payload.get('runtime'))
                    self._install_models(payload)
                    candidates = payload['libraries']
                    self.library_combo.configure(values=[lib['name'] for lib in candidates])
                    preferred = previous or payload.get('selected_library') or (self.source_context or {}).get('library')
                    selected = next((lib for lib in candidates if lib['name'] == preferred), None)
                    selected = selected or next((lib for lib in candidates if lib.get('available')), None)
                    if selected:
                        self.configure_library(selected['name'], apply_defaults=not previous)
                    if not previous and self.source_context:
                        source_cell = self.source_context.get('cell', '')
                        if source_cell:
                            self.vars['cell'].set(source_cell[:70] + '_generated')
                    self.render_presets(payload.get('presets', []))
                else:
                    entry = payload.get('library', payload) if isinstance(payload, dict) else payload
                    if not isinstance(entry, dict) or not entry.get('name'):
                        raise ValueError('Incompatible library load response.')
                    if not self.context:
                        self.context = {'libraries': [], 'defaults': {}}
                    catalog = payload.get('libraries') if isinstance(payload, dict) else None
                    if isinstance(catalog, list):
                        self.context['libraries'] = catalog
                    else:
                        self.context['libraries'] = [lib for lib in self.context['libraries'] if lib['name'] != entry['name']] + [entry]
                    if entry.get('runtime'):
                        self.context['runtime'] = entry['runtime']
                        self._show_runtime(entry['runtime'])
                    self.library_combo.configure(values=[lib['name'] for lib in self.context['libraries']])
                    self.configure_library(entry['name'], desired_layers=entry.get('model_default_layers', {}).get(self.current_model) or entry.get('default_layers'))
                available = self.library and self.library.get('available')
                self.connection.set('PDK connected' if available else 'No usable design PDK')
                if self.applied_design is None:
                    self.preview_state.set('Awaiting Apply Parameters')
                self.invalidate()
                if available:
                    self.status.set('Edit the draft, then select Apply Parameters. Applied geometry changes only on Apply.')
                else:
                    detail = (self.library or {}).get('error') or 'Load a mapped design library with an attached PDK.'
                    self.status.set(detail)
                    self.render_messages([dict(part='library', message=detail, fields=['library'])])
        elif kind == 'preview':
            self.preview_busy = False
            if not ok:
                self.preview_ok = False
                self.preview_errors = [dict(part='system', message=str(payload), fields=[])]
                self.preview_state.set('Applied validation failed')
                self.render_messages(self.preview_errors)
                self.status.set('Preview failed: ' + str(payload))
            elif not payload.get('skipped'):
                errors = normalize_messages(payload.get('errors'))
                warnings = normalize_messages(payload.get('warnings'))
                geometry = payload.get('geometry')
                self.preview_ok = payload.get('ok') is True and not errors
                self.preview_revision = revision
                if geometry and geometry.get('polygons') and self._bounds_valid(geometry.get('bounds')):
                    old_bounds = self.geometry.get('bounds') if self.geometry else None
                    self.geometry = geometry
                    self.geometry_design = copy.deepcopy(self.applied_design)
                    self.preview_library = next((lib for lib in self.context['libraries'] if lib['name'] == self.applied_design['library']), None)
                    self.push_3d()
                    self.refresh_legend()
                    if old_bounds != geometry['bounds'] or self.view is None:
                        self.fit_view()
                    self.update_metrics()
                elif self.preview_ok:
                    self.preview_ok = False
                    errors.append(dict(part='system', message='The generator returned no displayable geometry.', fields=[]))
                self.preview_errors = errors
                self._install_leg_info(payload)
                self.preview_state.set('Validated applied design' if self.preview_ok else 'Applied validation failed')
                self.render_messages(errors, warnings)
                leg_errors = [error for error in errors if error.get('code', '').startswith('leg_pin_')]
                if self.dirty:
                    self.status.set('Applied preview updated. Your draft has changed; apply it before generating.')
                elif getattr(self, 'leg_mode', False) and leg_errors:
                    self.status.set('Leg pin layout rejected: {} Fix it in the list or restore defaults.'.format(leg_errors[0]['message']))
                elif any(error.get('code') == 'leg_pin_unknown_leg' for error in leg_errors):
                    self.status.set('Some leg pins refer to legs this layout no longer has. Open Leg Pins to fix them, '
                                    'or select Restore default pins.')
                elif leg_errors:
                    self.status.set('Leg pin layout rejected: {} Open Leg Pins to fix it, or select Restore default pins.'.format(leg_errors[0]['message']))
                elif self.preview_ok:
                    self.status.set('Applied design validated. Generate Layout saves geometry JSON.' if (self.library or {}).get('offline') else 'Applied design validated. Generate creates a new layout cell from this snapshot.')
                else:
                    self.status.set('Adjust the reported draft inputs and apply again. Select a message to locate its input.')
            self.schedule_redraw()
        elif kind == 'generate':
            self.generating = False
            if ok and isinstance(payload, dict) and payload.get('ok') is True:
                self.preview_ok = True
                self.preview_revision = revision
                library = payload.get('library', self.applied_design['library'])
                cell = payload.get('cell', self.applied_design['cell'])
                self.status.set('Generated {}/{} / layout. Enter a new cell name and apply it to generate again.'.format(library, cell))
                self.generated_target = (library, cell)
                self.preview_state.set('Generated · Validated')
                self.last_job = str(payload.get('report') or '')
                self.render_messages([], normalize_messages(payload.get('warnings')))
                self.schedule_redraw()
            else:
                errors = normalize_messages(payload.get('errors')) if ok and isinstance(payload, dict) else []
                errors = errors or [dict(part='system', message=str(payload), fields=[])]
                self.preview_ok = False
                self.status.set('Generation failed. Review messages and inspect the target cell in Virtuoso.')
                self.render_messages(errors)
            if self.close_pending:
                self.close()
                return
        elif kind in ('save_preset', 'load_preset', 'presets'):
            self.preset_busy = False
            if not ok:
                self.status.set('Preset operation failed: ' + str(payload))
            elif kind == 'load_preset':
                try:
                    self.apply_design(payload)
                    self.status.set('Preset loaded into the draft. Select Apply Parameters to validate it.')
                except ValueError as exc:
                    self.status.set('Incompatible preset: ' + str(exc))
            elif kind == 'save_preset':
                self.status.set('Saved preset: ' + self.vars['preset_name'].get().strip())
                if isinstance(payload, dict) and isinstance(payload.get('presets'), list):
                    self.render_presets(payload['presets'])
                else:
                    self.submit('presets')
            else:
                self.render_presets(payload.get('presets', []) if isinstance(payload, dict) else payload)
        if self.close_pending and kind in ('context', 'library'):
            self.close()
            return
        self.update_controls()

    def load_context(self):
        if self.context_busy or self.generating or self.preset_busy:
            return
        self.context_busy = True
        self.connection.set('Identifying libraries and PDKs...')
        self.status.set('Reading actual workspace, metals and native via rules. Typically about 20 seconds.')
        if self.source_context:
            self.submit('context', self.source_context)
        else:
            self.submit('context')
        self.update_controls()

    def _show_runtime(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        self.runtime_info.set('Runtime: {}  ·  {}  ·  user {}'.format(
            snapshot.get('source', 'automatically identified'), snapshot.get('virtuoso', 'Cadence'),
            snapshot.get('user', 'current user')))

    def _configure_layer_choices(self):
        layers = sorted((self.library or {}).get('layers', []), key=lambda layer: layer.get('order', 0))
        names = list(dict.fromkeys(layer['name'] for layer in layers if isinstance(layer.get('name'), str) and layer['name']))
        for role in self.model_roles():
            if role in self.field_widgets:
                self.field_widgets[role].configure(values=names)
            if role in self.vars and self.vars[role].get() not in names:
                self.vars[role].set(self._preferred_layer(role))

    def _preferred_layer(self, role, desired=None, prefer_defaults=False):
        layers = sorted((self.library or {}).get('layers', []), key=lambda layer: layer.get('order', 0))
        names = list(dict.fromkeys(layer['name'] for layer in layers if isinstance(layer.get('name'), str) and layer['name']))
        if desired in names:
            return desired
        choices = ((self.library or {}).get('model_default_layers', {}).get(self.current_model)
                   or (self.library or {}).get('default_layers', {}))
        current = self.vars[role].get() if role in self.vars else None
        candidates = [choices.get(role), current] if prefer_defaults else [current, choices.get(role)]
        for candidate in candidates:
            if candidate in names:
                return candidate
        if not names:
            return ''
        if role == 'pgs':
            return names[0]
        if role == 'ground_route':
            return names[min(1, len(names) - 1)]
        if role == 'tap':
            used = {self.vars[key].get() for key in ('body', 'crossing', 'pgs') if key in self.vars}
            available = [name for name in names if name not in used]
            return available[-1] if available else ''
        offset = {'body': 1, 'secondary': 2, 'crossing': 2}
        declared = self.models.get(self.current_model, {}).get('default_layers') or {}
        offset.update({key: value for key, value in declared.items()
                       if isinstance(value, int) and not isinstance(value, bool) and value >= 1})
        return names[max(0, len(names) - offset.get(role, 1))]

    def _library_warnings(self):
        return normalize_messages((self.library or {}).get('warnings'), part='technology')

    def _show_library_warnings(self):
        warnings = self._library_warnings()
        self.render_messages([], warnings)
        provenance = library_provenance(self.library)
        if not warnings or not (self.library or {}).get('available') or provenance in self.warned_library_provenances:
            return
        self.warned_library_provenances.add(provenance)
        details = '\n\n'.join(message['message'] for message in warnings[:8])
        if len(warnings) > 8:
            details += '\n\nAdditional warnings are listed in the validation results.'
        messagebox.showwarning('PDK rule warnings',
            details + '\n\nApply and Generate remain available. Review these warnings and check the generated layout with the target PDK rules.',
            parent=self.root)

    def configure_library(self, name, apply_defaults=False, desired_layers=None):
        if not self.context:
            return
        self.library = next((lib for lib in self.context['libraries'] if lib['name'] == name), None)
        if not self.library:
            return
        was_suspended = self.suspended
        self.suspended = True
        try:
            self.vars['library'].set(name)
            local = self.library.get('defaults', {})
            defaults = dict(self.context.get('defaults', {}))
            defaults.update(local.get('parameters', local))
            defaults.update(self.models[self.current_model].get('defaults', {}))
            choices = desired_layers or self.library.get('model_default_layers', {}).get(self.current_model) or self.library.get('default_layers', {})
            for role in self.model_roles():
                self.ensure_variable(role, '', 'enum')
                preferred = choices.get(role) if apply_defaults or desired_layers else self.vars[role].get()
                self.vars[role].set(self._preferred_layer(role, preferred, prefer_defaults=bool(apply_defaults or desired_layers)))
            self._configure_layer_choices()
            if apply_defaults:
                for key, entry in self.schema_for().items():
                    self.ensure_variable(key, entry.get('default'), entry.get('type', 'float'))
                    value = defaults.get(key, entry.get('default'))
                    self.vars[key].set('' if value is None else value)
                self.spine_follow.set(self.vars['pgs_spine_width'].get() == '')
            tech = self.library.get('technology') or 'Technology unavailable'
            if isinstance(tech, dict):
                tech = tech.get('name', tech.get('library', 'Attached PDK'))
            detail = '{}  ·  {} metal layers  ·  {}'.format(tech, len(self.library.get('layers', [])), self.library.get('workspace', ''))
            if self.source_context:
                detail += '  ·  Source: {}/{}/{}'.format(self.source_context.get('library', ''), self.source_context.get('cell', ''), self.source_context.get('view', 'layout'))
            self.library_info.set(detail)
        finally:
            self.suspended = was_suspended
        self._show_library_warnings()

    def _library_changed(self, _event=None):
        name = self.vars['library'].get()
        entry = next((lib for lib in self.context['libraries'] if lib['name'] == name), None) if self.context else None
        choices = (entry or {}).get('model_default_layers', {}).get(self.current_model) or (entry or {}).get('default_layers')
        self.configure_library(name, desired_layers=choices)
        self.invalidate()

    def custom_library(self):
        if self.context_busy or self.generating or self.preset_busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Flux Studio — Load workspace library')
        fw.apply(dialog, product='flux')
        fw.header(dialog, 'Flux Studio', 'Load workspace library').pack(fill='x')
        dialog.configure(bg=COLORS['panel'])
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, style='Panel.TFrame', padding=16)
        frame.pack(fill='both', expand=True)
        name = tk.StringVar()
        current_workspace = (self.context or {}).get('runtime', {}).get('workspace') or (self.source_context or {}).get('workspace', '')
        workspace = tk.StringVar(value=current_workspace)
        mapping = tk.StringVar()
        ttk.Label(frame, text='Library name').pack(anchor='w')
        ttk.Entry(frame, textvariable=name, width=48).pack(fill='x', pady=(8, 12))
        ttk.Label(frame, text='Existing workspace folder').pack(anchor='w')
        ttk.Entry(frame, textvariable=workspace, width=48).pack(fill='x', pady=(8, 12))
        ttk.Label(frame, text='Library mapping file (blank uses workspace cds.lib)').pack(anchor='w')
        ttk.Entry(frame, textvariable=mapping, width=48).pack(fill='x', pady=(8, 16))
        def load():
            work = Path(workspace.get().strip()).expanduser()
            if not work.is_absolute():
                work = Path(current_workspace or Path.cwd()) / work
            work = work.resolve()
            library_mapping = Path(mapping.get().strip()).expanduser() if mapping.get().strip() else work / 'cds.lib'
            if not library_mapping.is_absolute():
                library_mapping = work / library_mapping
            library_mapping = library_mapping.resolve()
            if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', name.get().strip()) or
                    not workspace.get().strip() or not work.is_dir() or not library_mapping.is_file()):
                messagebox.showerror('Invalid library details', 'Enter a valid library name, an existing workspace folder and an existing library mapping file.', parent=dialog)
                return
            self.context_busy = True
            self.preview_ok = False
            self.submit('library', name.get().strip(), str(work), str(library_mapping))
            self.status.set('Loading the library and its attached PDK...')
            self.update_controls()
            dialog.destroy()
        ttk.Button(frame, text='Load and identify PDK', command=load, style='Primary.TButton').pack(anchor='e')
        dialog.grab_set()

    def collect_design(self):
        parameters = {}
        for key, entry in self.schema_for().items():
            self.ensure_variable(key, entry.get('default'), entry.get('type', 'float'))
            value = self.vars[key].get()
            kind = entry.get('type', 'float')
            raw = str(value).strip()
            if kind == 'bool':
                parameters[key] = boolean_value(value)
            elif key == 'pgs_spine_width' and (self.spine_follow.get() or not raw):
                parameters[key] = None
            elif entry.get('optional') and not raw:
                parameters[key] = None
            elif kind == 'enum':
                parameters[key] = raw
            else:
                try:
                    numeric = float(raw)
                    parameters[key] = int(numeric) if kind == 'int' and math.isfinite(numeric) and numeric.is_integer() else numeric
                except ValueError:
                    parameters[key] = None
        design = dict(model=self.current_model, library=self.vars['library'].get(), cell=self.vars['cell'].get().strip(),
                      structure=self._structure_value(saved=self.vars['structure'].get()),
                      parameters=parameters, layers={role: self.vars[role].get() for role in self.model_roles()
                                                         if role in SHARED_ROLES or role in self.active_model_roles(parameters=parameters)})
        pins = getattr(self, 'leg_pins_draft', None)
        if pins:
            design['leg_pins'] = copy.deepcopy(pins)
        return design

    def local_errors(self, design):
        errors = []
        library = next((entry for entry in (self.context or {}).get('libraries', []) if entry['name'] == design.get('library')), None)
        if not library or not library.get('available'):
            errors.append(dict(part='library', message=(library or {}).get('error', 'Select an available target library.'), fields=['library']))
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', design.get('cell', '')):
            errors.append(dict(part='cell', message='Cell names start with a letter; use letters, digits and underscores, up to 80 characters.', fields=['cell']))
        model = design.get('model', DEFAULT_MODEL)
        if model not in self.models:
            errors.append(dict(part='body', message='Select an available device model.', fields=['model']))
            return errors
        for key, entry in self.schema_for(model).items():
            if not shielding_parameter_active(key, design['parameters']):
                continue
            value = design['parameters'].get(key)
            kind = entry.get('type', 'float')
            part = entry.get('part', 'parameters')
            if entry.get('optional') and value is None:
                continue
            invalid = False
            if kind == 'bool':
                invalid = not isinstance(value, bool)
            elif kind == 'enum':
                invalid = value not in entry.get('options', [])
            else:
                invalid = isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                if not invalid:
                    minimum, maximum = entry.get('min', 0), entry.get('max', 5000)
                    invalid = value < minimum or value > maximum
                    if kind == 'int':
                        invalid = invalid or not float(value).is_integer()
            if invalid:
                rule = 'one of {}'.format(', '.join(map(str, entry.get('options', [])))) if kind == 'enum' else 'a boolean' if kind == 'bool' else 'a finite {} in [{}, {}]'.format('integer' if kind == 'int' else 'number', entry.get('min', 0), entry.get('max', 5000))
                errors.append(dict(part=part, message='{} must be {}.'.format(entry.get('label', key), rule), fields=[key]))
        names = {layer['name'] for layer in (library or {}).get('layers', [])}
        for role in self.active_model_roles(model, design['parameters']):
            if design['layers'].get(role) not in names:
                errors.append(dict(part=role, message='Select a metal layer from the attached PDK.', fields=[role]))
        variants = [key for key, _title in self.model_structures(model)]
        if variants and design.get('structure') not in variants:
            errors.append(dict(part='body', message='Select a valid lead mode for this model.', fields=['structure']))
        return errors

    def _changed(self, *_args):
        if not self.suspended and not self.generating:
            self.invalidate()

    def invalidate(self, immediate=False):
        # Draft edits never submit geometry work or replace the applied snapshot.
        if self.generating or self.suspended:
            return
        self.exit_leg_mode()
        self.dirty = True
        self.draft_status.set('Draft changed · Apply to commit')
        self.status.set('Draft changed. The preview shows the last applied design. Select Apply Parameters to update it.')
        self.schedule_redraw()
        self.update_controls()

    def apply_parameters(self):
        if self.generating or self.context_busy or self.preset_busy or self.preview_busy:
            return
        design = self.collect_design()
        errors = self.local_errors(design)
        if errors:
            self.render_messages(errors)
            self.status.set('Draft contains invalid inputs. Correct them, then select Apply Parameters.')
            return
        self._save_body_draft()
        self.applied_design = copy.deepcopy(design)
        self.dirty = False
        self.revision += 1
        self.preview_ok = False
        self.draft_status.set('Parameters applied')
        self.preview()

    def recheck_applied(self):
        if self.applied_design and not self.dirty:
            self.preview()

    def preview(self):
        if self.generating or self.context_busy or self.preset_busy or self.preview_busy or not self.applied_design:
            return
        self.preview_busy = True
        self.preview_state.set('Validating applied parameters...')
        self.status.set('Validating the explicitly applied design. Draft edits remain separate.')
        self.preview_task = self.submit('preview', copy.deepcopy(self.applied_design))
        self.update_controls()

    def generate(self):
        if getattr(self, 'emx_page', None) is not None and self.emx_page.is_running:
            self.status.set('EMX is generating its candidate layouts. Generate Layout becomes available when this campaign finishes.')
            return
        if self.generating or self.preset_busy or self.dirty or not self.applied_design or not self.preview_ok or self.preview_revision != self.revision or self.preview_busy or self.context_busy:
            return
        design = copy.deepcopy(self.applied_design)
        if (self.library or {}).get('offline'):
            import backend
            filename = filedialog.asksaveasfilename(parent=self.root, title='Generate layout JSON', defaultextension='.json', initialfile='layout.json')
            if filename:
                try:
                    result = backend.preview(design)
                    Path(filename).write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
                    self.status.set('Generated layout JSON: '+Path(filename).name)
                except (OSError, ValueError) as exc:
                    self.status.set('Layout export failed: '+str(exc)+'. Choose a writable file.')
            return
        if (design['library'], design['cell']) == self.generated_target:
            return
        self.generating = True
        self.status.set('Generating {}/{} / layout. Waiting for write and readback verification.'.format(design['library'], design['cell']))
        self.submit('generate', design)
        self.update_controls()

    def update_controls(self):
        ready = self.library is not None and self.library.get('available')
        snapshot = self.applied_design or {}
        target_is_new = (snapshot.get('library'), snapshot.get('cell')) != self.generated_target
        frozen = self.generating or self.preset_busy
        campaign_running = bool(getattr(self, 'emx_page', None) and self.emx_page.is_running)
        enabled = ready and target_is_new and self.preview_ok and self.preview_revision == self.revision and not self.dirty and bool(snapshot) and not frozen and not self.preview_busy and not self.context_busy and not campaign_running
        self.generate_button.configure(state='normal' if enabled else 'disabled', text='Generating...' if self.generating else 'Generate Layout')
        self.apply_button.configure(state='normal' if ready and not frozen and not self.context_busy and not self.preview_busy else 'disabled')
        for button in [self.context_button, self.custom_button]:
            button.configure(state='disabled' if self.context_busy or frozen else 'normal')
        self.refresh_button.configure(state='normal' if snapshot and not self.dirty and not self.context_busy and not frozen and not self.preview_busy else 'disabled')
        for button in [self.export_button, self.import_button]:
            button.configure(state='normal' if ready and not frozen else 'disabled')
        for button in [self.preset_load, self.preset_save]:
            button.configure(state='disabled' if frozen or not self.context else 'normal')
        for key, widget in self.field_widgets.items():
            if isinstance(widget, ttk.Combobox):
                widget.configure(state='disabled' if frozen or (key in ROLES and not self.library) else 'readonly')
            else:
                widget.configure(state='disabled' if frozen else 'normal')
        draft = {name: boolean_value(self.vars[name].get()) for name in
                 ('pgs_enabled', 'ring_enabled', 'pgs_connect_ring')}
        pgs, ring, linked = shielding_state(draft)
        for button in self.structure_buttons:
            button.configure(state='disabled' if frozen else 'normal')
        for buttons, active in ((self.pgs_shape_buttons, pgs), (self.ring_shape_buttons, ring)):
            for button in buttons:
                button.configure(state='disabled' if frozen or not active else 'normal')
        for name in ('pgs_checkbox', 'ring_checkbox'):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state='disabled' if frozen else 'normal')
        for key, widget in self.field_widgets.items():
            if (key in SHARED_ROLES and not shielding_role_active(key, draft) or
                    key in SHARED_KEYS and not shielding_parameter_active(key, draft)):
                widget.configure(state='disabled')
        self.spine_checkbox.configure(state='disabled' if frozen or not pgs else 'normal')
        self.ground_checkbox.configure(state='disabled' if frozen or not (pgs and ring) else 'normal')
        self.field_widgets['pgs_spine_width'].configure(state='disabled' if frozen or not pgs or self.spine_follow.get() else 'normal')
        self.field_widgets['ground_route_width'].configure(state='disabled' if frozen or not linked else 'normal')
        self.field_widgets['ground_route'].configure(state='disabled' if frozen or not linked else 'readonly')
        if 'tap' in self.field_widgets:
            enabled = 'tap' in self.active_model_roles()
            self.field_widgets['tap'].configure(state='readonly' if enabled and self.library and not frozen else 'disabled')
        if getattr(self, 'leg_mode', False):
            # The applied design is edited in place; draft inputs stay locked.
            for widget in self.field_widgets.values():
                widget.configure(state='disabled')
            for button in ([self.apply_button, self.import_button, self.preset_load, self.context_button, self.custom_button,
                            self.spine_checkbox, self.ground_checkbox] + list(self.structure_buttons) +
                           list(self.pgs_shape_buttons) + list(self.ring_shape_buttons) +
                           [getattr(self, name) for name in ('pgs_checkbox', 'ring_checkbox') if getattr(self, name, None) is not None]):
                button.configure(state='disabled')
            self._update_leg_buttons()
        leg_button = getattr(self, 'leg_button', None)
        if leg_button is not None:
            leg_button.configure(state='disabled' if self.generating else 'normal')
        if self.context_busy or self.preview_busy or self.generating or self.preset_busy:
            self.progress.grid()
            self.progress.start(14)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def render_messages(self, errors=None, warnings=None):
        errors, warnings = normalize_messages(errors), merge_messages(self._library_warnings(), warnings)
        for key, widget in self.field_widgets.items():
            if isinstance(widget, (ttk.Combobox, ttk.Entry)):
                widget.configure(style='TCombobox' if isinstance(widget, ttk.Combobox) else 'TEntry')
        for item in self.error_tree.get_children():
            self.error_tree.delete(item)
        self.messages = {}
        for items, tag in [(errors, 'error'), (warnings, 'warning')]:
            for message in items:
                iid = str(len(self.messages))
                self.messages[iid] = message
                self.error_tree.insert('', 'end', iid=iid, values=(PARTS.get(message.get('part'), message.get('part', 'Parameters')), message.get('message', '')), tags=(tag,))
                if tag == 'error':
                    for field in message.get('fields', []):
                        key = field.replace('parameters.', '').replace('layers.', '')
                        if key in self.field_widgets and isinstance(self.field_widgets[key], (ttk.Combobox, ttk.Entry)):
                            widget = self.field_widgets[key]
                            widget.configure(style='Invalid.TCombobox' if isinstance(widget, ttk.Combobox) else 'Invalid.TEntry')
        if errors:
            self.validation_label.configure(text='{} validation issues · select to locate input'.format(len(errors)), foreground=COLORS['red'])
        elif self.preview_ok:
            self.validation_label.configure(text='Applied parameters and geometry validated' + (' · review warnings' if warnings else ''), foreground=COLORS['green'])
        elif warnings:
            self.validation_label.configure(text='{} PDK warnings · Apply remains available'.format(len(warnings)), foreground=COLORS['gold'])
        else:
            self.validation_label.configure(text='Apply parameters to validate geometry and layers.', foreground=COLORS['muted'])

    def _callback_error(self, exception_type, value, tb):
        diagnostic = ROOT / 'ui_diagnostics.log'
        try:
            with diagnostic.open('a', encoding='utf-8') as stream:
                stream.write('\n' + datetime.datetime.now().isoformat(timespec='seconds') + '\n')
                stream.write(''.join(traceback.format_exception(exception_type, value, tb)))
        except OSError:
            pass
        self.preview_ok = False
        try:
            self.status.set('Interface operation failed: {}. Diagnostics: {}'.format(value, diagnostic))
            self.render_messages([dict(part='system', message=str(value), fields=[])])
            self.update_controls()
        except tk.TclError:
            pass

    def _focus_error(self, _event):
        selected = self.error_tree.selection()
        if not selected:
            return
        error = self.messages.get(selected[0], {})
        self.status.set(error.get('message', ''))
        for field in error.get('fields', []):
            key = field.replace('parameters.', '').replace('layers.', '')
            if key in self.field_widgets:
                if key in ('library', 'cell'):
                    self.set_target_visible(True)
                    self.root.update_idletasks()
                tab = self.field_tabs.get(key)
                if tab:
                    self.notebook.select(self.pages[tab])
                    self.root.update_idletasks()
                    page = self.pages[tab]
                    widget_y = self.field_widgets[key].winfo_rooty() - page.content.winfo_rooty()
                    page.canvas.yview_moveto(max(0, (widget_y - 40) / max(1, page.content.winfo_height())))
                self.field_widgets[key].focus_set()
                break

    @staticmethod
    def _bounds_valid(bounds):
        return isinstance(bounds, list) and len(bounds) == 2 and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p) for p in bounds) and bounds[1][0] > bounds[0][0] and bounds[1][1] > bounds[0][1]

    def layer_color(self, layer):
        layers = (self.geometry_design or {}).get('layers', {})
        for role, color in [('body', COLORS['cyan']), ('secondary', '#b6abd9'), ('crossing', COLORS['gold']), ('tap', '#e6a477'), ('pgs', COLORS['violet']), ('ring', COLORS['green']), ('ground_route', '#849ee0')]:
            if layers.get(role) == layer:
                return color
        return '#95b3cb'

    def refresh_legend(self):
        for widget in self.legend.winfo_children():
            widget.destroy()
        layers = list(dict.fromkeys([p['layer'] for p in (self.geometry or {}).get('polygons', [])] +
                                    [p['layer'] for p in (self.geometry or {}).get('ports', []) if p.get('layer')]))
        self.layer_flags = {}
        pin_colors = pin_layer_colors(self.geometry)
        for layer in layers:
            flag = tk.BooleanVar(value=self.layer_visible.get(layer, True))
            self.layer_flags[layer] = flag
            style_name = 'Layer{}.TCheckbutton'.format(len(self.layer_flags))
            ttk.Style(self.root).configure(style_name, foreground=pin_colors[layer])
            button = ttk.Checkbutton(self.legend, text=layer, variable=flag, style=style_name,
                                     command=lambda name=layer, var=flag: self._toggle_layer(name, var.get()))
            button.pack(side='left', padx=(0, 8))

    def _toggle_layer(self, layer, value):
        self.layer_visible[layer] = value
        self.schedule_redraw()

    def update_metrics(self):
        if not self.geometry:
            return
        bounds = self.geometry['bounds']
        metrics = self.geometry.get('metrics', {})
        cut_count = sum(v.get('rows', 1) * v.get('columns', 1) for v in self.geometry.get('vias', []))
        ring_gap = metrics.get('actual_ring_body_gap')
        ring_text = 'Ring off' if ring_gap is None else 'Ring gap {:.2f} μm'.format(ring_gap)
        self.metrics.set('{:.1f} × {:.1f} μm    {}    Cuts {}    Ports {}'.format(bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1], ring_text, cut_count, len(self.geometry.get('ports', []))))

    def fit_view(self):
        if not self.geometry:
            return
        bounds = self.geometry['bounds']
        width = (bounds[1][0] - bounds[0][0]) * 1.6
        height = (bounds[1][1] - bounds[0][1]) * 1.3
        ratio = max(1, self.canvas.winfo_width()) / max(1, self.canvas.winfo_height())
        self.view = [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2,
                     max(width, height * ratio), max(height, width / ratio)]
        self.zoom = 1.0
        self.zoom_label.configure(text='100%')
        self.schedule_redraw()

    def scale(self):
        return min(self.canvas.winfo_width() / self.view[2], self.canvas.winfo_height() / self.view[3]) if self.view else 1

    def xy(self, x, y):
        scale = self.scale()
        return (self.canvas.winfo_width() / 2 + (x - self.view[0]) * scale,
                self.canvas.winfo_height() / 2 - (y - self.view[1]) * scale)

    def zoom_view(self, factor, anchor=None):
        if not self.view:
            return
        target = min(8, max(.35, self.zoom * factor))
        factor = target / self.zoom
        old_scale = self.scale()
        if anchor:
            ax = self.view[0] + (anchor[0] - self.canvas.winfo_width() / 2) / old_scale
            ay = self.view[1] - (anchor[1] - self.canvas.winfo_height() / 2) / old_scale
            self.view[0] = ax - (ax - self.view[0]) / factor
            self.view[1] = ay - (ay - self.view[1]) / factor
        self.view[2] /= factor
        self.view[3] /= factor
        self.zoom = target
        self.zoom_label.configure(text='{}%'.format(round(self.zoom * 100)))
        self.schedule_redraw()

    def _canvas_resize(self, _event):
        self.status_label.configure(wraplength=max(500, self.root.winfo_width() - 260))
        self.library_detail_label.configure(wraplength=max(600, self.root.winfo_width() - 80))
        self.runtime_detail_label.configure(wraplength=max(600, self.root.winfo_width() - 80))
        if self.geometry and self.zoom == 1:
            self.fit_view()
        else:
            self.schedule_redraw()

    def _pan_press(self, event):
        self.press_at = (event.x, event.y)
        self.pin_drag = None
        if getattr(self, 'leg_mode', False):
            self.canvas.focus_set()
            if self.leg_drag_start(event.x, event.y):
                return
        if self.view:
            self.pan_start = (event.x, event.y, self.view[0], self.view[1], self.scale())

    def _pan_release(self, event):
        self.pan_start = None
        start, self.press_at = getattr(self, 'press_at', None), None
        moved = start and math.hypot(event.x - start[0], event.y - start[1]) >= 4
        if getattr(self, 'pin_drag', None):
            if moved:
                self.leg_drag_finish()
                return
            self.leg_drag_cancel(quiet=True)
        if getattr(self, 'leg_mode', False) and start and not moved:
            self.leg_canvas_click(event.x, event.y)

    # pin dragging ------------------------------------------------------------
    def leg_drag_start(self, cx, cy):
        """Press on a pin: grab it so it can be dragged along its own leg."""
        if not self._leg_edit_ready():
            return False
        hit = self.leg_hit(cx, cy)
        if not hit or hit[0] != 'pin':
            return False
        leg = self._leg(hit[1])
        pin = self.leg_layout[hit[1]][hit[2]]
        port, problem = self._place(leg, pin)
        if leg is None or problem:
            return False
        projected = leg_project(leg, *self._world(cx, cy))
        self.pin_drag = dict(leg=hit[1], index=hit[2], original=copy.deepcopy(pin),
                             grab=projected[1] - port['leg_distance'], distance=port['leg_distance'])
        self.leg_selected, self.leg_pin_selected = hit[1], (hit[1], hit[2])
        return True

    def _drag_distance(self, leg, desired, length):
        """Clamp a dragged pin to the leg and keep it on one straight section."""
        desired = min(max(0.0, desired), leg['length'] - length)
        grid = self.leg_grid or 0.001
        best = None
        for s0, s1, a, b in leg_segments(leg['centerline']):
            diagonal = abs(b[0] - a[0]) > 1e-9 and abs(b[1] - a[1]) > 1e-9
            if diagonal or s1 - s0 < length - 1e-9:
                continue
            candidate = min(max(desired, s0), s1 - length)
            if best is None or abs(candidate - desired) < abs(best - desired):
                best = candidate
        if best is None:
            return None
        return round(round(best / grid) * grid, 9)

    def _drag_pin(self):
        """The pin being dragged, or None (the drag is dropped) if it no longer exists."""
        drag = getattr(self, 'pin_drag', None)
        if not drag:
            return None
        pins = self.leg_layout.get(drag['leg'], [])
        if drag['index'] >= len(pins) or pins[drag['index']].get('name') != drag['original']['name'] or not self._leg(drag['leg']):
            self.pin_drag = None
            return None
        return pins[drag['index']]

    def leg_drag_move(self, cx, cy):
        drag = getattr(self, 'pin_drag', None)
        pin = self._drag_pin()
        if pin is None or not self.view:
            return False
        leg = self._leg(drag['leg'])
        projected = leg_project(leg, *self._world(cx, cy))
        distance = self._drag_distance(leg, projected[1] - drag['grab'], pin['length'])
        if distance is None:
            return False
        drag['distance'] = distance
        pin['ratio'] = round(min(1.0, distance / leg['length']), 9)
        self.status.set('Dragging {}: {:.3f} μm from the free end of {}. Release to apply.'.format(pin['name'], distance, drag['leg']))
        self.schedule_redraw()
        return True

    def leg_drag_finish(self):
        drag = getattr(self, 'pin_drag', None)
        pin = self._drag_pin()
        self.pin_drag = None
        if pin is None:
            return False
        leg_id, index = drag['leg'], drag['index']
        problems = self._leg_problems(leg_id, self.leg_layout[leg_id])
        if problems:
            self.leg_layout[leg_id][index] = drag['original']
            self.status.set('Pin not moved: ' + problems[0])
            self.schedule_redraw()
            self._refresh_leg_panel()
            return False
        self._select_pin_object(leg_id, pin)
        self._commit_leg_layout('Pin {} moved to {:.3f} μm from the free end of {}.'.format(pin['name'], drag['distance'], leg_id))
        return True

    def leg_drag_cancel(self, quiet=False):
        drag = getattr(self, 'pin_drag', None)
        pin = self._drag_pin()
        self.pin_drag = None
        if pin is None:
            return False
        self.leg_layout[drag['leg']][drag['index']] = drag['original']
        self.leg_pin_selected = (drag['leg'], drag['index'])
        if not quiet:
            self.status.set('Pin drag cancelled.')
        self._refresh_leg_panel()
        self.schedule_redraw()
        return True

    def _canvas_double(self, event):
        if not getattr(self, 'leg_mode', False):
            return
        hit = self.leg_hit(event.x, event.y)
        if hit and hit[0] == 'pin':
            self.leg_selected, self.leg_pin_selected = hit[1], (hit[1], hit[2])
            self._refresh_leg_panel()
            self.rename_leg_pin_dialog()

    def _pan_move(self, event):
        if getattr(self, 'pin_drag', None):
            self.leg_drag_move(event.x, event.y)
            return
        if not self.pan_start:
            return
        x, y, cx, cy, scale = self.pan_start
        self.view[0] = cx - (event.x - x) / scale
        self.view[1] = cy + (event.y - y) / scale
        self.schedule_redraw()

    def _wheel(self, event):
        direction = -1 if getattr(event, 'num', None) == 4 or getattr(event, 'delta', 0) > 0 else 1
        widget = event.widget
        if widget == self.canvas:
            self.zoom_view(1.12 if direction < 0 else 1 / 1.12, (event.x, event.y))
            return 'break'
        while widget is not None:
            for page in self.pages.values():
                if widget == page or widget == page.canvas:
                    page.canvas.yview_scroll(direction * 3, 'units')
                    return 'break'
            widget = getattr(widget, 'master', None)

    def schedule_redraw(self):
        if self.redraw_after:
            self.root.after_cancel(self.redraw_after)
        self.redraw_after = self.root.after(16, self.redraw)

    def redraw(self):
        self.redraw_after = None
        canvas = self.canvas
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.geometry or not self.view:
            for x in range(0, width, 25):
                canvas.create_line(x, 0, x, height, fill=fw.tk_plot_colors()['grid'])
            for y in range(0, height, 25):
                canvas.create_line(0, y, width, y, fill=fw.tk_plot_colors()['grid'])
            canvas.create_text(width / 2, height / 2 - 12, text='Load example to preview an RF layout', fill=fw.TEXT, font=('liberation sans', 15))
            canvas.create_text(width / 2, height / 2 + 21, text='Preview and generated layout use the same geometry core.', fill=fw.DIM, font=('liberation sans', 11))
            return
        scale = self.scale()
        grid = 10 ** math.floor(math.log10(max(self.view[2] / 30, .01)))
        if self.view[2] / grid > 65:
            grid *= 2
        xmin, xmax = self.view[0] - self.view[2] / 2, self.view[0] + self.view[2] / 2
        ymin, ymax = self.view[1] - self.view[3] / 2, self.view[1] + self.view[3] / 2
        for i in range(math.floor(xmin / grid), math.ceil(xmax / grid) + 1):
            x = self.xy(i * grid, 0)[0]
            canvas.create_line(x, 0, x, height, fill=fw.tk_plot_colors()['grid'])
        for i in range(math.floor(ymin / grid), math.ceil(ymax / grid) + 1):
            y = self.xy(0, i * grid)[1]
            canvas.create_line(0, y, width, y, fill=fw.tk_plot_colors()['grid'])
        ox, oy = self.xy(0, 0)
        canvas.create_line(ox, 0, ox, height, fill=fw.tk_plot_colors()['axis'], dash=(4, 5))
        canvas.create_line(0, oy, width, oy, fill=fw.tk_plot_colors()['axis'], dash=(4, 5))
        orders = {layer['name']: layer.get('order', 0) for layer in (self.preview_library or {}).get('layers', [])}
        leg_mode = getattr(self, 'leg_mode', False)
        for polygon in sorted(self.geometry.get('polygons', []), key=lambda p: orders.get(p['layer'], 100)):
            layer = polygon['layer']
            if not self.layer_visible.get(layer, True):
                continue
            points = [value for point in polygon['points'] for value in self.xy(*point)]
            color = '#877aab' if polygon.get('part') == 'pgs' else self.layer_color(layer)
            if leg_mode:
                # Legs are redrawn at full brightness by the leg overlay.
                color = dim_color(color)
                canvas.create_polygon(*points, fill=color, outline=color, width=.6,
                                      tags=('geometry', 'dimmed', layer, polygon.get('part', 'body')))
                continue
            canvas.create_polygon(*points, fill=color, outline=color, width=.6, tags=('geometry', layer, polygon.get('part', 'body')))
        via_cut_budget = 1000
        simplified_vias = False
        for via in self.geometry.get('vias', []):
            if not any(self.layer_visible.get(via.get(role, ''), True) for role in ('lower', 'upper')):
                continue
            if not self._bounds_valid(via.get('bbox')):
                continue
            x0, y0 = self.xy(*via['bbox'][0])
            x1, y1 = self.xy(*via['bbox'][1])
            if leg_mode:
                canvas.create_rectangle(x0, y1, x1, y0, outline=dim_color('#f1d5a4'), width=1, dash=(2, 2), tags=('via', 'dimmed'))
                continue
            canvas.create_rectangle(x0, y1, x1, y0, outline='#f1d5a4', width=1, dash=(2, 2), tags=('via',))
            origin, cuts, spacing = via.get('origin'), via.get('cut_size'), via.get('cut_spacing')
            rows, cols = via.get('rows', 0), via.get('columns', 0)
            if origin and cuts and spacing and rows > 0 and cols > 0 and rows * cols <= via_cut_budget and min(cuts) * scale >= 1.5:
                cw, ch = cuts
                sx, sy = spacing
                full_x, full_y = cols * cw + (cols - 1) * sx, rows * ch + (rows - 1) * sy
                for row in range(rows):
                    for col in range(cols):
                        x, y = origin[0] - full_x / 2 + col * (cw + sx), origin[1] - full_y / 2 + row * (ch + sy)
                        xa, ya = self.xy(x, y)
                        xb, yb = self.xy(x + cw, y + ch)
                        canvas.create_rectangle(xa, yb, xb, ya, fill='#f4ddb3', outline='', tags=('via_cut',))
                via_cut_budget -= rows * cols
            elif rows and cols:
                simplified_vias = True
        pin_colors = pin_layer_colors(self.geometry)
        pins = [] if leg_mode else pin_annotations(self.geometry, self.layer_visible, self.xy, width, height)
        if leg_mode:
            self._draw_leg_overlay(canvas, width, height, scale)
        for pin in pins:
            x0, y0, x1, y1 = pin['region']
            color = pin_colors.get(pin['layer'], '#95b3cb')
            canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline=color, width=2, tags=('port',))
        label_boxes = []
        for pin in pins:
            color = pin_colors.get(pin['layer'], '#95b3cb')
            label = canvas.create_text(*pin['point'],
                                       text=pin['name'], fill=color, font=PIN_LABEL_FONT, tags=('port_label',))
            box = canvas.bbox(label)
            if box:
                dx, dy, placed_box, frameless = pin_label_offset(
                    pin, box, [p['region'] for p in pins], label_boxes, width, height)
                canvas.move(label, dx, dy)
                label_boxes.append(placed_box)
                if frameless:
                    shadow = canvas.create_text(*pin['point'], text=pin['name'], fill='#101010',
                                                font=PIN_LABEL_FONT, tags=('port_label_shadow',))
                    canvas.move(shadow, dx + 1, dy + 1)
                    canvas.tag_lower(shadow, label)
                    canvas.itemconfigure(label, fill='#ffffff')
                else:
                    bg = canvas.create_rectangle(*placed_box,
                                                 fill=fw.PLOT_BG, outline=color, tags=('port_label',))
                    canvas.tag_lower(bg, label)
        canvas.create_oval(ox - 3, oy - 3, ox + 3, oy + 3, outline='#a0bdca')
        shown = self.geometry_design or {}
        model_name = self.models.get(shown.get('model'), {}).get('title', shown.get('model', ''))
        canvas.create_text(14, 14, anchor='nw', text='{}  /  {}'.format(shown.get('library', ''), model_name), fill=fw.DIM, font=('liberation sans', 10))
        canvas.create_text(14, height - 14, anchor='sw', text='Origin (0, 0)    Units μm', fill=fw.DIM, font=('liberation sans', 10))
        if simplified_vias:
            canvas.create_text(width - 14, height - 14, anchor='se', text='Simplified via preview · full arrays generated', fill=fw.DIM, font=('liberation sans', 10))
        if self.dirty or not self.preview_ok or self.preview_revision != self.revision:
            text = 'Draft pending · preview retained' if self.dirty else 'Validating applied design' if self.preview_busy else 'Applied validation failed'
            canvas.create_rectangle(width - 270, 38, width - 10, 67, fill=fw.PANEL, outline=fw.LINE)
            canvas.create_text(width - 140, 52, text=text, fill=COLORS['gold'], font=('liberation sans', 10))

    # ------------------------------------------------------------------
    # Leg Pin manager
    # ------------------------------------------------------------------
    def _build_leg_panel(self, parent):
        panel = ttk.Frame(parent, style='Panel.TFrame', padding=(14, 0, 14, 10))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        ttk.Label(panel, text='Leg Pin Manager', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(panel, text='Click a leg to mark the insert point, click a pin to select it. Drag a pin to move it '
                  'along its leg (Esc cancels). Delete removes it, double-click renames it. Distances are from the free end.',
                  style='Muted.TLabel', wraplength=320, justify='left').grid(row=1, column=0, sticky='ew', pady=(3, 6))
        holder = ttk.Frame(panel, style='Panel.TFrame')
        holder.grid(row=2, column=0, sticky='nsew')
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=('distance', 'length'), show='tree headings', height=8, selectmode='browse')
        tree.heading('#0', text='Leg / pin')
        tree.heading('distance', text='From end μm')
        tree.heading('length', text='Length μm')
        tree.column('#0', width=132, minwidth=100)
        tree.column('distance', width=82, minwidth=70, anchor='e')
        tree.column('length', width=72, minwidth=60, anchor='e')
        tree.tag_configure('leg', foreground=COLORS['gold'])
        tree.tag_configure('missing', foreground=COLORS['red'])
        tree.tag_configure('problem', foreground=COLORS['red'])
        tree.tag_configure('shared', foreground='#ffb3e6')
        tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(holder, orient='vertical', command=tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=scroll.set)
        tree.bind('<<TreeviewSelect>>', self._leg_tree_selected)
        tree.bind('<Double-Button-1>', lambda _e: self.rename_leg_pin_dialog() if self.leg_pin_selected else None)
        tree.bind('<Delete>', lambda _e: self.delete_leg_pin())
        self.leg_tree = tree
        editor = ttk.Frame(panel, style='Panel.TFrame')
        editor.grid(row=3, column=0, sticky='ew', pady=(8, 0))
        for column, weight in ((1, 1), (3, 1)):
            editor.columnconfigure(column, weight=weight)
        self.leg_entries = []

        def entry(row, column, label, variable, command, span=1):
            ttk.Label(editor, text=label, style='Muted.TLabel').grid(row=row, column=column, sticky='w', padx=(0 if column == 0 else 6, 5), pady=(0, 5))
            widget = ttk.Entry(editor, textvariable=variable, width=7)
            widget.grid(row=row, column=column + 1, columnspan=span, sticky='ew', pady=(0, 5))
            widget.bind('<Return>', lambda _e: command())
            self.leg_entries.append(widget)

        entry(0, 0, 'Name', self.leg_name_var, self._rename_from_entry, span=3)
        entry(1, 0, 'From end', self.leg_distance_var, self._geometry_from_entries)
        entry(1, 2, 'Length', self.leg_length_var, self._geometry_from_entries)
        self.leg_rename_button = ttk.Button(editor, text='Rename', command=self._rename_from_entry, width=7)
        self.leg_rename_button.grid(row=0, column=4, padx=(6, 0), pady=(0, 5), sticky='ew')
        self.leg_move_button = ttk.Button(editor, text='Set', command=self._geometry_from_entries, width=7)
        self.leg_move_button.grid(row=1, column=4, padx=(6, 0), pady=(0, 5), sticky='ew')
        actions = ttk.Frame(panel, style='Panel.TFrame')
        actions.grid(row=4, column=0, sticky='ew', pady=(3, 0))
        for column in range(2):
            actions.columnconfigure(column, weight=1, uniform='leg_actions')
        self.leg_add_button = ttk.Button(actions, text='Add pin', command=self.add_leg_pin)
        self.leg_add_button.grid(row=0, column=0, sticky='ew', padx=(0, 4))
        self.leg_delete_button = ttk.Button(actions, text='Delete pin', command=self.delete_leg_pin)
        self.leg_delete_button.grid(row=0, column=1, sticky='ew', padx=(4, 0))
        self.leg_restore_leg_button = ttk.Button(actions, text='Default for leg', command=self.restore_leg_default)
        self.leg_restore_leg_button.grid(row=1, column=0, sticky='ew', padx=(0, 4), pady=(6, 0))
        self.leg_restore_all_button = ttk.Button(actions, text='Default for all', command=self.restore_all_leg_defaults)
        self.leg_restore_all_button.grid(row=1, column=1, sticky='ew', padx=(4, 0), pady=(6, 0))
        ttk.Label(panel, textvariable=self.leg_info, style='Muted.TLabel', wraplength=320,
                  justify='left', foreground=COLORS['gold']).grid(row=5, column=0, sticky='ew', pady=(7, 0))
        self.leg_exit_button = ttk.Button(panel, text='Exit Leg Pins', style='Primary.TButton', command=self.exit_leg_mode)
        self.leg_exit_button.grid(row=6, column=0, sticky='ew', pady=(8, 0))
        return panel

    def leg_mode_block_reason(self):
        """None when Leg Pins can open, else the reason it cannot."""
        if self.generating:
            return 'Wait for layout generation to finish.'
        if self.context_busy or self.preset_busy:
            return 'Wait for the library or preset operation to finish.'
        if not self.applied_design:
            return 'Apply Parameters first. Leg Pins edits the applied, validated design.'
        if self.dirty:
            return ('The draft has changes that are not applied. Select Apply Parameters and wait for '
                    'validation before editing leg pins.')
        if self.preview_busy or self.preview_revision != self.revision:
            return 'Wait for validation of the applied design to finish.'
        errors = getattr(self, 'preview_errors', [])
        if any(not str(error.get('code') or '').startswith('leg_pin_') for error in errors) or \
                (not self.preview_ok and not errors):
            return 'The applied design failed validation. Correct the reported problems and apply again.'
        if not getattr(self, 'leg_lines', None):
            return 'This layout has no terminal legs to place pins on.'
        return None

    # 3D preview -----------------------------------------------------------------
    def is_3d(self):
        mode, view = getattr(self, 'view_mode', None), getattr(self, 'preview3d', None)
        return mode is not None and mode.get() != '2D' and view is not None and view.ok

    def switch_view(self, mode=None):
        """2D canvas, or the Layout 3D Viewer widget (created on first use)."""
        if mode is not None:
            self.view_mode.set(mode)
        mode = self.view_mode.get()
        if mode != '2D':
            if self.preview3d is None:
                ok, reason = embed.available() if embed is not None else (False, EMBED_NOTE)
                if not ok:
                    self.view_mode.set('2D')
                    self.status.set('3D preview unavailable: ' + reason)
                    self._arrange_preview()
                    return False
                self.preview3d = embed.Preview3D(self.preview_area, style='studio', theme='dark',
                                                 on_pin_click=self._pin_clicked_3d, bg=COLORS['panel'])
            if not self.preview3d.ok:
                reason = self.preview3d.reason
                self.view_mode.set('2D')
                self.status.set('3D preview unavailable: ' + reason)
                self._arrange_preview()
                return False
            self.preview3d.set_style('studio' if mode == '3D Studio' else 'metal')
        self._arrange_preview()
        if mode != '2D':
            self.push_3d()
            # OpenGL initialises when the widget is first mapped; fall back if it fails then.
            self._after_3d('_check3d_after', 400, self._check_3d)
        return True

    def _after_3d(self, slot, delay, callback):
        """One tracked Tk callback per slot (cancelled on replace and on close)."""
        previous = getattr(self, slot, None)
        if previous is not None:
            try:
                self.root.after_cancel(previous)
            except tk.TclError:
                pass
        def run():
            setattr(self, slot, None)
            callback()
        setattr(self, slot, self.root.after(delay, run))

    def _cancel_3d_callbacks(self):
        for slot in ('_pin3d_after', '_check3d_after'):
            handle = getattr(self, slot, None)
            setattr(self, slot, None)
            if handle is not None:
                try:
                    self.root.after_cancel(handle)
                except tk.TclError:
                    pass

    def _check_3d(self):
        view = getattr(self, 'preview3d', None)
        if view is not None and not view.ok and self.view_mode.get() != '2D':
            self.view_mode.set('2D')
            self._arrange_preview()
            self.status.set('3D preview unavailable: ' + str(view.reason))

    def _arrange_preview(self):
        """2D only, 3D only, or (Leg Pins + 3D) the 2D editor beside the 3D view."""
        area = getattr(self, 'preview_area', None)
        if area is None:
            return
        three = self.is_3d()
        both = three and getattr(self, 'leg_mode', False)
        area.columnconfigure(0, weight=1, uniform='preview' if both else '')
        area.columnconfigure(1, weight=1 if both else 0, uniform='preview' if both else '')
        if three:
            self.preview3d.grid(row=0, column=1 if both else 0, sticky='nsew', padx=(6, 0) if both else 0)
        elif self.preview3d is not None:
            self.preview3d.frame.grid_remove()
        if three and not both:
            self.canvas.grid_remove()
        else:
            self.canvas.grid(row=0, column=0, sticky='nsew')

    def push_3d(self):
        """Send the current applied geometry to the 3D view (debounced by the widget)."""
        if not self.is_3d() or not self.geometry or not self.preview_library:
            return
        try:
            self.preview3d.set_scene(embed.flux_scene({'geometry': self.geometry}, self.preview_library))
        except Exception as exc:
            self.status.set('3D preview could not show this geometry: {}'.format(exc))
            return
        # The scene is rebuilt after the widget's debounce; mark the pin afterwards.
        self._after_3d('_pin3d_after', 250, self.sync_3d_pin)

    def sync_3d_pin(self):
        """Mark the selected leg pin in 3D (cleared outside Leg Pins)."""
        if not self.is_3d():
            return
        name = None
        if getattr(self, 'leg_mode', False) and self.leg_pin_selected:
            pin = self._selected_pin()[2]
            name = pin['name'] if pin else None
        self.preview3d.highlight_pin(name)

    def _pin_clicked_3d(self, name):
        """A pin clicked in 3D selects that leg pin in the editor when Leg Pins is open."""
        if getattr(self, 'leg_mode', False):
            matches = [(leg_id, index) for leg_id, pins in self.leg_layout.items()
                       for index, pin in enumerate(pins) if pin['name'] == name]
            if matches:
                # Same-named pins share one 3D label: repeated clicks step through them.
                current = self.leg_pin_selected
                leg_id, index = matches[(matches.index(current) + 1) % len(matches)] if current in matches else matches[0]
                self.leg_selected, self.leg_pin_selected = leg_id, (leg_id, index)
                self._refresh_leg_panel()
                self.schedule_redraw()
                self.status.set('Pin {} on leg {} selected from the 3D view.'.format(name, leg_id))
                return
        self.status.set('Pin ' + str(name))

    def toggle_leg_mode(self):
        if getattr(self, 'leg_mode', False):
            self.exit_leg_mode()
        else:
            self.enter_leg_mode()

    def enter_leg_mode(self):
        reason = self.leg_mode_block_reason()
        if reason:
            self.status.set('Leg Pins unavailable: ' + reason)
            messagebox.showinfo('Leg Pins', reason, parent=self.root)
            return False
        self.leg_layout = self._working_layout((self.applied_design or {}).get('leg_pins'))
        self.leg_mode = True
        self.leg_selected = self.leg_lines[0]['id']
        self.leg_pin_selected = None
        self.leg_clicks = {}
        self._show_leg_panel(True)
        self._refresh_leg_panel()
        self._arrange_preview()
        self.update_leg_status()
        self.update_controls()
        self.schedule_redraw()
        self.status.set('Leg Pins: edits apply to the applied design immediately and are revalidated in the background.')
        return True

    def exit_leg_mode(self):
        if getattr(self, 'pin_drag', None):
            self.leg_drag_cancel()
        if not getattr(self, 'leg_mode', False):
            return
        self.leg_mode = False
        self.leg_pin_selected = None
        self._show_leg_panel(False)
        self._arrange_preview()
        self.sync_3d_pin()
        self.update_leg_status()
        self.update_controls()
        self.schedule_redraw()
        self.status.set('Leg Pins closed. Pin edits are part of the applied design and its draft.')

    def _show_leg_panel(self, visible):
        if not hasattr(self, 'leg_panel'):
            return
        regular = (self.notebook, self.apply_panel, self.presets_panel)
        if visible:
            for widget in regular:
                widget.grid_remove()
            self.leg_panel.grid()
            self.param_eyebrow.configure(text='LEG PINS  /  μm')
        else:
            self.leg_panel.grid_remove()
            for widget in regular:
                widget.grid()
            self.param_eyebrow.configure(text='DESIGN PARAMETERS  /  μm')
        self.leg_button.configure(text='Exit Leg Pins' if visible else 'Leg Pins',
                                  style='ModeActive.TButton' if visible else 'Mode.TButton')

    def _working_layout(self, stored):
        layout = copy.deepcopy(getattr(self, 'leg_default', {}) or {})
        for leg, pins in (stored or {}).items():
            layout[leg] = copy.deepcopy(pins)
        for pins in layout.values():
            pins.sort(key=lambda pin: pin['ratio'])
        return layout

    def _stored_layout(self, layout):
        """Only legs that differ from their default pin are stored."""
        default = getattr(self, 'leg_default', {}) or {}
        stored = {leg: copy.deepcopy(pins) for leg, pins in layout.items()
                  if leg not in default or not same_pins(pins, default[leg])}
        return stored or None

    def _leg(self, leg_id):
        return next((leg for leg in getattr(self, 'leg_lines', []) if leg['id'] == leg_id), None)

    def _place(self, leg, pin):
        try:
            return leg_pins.place(leg, pin, self.leg_grid)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            return None, dict(code='leg_pin_place', message=str(exc))

    def _leg_problems(self, leg_id, pins):
        """Local placement checks mirroring the backend (fit, segment, overlap)."""
        leg = self._leg(leg_id)
        if not leg:
            return []
        problems, placed = [], []
        for pin in pins:
            port, problem = self._place(leg, pin)
            if problem:
                problems.append(problem['message'])
                continue
            placed.append(port)
        problems += [item['message'] for item in leg_pins.overlaps(placed)]
        return problems

    def _all_pin_names(self, skip=None):
        return [pin['name'] for leg, pins in self.leg_layout.items() for index, pin in enumerate(pins)
                if (leg, index) != skip]

    def _next_pin_name(self):
        used = set(self._all_pin_names()) | {leg['id'] for leg in self.leg_lines} | set(self.leg_layout)
        used |= set(RESERVED_PIN_NAMES) | set(getattr(self, 'leg_reserved', []))
        index = 1
        while 'pin_{}'.format(index) in used:
            index += 1
        return 'pin_{}'.format(index)

    def leg_name_rejection(self, leg_id, name):
        """Reason a pin on ``leg_id`` cannot be called ``name``; None if allowed."""
        if not leg_pins.NAME.fullmatch(name):
            return 'Pin names start with a letter and use letters, digits and underscores (up to 80 characters).'
        if name in RESERVED_PIN_NAMES or name in getattr(self, 'leg_reserved', []):
            return 'Pin name {} is reserved by another pin of this layout (ground pins cannot be reused).'.format(name)
        legs = {leg['id'] for leg in self.leg_lines} | set(self.leg_layout)
        if name in legs and name != leg_id:
            return 'Pin name {0} is the default name of leg {0}; use it only on that leg.'.format(name)
        leg = self._leg(leg_id) or {}
        kind = leg.get('kind', 'signal')
        for other_id, pins in self.leg_layout.items():
            other = self._leg(other_id)
            if other_id == leg_id or not other or not any(pin['name'] == name for pin in pins):
                continue
            if other.get('kind', 'signal') != kind:
                return 'Pin name {} is already used on {} leg {}; signal and TAP pins cannot share a name.'.format(
                    name, 'TAP' if other.get('kind') == 'tap' else 'signal', other_id)
            if other.get('net') != leg.get('net'):
                return ('Pin name {} is already used on leg {} of another winding (net {} vs {}); one terminal '
                        'cannot join separate nets.').format(name, other_id, other.get('net'), leg.get('net'))
        return None

    def _leg_edit_ready(self):
        if not getattr(self, 'leg_mode', False):
            return False
        if getattr(self, 'pin_drag', None):
            # Any other edit ends an unfinished drag first (restoring the pin).
            self.leg_drag_cancel()
        if self.generating or self.context_busy or self.preset_busy:
            self.status.set('Wait for the current generation, library or preset task before editing leg pins.')
            return False
        return True

    def _selected_pin(self, leg_id=None, index=None):
        if leg_id is None or index is None:
            if not self.leg_pin_selected:
                return None, None, None
            leg_id, index = self.leg_pin_selected
        pins = self.leg_layout.get(leg_id, [])
        if not 0 <= index < len(pins):
            return None, None, None
        return leg_id, index, pins[index]

    def _select_pin_object(self, leg_id, pin):
        pins = self.leg_layout.get(leg_id, [])
        pins.sort(key=lambda item: item['ratio'])
        self.leg_selected = leg_id
        self.leg_pin_selected = next(((leg_id, i) for i, item in enumerate(pins) if item is pin), None)

    def add_leg_pin(self, leg_id=None):
        """Add a 1 μm pin at the last click on the leg (or at its free end)."""
        if not self._leg_edit_ready():
            return False
        leg_id = leg_id or self.leg_selected
        leg = self._leg(leg_id)
        if not leg:
            self.status.set('Select a leg on the canvas or in the list before adding a pin.')
            return False
        pins = self.leg_layout.setdefault(leg_id, [])
        if len(pins) >= leg_pins.MAX_PINS_PER_LEG:
            messagebox.showerror('Cannot add pin', 'A leg holds at most {} pins.'.format(leg_pins.MAX_PINS_PER_LEG), parent=self.root)
            return False
        total = leg['length']
        length = min(leg_pins.DEFAULT_LENGTH, total)
        click = self.leg_clicks.get(leg_id)
        start = 0.0
        if click is not None:
            start = click - length / 2
            for s0, s1, _a, _b in leg_segments(leg['centerline']):
                if s0 - 1e-9 <= click <= s1 + 1e-9 and s1 - s0 >= length:
                    start = max(s0, min(start, s1 - length))
                    break
            start = max(0.0, min(start, total - length))
        name = self._next_pin_name()
        candidates = [start]
        if click is None:
            # No insert point: the free end, or the first free gap walking inward from it.
            # Keep a one-pin-length gap after an existing pin.
            for item in pins:
                port, _problem = self._place(leg, item)
                if port:
                    candidates.append(port['leg_distance'] + item['length'] + length)
            candidates = sorted(value for value in candidates if value <= total - length + 1e-9)
        pin = problems = None
        for value in candidates or [start]:
            # Round the ratio up so the pin never creeps back into its neighbour.
            pin = dict(name=name, ratio=min(1.0, math.ceil(value / total * 1e9) / 1e9) if total > 0 else 0.0, length=length)
            problems = self._leg_problems(leg_id, pins + [pin])
            if not problems:
                start = value
                break
        if problems:
            messagebox.showerror('Cannot add pin', problems[0] + '\nClick a free straight part of the leg and try again.', parent=self.root)
            return False
        pins.append(pin)
        self._select_pin_object(leg_id, pin)
        self._commit_leg_layout('Added {} on leg {} at {:.3f} μm from the free end.'.format(pin['name'], leg_id, start))
        return True

    def delete_leg_pin(self, leg_id=None, index=None):
        if not self._leg_edit_ready():
            return False
        leg_id, index, pin = self._selected_pin(leg_id, index)
        if pin is None:
            self.status.set('Select a pin to delete.')
            return False
        self.leg_layout[leg_id].pop(index)
        self.leg_pin_selected = None
        self.leg_selected = leg_id
        left = len(self.leg_layout[leg_id])
        self._commit_leg_layout('Deleted {} from leg {}; {} pin{} left on this leg.'.format(
            pin['name'], leg_id, left, '' if left == 1 else 's'))
        return True

    def rename_leg_pin(self, leg_id, index, name):
        if not self._leg_edit_ready():
            return False
        leg_id, index, pin = self._selected_pin(leg_id, index)
        if pin is None:
            return False
        name = str(name or '').strip()
        if name == pin['name']:
            return True
        reason = self.leg_name_rejection(leg_id, name)
        if reason:
            messagebox.showerror('Pin name not allowed', reason, parent=self.root)
            self.status.set(reason)
            return False
        shared = sorted({leg for leg, pins in self.leg_layout.items() for i, other in enumerate(pins)
                         if other['name'] == name and (leg, i) != (leg_id, index)})
        if shared and not messagebox.askyesno(
                'Same pin name',
                'Pin name {} is already used on leg {}.\n\nEMX treats pins with the same name as one shorted '
                'terminal. Use the same name anyway?'.format(name, ', '.join(shared)), parent=self.root):
            return False
        old = pin['name']
        pin['name'] = name
        self._select_pin_object(leg_id, pin)
        self._commit_leg_layout('Renamed {} to {} on leg {}.'.format(old, name, leg_id) +
                                (' Same-named pins form one EMX terminal.' if shared else ''))
        return True

    def rename_leg_pin_dialog(self):
        leg_id, index, pin = self._selected_pin()
        if pin is None:
            return False
        name = simpledialog.askstring('Rename pin', 'New name for pin {} on leg {}:'.format(pin['name'], leg_id),
                                      initialvalue=pin['name'], parent=self.root)
        if name is None:
            return False
        return self.rename_leg_pin(leg_id, index, name)

    def set_leg_pin_geometry(self, leg_id, index, distance, length):
        """Move/resize a pin; ``distance`` is from the free end, both in μm."""
        if not self._leg_edit_ready():
            return False
        leg_id, index, pin = self._selected_pin(leg_id, index)
        leg = self._leg(leg_id)
        if pin is None or leg is None:
            return False
        try:
            distance, length = float(distance), float(length)
        except (TypeError, ValueError):
            distance = length = float('nan')
        total = leg['length']
        problem = None
        if not (math.isfinite(distance) and math.isfinite(length)):
            problem = 'Enter numeric distance and length in μm.'
        elif length <= 0 or distance < 0:
            problem = 'Length must be positive and the distance from the free end cannot be negative.'
        elif distance + length > total + 1e-9:
            problem = 'Pin {} ends {:.3f} μm from the free end, beyond leg {} ({:.3f} μm).'.format(
                pin['name'], distance + length, leg_id, total)
        if problem is None:
            candidate = dict(pin, ratio=round(min(1.0, distance / total), 9), length=length)
            others = [item for i, item in enumerate(self.leg_layout[leg_id]) if i != index]
            problems = self._leg_problems(leg_id, others + [candidate])
            problem = problems[0] if problems else None
        if problem:
            messagebox.showerror('Pin position not applied', problem, parent=self.root)
            self.status.set(problem)
            return False
        pin.update(ratio=candidate['ratio'], length=length)
        self._select_pin_object(leg_id, pin)
        self._commit_leg_layout('Pin {} now starts {:.3f} μm from the free end of {} ({:.3f} μm long, ratio {:.4f}).'.format(
            pin['name'], distance, leg_id, length, pin['ratio']))
        return True

    def _rename_from_entry(self):
        if self.leg_pin_selected:
            self.rename_leg_pin(*self.leg_pin_selected, self.leg_name_var.get())

    def _geometry_from_entries(self):
        if self.leg_pin_selected:
            self.set_leg_pin_geometry(*self.leg_pin_selected, self.leg_distance_var.get(), self.leg_length_var.get())

    def restore_leg_default(self, leg_id=None):
        if not self._leg_edit_ready():
            return False
        leg_id = leg_id or self.leg_selected
        if leg_id not in self.leg_layout and leg_id not in self.leg_default:
            self.status.set('Select a leg to restore.')
            return False
        if leg_id in self.leg_default:
            self.leg_layout[leg_id] = copy.deepcopy(self.leg_default[leg_id])
        else:
            self.leg_layout.pop(leg_id, None)
            leg_id = None
        self.leg_selected = leg_id or (self.leg_lines[0]['id'] if self.leg_lines else None)
        self.leg_pin_selected = None
        self._commit_leg_layout('Restored the default pin of leg {}.'.format(leg_id) if leg_id else
                                'Removed pins of a leg that no longer exists.')
        return True

    def restore_all_leg_defaults(self):
        if not self._leg_edit_ready():
            return False
        self.leg_layout = copy.deepcopy(self.leg_default)
        self.leg_pin_selected = None
        self._commit_leg_layout('Restored the default pin on every leg.')
        return True

    def restore_applied_leg_pins(self, confirm=True):
        """Outside the manager: drop the applied leg-pin layout (e.g. after an unknown-leg error)."""
        if self.generating or self.context_busy or self.preset_busy or not self.applied_design:
            return False
        if getattr(self, 'leg_mode', False):
            return self.restore_all_leg_defaults()
        if confirm and not messagebox.askyesno('Restore default pins',
                'Remove every custom leg pin and use one default pin at the end of each leg?', parent=self.root):
            return False
        model = self.applied_design.get('model')
        self.applied_design.pop('leg_pins', None)
        # Only the applied model's pins are restored; other models keep theirs.
        if self.current_model == model:
            self.leg_pins_draft = None
            if not self.dirty:
                self._save_body_draft()
            elif model in self.body_drafts:
                self.body_drafts[model]['leg_pins'] = None
        elif model in self.body_drafts:
            self.body_drafts[model]['leg_pins'] = None
        self.revision += 1
        self.preview_ok = False
        self._request_leg_preview()
        self.update_leg_status()
        self.status.set('Default leg pins restored; revalidating the applied design.')
        return True

    def _install_leg_info(self, payload):
        """Remember the legs reported with the latest applied preview."""
        if isinstance(payload, dict) and payload.get('leg_lines'):
            self.leg_lines = copy.deepcopy(payload['leg_lines'])
            self.leg_default = copy.deepcopy(payload.get('leg_default') or {})
            self.leg_grid = payload.get('leg_grid') or 0.001
            self.leg_reserved = list(payload.get('leg_reserved') or [])
        else:
            self.leg_lines, self.leg_default, self.leg_reserved = [], {}, []
        if getattr(self, 'leg_mode', False):
            if not self.leg_lines:
                self.exit_leg_mode()
            else:
                if not self._leg(self.leg_selected) and self.leg_selected not in self.leg_layout:
                    self.leg_selected = self.leg_lines[0]['id']
                if self.leg_pin_selected and self._selected_pin()[2] is None:
                    self.leg_pin_selected = None
                self._refresh_leg_panel()
        self.update_leg_status()

    def _request_leg_preview(self):
        if self.preview_busy:
            self.leg_preview_pending = True
            self.preview_state.set('Validating leg pin edits...')
        else:
            self.preview()

    def _flush_leg_preview(self):
        if getattr(self, 'leg_preview_pending', False) and self.applied_design and not self.preview_busy:
            self.leg_preview_pending = False
            self.preview()

    def _commit_leg_layout(self, message=None):
        """Write the working layout into the applied design and its draft, then revalidate."""
        stored = self._stored_layout(self.leg_layout)
        if stored:
            self.applied_design['leg_pins'] = copy.deepcopy(stored)
        else:
            self.applied_design.pop('leg_pins', None)
        self.leg_pins_draft = copy.deepcopy(stored)
        self._save_body_draft()
        self.dirty = False
        self.draft_status.set('Parameters applied')
        self.revision += 1
        self.preview_ok = False
        self._request_leg_preview()
        self._refresh_leg_panel()
        self.update_leg_status()
        self.update_controls()
        self.schedule_redraw()
        if message:
            self.status.set(message)

    def update_leg_status(self):
        stored = (self.applied_design or {}).get('leg_pins')
        variable = getattr(self, 'leg_status', None)
        if variable is None:
            return
        if not stored:
            text = 'Leg pins: default'
        else:
            effective = dict(getattr(self, 'leg_default', {}) or {})
            effective.update(stored)
            count = sum(len(pins) for pins in effective.values())
            text = 'Leg pins: custom · {} pin{}'.format(count, '' if count == 1 else 's')
            if leg_pins.requires_pin_mapping(stored):
                text += ' · EMX targets must name pins'
        leg_errors = [error for error in getattr(self, 'preview_errors', []) if str(error.get('code') or '').startswith('leg_pin_')]
        if leg_errors:
            text += ' · {} leg pin issue{}'.format(len(leg_errors), '' if len(leg_errors) == 1 else 's')
        variable.set(text)
        button = getattr(self, 'leg_restore_button', None)
        if button is not None:
            if stored and leg_errors and not getattr(self, 'leg_mode', False):
                button.pack(side='right')
            else:
                button.pack_forget()
        if getattr(self, 'leg_mode', False):
            info = []
            leg = self._leg(self.leg_selected)
            if leg:
                info.append('Leg {} · {} · {:.3f} μm long · {:.3f} μm wide'.format(leg['id'], leg['layer'], leg['length'], leg['width']))
                if self.leg_clicks.get(leg['id']) is not None:
                    info.append('Insert point {:.3f} μm from the free end.'.format(self.leg_clicks[leg['id']]))
            if leg_pins.requires_pin_mapping(stored):
                info.append('Pin names or counts differ from the defaults: EMX targets must name pins explicitly.')
            self.leg_info.set('\n'.join(info))

    def _refresh_leg_panel(self):
        tree = getattr(self, 'leg_tree', None)
        if tree is None:
            self.update_leg_status()
            return
        self.leg_tree_syncing = True
        try:
            tree.delete(*tree.get_children())
            names = self._all_pin_names()
            order = [leg['id'] for leg in self.leg_lines] + [leg for leg in self.leg_layout if not self._leg(leg)]
            for leg_id in order:
                leg = self._leg(leg_id)
                pins = self.leg_layout.get(leg_id, [])
                if leg:
                    text = '{}  ·  {}'.format(leg_id, leg['layer'])
                    tags = ('leg',)
                else:
                    text = '{}  ·  leg missing'.format(leg_id)
                    tags = ('missing',)
                tree.insert('', 'end', iid='leg:' + leg_id, text=text, open=True, tags=tags,
                            values=('L {:.2f}'.format(leg['length']) if leg else '',
                                    '{} pin{}'.format(len(pins), '' if len(pins) == 1 else 's')))
                for index, pin in enumerate(pins):
                    port, problem = self._place(leg, pin) if leg else (None, True)
                    distance = port['leg_distance'] if port else pin['ratio'] * (leg or {}).get('length', 0)
                    shared = names.count(pin['name']) > 1
                    tag = 'problem' if problem else 'shared' if shared else 'pin'
                    tree.insert('leg:' + leg_id, 'end', iid='pin:{}:{}'.format(leg_id, index),
                                text=pin['name'] + (' (shared)' if shared else ''), tags=(tag,),
                                values=('{:.3f}'.format(distance), '{:.3f}'.format(pin['length'])))
            target = ('pin:{}:{}'.format(*self.leg_pin_selected) if self.leg_pin_selected else
                      'leg:' + self.leg_selected if self.leg_selected else None)
            if target and tree.exists(target):
                tree.selection_set(target)
                tree.see(target)
            else:
                tree.selection_set(())
        finally:
            self.leg_tree_syncing = False
        self._fill_leg_editor()
        self.update_leg_status()
        self._update_leg_buttons()
        if getattr(self, 'preview3d', None) is not None:
            self.sync_3d_pin()

    def _fill_leg_editor(self):
        leg_id, _index, pin = self._selected_pin()
        if pin is None:
            for variable in (self.leg_name_var, self.leg_distance_var, self.leg_length_var):
                variable.set('')
            return
        leg = self._leg(leg_id)
        port, _problem = self._place(leg, pin) if leg else (None, None)
        distance = port['leg_distance'] if port else pin['ratio'] * (leg or {}).get('length', 0)
        self.leg_name_var.set(pin['name'])
        self.leg_distance_var.set('{:.4g}'.format(round(distance, 6)))
        self.leg_length_var.set('{:.4g}'.format(round(pin['length'], 6)))

    def _update_leg_buttons(self):
        if not hasattr(self, 'leg_add_button'):
            return
        busy = self.generating or self.context_busy or self.preset_busy
        pin = self._selected_pin()[2] is not None
        leg = self._leg(self.leg_selected) is not None
        states = {self.leg_add_button: leg, self.leg_delete_button: pin, self.leg_rename_button: pin,
                  self.leg_move_button: pin, self.leg_restore_leg_button: bool(self.leg_selected),
                  self.leg_restore_all_button: True}
        for button, enabled in states.items():
            button.configure(state='normal' if enabled and not busy else 'disabled')
        for entry in self.leg_entries:
            entry.configure(state='normal' if pin and not busy else 'disabled')

    def _leg_tree_selected(self, _event=None):
        if self.leg_tree_syncing:
            return
        selection = self.leg_tree.selection()
        if not selection:
            return
        parts = selection[0].split(':')
        if parts[0] == 'pin':
            self.leg_selected, self.leg_pin_selected = parts[1], (parts[1], int(parts[2]))
        else:
            self.leg_selected, self.leg_pin_selected = parts[1], None
        self._fill_leg_editor()
        self.update_leg_status()
        self._update_leg_buttons()
        self.schedule_redraw()

    def _world(self, cx, cy):
        scale = self.scale()
        return (self.view[0] + (cx - self.canvas.winfo_width() / 2) / scale,
                self.view[1] - (cy - self.canvas.winfo_height() / 2) / scale)

    def _leg_visible(self, leg):
        return self.layer_visible.get(leg['layer'], True)

    def leg_pin_regions(self):
        """Canvas rectangles of every placeable pin, enlarged to stay clickable."""
        regions = []
        for leg in self.leg_lines:
            if not self._leg_visible(leg):
                continue
            for index, pin in enumerate(self.leg_layout.get(leg['id'], [])):
                port, problem = self._place(leg, pin)
                if problem:
                    continue
                x0, y0 = self.xy(*port['bbox'][0])
                x1, y1 = self.xy(*port['bbox'][1])
                x0, x1 = min(x0, x1), max(x0, x1)
                y0, y1 = min(y0, y1), max(y0, y1)
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                half_x = max((x1 - x0) / 2, LEG_PIN_MIN_PIXELS / 2)
                half_y = max((y1 - y0) / 2, LEG_PIN_MIN_PIXELS / 2)
                regions.append(dict(leg=leg['id'], index=index, name=pin['name'], port=port,
                                    region=(cx - half_x, cy - half_y, cx + half_x, cy + half_y), point=(cx, cy)))
        return regions

    def leg_hit(self, cx, cy):
        """('pin', leg, index) / ('leg', leg, distance from free end) / None."""
        if not self.view or not getattr(self, 'leg_lines', None):
            return None
        best = None
        for region in self.leg_pin_regions():
            x0, y0, x1, y1 = region['region']
            if x0 - 3 <= cx <= x1 + 3 and y0 - 3 <= cy <= y1 + 3:
                area = (x1 - x0) * (y1 - y0)
                if best is None or area < best[0]:
                    best = (area, region)
        if best:
            return ('pin', best[1]['leg'], best[1]['index'])
        x, y = self._world(cx, cy)
        tolerance = 4 / max(self.scale(), 1e-9)
        nearest = None
        for leg in self.leg_lines:
            if not self._leg_visible(leg):
                continue
            projected = leg_project(leg, x, y)
            if projected and projected[0] <= leg['width'] / 2 + tolerance and (nearest is None or projected[0] < nearest[0]):
                nearest = (projected[0], leg, projected[1])
        if nearest:
            return ('leg', nearest[1]['id'], min(max(nearest[2], 0.0), nearest[1]['length']))
        return None

    def leg_canvas_click(self, cx, cy):
        hit = self.leg_hit(cx, cy)
        if hit and hit[0] == 'pin':
            self.leg_selected, self.leg_pin_selected = hit[1], (hit[1], hit[2])
            pin = self.leg_layout[hit[1]][hit[2]]
            self.status.set('Pin {} on leg {} selected. Delete removes it; double-click renames it.'.format(pin['name'], hit[1]))
        elif hit:
            self.leg_selected, self.leg_pin_selected = hit[1], None
            self.leg_clicks[hit[1]] = round(hit[2], 6)
            self.status.set('Leg {} selected; insert point {:.3f} μm from the free end. Add pin places a 1 μm pin there.'.format(hit[1], hit[2]))
        else:
            self.leg_pin_selected = None
        self._refresh_leg_panel()
        self.schedule_redraw()
        return hit

    def _draw_leg_overlay(self, canvas, width, height, scale):
        # Drawn first so pin labels stay on top of it.
        canvas.create_text(14, 32, anchor='nw', text='LEG PIN MANAGER  ·  non-leg geometry dimmed',
                           fill=LEG_PIN_COLOR, font=('liberation sans', 10, 'bold'), tags=('leg_banner',))
        ids = {leg['id'] for leg in self.leg_lines}
        for port in (self.geometry or {}).get('ports', []):
            if port.get('custom_leg_pin') or port.get('name') in ids or not self._bounds_valid(port.get('bbox')):
                continue
            if not self.layer_visible.get(port.get('layer', ''), True):
                continue
            x0, y0 = self.xy(*port['bbox'][0])
            x1, y1 = self.xy(*port['bbox'][1])
            color = dim_color('#95b3cb')
            canvas.create_rectangle(x0, y1, x1, y0, outline=color, width=1, tags=('port', 'dimmed'))
            canvas.create_text((x0 + x1) / 2, min(y0, y1) - 6, text=port.get('name', ''), fill=color,
                               font=PIN_LABEL_FONT, tags=('port_label', 'dimmed'))
        for leg in self.leg_lines:
            if not self._leg_visible(leg):
                continue
            points = [value for point in leg['centerline'] for value in self.xy(*point)]
            line_width = max(2.0, leg['width'] * scale)
            selected = leg['id'] == self.leg_selected
            if selected:
                canvas.create_line(*points, width=line_width + 6, fill=LEG_SELECTED_COLOR, capstyle='projecting',
                                   joinstyle='miter', tags=('leg_selected', 'leg:' + leg['id']))
            canvas.create_line(*points, width=line_width, fill=self.layer_color(leg['layer']), capstyle='butt',
                               joinstyle='miter', tags=('leg', 'leg:' + leg['id']))
            ex, ey = self.xy(*leg['centerline'][-1])
            canvas.create_oval(ex - 3, ey - 3, ex + 3, ey + 3, fill=LEG_SELECTED_COLOR if selected else '#d9e6ee',
                               outline='', tags=('leg_end', 'leg:' + leg['id']))
            click = self.leg_clicks.get(leg['id'])
            if click is not None:
                px, py = self.xy(*leg_point(leg, click))
                canvas.create_line(px - 7, py, px + 7, py, fill=LEG_CLICK_COLOR, width=2, tags=('leg_click',))
                canvas.create_line(px, py - 7, px, py + 7, fill=LEG_CLICK_COLOR, width=2, tags=('leg_click',))
        regions = self.leg_pin_regions()
        for region in regions:
            selected = self.leg_pin_selected == (region['leg'], region['index'])
            x0, y0, x1, y1 = region['region']
            canvas.create_rectangle(x0, y0, x1, y1, outline=LEG_PIN_SELECTED if selected else LEG_PIN_COLOR,
                                    width=3 if selected else 2, fill=LEG_PIN_COLOR,
                                    stipple='gray50' if selected else 'gray25',
                                    tags=('leg_pin', 'leg_pin:{}:{}'.format(region['leg'], region['index'])) +
                                         (('leg_pin_selected',) if selected else ()))
        label_boxes = []
        boxes = [region['region'] for region in regions]
        for region in regions:
            selected = self.leg_pin_selected == (region['leg'], region['index'])
            color = LEG_PIN_SELECTED if selected else LEG_PIN_COLOR
            label = canvas.create_text(*region['point'], text=region['name'], fill=color,
                                       font=('liberation sans', -11, 'bold'), tags=('leg_pin_label',))
            box = canvas.bbox(label)
            if not box:
                continue
            dx, dy, placed, _frameless = pin_label_offset(region, box, boxes, label_boxes, width, height)
            canvas.move(label, dx, dy)
            label_boxes.append(placed)
            background = canvas.create_rectangle(*placed, fill='#1d0f1a', outline=color, tags=('leg_pin_label',))
            canvas.tag_lower(background, label)

    def validate_imported(self, source):
        if not isinstance(source, dict):
            raise ValueError('The design file must contain a JSON object.')
        schema = source.get('schema')
        if schema not in (None, 'tfgen.design.v1', 'tfgen.design.v2') or source.get('version') not in (None, 1, 2):
            raise ValueError('Unsupported design file version.')
        design = source.get('design', source)
        if not isinstance(design, dict) or not isinstance(design.get('parameters'), dict) or not isinstance(design.get('layers'), dict):
            raise ValueError('Missing complete parameters / layers fields.')
        if not self.context:
            raise ValueError('Load a target library and technology first.')
        if schema == 'tfgen.design.v2' and 'model' not in design:
            raise ValueError('A v2 design must explicitly identify its model.')
        legacy = schema == 'tfgen.design.v1' or 'model' not in design
        # Legacy v1 files predate model ids; they always describe transformer_implicit,
        # which is now provided by a structure pack rather than built in.
        model = design.get('model', 'transformer_implicit')
        design = dict(design, model=model)
        if legacy and model != 'transformer_implicit':
            raise ValueError('Legacy v1 files only support transformer_implicit.')
        if model not in self.models:
            raise ValueError('The design model %r is not available. Load the structure pack that provides it '
                             '(FLUX_PACKS or the packs/ folder) and restart Flux.' % model)
        design = copy.deepcopy(design)
        for name in ('pgs_enabled', 'ring_enabled'):
            design['parameters'].setdefault(name, True)
        if not legacy:
            defaults = self.models[model].get('defaults', {})
            for name in ('center_tap', 'rule_policy'):
                entry = self.schema_for(model).get(name)
                if entry is not None and name not in design['parameters']:
                    design['parameters'][name] = defaults.get(name, entry.get('default'))
        if legacy:
            required = ['inner_diameter', 'inner_width', 'outer_width', 'gap', 'leg_length',
                        'center_leg_gap', 'pair_leg_gap', 'pgs_extent', 'pgs_width', 'pgs_spacing', 'pgs_shape', 'ring_width']
            if 'ring_extent' not in design['parameters']:
                required.append('ring_gap')
        else:
            required = [name for name, entry in self.schema_for(model).items() if not entry.get('optional')]
        required = [name for name in required if shielding_parameter_active(name, design['parameters'])]
        missing = [name for name in required if name not in design['parameters']]
        if missing:
            raise ValueError('Missing required parameters: ' + ', '.join(missing))
        if legacy:
            library = next((entry for entry in self.context['libraries'] if entry['name'] == design.get('library')), {})
            defaults = library.get('model_default_layers', {}).get(model, library.get('default_layers', {}))
            for role in ['ground_route']:
                if role not in design['layers'] and role in defaults:
                    design['layers'][role] = defaults[role]
        import backend
        normalized = backend.normalize_design(design)
        errors = self.local_errors(normalized)
        if errors:
            raise ValueError(errors[0]['message'])
        return normalized

    def apply_design(self, source):
        """Load a single-model preset into draft controls; never commit geometry."""
        if self.generating:
            raise ValueError('Wait for generation to finish before loading a design.')
        design = self.validate_imported(source)
        was_suspended = self.suspended
        self.suspended = True
        try:
            self.switch_model(design['model'], mark_dirty=False)
            self.configure_library(design['library'], desired_layers=design['layers'])
            for key, value in design['parameters'].items():
                if key in self.schema_for():
                    self.vars[key].set('' if value is None else value)
            self.spine_follow.set(design['parameters'].get('pgs_spine_width') is None)
            self.vars['cell'].set(design['cell'])
            self.vars['structure'].set(self._structure_value(design['model'], design.get('structure')))
            self.leg_pins_draft = copy.deepcopy(design.get('leg_pins'))
            self._save_body_draft()
        finally:
            self.suspended = was_suspended
        self.invalidate()

    def export_design(self):
        design = self.collect_design()
        if self.local_errors(design):
            self.status.set('Correct invalid draft parameters before exporting.')
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title='Export RF layout parameters', initialdir=str(ROOT),
                                              initialfile=design['cell'] + '.json', defaultextension='.json',
                                              filetypes=[('JSON design files', '*.json')])
        if not filename:
            return
        try:
            destination = Path(filename)
            if destination.exists():
                backup = destination.with_name(destination.name + '.bak-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
                backup.write_bytes(destination.read_bytes())
            from backend import public_source_context
            destination.write_text(json.dumps({'schema': 'tfgen.design.v2', 'design': design, 'source_context': public_source_context(self.source_context)}, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            self.status.set('Design file saved: ' + filename)
        except Exception as exc:
            self.status.set('Export failed: ' + str(exc))

    def import_design(self):
        filename = filedialog.askopenfilename(parent=self.root, title='Load RF layout parameters', initialdir=str(ROOT), filetypes=[('JSON design files', '*.json')])
        if not filename:
            return
        try:
            source = Path(filename)
            if source.stat().st_size > 524288:
                raise ValueError('The design file exceeds 512 KB.')
            self.apply_design(json.loads(source.read_text(encoding='utf-8'), parse_constant=reject_constant))
            self.status.set('Design loaded into the draft. Select Apply Parameters to validate it.')
        except Exception as exc:
            self.status.set('Load failed: ' + str(exc))

    def render_presets(self, presets):
        names = [preset if isinstance(preset, str) else preset.get('name', '') for preset in presets if isinstance(preset, (str, dict))]
        self.preset_combo.configure(values=[name for name in names if name])

    def save_preset(self):
        if self.preset_busy or self.generating:
            return
        name = self.vars['preset_name'].get().strip()
        if not re.fullmatch(r'[\w \-]{1,64}', name):
            self.status.set('Preset names allow letters, digits, spaces, underscores and hyphens, up to 64 characters.')
            return
        design = self.collect_design()
        errors = self.local_errors(design)
        if errors:
            self.render_messages(errors)
            self.status.set('Correct invalid parameters before saving the preset.')
            return
        self.preset_busy = True
        self.submit('save_preset', name, design)
        self.update_controls()

    def load_preset(self):
        if self.preset_busy or self.generating:
            return
        name = self.vars['preset'].get()
        if not name:
            self.status.set('Select a saved preset first.')
            return
        self.preset_busy = True
        self.submit('load_preset', name)
        self.update_controls()

    def close(self):
        emx_page = getattr(self, 'emx_page', None)
        if emx_page is not None and emx_page.is_running:
            if self.close_pending or messagebox.askyesno('EMX campaign in progress',
                    'Cancel this campaign and close after its owned jobs stop? Completed layouts, logs and SNP files are retained.',
                    parent=self.root):
                self.close_pending = True
                self.status.set('Closing: waiting for this campaign and any native write to finish.')
                emx_page.request_close(self.close)
            return
        if self.generating or self.context_busy:
            title = 'Generation in progress' if self.generating else 'Technology read in progress'
            prompt = 'Layout writing is still in progress. Close after this write and verification finish?' if self.generating else 'Technology reading is still in progress. Close after this read finishes?'
            if self.close_pending or messagebox.askyesno(title, prompt + '\nSelect No to keep the window open.', parent=self.root):
                self.close_pending = True
                self.status.set('Close requested. Waiting for the current task to finish.')
            return
        self.destroy()

    def destroy(self):
        emx_page = getattr(self, 'emx_page', None)
        if emx_page is not None:
            if emx_page.is_running:
                self.close()
                return
            emx_page.destroy()
        self._cancel_3d_callbacks()
        self.closed = True
        for timer in (getattr(self, '_poll_after', None), self.preview_after, self.redraw_after):
            if timer is not None:
                try:
                    self.root.after_cancel(timer)
                except tk.TclError:
                    pass
        self.tasks.put(None)
        self.root.destroy()


def read_launch_context(filename):
    source = Path(filename).expanduser()
    if not source.is_absolute():
        source = Path(os.environ.get('TFGEN_LAUNCH_CWD') or Path.cwd()) / source
    if source.stat().st_size > 524288:
        raise ValueError('The layout context file exceeds 512 KB.')
    context = json.loads(source.read_text(encoding='utf-8'), parse_constant=reject_constant)
    if not isinstance(context, dict) or context.get('schema') != 'tfgen.layout.context.v1':
        raise ValueError('Unsupported layout context schema.')
    if not all(isinstance(context.get(key), str) and context[key] and len(context[key]) <= 4096
               for key in ('library', 'cell', 'view', 'workspace')):
        raise ValueError('Layout context requires library, cell, view and workspace strings.')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', context['library']):
        raise ValueError('Invalid source library name.')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', context['cell']):
        raise ValueError('Invalid source cell name.')
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(description='Flux Studio RF layout generator')
    parser.add_argument('--example', action='store_true', help='Preview the fictional generic technology without EDA tools.')
    parser.add_argument('--technology', metavar='JSON', help='Load an offline user technology profile.')
    parser.add_argument('--context', metavar='JSON', help='Bind the source library and cell from a Virtuoso layout context file.')
    args = parser.parse_args(argv)
    context = None
    if args.context:
        try:
            context = read_launch_context(args.context)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    root = tk.Tk()
    application = TransformerApp(root, launch_context=context, technology_path=args.technology)
    if args.example:
        root.after(180, application.load_example)
    root.mainloop()


if __name__ == '__main__':
    main()
