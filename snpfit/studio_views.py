"""English comparison plots and exact connected-circuit views for SNPFit Studio."""
from __future__ import annotations

import fieldwork_tk as fw
fw.apply_matplotlib()

import csv
import math
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk

import numpy as np
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrowPatch, Rectangle
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

COLORS = {'Source': fw.SERIES[0], 'Fit': fw.accent('flux'), 'Before upgrade': fw.SERIES[4], 'After upgrade': fw.accent('flux')}
DIFF_COLORS = {'added': '#168253', 'removed': '#c64e50', 'changed': '#c1831b', 'unchanged': fw.TEXT}


def _results(page):
    result = dict(page.get('results') or {})
    for key, alias in [('quick', 'quick'), ('current', 'current'), ('before_upgrade', 'before_upgrade'), ('upgraded', 'after_upgrade')]:
        if result.get(key) is None and page.get(alias) is not None:
            result[key] = page[alias]
    return result


def _snapshot_curves(snapshot):
    if not snapshot:
        return {}
    curves = snapshot.get('curves') or {}
    if not curves:
        metrics = snapshot.get('metrics') or {}
        curves = (metrics.get('lq') or metrics).get('curve') or {}
    if not isinstance(curves, dict):
        return {}
    result = dict(curves)
    if 'source' not in result:
        source, fitted = {}, {}
        for name, values in (result.get('branches') or {}).items():
            source[name] = {key: values.get('source_' + alias) for key, alias in [('l_h', 'l_h'), ('q', 'q'), ('r_ohm', 'r_ohm')]}
            fitted[name] = {key: values.get('fit_' + alias) for key, alias in [('l_h', 'l_h'), ('q', 'q'), ('r_ohm', 'r_ohm')]}
        result.update(source=source, fitted=fitted)
    if 'fitted_k' not in result:
        result['fitted_k'] = result.get('fit_k')
    return result


def comparison_series(page):
    """Return source plus one fit, or exactly source/before/after versions."""
    results = _results(page)
    before, after = results.get('before_upgrade'), results.get('upgraded')
    current = results.get('current') or results.get('quick')
    versions = [('Before upgrade', before), ('After upgrade', after)] if before and after else [('Fit', current)]
    source_snapshot = next((snap for _, snap in versions if _snapshot_curves(snap).get('source')), None)
    if source_snapshot is None and page.get('preview'):
        source_snapshot = page['preview']
    series = []
    if source_snapshot:
        curves = _snapshot_curves(source_snapshot)
        series.append({'label': 'Source', 'freq_hz': curves.get('freq_hz', []),
                       'branches': curves.get('source') or {}, 'k': curves.get('source_k'),
                       'srf': curves.get('source_srf'), 'snapshot': source_snapshot})
    for label, snapshot in versions:
        if not snapshot:
            continue
        curves = _snapshot_curves(snapshot)
        if not curves.get('fitted'):
            continue
        series.append({'label': label, 'freq_hz': curves.get('freq_hz', []),
                       'branches': curves.get('fitted') or {}, 'k': curves.get('fitted_k'),
                       'srf': curves.get('fitted_srf'), 'snapshot': snapshot})
    return series


def _numeric(values, count=None):
    if values is None:
        return np.full(count or 0, np.nan)
    output = np.asarray([np.nan if v is None else v for v in values], dtype=float)
    if count is not None and len(output) != count:
        return np.full(count, np.nan)
    return output


def engineering(value, kind=''):
    value = float(value)
    if kind == 'mind':
        return '%.5g' % value
    unit = {'res': 'Ohm', 'ind': 'H', 'cap': 'F'}.get(kind, '')
    if value == 0:
        return '0 ' + unit
    scales = [(1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1, ''), (1e-3, 'm'), (1e-6, 'u'), (1e-9, 'n'), (1e-12, 'p'), (1e-15, 'f')]
    for scale, prefix in scales:
        if abs(value) >= scale:
            return '%.5g %s%s' % (value / scale, prefix, unit)
    return '%.5g %s' % (value, unit)


def circuit_diff(before, after):
    """Exact instance-name comparison, distinguishing value and wiring changes."""
    old = {e['name']: e for e in (before or {}).get('elements', [])}
    new = {e['name']: e for e in (after or {}).get('elements', [])}
    out = {'added': [], 'removed': [], 'changed': [], 'rewired': [], 'unchanged': []}
    for name in sorted(set(old) | set(new)):
        if name not in old:
            out['added'].append(name)
        elif name not in new:
            out['removed'].append(name)
        else:
            fields = ('kind', 'n1', 'n2', 'ind1', 'ind2')
            if any(old[name].get(k) != new[name].get(k) for k in fields):
                out['rewired'].append(name)
                out['changed'].append(name)
            elif not math.isclose(float(old[name]['value']), float(new[name]['value']), rel_tol=1e-12, abs_tol=0):
                out['changed'].append(name)
            else:
                out['unchanged'].append(name)
    return out


def _counts(circuit):
    return {kind: sum(e['kind'] == kind for e in circuit.get('elements', [])) for kind in ('res', 'ind', 'cap', 'mind')}


