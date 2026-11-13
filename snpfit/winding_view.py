"""Layout-inspired previews of the unchanged fitted circuit and its local Q."""
from __future__ import annotations

import fieldwork_tk as fw
fw.apply_matplotlib()
import csv
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


COLORS = tuple(fw.SERIES)


def local_q_text(row):
    value = row.get('q', row.get('Q'))
    if value is None:
        return 'Infinity' if 'lossless' in str(row.get('status', '')).lower() else 'N/A'
    if math.isinf(float(value)):
        return 'Infinity'
    return '%.4g' % value


def draw_winding(ax, plan, circuit, title='Winding schematic', *, symbol=False, differences=None, side='after'):
    """Render the common native/preview plan, never a replacement circuit."""
    from .studio_views import engineering, DIFF_COLORS
    ax.clear()
    graphic = plan.get('symbol', {}) if symbol else plan
    paths = graphic.get('paths', [])
    for path in paths:
        points = np.asarray(path['points'], dtype=float)
        if len(points) < 2:
            continue
        raw_winding=path.get('winding',0)
        winding={'primary':0,'secondary':1}.get(raw_winding,raw_winding)
        try:winding=int(winding)
        except (TypeError,ValueError):winding=0
        role=path.get('role','winding')
        color=COLORS[winding % len(COLORS)]
        style='-';width=1.7 if symbol else 2.1;alpha=.9 if symbol else .24
        if role=='association':color=fw.DIM;style='--';width=.7;alpha=.6
        elif role=='annotation_box':color='#aebbc5';width=.7;alpha=.7
        elif role=='boundary':width=1.;alpha=.65
        elif role=='segment':width=2.2;alpha=.42
        ax.plot(points[:, 0], points[:, 1], color=color,
                lw=width, alpha=alpha, linestyle=style, zorder=1)
    elements = {e['name']: e for e in circuit.get('elements', [])}
    if not symbol:
        dense = len(elements) > 18
        for placement in plan.get('placements', []):
            if placement.get('group') not in ('winding','loss_auxiliary'):
                continue
            element = elements[placement['name']]
            center = np.asarray(placement['center'], dtype=float)
            color = fw.TEXT
            if differences:
                key = 'removed' if side == 'before' else 'added'
                if element['name'] in differences.get(key, []):
                    color = DIFF_COLORS[key]
                elif element['name'] in differences.get('changed', []):
                    color = DIFF_COLORS['changed']
            if element['kind'] == 'mind':
                ax.plot(center[0], center[1], 'o', ms=3.5, mfc='white', mec=color, zorder=3)
                ax.text(center[0]+.6, center[1], element['name'], color=color, fontsize=6.2, va='center')
                continue
            angle = int(str(placement.get('orientation', 'R90'))[1:])
            # Common plans describe the desired path axis; native placement
            # derives the corresponding rotation from each real master.
            direction = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
            span = float(placement.get('symbol_length', .8))
            if placement.get('group')=='winding':span*=2.0
            draw_passive(ax, center, direction, span, element['kind'], color)
            label = element['name']
            if not dense or placement.get('group')=='winding':
                label += '\n'+engineering(element['value'], element['kind'])
            ax.text(center[0], center[1]+.65, label, ha='center', va='bottom',
                    fontsize=5.8 if dense else 6.6, color=color,
                    bbox={'facecolor':fw.PLOT_BG,'edgecolor':'none','alpha':.8,'pad':.3}, zorder=4)
        for label in plan.get('labels', []):
            role = label.get('role', '')
            if role.startswith('q_table') or role in ('device', 'caption', 'title', 'heading', 'panel_heading'):
                continue
            p = label['position']
            ax.text(p[0], p[1], label['text'], fontsize=7 if role == 'segment_q' else 6.4,
                    ha='center', va='center', color=fw.TEXT,
                    bbox={'facecolor':fw.PLOT_BG,'edgecolor':'none','alpha':.9,'pad':.4}, zorder=5)
    for pin in graphic.get('pins', []):
        p = np.asarray(pin['position'], dtype=float)
        direction = pin.get('direction', 'left')
        delta = {'left':(-.35,0), 'right':(.35,0), 'top':(0,.35), 'bottom':(0,-.35)}.get(direction, (0,-.35))
        ax.plot(p[0], p[1], 'o', ms=4, color=fw.TEXT, zorder=6)
        name = pin['name']
        ax.annotate(name, p, xytext=(p[0]+delta[0], p[1]+delta[1]), textcoords='data',
                    ha='right' if direction=='left' else 'left' if direction=='right' else 'center',
                    va='bottom' if direction=='top' else 'top' if direction=='bottom' else 'center',
                    fontsize=7.5, color=fw.TEXT, fontweight='bold', zorder=6,
                    bbox={'facecolor':fw.PLOT_BG,'edgecolor':'none','alpha':.85,'pad':.4})
    bounds = graphic.get('plot_bounds', graphic.get('bounds'))
    if not symbol:
        points=[p for path in paths for p in path['points']]
        points.extend(p['position'] for p in graphic.get('pins',[]))
        for label in plan.get('labels',[]):
            if label.get('role') in ('segment_q','segment_tag','card_note'):points.append(label['position'])
        if points:
            bounds=[min(p[0] for p in points)-1.5,min(p[1] for p in points)-1.5,
                    max(p[0] for p in points)+1.5,max(p[1] for p in points)+1.5]
    if bounds:
        x0, y0, x1, y1 = bounds
        margin = .06*max(x1-x0,y1-y0,1)
        ax.set_xlim(x0-margin,x1+margin);ax.set_ylim(y0-margin,y1+margin)
    ax.set_aspect('equal', adjustable='box')
    ax.set_title(title, fontsize=10, loc='left')
    ax.set_axis_off()
    return {'placements':len(plan.get('placements', [])), 'pins':len(graphic.get('pins', []))}