def circuit_layout(circuit, shared=None):
    """Deterministic winding paths, substrate positions and a shared REF rail."""
    elements = circuit.get('elements', [])
    emap = {e['name']: e for e in elements}
    ports = circuit.get('ports', [])
    reference = circuit.get('reference', 'REF')
    segments = {}
    for element in elements:
        match = re.fullmatch(r'rw(\d+)_(\d+)', element['name'])
        if match and element['kind'] == 'res':
            i, j = map(int, match.groups())
            lname = 'lw%d_%d' % (i, j)
            skin = emap.get('lskinw%d_%d' % (i, j))
            if lname in emap:
                lind = emap[lname]
                segments[i, j] = {'first': element['n1'], 'rnode': element['n2'],
                                  'lnode': lind['n2'], 'last': skin['n2'] if skin else lind['n2'],
                                  'res': element['name'], 'ind': lname,
                                  'skin': skin['name'] if skin else None}
    paths = {}
    for (i, j), segment in segments.items():
        paths.setdefault(i, []).append((j, segment))
    windings = {}
    for i in sorted(paths):
        path = sorted(paths[i])
        boundaries = {path[0][1]['first'], path[-1][1]['last']}
        winding = 1 if boundaries & {'S', 'S1', 'S2', 'STAP'} else 0
        windings.setdefault(winding, []).extend(path)
    positions, edge_refs = {}, {}
    max_sections = max((len(path) for path in windings.values()), default=1)
    width = max(8., 4.2 * max_sections)
    for winding, path in sorted(windings.items()):
        y = -3.6 * winding
        step = width / len(path)
        for j, (_, segment) in enumerate(path):
            for node, frac in [(segment['first'], 0), (segment['rnode'], .30),
                               (segment['lnode'], .65), (segment['last'], 1)]:
                point = (j * step + frac * step, y)
                if node == reference:
                    for edge in (segment['res'], segment['ind'], segment['skin']):
                        if edge and reference in (emap[edge].get('n1'), emap[edge].get('n2')):
                            edge_refs[edge] = point
                else:
                    positions[node] = point
    # Generic circuits still receive all nodes and exact connectivity; no node
    # is invented to make an unroutable branch disappear.
    all_nodes = set(ports)
    for e in elements:
        if e['kind'] != 'mind':
            all_nodes.update((e['n1'], e['n2']))
    for j, port in enumerate(ports):
        if port not in positions:
            positions[port] = (width * (j % 2), -3.6 * (j // 2))
    for e in elements:
        if e['kind'] == 'cap' and e['name'].startswith('cox'):
            a, b = e['n1'], e['n2']
            if a in positions and b not in positions:
                x, y = positions[a]
                positions[b] = (x + .35, y - 1.40)
    pending = sorted(all_nodes - set(positions) - {reference})
    for round_index in range(4):
        changed = False
        for node in pending[:]:
            neighbors = []
            for e in elements:
                if e['kind'] != 'mind' and node in (e['n1'], e['n2']):
                    other = e['n2'] if node == e['n1'] else e['n1']
                    if other in positions:
                        neighbors.append(positions[other])
            if neighbors:
                x = float(np.mean([p[0] for p in neighbors]))
                y = float(np.mean([p[1] for p in neighbors])) - .8 - .2 * (len(positions) % 3)
                positions[node] = (x, y)
                pending.remove(node)
                changed = True
        if not changed:
            break
    for j, node in enumerate(pending):
        positions[node] = (width * (j + 1) / (len(pending) + 1), -5.2 - .5 * (j % 2))
    ref_y = min((p[1] for p in positions.values()), default=0.) - 2.25
    positions[reference] = (width / 2, ref_y)
    if shared:
        for node in set(positions) & set(shared):
            positions[node] = shared[node]
        # Shared topology diagrams also share the visible global return rail.
        if reference in shared:
            ref_y = shared[reference][1]
    return {'nodes': positions, 'reference_y': ref_y, 'reference': reference,
            'width': width, 'reference_edge_anchors': edge_refs}


def _symbol(ax, a, b, kind, color, parallel_offset=0.):
    """Draw a standard two-terminal symbol on an exact branch's endpoints."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    delta = b - a
    length = float(np.linalg.norm(delta))
    if length < 1e-10:
        return (a + b) / 2
    direction = delta / length
    normal = np.array([-direction[1], direction[0]])
    bend = normal * parallel_offset
    start, end = a + bend, b + bend
    ax.plot([a[0], start[0]], [a[1], start[1]], color=color, lw=.9)
    ax.plot([b[0], end[0]], [b[1], end[1]], color=color, lw=.9)
    center = (start + end) / 2
    half = min(.34, length * .24)
    left, right = center - direction * half, center + direction * half
    ax.plot([start[0], left[0]], [start[1], left[1]], color=color, lw=1.)
    ax.plot([right[0], end[0]], [right[1], end[1]], color=color, lw=1.)
    def xy(x, y):
        return center + x * direction + y * normal
    if kind == 'res':
        points = [xy(-half, 0)]
        points += [xy(v, .09 if i % 2 == 0 else -.09) for i, v in enumerate(np.linspace(-half*.85, half*.85, 8))]
        points += [xy(half, 0)]
        array = np.asarray(points)
        ax.plot(array[:, 0], array[:, 1], color=color, lw=1.2)
    elif kind == 'cap':
        for sign in (-1, 1):
            p, q = xy(sign * .07, -.16), xy(sign * .07, .16)
            ax.plot([p[0], q[0]], [p[1], q[1]], color=color, lw=1.4)
        for p, q in [(left, xy(-.07, 0)), (xy(.07, 0), right)]:
            ax.plot([p[0], q[0]], [p[1], q[1]], color=color, lw=1.)
    elif kind == 'ind':
        xx = np.linspace(-half, half, 80)
        yy = .12 * np.abs(np.sin(np.linspace(0, 4*np.pi, 80)))
        points = np.asarray([xy(x, y) for x, y in zip(xx, yy)])
        ax.plot(points[:, 0], points[:, 1], color=color, lw=1.2)
    return center


def draw_circuit(ax, circuit, title='Circuit', changes=None, side='after', shared_layout=None):
    """Draw every actual R/L/C edge and mutual link; values remain in the table."""
    ax.clear()
    if not circuit:
        ax.text(.5, .5, 'Generate an initial circuit to view its structure.', transform=ax.transAxes,
                ha='center', va='center', color=fw.DIM, fontsize=11)
        ax.set_axis_off()
        return {'nodes': {}, 'elements': []}
    layout = circuit_layout(circuit, shared_layout)
    positions = layout['nodes'];ref = layout['reference'];ref_y = layout['reference_y']
    width = max(layout['width'], max((p[0] for p in positions.values()), default=8.))
    ax.plot([-.7, width+.7], [ref_y, ref_y], color='#9caab4', lw=.9)
    edge_centers, drawn = {}, []
    dense = len(circuit['elements']) > 10
    branch_pairs = {}
    for e in circuit['elements']:
        if e['kind'] != 'mind':
            pair = tuple(sorted((e['n1'], e['n2'])))
            branch_pairs.setdefault(pair, []).append(e['name'])
    def status(name):
        if not changes:
            return 'unchanged'
        if side == 'before' and name in changes['removed']:
            return 'removed'
        if side == 'after' and name in changes['added']:
            return 'added'
        if name in changes['changed']:
            return 'changed'
        return 'unchanged'
    for e in circuit['elements']:
        if e['kind'] == 'mind':
            continue
        name, a, b = e['name'], e['n1'], e['n2']
        color = DIFF_COLORS[status(name)]
        pa, pb = positions[a], positions[b]
        if a == ref or b == ref:
            other = pb if a == ref else pa
            anchor = layout['reference_edge_anchors'].get(name)
            if anchor:
                ground = anchor
                ax.plot([ground[0], ground[0]], [ground[1], ref_y], color=color, lw=.8, alpha=.7)
            else:
                ground = (other[0], ref_y)
            if a == ref: pa = ground
            else: pb = ground
        pair = tuple(sorted((a, b)))
        peers = branch_pairs[pair]
        index = peers.index(name)
        offset = (index - (len(peers)-1)/2) * .48 if len(peers)>1 else 0.
        # Terminal-to-terminal parasitics arc above the winding, not through it.
        if e['kind'] == 'cap' and e['name'].startswith(('cp', 'cbridge')):
            p1, p2 = np.asarray(pa,float),np.asarray(pb,float)
            height = .7 + .18 * (int(sum(map(ord,name))) % 4)
            elbow1, elbow2 = p1 + [0,height], p2 + [0,height]
            ax.plot([p1[0],elbow1[0]],[p1[1],elbow1[1]],color=color,lw=.8)
            ax.plot([p2[0],elbow2[0]],[p2[1],elbow2[1]],color=color,lw=.8)
            center = _symbol(ax,elbow1,elbow2,e['kind'],color,offset)
        else:
            center = _symbol(ax,pa,pb,e['kind'],color,offset)
        edge_centers[name]=center
        value = engineering(e['value'],e['kind'])
        text = name if dense else name+'\n'+value
        a_point,b_point=np.asarray(pa),np.asarray(pb)
        vertical=abs(b_point[1]-a_point[1])>abs(b_point[0]-a_point[0])*1.5
        tx,ty=center[0],center[1]+.20
        align, valign='center','bottom'
        if vertical:
            direction=-1 if offset<0 else 1
            tx+=direction*.09;ty=center[1];align='right' if direction<0 else 'left';valign='center'
        elif len(peers)>1 and offset<0:
            ty=center[1]-.20;valign='top'
        ax.text(tx,ty,text,ha=align,va=valign,fontsize=6.6 if dense else 7.1,
                color=color,bbox={'facecolor':fw.PLOT_BG,'edgecolor':'none','alpha':.86,'pad':.5})
        drawn.append(name)
    couplings=[e for e in circuit['elements'] if e['kind']=='mind']
    for i,e in enumerate(couplings):
        a,b=edge_centers.get(e['ind1']),edge_centers.get(e['ind2'])
        if a is None or b is None:
            continue
        color=DIFF_COLORS[status(e['name'])]
        patch=FancyArrowPatch(a,b,connectionstyle='arc3,rad=%.3f'%(.20+.045*(i%4)),
                              arrowstyle='-',linestyle=':',lw=.8,color=color,alpha=.58)
        ax.add_patch(patch)
        middle=(np.asarray(a)+np.asarray(b))/2
        if not dense:ax.text(middle[0],middle[1]+.12*(i%3),e['name'],fontsize=5.8,color=color,alpha=.85)
        drawn.append(e['name'])
    for node,(x,y) in positions.items():
        if node==ref:
            continue
        port=node in circuit.get('ports',[])
        ax.plot(x,y,'o',markersize=4.4 if port else 2.2,color=fw.TEXT)
        ax.text(x,y-.20,node,fontsize=7.7 if port else 5.7,ha='center',va='top',color=fw.TEXT,
                fontweight='bold' if port else 'normal',bbox={'facecolor':fw.PLOT_BG,'alpha':.75,'edgecolor':'none','pad':.2})
    reftext='REF / GND pin' if circuit.get('metadata',{}).get('export_reference',circuit.get('metadata',{}).get('explicit_reference',True)) else 'REF (internal reference)'
    x=positions[ref][0]
    for off,length in [(0,.35),(-.12,.24),(-.24,.12)]:
        ax.plot([x-length,x+length],[ref_y+off,ref_y+off],color=fw.TEXT,lw=1.)
    ax.text(x,ref_y-.34,reftext,ha='center',va='top',fontsize=8,color=fw.TEXT)
    counts=_counts(circuit)
    ax.set_title(title+'\nR%d  L%d  C%d  K%d'%(counts['res'],counts['ind'],counts['cap'],counts['mind']),fontsize=10,loc='left',pad=12)
    ax.text(0.,-.04,('Exact nets; %d dotted mind links.\nDevice values: parameter table.'%len(couplings)),
            transform=ax.transAxes,fontsize=7,color=fw.DIM,va='top')
    ys=[p[1] for p in positions.values()]
    ax.set_xlim(-1.1,width+1.1);ax.set_ylim(min(ys)-.9,max(ys)+1.9)
    ax.set_aspect('auto');ax.set_axis_off()
    return {'nodes':positions,'elements':drawn,'layout':layout}


class ComparisonView(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.page=None;self._series=[];self._legend_map={};self._lines=[];self._source_identity=None
        self.message=tk.StringVar(value='Load a SNP and select its model to preview frequency responses.')
        self.message_label=ttk.Label(self,textvariable=self.message,wraplength=900);self.message_label.pack(fill='x',padx=8,pady=(4,4))
        selectors=ttk.Frame(self);selectors.pack(fill='x',padx=8,pady=(0,4))
        ttk.Label(selectors,text='Winding').pack(side='left')
        self.winding=tk.StringVar(value='Single winding')
        self.winding_box=ttk.Combobox(selectors,textvariable=self.winding,values=['Single winding'],state='readonly',width=16)
        self.winding_box.pack(side='left',padx=(4,16));self.winding_box.bind('<<ComboboxSelected>>',self._selection_changed)
        ttk.Label(selectors,text='Display').pack(side='left')
        self.metric=tk.StringVar(value='All metrics')
        self.metric_box=ttk.Combobox(selectors,textvariable=self.metric,values=['All metrics','L and Q','Inductance','Quality factor','Effective coupling','Resistance'],state='readonly',width=19)
        self.metric_box.pack(side='left',padx=4);self.metric_box.bind('<<ComboboxSelected>>',self._selection_changed)
        self.figure=Figure(figsize=(9.1,5.5),dpi=100,facecolor=fw.PLOT_BG,constrained_layout=True)
        self.figure.get_layout_engine().set(w_pad=.025,h_pad=.018,wspace=.025,hspace=.035)
        self.canvas=FigureCanvasTkAgg(self.figure,master=self)
        self.canvas.get_tk_widget().pack(fill='both',expand=True)
        self.toolbar=NavigationToolbar2Tk(self.canvas,self,pack_toolbar=False)
        self.toolbar.update();self.toolbar.pack(fill='x')
        self.readout=tk.StringVar(value='Hover for sampled values. Click a legend to hide or show its curve.')
        self.readout_label=ttk.Label(self,textvariable=self.readout,wraplength=900);self.readout_label.pack(fill='x',padx=8,pady=(0,4))
        self._wrap_width=None;self.bind('<Configure>',self._wrap_labels,add='+')
        self._connections=[self.canvas.mpl_connect('motion_notify_event',self._hover),self.canvas.mpl_connect('pick_event',self._pick)]
        self.set_data({})

    def set_data(self,page):
        old_limits=None
        config=page.get('config') or {}
        identity=((page.get('source') or {}).get('sha256'),config.get('model_id'),str(config.get('port_order')),str(config.get('fmin_ghz')),str(config.get('fmax_ghz')),bool(config.get('short_unused')))
        if self.page is not None and identity==self._source_identity and self.figure.axes:
            if getattr(self,'_auto_limits',None):
                changed=any(not np.allclose(ax.get_xlim()+ax.get_ylim(),saved) for ax,saved in zip(self.figure.axes,self._auto_limits))
                if changed:old_limits=[ax.get_xlim()+ax.get_ylim() for ax in self.figure.axes]
        self.page=page;self._source_identity=identity;self._series=comparison_series(page)
        self.figure.clear();self._legend_map={};self._lines=[]
        view=page.get('view') or page.get('view_state') or {}
        metrics=[('l_h','Inductance','nH',1e9),('q','Quality factor','Q',1),('k','Effective coupling','k',1),('r_ohm','Resistance','Ohm',1)]
        markers=bool(view.get('show_markers',True));xscale=view.get('xscale','log')
        if xscale not in ('log','linear'):xscale='log'
        branches=sorted(set(name for series in self._series for name in series['branches']))
        desired=view.get('winding','primary' if 'primary' in branches else 'winding')
        if desired!='all' and desired not in branches:desired=branches[0] if branches else 'winding'
        winding_labels={'winding':'Single winding','primary':'Primary','secondary':'Secondary','all':'All windings'}
        self.winding_box.configure(values=[winding_labels.get(name,name.capitalize()) for name in branches]+(['All windings'] if len(branches)>1 else []))
        self.winding.set(winding_labels.get(desired,desired.capitalize()))
        selected_branches=branches if desired=='all' else [desired]
        selected_metric=view.get('selected_metric','All metrics')
        allowed_metrics={'All metrics':metrics,'L and Q':metrics[:2],'Inductance':metrics[:1],'Quality factor':metrics[1:2],'Effective coupling':metrics[2:3],'Resistance':metrics[3:4]}
        metrics=allowed_metrics.get(selected_metric,metrics);self.metric.set(selected_metric if selected_metric in allowed_metrics else 'All metrics')
        self.axes=np.atleast_1d(self.figure.subplots(2,2) if len(metrics)==4 else self.figure.subplots(2,1) if len(metrics)==2 else self.figure.subplots(1,1)).reshape(-1)
        style={name:('-' if j==0 else '--' if j==1 else ':') for j,name in enumerate(branches)}
        for ax,(key,title,unit,scale) in zip(self.axes,metrics):
            plotted=False
            for series in self._series:
                freq=_numeric(series['freq_hz'])/1e9
                if not len(freq):continue
                if key=='k':
                    values=_numeric(series.get('k'),len(freq))
                    groups=[('',values)]
                else:
                    groups=[(name,_numeric(values.get(key),len(freq))*scale) for name,values in series['branches'].items() if name in selected_branches]
                for branch,values in groups:
                    if not np.any(np.isfinite(values)):continue
                    label=series['label']+(' / '+branch.capitalize() if len(selected_branches)>1 and branch else '')
                    line,=ax.plot(freq,values,color=COLORS[series['label']],linestyle=style.get(branch,'-'),
                                  marker='o' if markers else None,markersize=2.7,lw=2.4 if series['label'] in ('Fit', 'After upgrade') else 1.6,alpha=1.0 if series['label'] in ('Fit', 'After upgrade') else .65,label=label,picker=5)
                    visibility=(view.get('legend_visibility') or {}).get(key+'|'+label)
                    if isinstance(visibility,bool):line.set_visible(visibility)
                    self._lines.append((ax,line,key,unit));plotted=True
                    if series['label']=='Source' and key in ('l_h','q','k') and view.get('show_tolerance',True):
                        targetkey={'l_h':'l_pct','q':'q_pct','k':'k_pct'}[key]
                        try:target=float(config.get(targetkey,''))/100
                        except (TypeError,ValueError):target=0.
                        if np.isfinite(target) and target>0:
                            band=np.abs(values)*target
                            ax.fill_between(freq,values-band,values+band,color=COLORS['Source'],alpha=.08,linewidth=0)
            ax.set_title(title+(' / '+desired.capitalize() if len(branches)>1 and desired!='all' and key!='k' else ''),loc='left',fontsize=10,fontweight='bold');ax.set_ylabel(unit)
            top_row=(len(metrics)==4 and list(self.axes).index(ax)<2) or (len(metrics)==2 and ax is self.axes[0])
            ax.set_xlabel('' if top_row else 'Frequency (GHz)')
            if top_row:ax.tick_params(axis='x',labelbottom=False)
            ax.set_xscale(xscale);ax.grid(bool(view.get('grid',True)),alpha=.2)
            if key in ('l_h','q','k'):ax.axhline(0,color='#9aa8b0',lw=.6,alpha=.6)
            if not plotted:
                message='Not applicable to a single winding.' if key=='k' and self._series else 'No sampled data available.'
                if key=='k' and self._series and len(branches)>1:message='k is undefined on the selected response domain.'
                ax.text(.5,.5,message,transform=ax.transAxes,ha='center',va='center',color=fw.DIM,fontsize=9,wrap=True)
            else:
                legend=ax.legend(loc='best',fontsize=7,framealpha=.85)
                if legend:
                    for legline,plotline in zip(legend.get_lines(),ax.get_lines()):
                        legline.set_picker(6);self._legend_map[legline]=plotline
            if view.get('show_srf',True) and key in ('l_h','q'):
                self._srf_markers(ax,selected_branches)
        self._auto_limits=[ax.get_xlim()+ax.get_ylim() for ax in self.axes]
        limits=old_limits or view.get('axis_limits')
        if isinstance(limits,list) and len(limits)==len(self.axes):
            for ax,lim in zip(self.axes,limits):
                if isinstance(lim,(list,tuple)) and len(lim)==4 and np.all(np.isfinite(lim)) and lim[0]<lim[1] and lim[2]<lim[3] and (xscale!='log' or lim[0]>0):
                    ax.set_xlim(lim[:2]);ax.set_ylim(lim[2:])
        if len(self._series)==3:
            self.message.set('Source / Before upgrade / After upgrade. Select a winding for a three-curve view.')
        elif self._series:
            self.message.set('Original SNP samples / Current circuit. Shading shows source SRF brackets and target bands.')
        else:
            self.message.set('Load a SNP, select its model, and confirm port mapping to preview L/Q/k.')
        if page.get('results_stale') or not page.get('source_valid',True):
            if self._series:self.message.set(self.message.get()+' Historical data: verify the source to continue.')
        self.toolbar.update();self.canvas.draw_idle()

    def _wrap_labels(self,event=None):
        width=max(220,self.winfo_width()-16)
        if width!=self._wrap_width:
            self._wrap_width=width;self.message_label.configure(wraplength=width);self.readout_label.configure(wraplength=width)

    def _selection_changed(self,event=None):
        if self.page is None:return
        labels={'Single winding':'winding','Primary':'primary','Secondary':'secondary','All windings':'all'}
        view=self.page.setdefault('view',{})
        view['winding']=labels.get(self.winding.get(),'winding');view['selected_metric']=self.metric.get()
        view.pop('axis_limits',None);self._source_identity=None
        self.set_data(self.page)

    def _srf_markers(self,ax,selected_branches=None):
        for series in self._series:
            srf=series.get('srf') or {}
            for branch,item in (srf.get('branches') or {}).items():
                if selected_branches and branch not in selected_branches:continue
                if series['label']=='Source':
                    bracket=item.get('bracket_hz')
                    if bracket and len(bracket)==2:
                        ax.axvspan(bracket[0]/1e9,bracket[1]/1e9,color=COLORS['Source'],alpha=.12)
                else:
                    value=item.get('model_hz')
                    if value:
                        ax.axvline(value/1e9,color=COLORS[series['label']],ls=':',lw=.8,alpha=.65)

    def _hover(self,event):
        if event.inaxes is None or event.x is None:return
        best=None
        for ax,line,key,unit in self._lines:
            if ax is not event.inaxes or not line.get_visible():continue
            x,y=line.get_data();points=np.column_stack((x,y));valid=np.all(np.isfinite(points),axis=1)
            if not np.any(valid):continue
            indices=np.flatnonzero(valid);screen=ax.transData.transform(points[valid]);dist=np.linalg.norm(screen-np.array([event.x,event.y]),axis=1)
            ix=int(np.argmin(dist));d=float(dist[ix])
            if best is None or d<best[0]:best=(d,line,indices[ix],unit)
        if best and best[0]<30:
            _,line,i,unit=best;x,y=line.get_data()
            self.readout.set('%s | %.7g GHz | %.7g %s (original sample)'%(line.get_label(),x[i],y[i],unit))

    def _pick(self,event):
        line=self._legend_map.get(event.artist)
        if line is not None:
            line.set_visible(not line.get_visible());event.artist.set_alpha(1. if line.get_visible() else .25);self.canvas.draw_idle()

    def capture_view_state(self):
        labels={'Single winding':'winding','Primary':'primary','Secondary':'secondary','All windings':'all'}
        output={'legend_visibility':{key+'|'+line.get_label():line.get_visible() for ax,line,key,unit in self._lines},
                'winding':labels.get(self.winding.get(),'winding'),'selected_metric':self.metric.get()}
        limits=[list(ax.get_xlim()+ax.get_ylim()) for ax in self.axes]
        if any(not np.allclose(lim,saved) for lim,saved in zip(limits,self._auto_limits)):
            output['axis_limits']=limits
        return output

    def destroy(self):
        for identifier in self._connections:self.canvas.mpl_disconnect(identifier)
        self.figure.clear()
        super().destroy()

    def export_plot(self,path):
        self.figure.savefig(path,dpi=180,bbox_inches='tight')

    def export_csv(self,path):
        with Path(path).open('w',encoding='utf-8',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['version','winding','frequency_hz','L_h','Q','R_ohm','effective_k'])
            for series in self._series:
                freq=_numeric(series['freq_hz']);k=_numeric(series.get('k'),len(freq))
                for branch,values in series['branches'].items():
                    arrays=[_numeric(values.get(key),len(freq)) for key in ('l_h','q','r_ohm')]
                    for i,f in enumerate(freq):
                        row=[series['label'],branch,float(f)]+[None if not np.isfinite(a[i]) else float(a[i]) for a in arrays]+[None if not np.isfinite(k[i]) else float(k[i])]
                        writer.writerow(row)


class CircuitView(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.page={};self.summary=tk.StringVar(value='No circuit generated yet.')
        self.summary_label=ttk.Label(self,textvariable=self.summary,wraplength=900);self.summary_label.pack(fill='x',padx=8,pady=(4,4))
        self._wrap_width=None;self.bind('<Configure>',self._wrap_labels,add='+')
        self.tabs=ttk.Notebook(self);self.tabs.pack(fill='both',expand=True)
        graph=ttk.Frame(self.tabs);details=ttk.Frame(self.tabs)
        self.tabs.add(graph,text='Connections');self.tabs.add(details,text='Parameters')
        zoom_bar=ttk.Frame(graph);zoom_bar.pack(fill='x',padx=4,pady=(0,4))
        ttk.Label(zoom_bar,text='Diagram scale').pack(side='left')
        self.scale=tk.StringVar(value='Fit')
        zoom=ttk.Combobox(zoom_bar,textvariable=self.scale,values=['Fit','75%','100%','125%','150%','200%'],state='readonly',width=7)
        zoom.pack(side='left',padx=8);zoom.bind('<<ComboboxSelected>>',self._scale_changed)
        self.zoom_hint=ttk.Label(zoom_bar,text='Fit shows both circuits. Zoom for detail; all values are in the next tab.',wraplength=680);self.zoom_hint.pack(side='left',padx=4)
        scroll_frame=ttk.Frame(graph);scroll_frame.pack(fill='both',expand=True)
        self.scroller=tk.Canvas(scroll_frame,background='white',highlightthickness=0)
        sy=ttk.Scrollbar(scroll_frame,orient='vertical',command=self.scroller.yview)
        sx=ttk.Scrollbar(scroll_frame,orient='horizontal',command=self.scroller.xview)
        self.scroller.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self._fit_extent=None;self._fit_after=None
        self.scroller.bind('<Configure>',self._viewport_resized,add='+')
        self.scroller.bind('<Map>',self._viewport_resized,add='+')
        self.scroller.grid(row=0,column=0,sticky='nsew');sy.grid(row=0,column=1,sticky='ns');sx.grid(row=1,column=0,sticky='ew')
        scroll_frame.rowconfigure(0,weight=1);scroll_frame.columnconfigure(0,weight=1)
        self.figure=Figure(figsize=(11.5,5.4),dpi=100,facecolor=fw.PLOT_BG,constrained_layout=True)
        self.canvas=FigureCanvasTkAgg(self.figure,master=self.scroller)
        self.canvas_window=self.scroller.create_window(0,0,anchor='nw',window=self.canvas.get_tk_widget())
        # Preserve TkAgg's own resize handler: it resizes both the PhotoImage
        # and the FigureCanvas backing widget when a previously hidden tab maps.
        self.canvas.get_tk_widget().bind('<Configure>',lambda event:self.scroller.configure(scrollregion=self.scroller.bbox('all')),add='+')
        self.toolbar=NavigationToolbar2Tk(self.canvas,graph,pack_toolbar=False);self.toolbar.update();self.toolbar.pack(fill='x')
        columns=('change','kind','name','from','to','before','after')
        self.parameters=ttk.Treeview(details,columns=columns,show='headings',selectmode='browse')
        for key,label,width in [('change','Change',95),('kind','Device',70),('name','Instance',130),('from','Node / inductor 1',150),('to','Node / inductor 2',150),('before','Before / current value',160),('after','After value',150)]:
            self.parameters.heading(key,text=label);self.parameters.column(key,width=width,minwidth=65,stretch=True)
        yscroll=ttk.Scrollbar(details,orient='vertical',command=self.parameters.yview);xscroll=ttk.Scrollbar(details,orient='horizontal',command=self.parameters.xview)
        self.parameters.configure(yscrollcommand=yscroll.set,xscrollcommand=xscroll.set)
        self.parameters.grid(row=0,column=0,sticky='nsew');yscroll.grid(row=0,column=1,sticky='ns');xscroll.grid(row=1,column=0,sticky='ew')
        details.rowconfigure(0,weight=1);details.columnconfigure(0,weight=1)
        for tag,color in DIFF_COLORS.items():self.parameters.tag_configure(tag,foreground=color)
        self.parameters.bind('<<TreeviewSelect>>',self._parameter_selected)
        self.detail=tk.StringVar(value='Select a device to read its exact value and connected nets.')
        self.detail_label=ttk.Label(details,textvariable=self.detail,wraplength=900);self.detail_label.grid(row=2,column=0,columnspan=2,sticky='ew',padx=8,pady=4)
        from .winding_view import WindingPages
        self.physical=WindingPages(self.tabs)
        for index,(frame,label) in enumerate(((self.physical.schematic,'Winding'),
                                             (self.physical.symbol,'Symbol'),
                                             (self.physical.quality,'Segment Q'))):
            self.tabs.insert(index,frame,text=label)
        self.tabs.select(0)
        self.set_data({})

    def set_data(self,page):
        self.page=page;results=_results(page);before=results.get('before_upgrade');after=results.get('upgraded');current=results.get('current') or results.get('quick')
        bc=(before or {}).get('circuit');ac=(after or {}).get('circuit');cc=(current or {}).get('circuit')
        self.figure.clear();self.parameters.delete(*self.parameters.get_children());self._parameter_rows={}
        if bc and ac:
            difference=circuit_diff(bc,ac);axes=self.figure.subplots(1,2)
            shared=circuit_layout(ac)['nodes']
            draw_circuit(axes[0],bc,'Before upgrade',difference,'before',shared)
            draw_circuit(axes[1],ac,'After upgrade',difference,'after',shared)
            self.summary.set('+%d added / -%d removed; %d changed; %d rewired.'%(len(difference['added']),len(difference['removed']),len(difference['changed']),len(difference['rewired'])))
            old={e['name']:e for e in bc['elements']};new={e['name']:e for e in ac['elements']}
            for name in sorted(set(old)|set(new)):
                element=new.get(name) or old[name]
                change=next((key for key in ('added','removed','changed') if name in difference[key]),'unchanged')
                a=element.get('n1',element.get('ind1',''));b=element.get('n2',element.get('ind2',''))
                if name in difference['rewired']:
                    a=str(old[name].get('n1',old[name].get('ind1')))+' -> '+str(a)
                    b=str(old[name].get('n2',old[name].get('ind2')))+' -> '+str(b)
                previous=engineering(old[name]['value'],old[name]['kind']) if name in old else '—'
                following=engineering(new[name]['value'],new[name]['kind']) if name in new else '—'
                values=(change.capitalize(),element['kind'],name,a,b,previous,following)
                item=self.parameters.insert('', 'end',values=values,tags=(change,));self._parameter_rows[item]=(old.get(name),new.get(name))
        else:
            ax=self.figure.add_subplot(111);draw_circuit(ax,cc,'Current circuit')
            if cc:
                counts=_counts(cc);candidate=cc.get('metadata',{}).get('candidate','custom')
                self.summary.set('%s | R%d L%d C%d K%d | Exact values: All device values tab.'%(candidate,counts['res'],counts['ind'],counts['cap'],counts['mind']))
                for element in cc['elements']:
                    values=('Current',element['kind'],element['name'],element.get('n1',element.get('ind1','')),element.get('n2',element.get('ind2','')),engineering(element['value'],element['kind']),'—')
                    item=self.parameters.insert('','end',values=values,tags=('unchanged',));self._parameter_rows[item]=(element,None)
            else:self.summary.set('Generate an initial circuit to inspect exact connections and device values.')
        view=page.get('view') or {}
        saved_scale=view.get('diagram_scale','Fit')
        self.scale.set(saved_scale if saved_scale in ('Fit','75%','100%','125%','150%','200%') else 'Fit')
        self._fit_extent=None
        self._scale_changed()
        scroll=view.get('diagram_scroll')
        if isinstance(scroll,list) and len(scroll)==2:
            self.scroller.xview_moveto(max(0,min(1,float(scroll[0]))));self.scroller.yview_moveto(max(0,min(1,float(scroll[1]))))
        self.toolbar.update();self.canvas.draw_idle()
        self.physical.set_data(page)
        try:self.tabs.select(int(view.get('circuit_panel',0)))
        except (ValueError,TypeError,tk.TclError):pass

    def _parameter_selected(self,event=None):
        selection=self.parameters.selection()
        if not selection:return
        before,after=self._parameter_rows.get(selection[0],(None,None))
        pieces=[]
        for label,element in [('Before/current',before),('After',after)]:
            if element:
                a=element.get('n1',element.get('ind1'));b=element.get('n2',element.get('ind2'))
                pieces.append('%s: %s %s (%s -> %s), exact value %.16g'%(label,element['kind'],element['name'],a,b,float(element['value'])))
        self.detail.set(' | '.join(pieces))

    def _wrap_labels(self,event=None):
        width=max(220,self.winfo_width()-16)
        if width!=self._wrap_width:
            self._wrap_width=width
            self.summary_label.configure(wraplength=width)
            if hasattr(self,'detail_label'):self.detail_label.configure(wraplength=width)
            if hasattr(self,'zoom_hint'):self.zoom_hint.configure(wraplength=max(180,width-205))

    def _viewport_resized(self,event=None):
        if self.scale.get()!='Fit':return
        extent=(self.scroller.winfo_width(),self.scroller.winfo_height())
        if extent==self._fit_extent or min(extent)<20 or not self.scroller.winfo_ismapped():return
        if self._fit_extent is None:
            self._apply_fit();return
        if self._fit_after is not None:
            self.after_cancel(self._fit_after)
        self._fit_after=self.after_idle(self._apply_fit)

    def _apply_fit(self):
        self._fit_after=None
        if self.scale.get()!='Fit' or not self.winfo_exists():return
        if not self.scroller.winfo_ismapped():
            self._fit_extent=None
            return
        width,height=self.scroller.winfo_width(),self.scroller.winfo_height()
        if min(width,height)<20:return
        extent=(width,height)
        if extent==self._fit_extent:return
        self._fit_extent=extent
        self.figure.set_dpi(100)
        self.figure.set_size_inches(width/100.,height/100.,forward=False)
        self.canvas.get_tk_widget().configure(width=width,height=height)
        self.scroller.itemconfigure(self.canvas_window,width=width,height=height)
        self.scroller.configure(scrollregion=(0,0,width,height))
        self.scroller.xview_moveto(0);self.scroller.yview_moveto(0)
        self.canvas.draw_idle()

    def _scale_changed(self,event=None):
        self.page.setdefault('view',{})['diagram_scale']=self.scale.get()
        if self._fit_after is not None:
            self.after_cancel(self._fit_after);self._fit_after=None
        if self.scale.get()=='Fit':
            self._fit_extent=None;self._apply_fit()
            return
        try:scale=float(self.scale.get().rstrip('%'))/100
        except ValueError:scale=1.
        self._fit_extent=None
        self.figure.set_size_inches(11.5,5.4,forward=False)
        self.figure.set_dpi(100*scale)
        width,height=self.figure.get_size_inches()*self.figure.dpi
        self.canvas.get_tk_widget().configure(width=int(width),height=int(height))
        self.scroller.itemconfigure(self.canvas_window,width=int(width),height=int(height))
        self.scroller.configure(scrollregion=(0,0,int(width),int(height)))
        self.canvas.draw_idle()

    def capture_view_state(self):
        return {'diagram_scale':self.scale.get(),'diagram_scroll':[self.scroller.xview()[0],self.scroller.yview()[0]],
                'circuit_panel':self.tabs.index(self.tabs.select()), **self.physical.capture_view_state()}

    def destroy(self):
        if self._fit_after is not None:
            self.after_cancel(self._fit_after);self._fit_after=None
        self.figure.clear()
        self.physical.destroy()
        super().destroy()

    def export_figure(self,path):
        panel=self.tabs.index(self.tabs.select())
        if panel<3:self.physical.export_figure(path,symbol=panel==1)
        else:self.figure.savefig(path,dpi=200,bbox_inches='tight')