def draw_passive(ax, center, direction, span, kind, color):
    """A legible glyph on the winding; native exports use real analogLib masters."""
    normal=np.array([-direction[1],direction[0]])
    def plot(along,across):
        points=center+np.asarray(along)[:,None]*direction+np.asarray(across)[:,None]*normal
        ax.plot(points[:,0],points[:,1],color=color,lw=1.1,zorder=3)
    amplitude=.2*span
    if kind=='res':
        x=np.linspace(-span/2,span/2,11);y=np.array([0,0,1,-1,1,-1,1,-1,1,0,0])*amplitude
        plot(x,y)
    elif kind=='ind':
        t=np.linspace(0,1,100);x=(t-.5)*span
        y=amplitude*np.abs(np.sin(4*np.pi*t))
        plot(x,y)
    elif kind=='cap':
        plot([-span/2,-span*.1],[0,0]);plot([span*.1,span/2],[0,0])
        for x in (-span*.1,span*.1):plot([x,x],[-amplitude,amplitude])


class WindingPages:
    """Three sibling notebook pages; all data stays in the parent SNP page."""
    def __init__(self, notebook):
        self.notebook = notebook
        self.page = {}
        self.plans = []
        self.schematic = ttk.Frame(notebook)
        self.symbol = ttk.Frame(notebook)
        self.quality = ttk.Frame(notebook)
        self.caption = tk.StringVar(value='Choose a model to preview its winding structure.')
        self.symbol_caption = tk.StringVar(value='Symbol pins follow the same layout reference as the schematic.')
        self.quality_caption = tk.StringVar(value='Generate an initial circuit to inspect local segment loss.')
        self.figures = []
        self.canvases = []
        self.toolbars=[]
        self._auto_limits=[[],[]]
        for frame, caption in ((self.schematic, self.caption), (self.symbol, self.symbol_caption)):
            text = ttk.Label(frame, textvariable=caption, wraplength=900, foreground=fw.DIM)
            text.pack(fill='x', padx=8, pady=4)
            frame.bind('<Configure>',lambda e,label=text:label.configure(wraplength=max(200,e.width-18)),add='+')
            figure = Figure(figsize=(8.5,5),dpi=100,facecolor=fw.PLOT_BG,constrained_layout=True)
            canvas = FigureCanvasTkAgg(figure, master=frame)
            toolbar = NavigationToolbar2Tk(canvas, frame, pack_toolbar=False)
            toolbar.update();toolbar.pack(side='bottom',fill='x')
            canvas.get_tk_widget().pack(fill='both',expand=True)
            self.figures.append(figure);self.canvases.append(canvas)
            self.toolbars.append(toolbar)
        label = ttk.Label(self.quality,textvariable=self.quality_caption,wraplength=900,foreground=fw.DIM)
        label.pack(fill='x',padx=8,pady=8)
        self.quality.bind('<Configure>',lambda e:label.configure(wraplength=max(200,e.width-18)),add='+')
        columns=('rank','segment','winding','main_l','q','l_eff','r_eff','status')
        holder=ttk.Frame(self.quality);holder.pack(fill='both',expand=True,padx=8)
        self.table=ttk.Treeview(holder,columns=columns,show='headings',height=10)
        for key,title,width in [('rank','Rank',45),('segment','Segment',90),('winding','Winding',75),
                                ('main_l','Main L',90),('q','Local Q',70),
                                ('l_eff','L (nH)',80),('r_eff','R (Ohm)',85),('status','Status',85)]:
            self.table.heading(key,text=title);self.table.column(key,width=width,minwidth=40,stretch=True)
        self.table.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(holder,orient='vertical',command=self.table.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(holder,orient='horizontal',command=self.table.xview);horizontal.grid(row=1,column=0,sticky='ew')
        holder.rowconfigure(0,weight=1);holder.columnconfigure(0,weight=1)
        self.table.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        self.detail=tk.StringVar(value='Select a segment to see its exact assigned devices.')
        ttk.Label(self.quality,textvariable=self.detail,wraplength=900).pack(fill='x',padx=8,pady=8)
        self.table.bind('<<TreeviewSelect>>',self._select)
        footer=ttk.Frame(self.quality);footer.pack(fill='x',padx=8,pady=4)
        ttk.Button(footer,text='Export segment Q CSV...',command=self.export_quality).pack(side='right')
        note=ttk.Label(footer,text='Local Q excludes mutual coupling and shunt loading. Exported annotations are snapshots.',
                  wraplength=650,foreground=fw.DIM);note.pack(side='left',fill='x',expand=True)
        footer.bind('<Configure>',lambda e:note.configure(wraplength=max(150,e.width-200)),add='+')
        self._rows={}

    def set_data(self, page):
        from .models import get_model
        from .winding_visual import build_visual
        from .segment_q import rank_segments
        from .studio_views import circuit_diff
        self.page=page
        cfg=page.get('config') or {};results=page.get('results') or {}
        current=results.get('current') or results.get('quick') or {}
        snapshots=[]
        if (results.get('before_upgrade') or {}).get('circuit') and (results.get('upgraded') or {}).get('circuit'):
            snapshots=[('Before upgrade',results['before_upgrade']),('After upgrade',results['upgraded'])]
        elif current.get('circuit'):
            snapshots=[('Current circuit',current)]
        elif cfg.get('model_id'):
            spec=get_model(cfg['model_id'])
            snapshots=[('Selected structure',{'circuit':{'ports':list(spec.port_roles),'reference':'REF','elements':[],
                         'metadata':{'model_id':spec.id,'export_reference':spec.explicit_reference}}})]
        self.plans=[]
        try:
            lo,hi=float(cfg.get('fmin_ghz',1)),float(cfg.get('fmax_ghz',50))
            raw=cfg.get('q_annotation_ghz','')
            frequency=float(raw) if str(raw).strip() else math.sqrt(lo*hi)
            if not math.isfinite(frequency) or not 0<lo<=frequency<=hi:
                raise ValueError('Segment Q frequency must be within the fitting band.')
            order=cfg.get('port_order') or None
            if isinstance(order,str):order=[int(v.strip()) for v in order.split(',') if v.strip()]
            for title,snapshot in snapshots:
                circuit=snapshot['circuit'];model=circuit.get('metadata',{}).get('model_id',cfg.get('model_id'))
                source=snapshot.get('source') or page.get('source') or {}
                plan=build_visual(circuit,model,reference_id=cfg.get('visual_reference','auto'),
                                  source_sha256=source.get('sha256'),port_order=order,
                                  short_unused=bool(cfg.get('short_unused')),q_frequency_hz=frequency*1e9)
                self.plans.append((title,plan,circuit))
        except (ValueError,TypeError,KeyError) as error:
            self.caption.set('Drawing settings: '+str(error))
            return
        differences=circuit_diff(snapshots[0][1]['circuit'],snapshots[1][1]['circuit']) if len(snapshots)==2 else None
        for figure_index,(symbol,figure,canvas) in enumerate(zip((False,True),self.figures,self.canvases)):
            figure.clear()
            if not self.plans:
                ax=figure.add_subplot(111);ax.text(.5,.5,'Load a SNP and choose its model to preview the winding.',ha='center',va='center',transform=ax.transAxes);ax.set_axis_off()
            else:
                axes=np.atleast_1d(figure.subplots(1,len(self.plans)))
                for i,(title,plan,circuit) in enumerate(self.plans):
                    draw_winding(axes[i],plan,circuit,title if not symbol else title+' / symbol',symbol=symbol,
                                 differences=differences,side='before' if i==0 else 'after')
            self._auto_limits[figure_index]=[list(ax.get_xlim())+list(ax.get_ylim()) for ax in figure.axes]
            saved=(page.get('view') or {}).get('symbol_axis_limits' if symbol else 'winding_axis_limits')
            if isinstance(saved,list) and len(saved)==len(figure.axes):
                for ax,limits in zip(figure.axes,saved):
                    if isinstance(limits,list) and len(limits)==4 and all(math.isfinite(float(v)) for v in limits):
                        ax.set_xlim(limits[0],limits[1]);ax.set_ylim(limits[2],limits[3])
            self.toolbars[figure_index].update();canvas.draw_idle()
        self.table.delete(*self.table.get_children());self._rows={}
        rows=[]
        if self.plans:
            _,plan,_=self.plans[-1]
            label=plan.get('reference_label',plan.get('reference_id','Model shape'))
            self.caption.set('%s | Local segment Q @ %.5g GHz. Shunt/coupling details: Connections and Parameters.'%(label,frequency))
            self.symbol_caption.set('%s | Same winding shape and pin anchors; no fitted values are changed.'%label)
            rows=plan.get('segments',[])
        else:
            self.caption.set('Choose a model to preview its winding structure.')
        variant=self.plans[-1][0] if self.plans else 'Current circuit'
        self.quality_caption.set('%s: lowest segment Q, ascending (up to 10) @ %.5g GHz. Main winding segments; auxiliary loss branches stay with their segment.'%(variant,frequency))
        for index,row in enumerate(rank_segments(rows,limit=10),1):
            q=row.get('q',row.get('Q'));l=row.get('l_effective_h',row.get('l_effective'));r=row.get('z_real')
            winding={'primary':'Primary','secondary':'Secondary',0:'Primary',1:'Secondary'}.get(row.get('winding'),'Winding')
            values=(index,row.get('id',''),winding,row.get('main_l',''),local_q_text(row),
                    'N/A' if l is None else '%.5g'%(l*1e9),'N/A' if r is None else '%.5g'%r,row.get('status',''))
            item=self.table.insert('','end',values=values);self._rows[item]=row

    def _select(self,event=None):
        selection=self.table.selection()
        if not selection:return
        row=self._rows.get(selection[0],{})
        self.detail.set('%s: %s → %s | Devices: %s'%(row.get('id',''),row.get('start_net',''),row.get('end_net',''),', '.join(row.get('element_names',[]))))

    def export_quality(self,path=None):
        if path is None:path=filedialog.asksaveasfilename(parent=self.quality,title='Export local segment Q',defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if not path:return
        try:
            with Path(path).open('w',newline='',encoding='utf-8') as handle:
                fields=['variant','frequency_hz','segment','winding','main_l','q','z_real_ohm','z_imag_ohm','l_effective_h','status','devices']
                writer=csv.writer(handle);writer.writerow(fields)
                for title,plan,_ in self.plans:
                    for row in plan.get('segments',[]):
                        writer.writerow([title,plan.get('q_frequency_hz'),row.get('id'),row.get('winding'),row.get('main_l'),
                                         local_q_text(row),row.get('z_real'),row.get('z_imag'),row.get('l_effective_h'),row.get('status'),';'.join(row.get('element_names',[]))])
        except OSError as error:
            messagebox.showerror('Export failed',str(error),parent=self.quality)

    def export_figure(self,path,symbol=False):
        self.figures[1 if symbol else 0].savefig(path,dpi=200,bbox_inches='tight')

    def capture_view_state(self):
        output={}
        for index,(key,figure) in enumerate(zip(('winding_axis_limits','symbol_axis_limits'),self.figures)):
            limits=[list(ax.get_xlim())+list(ax.get_ylim()) for ax in figure.axes]
            auto=self._auto_limits[index]
            if len(limits)==len(auto) and all(np.allclose(a,b,rtol=1e-10,atol=1e-10) for a,b in zip(limits,auto)):
                self.page.setdefault('view',{}).pop(key,None)
            else:output[key]=limits
        return output

    def destroy(self):
        for figure in self.figures:figure.clear()
