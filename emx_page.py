import fieldwork_tk as fw
from fieldwork_ui import bind_status
from plot_theme import value_color
"""Independent Tk EMX campaign workspace; all native work belongs to Campaign."""
import copy
import csv
import json
import math
import os
from pathlib import Path
import queue
import threading
import uuid
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

BG = fw.PANEL
INK = fw.TEXT
MUTED = fw.DIM
CYAN = fw.accent('flux')
METRICS = ('L', 'Q', 'Lp', 'Ls', 'k', 'Qp', 'Qs')
SERIES_COLORS = tuple(fw.SERIES)


def _number(value, name, integer=False):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number.' % name)
    if not math.isfinite(n) or (integer and not n.is_integer()):
        raise ValueError('%s must be a finite %s.' % (name, 'integer' if integer else 'number'))
    return int(n) if integer else n


def _choice(text, field):
    kind = field.get('type') or ('enum' if field.get('options') or field.get('choices') else 'float')
    if text.strip().lower() in ('none', 'null') and field.get('optional'):
        return None
    if kind in ('bool', 'boolean'):
        if text.strip().lower() not in ('true', 'false'):
            raise ValueError('Boolean choices must be true or false.')
        return text.strip().lower() == 'true'
    if kind in ('enum', 'choice', 'select', 'string', 'str'):
        options = [x.get('id', x.get('value')) if isinstance(x, dict) else x for x in (field.get('options') or field.get('choices') or [])]
        if options and text not in options:
            raise ValueError('Choice %s is not allowed for %s.' % (text, field['name']))
        return text
    return _number(text, field['name'], kind == 'int')


def axis_from_editor(field, values):
    """Translate editor contents without silently coercing types."""
    sampling = values['sampling']
    axis = dict(name=field['name'], enabled=True, sampling=sampling, group=values.get('group', '').strip())
    if sampling == 'choices':
        axis['values'] = [_choice(s.strip(), field) for s in values.get('values', '').split(',') if s.strip()]
        if not axis['values']:
            raise ValueError('Enter at least one choice.')
    else:
        integer = field.get('type') == 'int'
        axis['minimum'] = _number(values.get('minimum'), 'Minimum', integer)
        axis['maximum'] = _number(values.get('maximum'), 'Maximum', integer)
        if sampling == 'step':
            axis['step'] = _number(values.get('step'), 'Step', integer)
        else:
            axis['count'] = _number(values.get('count'), 'Count', True)
    return axis


def frequency_list(start, stop, mode, amount):
    if mode == 'single':
        value = _number(start, 'Frequency')
        if value <= 0: raise ValueError('Frequency must be positive.')
        return [value]
    if mode == 'list':
        parts = str(start).replace(',', ' ').split()
        if not parts or len(parts) > 256: raise ValueError('Enter 1–256 frequencies.')
        values = [_number(part, 'Frequency') for part in parts]
        if any(value <= 0 for value in values): raise ValueError('Frequencies must be positive.')
        return values
    start, stop = _number(start, 'Start frequency'), _number(stop, 'Stop frequency')
    if start <= 0 or stop < start:
        raise ValueError('Frequency range must be positive and ordered.')
    if mode in ('count', 'linear', 'log'):
        count = _number(amount, 'Frequency count', True)
        if count < 1 or count > 256 or (count == 1 and start != stop):
            raise ValueError('Count must be 1–256; one point requires equal endpoints.')
        if mode == 'log' and count < 2: raise ValueError('Log sampling requires at least two points.')
        if mode == 'log':
            ratio=stop/start
            if math.isfinite(ratio):
                points=[start * ratio**(i/(count-1)) for i in range(count)]
            else:
                points=[math.exp(math.log(start) + (math.log(stop)-math.log(start))*i/(count-1)) for i in range(count)]
            points[0],points[-1]=start,stop
            return points
        return [start] if count == 1 else [start + (stop-start)*i/(count-1) for i in range(count)]
    if mode != 'step': raise ValueError('Unknown frequency mode.')
    step = _number(amount, 'Frequency step')
    if step <= 0:
        raise ValueError('Frequency step must be positive.')
    count = int(math.floor((stop-start)/step + 1e-12)) + 1
    if count > 256:
        raise ValueError('Frequency range exceeds 256 points.')
    return [start+i*step for i in range(count)]


class ScrollSettings(ttk.Frame):
    """Scrollable settings area, including at the application's minimum size."""
    def __init__(self, parent, padding=0):
        super().__init__(parent, style='Panel.TFrame')
        self.canvas=tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        scroll=ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        self.content=ttk.Frame(self.canvas,style='Panel.TFrame',padding=padding)
        self.window=self.canvas.create_window(0,0,anchor='nw',window=self.content)
        self.last_size=None
        self._wheel_tag='SidebarWheel%s' % id(self)
        self._wheel_sequences=('<MouseWheel>','<Button-4>','<Button-5>')
        self._wheel_commands=[]
        self.content.bind('<Configure>',self._resize_content)
        self.canvas.bind('<Configure>',self._resize_canvas)

    def _resize_content(self,event):
        size=(event.width,event.height)
        if size != self.last_size:
            self.last_size=size;self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _resize_canvas(self,event):
        if int(self.canvas.itemcget(self.window,'width') or 0) != event.width:
            self.canvas.itemconfigure(self.window,width=event.width)

    def install_wheel_bindings(self):
        tag=self._wheel_tag
        if not self._wheel_commands:
            def scroll(event):
                steps=(-1 if event.num==4 else 1) if event.num in (4,5) else (-1 if event.delta>0 else 1)
                self.canvas.yview_scroll(steps,'units')
                return 'break'
            for sequence in self._wheel_sequences:
                self._wheel_commands.append(self.bind_class(tag,sequence,scroll))
        def attach(widget):
            if isinstance(widget,(tk.Text,tk.Listbox,ttk.Treeview)):
                return
            tags=widget.bindtags()
            if tag not in tags:widget.bindtags((tag,)+tags)
            for child in widget.winfo_children():attach(child)
        attach(self)

    def destroy(self):
        if self._wheel_commands:
            for sequence in self._wheel_sequences:self.unbind_class(self._wheel_tag,sequence)
            # bind_class registers Tcl commands on the root, not on this frame.
            root=self._root()
            for command in self._wheel_commands:root.deletecommand(command)
            self._wheel_commands=[]
        super().destroy()


class Plot(tk.Canvas):
    """Draw only measured points; pan and zoom do not alter data."""
    def __init__(self, parent, select):
        super().__init__(parent, background=fw.PLOT_BG, highlightthickness=1, highlightbackground='#2e4355')
        self.select = select
        self.points = []
        self.view = None
        self.manual_view = False
        self.drag = None
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<ButtonPress-1>', self.press)
        self.bind('<B1-Motion>', self.pan)
        self.bind('<ButtonRelease-1>', self.release)
        self.bind('<Motion>', self.hover)
        self.bind('<MouseWheel>', self.wheel)
        self.bind('<Button-4>', self.wheel)
        self.bind('<Button-5>', self.wheel)
        self.readout = tk.StringVar(value='')
        self.moved = False
        self.highlight = None
        self.x_grid=[]; self.y_grid=[]; self.surface_points=[]; self.triangles=[]; self.zlabel=''; self.surface_axes=None; self.azimuth=-.7; self.elevation=.5; self.surface_zoom=1.; self.surface_screen=[]
        self.xlabel = ''; self.ylabel = ''; self.chart_type='line'; self.x_ticks=[]; self.y_ticks=[]; self.breaks=set(); self.color_label=''; self.color_values={}; self.color_range=None; self.top_margin=20

    def set_points(self, points, xlabel, ylabel):
        axes_changed = self.xlabel != xlabel or self.ylabel != ylabel
        changed = self.points != points or axes_changed
        self.points, self.xlabel, self.ylabel = points, xlabel, ylabel
        if axes_changed or not points or (changed and not self.manual_view): self.reset()
        else: self.draw()

    def reset(self):
        self.azimuth=-.7; self.elevation=.5; self.surface_zoom=1.
        self.manual_view = False
        finite = [(p[0], p[1]) for p in self.points if math.isfinite(p[0]) and math.isfinite(p[1])]
        if finite:
            xs, ys = zip(*finite)
            if self.chart_type=='heatmap' and self.x_grid and self.y_grid:
                xb=self._grid_edges(self.x_grid);yb=self._grid_edges(self.y_grid)
                self.view=[xb[0],xb[-1],yb[0],yb[-1]];self.draw();return
            dx, dy = max(xs)-min(xs), max(ys)-min(ys)
            dx, dy = dx or max(abs(xs[0])*.1, 1), dy or max(abs(ys[0])*.1, 1)
            self.view = [min(xs)-dx*.08, max(xs)+dx*.08, min(ys)-dy*.08, max(ys)+dy*.08]
        else:
            self.view = None
        self.draw()

    def xy(self, x, y):
        a,b,c,d = self.view
        w,h = max(1,self.winfo_width()-85), max(1,self.winfo_height()-self.top_margin-40)
        return 65+(x-a)/(b-a)*w, self.top_margin+(d-y)/(d-c)*h

    @staticmethod
    def _grid_edges(grid):
        values=sorted(set(grid))
        if len(values)==1:
            half=max(abs(values[0])*.05,.5)
            return [values[0]-half,values[0]+half]
        mids=[(a+b)/2 for a,b in zip(values,values[1:])]
        return [values[0]-(mids[0]-values[0])]+mids+[values[-1]+(values[-1]-mids[-1])]

    def _cell_bounds(self,x,y):
        xs=sorted(set(self.x_grid));ys=sorted(set(self.y_grid))
        if x not in xs or y not in ys:return None
        xi=xs.index(x);yi=ys.index(y);xe=self._grid_edges(xs);ye=self._grid_edges(ys)
        p=self.xy(xe[xi],ye[yi]);q=self.xy(xe[xi+1],ye[yi+1])
        return (p[0],q[1],q[0],p[1])

    def draw(self):
        self.delete('all')
        w,h = self.winfo_width(), self.winfo_height()
        if self.chart_type == 'surface':
            self.draw_surface()
            return
        if not self.view:
            self.create_text(w/2,h/2,text='No measured data', fill=MUTED)
            return
        self.create_rectangle(65,self.top_margin,max(66,w-20),max(self.top_margin+1,h-40),outline=fw.LINE)
        a,b,c,d = self.view
        for i in range(5):
            f=i/4
            x=65+f*(w-85); y=self.top_margin+f*(h-self.top_margin-40)
            if not self.x_ticks:self.create_text(x,h-25,text='%.4g' % (a+(b-a)*f),fill=MUTED,font=('sans',8),anchor='w' if i==0 else 'e' if i==4 else 'center')
            if not self.y_ticks:self.create_text(58,y,text='%.4g' % (d-(d-c)*f),fill=MUTED,anchor='e',font=('sans',8))
        for value,label in self.x_ticks:
            x,_=self.xy(value,c)
            if 65 <= x <= w-20:self.create_text(x,h-25,text=str(label),fill=INK,font=('sans',8))
        for value,label in self.y_ticks:
            _,y=self.xy(a,value)
            if self.top_margin <= y <= h-40:self.create_text(58,y,text=str(label),fill=INK,anchor='e',font=('sans',8))
        if self.color_label and self.color_range:
            lo,hi=self.color_range;left=max(70,w-190);right=max(left+20,w-25)
            for i in range(40):
                t=i/39; shade=value_color(t if hi != lo else .5)
                x=left+(right-left)*i/40;self.create_rectangle(x,5,left+(right-left)*(i+1)/40+1,13,fill=shade,outline=shade)
            self.create_text(left,15,text='%.5g' % lo,fill=INK,anchor='nw',font=('sans',8))
            self.create_text(right,15,text='%.5g' % hi,fill=INK,anchor='ne',font=('sans',8))
            self.create_text((left+right)/2,27,text=self.color_label,fill=INK,anchor='n',font=('sans',8))
        self.create_text(w/2,h-8,text=self.xlabel,fill=INK,font=('sans',9))
        self.create_text(4,3,text=self.ylabel,fill=INK,anchor='nw',font=('sans',9))
        last = {}
        selected_series={p[3] for p in self.points if p[2]==self.highlight}
        for x,y,ident,series,color in self.points:
            if not (math.isfinite(x) and math.isfinite(y)):continue
            px,py=self.xy(x,y)
            if not (65 <= px <= w-20 and self.top_margin <= py <= h-40):
                last.pop(series,None);continue
            if self.chart_type == 'line' and series in last and ident not in self.breaks:
                self.create_line(*last[series],px,py,fill=color,width=3 if series in selected_series else 1)
            last[series]=(px,py)
        ordered=sorted(enumerate(self.points),key=lambda pair:(pair[1][2]==self.highlight,-pair[0] if self.chart_type=='heatmap' else pair[0]))
        for _,(x,y,ident,series,color) in ordered:
            if not (math.isfinite(x) and math.isfinite(y)):continue
            px,py=self.xy(x,y)
            if self.chart_type != 'heatmap' and not (65 <= px <= w-20 and self.top_margin <= py <= h-40):continue
            radius=5 if ident == self.highlight else 3
            if self.chart_type == 'heatmap':
                cell=self._cell_bounds(x,y)
                if cell:
                    x0,y0,x1,y1=cell;x0=max(65,x0);y0=max(self.top_margin,y0);x1=min(w-20,x1);y1=min(h-40,y1)
                    if x1>x0 and y1>y0:self.create_rectangle(x0,y0,x1,y1,fill=color,outline=INK if ident == self.highlight else fw.PLOT_BG,width=2 if ident == self.highlight else 1)
            else:self.create_oval(px-radius,py-radius,px+radius,py+radius,fill=color,outline=INK if ident == self.highlight else '')
        if self.chart_type == 'heatmap':
            # Match the last painted tile: normally the first candidate at a
            # coordinate, or the selected candidate when cycling duplicates.
            visible={}
            for _,point in ordered:visible[(point[0],point[1])]=point
            for x,y,ident,series,color in visible.values():
                value=self.color_values.get(str(ident));cell=self._cell_bounds(x,y)
                if cell is None or not isinstance(value,(int,float)) or not math.isfinite(value):continue
                x0,y0,x1,y1=cell
                if x0 < 65 or y0 < self.top_margin or x1 > w-20 or y1 > h-40:continue
                # Negative font sizes are pixels and do not grow with Tk DPI.
                font_size=max(7,min(12,int((y1-y0)*.35)))
                label='%.4g' % value
                rgb=self.winfo_rgb(color)
                def linear(channel):
                    channel=channel/65535.0
                    return channel/12.92 if channel <= .04045 else ((channel+.055)/1.055)**2.4
                luminance=.2126*linear(rgb[0])+.7152*linear(rgb[1])+.0722*linear(rgb[2])
                self.create_text((x0+x1)/2,(y0+y1)/2,text=label,
                    fill='#101820' if luminance > .179 else '#ffffff',
                    font=('sans',-font_size,'bold'),tags=('heatmap_value','heatmap_value:'+str(ident)))

    def draw_surface(self):
        w,h=self.winfo_width(),self.winfo_height()
        pts=self.surface_points
        self.surface_screen=[]
        if not pts:
            self.create_text(w/2,h/2,text='No measured data',fill=MUTED)
            return
        ranges=[]
        for key in ('x','y','z'):
            values=[p[key] for p in pts];lo=min(values);hi=max(values)
            ranges.append((lo,hi))
        ca,sa=math.cos(self.azimuth),math.sin(self.azimuth)
        ce,se=math.cos(self.elevation),math.sin(self.elevation)
        # Fit the rotated unit cube within a plot rectangle that reserves
        # room for axis labels and the independent colour scale.
        corners=[(ca*x-sa*y, -ce*z+se*(sa*x+ca*y))
                 for x in (-1,1) for y in (-1,1) for z in (-1,1)]
        u0,u1=min(p[0] for p in corners),max(p[0] for p in corners)
        v0,v1=min(p[1] for p in corners),max(p[1] for p in corners)
        left,right=70,max(71,w-100);top,bottom=45,max(46,h-45)
        scale=max(1,min((right-left)/(u1-u0),(bottom-top)/(v1-v0))*.94)*self.surface_zoom
        cx=(left+right)/2-scale*(u0+u1)/2;cy=(top+bottom)/2-scale*(v0+v1)/2
        def project(x,y,z):
            a=ca*x-sa*y; b=sa*x+ca*y
            return (cx+scale*a,cy+scale*(-ce*z+se*b),ce*b+se*z)
        for p in pts:
            xyz=[(p[k]-lo)/(hi-lo)*2-1 if hi!=lo else 0 for k,(lo,hi) in zip(('x','y','z'),ranges)]
            self.surface_screen.append(project(*xyz))
        lo,hi=ranges[2]
        def shade(z):
            t=(z-lo)/(hi-lo) if hi!=lo else .5
            return value_color(t)
        valid=[t for t in self.triangles if len(t)==3 and all(isinstance(i,int) and 0<=i<len(pts) for i in t)]
        for tri in sorted(valid,key=lambda t:sum(self.surface_screen[i][2] for i in t)):
            coords=[v for i in tri for v in self.surface_screen[i][:2]]
            self.create_polygon(*coords,fill=shade(sum(pts[i]['z'] for i in tri)/3),outline=fw.LINE,tags=('mesh',))
        def short_label(label):
            label=label.replace(' (dimensionless)','')
            head,sep,tail=label.rpartition(' @ ')
            if sep and tail.endswith(' Hz'):
                try:return '%s @ %.6g GHz' % (head,float(tail[:-3])/1e9)
                except ValueError:pass
            return label
        def bounded_text(x,y,**options):
            item=self.create_text(x,y,**options);box=self.bbox(item)
            if box:
                dx=max(0,3-box[0])+min(0,w-3-box[2]);dy=max(0,3-box[1])+min(0,h-3-box[3])
                self.move(item,dx,dy)
            return item
        origin=project(-1,-1,-1)
        for axis,(endpoint,label) in enumerate((((1,-1,-1),self.xlabel),((-1,1,-1),self.ylabel),((-1,-1,1),self.zlabel))):
            end=project(*endpoint)
            self.create_line(origin[0],origin[1],end[0],end[1],fill=INK,arrow='last')
            for i in range(3):
                t=i/2;pos=[-1.,-1.,-1.];pos[axis]=-1+2*t;tick=project(*pos)
                self.create_line(tick[0]-3,tick[1]+3,tick[0]+3,tick[1]-3,fill=INK)
                value=ranges[axis][0]+t*(ranges[axis][1]-ranges[axis][0])
                ox,oy=((0,12),(-12,7),(-14,-2))[axis]
                bounded_text(tick[0]+ox,tick[1]+oy,text='%.4g' % value,fill=MUTED,font=('sans',8))
            bounded_text(end[0],end[1]-12,text=short_label(label),width=max(50,min(160,w//4)),fill=INK,anchor='s',font=('sans',9))
        for i in sorted(range(len(pts)),key=lambda i:(str(pts[i]['id'])==self.highlight,self.surface_screen[i][2])):
            x,y,_=self.surface_screen[i];sel=str(pts[i]['id'])==self.highlight;r=6 if sel else 4
            self.create_oval(x-r,y-r,x+r,y+r,fill=shade(pts[i]['z']),outline=INK if sel else fw.PLOT_BG,width=2 if sel else 1,tags=('surface_point',))
        left=max(5,w-72);top=55;bottom=max(top+20,h-70)
        for i in range(40):
            t=i/39;c=shade(lo+(hi-lo)*t);y=bottom-(bottom-top)*i/40
            self.create_rectangle(left,y-(bottom-top)/40,left+10,y+1,fill=c,outline=c)
        self.create_text(left+14,top,text='%.5g' % hi,fill=INK,anchor='w',font=('sans',8))
        self.create_text(left+14,bottom,text='%.5g' % lo,fill=INK,anchor='w',font=('sans',8))
        bounded_text(left,10,text=short_label(self.zlabel),width=68,fill=INK,anchor='nw',font=('sans',8))
        if not valid:self.create_text(w/2,h-18,text='Measured point cloud (no surface)' ,fill=MUTED,font=('sans',8))

    def nearest(self, event):
        if self.chart_type == 'surface':
            candidates=[((xy[0]-event.x)**2+(xy[1]-event.y)**2,i) for i,xy in enumerate(self.surface_screen)]
            if not candidates:return None
            distance,i=min(candidates)
            p=self.surface_points[i]
            return (p['x'],p['y'],str(p['id']),0,'') if distance<=100 else None
        if not self.view: return None
        if self.chart_type=='heatmap':
            hits=[]
            for p in self.points:
                bounds=self._cell_bounds(p[0],p[1])
                if bounds and bounds[0]<=event.x<=bounds[2] and bounds[1]<=event.y<=bounds[3] and 65<=event.x<=self.winfo_width()-20 and self.top_margin<=event.y<=self.winfo_height()-40:hits.append(p)
            return next((p for p in hits if p[2]==self.highlight),hits[0] if hits else None)
        candidates=[];last={}
        for p in self.points:
            if not (math.isfinite(p[0]) and math.isfinite(p[1])): continue
            x,y=self.xy(p[0],p[1]); candidates.append(((x-event.x)**2+(y-event.y)**2,p))
            if self.chart_type == 'line' and p[3] in last and p[2] not in self.breaks:
                x0,y0=last[p[3]];dx=x-x0;dy=y-y0;length=dx*dx+dy*dy
                t=max(0,min(1,((event.x-x0)*dx+(event.y-y0)*dy)/length)) if length else 0
                candidates.append(((x0+t*dx-event.x)**2+(y0+t*dy-event.y)**2,p))
            last[p[3]]=(x,y)
        if not candidates: return None
        distance,p=min(candidates,key=lambda z:(z[0],z[1][2]!=self.highlight))
        return p if distance <= 100 else None

    def hover(self,event):
        p=self.nearest(event)
        label=('%s: %.7g, %.7g' % (p[2],p[0],p[1])) if p else ''
        if p and p[2] in self.color_values:label+=' · %s = %.7g' % (self.color_label,self.color_values[p[2]])
        if p and self.chart_type=='heatmap':
            count=sum(1 for q in self.points if q[:2]==p[:2])
            if count>1:label+=' · %s overlapping candidates' % count
        self.readout.set(label)

    def press(self,event):
        self.drag=(event.x,event.y,(self.azimuth,self.elevation)) if self.chart_type=='surface' and self.surface_points else ((event.x,event.y,copy.copy(self.view)) if self.view else None)
        self.moved=False

    def pan(self,event):
        if not self.drag:return
        x,y,v=self.drag
        if self.chart_type=='surface':
            self.moved=abs(event.x-x)+abs(event.y-y)>3
            if self.moved:
                self.azimuth=v[0]+(event.x-x)*.01;self.elevation=max(-1.5,min(1.5,v[1]+(event.y-y)*.01));self.manual_view=True;self.draw()
            return
        dx=(event.x-x)*(v[1]-v[0])/max(1,self.winfo_width()-85)
        dy=(event.y-y)*(v[3]-v[2])/max(1,self.winfo_height()-self.top_margin-40)
        self.view=[v[0]-dx,v[1]-dx,v[2]+dy,v[3]+dy]
        self.moved = abs(event.x-x)+abs(event.y-y)>3
        if self.moved: self.manual_view = True
        self.draw()

    def release(self,event):
        if not self.moved:
            p=self.nearest(event)
            if p:
                if self.chart_type=='heatmap':
                    matches=[q[2] for q in self.points if q[:2]==p[:2]]
                    if len(matches)>1 and self.highlight in matches:p=next(q for q in self.points if q[:2]==p[:2] and q[2]==matches[(matches.index(self.highlight)+1)%len(matches)])
                self.select(p[2])
        self.drag=None

    def wheel(self,event):
        if self.chart_type=='surface' and self.surface_points:
            self.surface_zoom=max(.35,min(4.,self.surface_zoom*(1.25 if getattr(event,'num',0)==4 or getattr(event,'delta',0)>0 else .8)));self.manual_view=True;self.draw();return 'break'
        if self.view:
            f=.8 if getattr(event,'num',0)==4 or getattr(event,'delta',0)>0 else 1.25
            a,b,c,d=self.view; x=(a+b)/2; y=(c+d)/2
            self.view=[x+(a-x)*f,x+(b-x)*f,y+(c-y)*f,y+(d-y)*f]; self.manual_view=True; self.draw()
        return 'break'


class EMXPage(ttk.Frame):
    def __init__(self, parent, get_source, settings_parent=None, **kwargs):
        super().__init__(parent, style='Panel.TFrame', **kwargs)
        self.get_source=get_source
        self._external_settings=settings_parent is not None
        if settings_parent is None:
            self.columnconfigure(0,weight=1);self.rowconfigure(0,weight=1)
            self.pages=ttk.Notebook(self);self.pages.grid(row=0,column=0,sticky='nsew')
            settings_parent=ttk.Frame(self.pages,style='Panel.TFrame')
            self.results_parent=ttk.Frame(self.pages,style='Panel.TFrame')
            self.pages.add(settings_parent,text='EMX Settings')
            self.pages.add(self.results_parent,text='Sweep & Optimization')
            self.pages.select(self.results_parent)
        else:
            self.results_parent=self
        self.settings_parent=settings_parent
        self.source=None; self.source_model=None; self.auto_output=None; self.campaign=None; self.rows={}; self.pareto_ids=[]
        self.axes={}; self.active_field=None; self._after=None; self._plot_after=None; self._close_callback=None
        self._load_queue=queue.Queue();self._loading=False;self._load_generation=0
        self._hydrate_tasks=queue.Queue();self._hydrate_done=queue.Queue();self._hydrate_state={};self._hydrate_serial=0
        tasks=self._hydrate_tasks;done=self._hydrate_done
        def hydrate_worker():
            from result_data import hydrate_row
            while True:
                task=tasks.get()
                if task is None:return
                revision,ident,serial,signature,row=task
                done.put((revision,ident,serial,signature,hydrate_row(row)))
        threading.Thread(target=hydrate_worker,daemon=True).start()
        self._result_queue=queue.Queue();self._result_worker=None;self._result_cancel=None;self._result_revision=0;self._fit_tabs=[];self._pre_fit_tab_states={}
        self._video_active = False
        self._video_progress = queue.Queue(maxsize=1)
        self._discovery_queue=queue.Queue();self._discovery_generation=0;self._auto_paths={};self._discovery_identity=None
        self._discovery_request=None
        self.last_completed=0;self.last_total=None
        self.sort_column=None;self.sort_reverse=False;self._legend_signature=None;self.charts=[];self._chart_counter=0
        self._build()
        if not self._external_settings:
            self.pages.bind('<<NotebookTabChanged>>',self._page_selected)
        self._poll()

    def _page_selected(self, _event=None):
        if not self.is_running:
            self.refresh_source()

    @property
    def is_running(self):
        return bool(self.campaign and self.campaign.is_running)

    def _entry(self, parent, label, var, row, width=12):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',pady=0)
        ttk.Entry(parent,textvariable=var,width=width).grid(row=row,column=1,sticky='ew',pady=0)

    def _build(self):
        host=self.results_parent
        host.columnconfigure(1,weight=1); host.rowconfigure(1,weight=1)
        bar=ttk.Frame(host,style='Panel.TFrame'); bar.grid(row=0,column=0,columnspan=2,sticky='ew',padx=8,pady=4)
        bar.columnconfigure(1,weight=1)
        ttk.Button(bar,text='Sync Applied Layout',command=self.refresh_source).grid(row=0,column=0,padx=(0,8))
        self.source_text=tk.StringVar(value='Apply and validate a layout, then sync.')
        ttk.Label(bar,textvariable=self.source_text,style='Muted.TLabel',wraplength=260).grid(row=0,column=1,sticky='w')
        ttk.Button(bar,text='Load settings',command=self.load_settings).grid(row=0,column=2)
        export_bar = ttk.Frame(bar, style='Panel.TFrame')
        export_bar.grid(row=1, column=1, columnspan=5, sticky='e')
        self.video_fps = tk.StringVar(value='8')
        ttk.Label(export_bar, text='FPS').pack(side='left', padx=(0, 4))
        ttk.Entry(export_bar, textvariable=self.video_fps, width=4).pack(side='left')
        self.video_btn = ttk.Button(export_bar, text='Export Video', command=self.export_video, state='disabled')
        self.video_btn.pack(side='left', padx=4)
        self.video_play_btn = ttk.Button(export_bar, text='Play Video', command=self.play_video, state='disabled')
        self.video_play_btn.pack(side='left', padx=(0, 4))
        self.load_results_btn = ttk.Button(export_bar, text='Load Results', command=self.load_results)
        self.load_results_btn.pack(side='left')
        ttk.Button(bar,text='Save settings',command=self.save_settings).grid(row=0,column=3,padx=4)
        self.mode=tk.StringVar(value='sweep')
        modebar=ttk.Frame(bar,style='Panel.TFrame');modebar.grid(row=1,column=0,columnspan=6,sticky='w')
        for label,value in (('Sweep','sweep'),('Optimize','optimize')):
            ttk.Radiobutton(modebar,text=label,value=value,variable=self.mode,command=self._refresh_chart_choices).pack(side='left')
        self.start_btn=ttk.Button(bar,text='Start',command=self.start,style='Primary.TButton'); self.start_btn.grid(row=0,column=4,padx=4)
        self.stop_btn=ttk.Button(bar,text='Stop',command=self.stop,state='disabled'); self.stop_btn.grid(row=0,column=5)
        left=ttk.Frame(host,style='Panel.TFrame',width=355); left.grid(row=1,column=0,sticky='nsew',padx=(8,4)); left.grid_propagate(False)
        left.rowconfigure(0,weight=1);left.columnconfigure(0,weight=1)
        self.sidebar=ScrollSettings(left);self.sidebar_scroll=self.sidebar;self.section_widgets={};self.sidebar.grid(row=0,column=0,sticky='nsew')
        settings=self.sidebar.content
        self.parameter_tab=self._settings_card(settings,'Parameters')
        self.parameter_tab.rowconfigure(1,weight=1);self.parameter_tab.columnconfigure(0,weight=1)
        ttk.Label(self.parameter_tab,text='Select fields to vary. Paired axes require matching sampling, range and count.',style='Muted.TLabel',wraplength=320).grid(row=0,column=0,sticky='w',padx=4,pady=4)
        holder=ttk.Frame(self.parameter_tab);holder.grid(row=1,column=0,sticky='nsew');holder.rowconfigure(0,weight=1);holder.columnconfigure(0,weight=1)
        self.field_tree=ttk.Treeview(holder,columns=('current','vary'),show='tree headings',selectmode='extended',height=4)
        self.field_tree.heading('#0',text='Parameter');self.field_tree.heading('current',text='Current');self.field_tree.heading('vary',text='Vary')
        self.field_tree.column('#0',width=135);self.field_tree.column('current',width=88);self.field_tree.column('vary',width=48,stretch=False)
        self.field_tree.grid(row=0,column=0,sticky='nsew'); sb=ttk.Scrollbar(holder,command=self.field_tree.yview);sb.grid(row=0,column=1,sticky='ns');self.field_tree.configure(yscrollcommand=sb.set)
        self.field_tree.bind('<<TreeviewSelect>>',self._selected_field)
        editor=ttk.LabelFrame(self.parameter_tab,text='Selected range',padding=4);editor.grid(row=2,column=0,sticky='ew',padx=4,pady=4)
        editor.columnconfigure(1,weight=1);editor.columnconfigure(3,weight=1)
        self.edit={k:tk.StringVar(value=v) for k,v in dict(sampling='linear',minimum='',maximum='',count='3',step='',values='',group='').items()}
        fields=[('Sampling','sampling'),('Minimum','minimum'),('Maximum','maximum'),('Count','count'),('Step','step'),('Paired group','group'),('Choices','values')]
        for i,(label,key) in enumerate(fields):
            row,col=divmod(i,2);col*=2
            ttk.Label(editor,text=label).grid(row=row,column=col,sticky='w')
            if key=='sampling':
                self.sampling=ttk.Combobox(editor,textvariable=self.edit[key],values=('linear','log','step','choices'),state='readonly',width=8);widget=self.sampling
            else:widget=ttk.Entry(editor,textvariable=self.edit[key],width=8)
            widget.grid(row=row,column=col+1,sticky='ew',padx=(0,4))
        buttons=ttk.Frame(editor);buttons.grid(row=4,column=0,columnspan=4,sticky='ew')
        ttk.Button(buttons,text='Set selected',command=self.set_axis).pack(side='left');ttk.Button(buttons,text='Remove selected',style='Danger.TButton',command=self.remove_axis).pack(side='left',padx=4)
        self.estimate=tk.StringVar(value='Sweep: 1 combination');ttk.Label(self.parameter_tab,textvariable=self.estimate,style='Muted.TLabel').grid(row=3,column=0,sticky='w',padx=4)
        self.settings_parent.columnconfigure(1,weight=1);self.settings_parent.rowconfigure(0,weight=1)
        config_left=ttk.Frame(self.settings_parent,style='Panel.TFrame',width=470)
        self._settings_widgets=[config_left]
        config_left.grid(row=0,column=0,sticky='ns',padx=(8,4),pady=8);config_left.grid_propagate(False)
        config_left.rowconfigure(0,weight=1);config_left.columnconfigure(0,weight=1)
        self.settings_scroll=ScrollSettings(config_left);self.settings_scroll.grid(row=0,column=0,sticky='nsew')
        self.config_tab=self._settings_card(self.settings_scroll.content,'EMX');self.config_tab.columnconfigure(0,weight=1)
        defaults=dict(edge='0.5',thickness='0.5',via='0.5',freq_start='1e9',freq_stop='3e9',freq_mode='linear',freq_amount='3',freq_values='1e9 2e9 3e9',extra='--3d=-*',parallel='2',memory='1024',timeout='300',concurrency='2',output='',emx_executable='',process_file='')
        self.v={k:tk.StringVar(value=v) for k,v in defaults.items()};self.three_d=tk.BooleanVar(value=False);self.cleanup=tk.BooleanVar(value=False)
        for row,(label,key) in enumerate((('EMX executable','emx_executable'),('Process file','process_file'))):
            path_row=ttk.Frame(self.config_tab);path_row.grid(row=row,column=0,sticky='ew');path_row.columnconfigure(0,weight=1)
            ttk.Label(path_row,text=label).grid(row=0,column=0,columnspan=2,sticky='w')
            ttk.Entry(path_row,textvariable=self.v[key],width=16).grid(row=1,column=0,sticky='ew',padx=(0,4))
            ttk.Button(path_row,text='Browse',command=lambda k=key:self._browse_path(k)).grid(row=1,column=1,sticky='e')
        ttk.Button(self.config_tab,text='Detect / reset auto paths',command=self._reset_auto_paths).grid(row=2,column=0,sticky='w')
        self.discovery_text=tk.StringVar(value='Sync an applied layout to detect paths.')
        ttk.Label(self.config_tab,textvariable=self.discovery_text,style='Muted.TLabel',wraplength=300).grid(row=3,column=0,sticky='w')
        settings_group=ttk.Frame(self.config_tab);settings_group.grid(row=4,column=0,sticky='ew');settings_group.columnconfigure(1,weight=1);settings_group.columnconfigure(3,weight=1)
        config_fields=[('Edge (µm)','edge'),('Thickness (µm)','thickness'),('Via merge (µm)','via'),('Threads','parallel'),('Memory (MB)','memory'),('Timeout (s)','timeout'),('Jobs','concurrency')]
        for i,(label,key) in enumerate(config_fields):
            row,col=divmod(i,2);col*=2
            ttk.Label(settings_group,text=label).grid(row=row,column=col,sticky='w')
            ttk.Entry(settings_group,textvariable=self.v[key],width=8).grid(row=row,column=col+1,sticky='ew',padx=(0,4))
        options_row=ttk.Frame(self.config_tab);options_row.grid(row=5,column=0,sticky='ew');options_row.columnconfigure(1,weight=1)
        ttk.Label(options_row,text='Solver options').grid(row=0,column=0,sticky='w');ttk.Entry(options_row,textvariable=self.v['extra'],width=12).grid(row=0,column=1,sticky='ew',padx=(4,0))
        ttk.Label(self.config_tab,text='Solver frequencies (Hz)',style='Muted.TLabel').grid(row=6,column=0,sticky='w',pady=(4,0))
        frequency_group=ttk.Frame(self.config_tab);frequency_group.grid(row=7,column=0,sticky='ew');frequency_group.columnconfigure(1,weight=1);frequency_group.columnconfigure(3,weight=1)
        frequency_fields=[('Mode','freq_mode'),('Count / step','freq_amount'),('Start / single','freq_start'),('Stop','freq_stop')]
        for i,(label,key) in enumerate(frequency_fields):
            row,col=divmod(i,2);col*=2
            ttk.Label(frequency_group,text=label).grid(row=row,column=col,sticky='w')
            if key=='freq_mode':widget=ttk.Combobox(frequency_group,textvariable=self.v[key],values=('single','list','linear','log','step'),state='readonly',width=8)
            else:widget=ttk.Entry(frequency_group,textvariable=self.v[key],width=8)
            widget.grid(row=row,column=col+1,sticky='ew',padx=(0,4))
        list_row=ttk.Frame(self.config_tab);list_row.grid(row=8,column=0,sticky='ew');list_row.columnconfigure(1,weight=1)
        ttk.Label(list_row,text='List (Hz)').grid(row=0,column=0,sticky='w');ttk.Entry(list_row,textvariable=self.v['freq_values'],width=12).grid(row=0,column=1,sticky='ew',padx=(4,0))
        output_row=ttk.Frame(self.config_tab);output_row.grid(row=9,column=0,sticky='ew');output_row.columnconfigure(1,weight=1)
        ttk.Label(output_row,text='Output folder').grid(row=0,column=0,sticky='w');ttk.Entry(output_row,textvariable=self.v['output'],width=12).grid(row=0,column=1,sticky='ew',padx=(4,0))
        ttk.Button(self.config_tab,text='Choose folder',command=self.choose_output).grid(row=10,column=0,sticky='w')
        ttk.Checkbutton(self.config_tab,text='Clean successful intermediates',variable=self.cleanup).grid(row=11,column=0,sticky='w')
        summary=ttk.LabelFrame(self.settings_parent,text='Applied source and pins',padding=8)
        self._settings_widgets.append(summary)
        summary.grid(row=0,column=1,sticky='new',padx=(4,8),pady=8)
        summary.columnconfigure(0,weight=1)
        ttk.Button(summary,text='Sync Applied Layout',command=self.refresh_source).grid(row=0,column=0,sticky='w')
        ttk.Label(summary,textvariable=self.source_text,style='Muted.TLabel',wraplength=300,justify='left').grid(row=1,column=0,sticky='w',pady=(8,0))
        self.pin_text=tk.StringVar(value='Pins: apply and sync a layout.')
        ttk.Label(summary,textvariable=self.pin_text,style='Muted.TLabel',wraplength=300,justify='left').grid(row=2,column=0,sticky='w',pady=(8,0))
        goal=self._settings_card(settings,'Objectives & Constraints');self.goal_tab=goal;goal.columnconfigure(0,weight=1)
        self.goal_help=tk.StringVar(value='Goal frequency must match an EMX Settings sample. Inductance is in nH.')
        ttk.Label(goal,textvariable=self.goal_help,style='Muted.TLabel',wraplength=300).grid(row=1,column=0,sticky='w')
        self.goal_tree=ttk.Treeview(goal,columns=('kind','metric','freq','rule','value'),show='headings',height=3,selectmode='browse')
        for col,title,width in (('kind','Type',70),('metric','Metric',52),('freq','GHz',54),('rule','Rule',65),('value','Value',70)):
            self.goal_tree.heading(col,text=title);self.goal_tree.column(col,width=width,stretch=True)
        self.goal_tree.grid(row=2,column=0,sticky='ew');self.goal_tree.bind('<<TreeviewSelect>>',self._goal_selected)
        editor=ttk.Frame(goal);editor.grid(row=3,column=0,sticky='ew');editor.columnconfigure(1,weight=1);editor.columnconfigure(3,weight=1)
        self.goal_vars={k:tk.StringVar(value=v) for k,v in dict(kind='Objective',metric='',frequency='',rule='maximize',low='',high='').items()}
        self.goal_widgets={}
        for i,(label,key) in enumerate((('Type','kind'),('Metric','metric'),('Freq. (GHz)','frequency'),('Rule','rule'),('Value / min','low'),('Upper','high'))):
            row,col=divmod(i,2);col*=2;ttk.Label(editor,text=label).grid(row=row,column=col,sticky='w')
            if key in ('kind','metric','frequency','rule'):
                values={'kind':('Objective','Constraint'),'metric':('L','Q','Lp','Ls','k','Qp','Qs'),'frequency':(),'rule':('maximize','minimize','target')}[key]
                widget=ttk.Combobox(editor,textvariable=self.goal_vars[key],values=values,state='normal' if key=='frequency' else 'readonly',width=8)
            else:widget=ttk.Entry(editor,textvariable=self.goal_vars[key],width=8)
            widget.grid(row=row,column=col+1,sticky='ew',padx=(0,4));self.goal_widgets[key]=widget
        self.goal_widgets['kind'].bind('<<ComboboxSelected>>',self._goal_kind_changed)
        controls=ttk.Frame(goal);controls.grid(row=4,column=0,sticky='ew')
        self.goal_add_btn=ttk.Button(controls,text='Add',command=lambda:self._goal_apply(False));self.goal_add_btn.pack(side='left')
        self.goal_update_btn=ttk.Button(controls,text='Update selected',command=lambda:self._goal_apply(True));self.goal_update_btn.pack(side='left',padx=4)
        self.goal_remove_btn=ttk.Button(controls,text='Remove',style='Danger.TButton',command=self._goal_remove);self.goal_remove_btn.pack(side='left')
        self.goal_status=tk.StringVar(value='Add objectives or constraints, then select a row to edit.')
        ttk.Label(goal,textvariable=self.goal_status,style='Muted.TLabel',wraplength=300).grid(row=5,column=0,sticky='w')
        self.goal_text_open=tk.BooleanVar(value=False)
        ttk.Checkbutton(goal,text='Advanced text (Hz)',variable=self.goal_text_open,command=self._toggle_goal_text).grid(row=6,column=0,sticky='w')
        self.goal_text_frame=ttk.Frame(goal);self.goal_text_frame.grid(row=7,column=0,sticky='ew');self.goal_text_frame.columnconfigure(0,weight=1)
        ttk.Label(self.goal_text_frame,text='Objectives (one per line)').grid(row=0,column=0,sticky='w')
        self.objectives=tk.Text(self.goal_text_frame,height=3,bg=fw.PLOT_BG,fg=INK,insertbackground=INK,wrap='none');self.objectives.grid(row=1,column=0,sticky='ew')
        ttk.Label(self.goal_text_frame,text='Constraints (between uses low:high)').grid(row=2,column=0,sticky='w')
        self.constraints=tk.Text(self.goal_text_frame,height=3,bg=fw.PLOT_BG,fg=INK,insertbackground=INK,wrap='none');self.constraints.grid(row=3,column=0,sticky='ew')
        self.goal_text_frame.grid_remove()
        self.advanced_open=tk.BooleanVar(value=False);ttk.Checkbutton(goal,text='Advanced optimizer',variable=self.advanced_open,command=self._toggle_advanced).grid(row=8,column=0,sticky='w')
        self.advanced=ttk.Frame(goal);self.advanced.grid(row=9,column=0,sticky='ew');self.advanced.columnconfigure(1,weight=1)
        self.opt={k:tk.StringVar(value=v) for k,v in dict(algorithm='nsga2',population='12',generations='3',seed='1').items()}
        ttk.Label(self.advanced,text='Algorithm').grid(row=0,column=0,sticky='w');ttk.Combobox(self.advanced,textvariable=self.opt['algorithm'],values=('nsga2','de','bayesian'),state='readonly').grid(row=0,column=1,sticky='ew')
        for row,key in enumerate(('population','generations','seed'),1):self._entry(self.advanced,key.title(),self.opt[key],row)
        self.advanced.grid_remove()
        self._build_results(settings)
        self.sidebar.install_wheel_bindings()
        self.settings_scroll.install_wheel_bindings()
        self.objectives.bind('<FocusOut>',self._goal_text_changed)
        self.constraints.bind('<FocusOut>',self._goal_text_changed)
        self._goal_refresh_frequencies()
        for key in ('freq_start','freq_stop','freq_mode','freq_amount','freq_values'):
            self.v[key].trace_add('write',lambda *args:(self._goal_refresh_frequencies(),self._refresh_chart_choices()))

    def _toggle_goal_text(self):
        if self.goal_text_open.get():self.goal_text_frame.grid()
        else:self._goal_sync_tree();self.goal_text_frame.grid_remove()

    def _goal_text_changed(self,event=None):
        self._goal_sync_tree();self._refresh_chart_choices()

    def _goal_refresh_frequencies(self):
        try:
            get=lambda k:self.v[k].get()
            freqs=frequency_list(get('freq_values') if get('freq_mode')=='list' else get('freq_start'),get('freq_stop'),get('freq_mode'),get('freq_amount'))
            values=tuple(format(f/1e9,'.17g') for f in freqs)
        except (ValueError,TypeError):values=()
        self.goal_widgets['frequency'].configure(values=values)
        if values and self.goal_vars['frequency'].get() not in values and not self.goal_tree.selection():self.goal_vars['frequency'].set(values[0])

    def _goal_kind_changed(self,event=None):
        constraint=self.goal_vars['kind'].get()=='Constraint'
        rules=('<=','>=','between') if constraint else ('maximize','minimize','target')
        self.goal_widgets['rule'].configure(values=rules)
        if self.goal_vars['rule'].get() not in rules:self.goal_vars['rule'].set(rules[0])

    def _goal_lines(self,widget):
        return [line.strip() for line in widget.get('1.0','end').splitlines() if line.strip()]

    def _goal_sync_tree(self):
        for item in self.goal_tree.get_children():self.goal_tree.delete(item)
        for prefix,widget,constraint in (('o',self.objectives,False),('c',self.constraints,True)):
            for index,line in enumerate(self._goal_lines(widget)):
                try:
                    g=self._parse_goals(line,constraint)[0];rule=g.get('operator',g.get('direction'));val=g.get('limit',g.get('target',''))
                    if isinstance(val,list):val='%s : %s' % tuple(val)
                    values=('Constraint' if constraint else 'Objective',g['metric'],format(g['frequency_hz']/1e9,'.12g'),rule,str(val))
                except (ValueError,IndexError):values=('Invalid',line,'','','')
                self.goal_tree.insert('', 'end',iid='%s:%s' % (prefix,index),values=values)

    def _goal_selected(self,event=None):
        selected=self.goal_tree.selection()
        if not selected:return
        prefix,index=selected[0].split(':');constraint=prefix=='c';widget=self.constraints if constraint else self.objectives
        try:g=self._parse_goals(self._goal_lines(widget)[int(index)],constraint)[0]
        except (ValueError,IndexError):self.goal_status.set('Invalid text row: open Advanced text to correct it.');return
        self.goal_vars['kind'].set('Constraint' if constraint else 'Objective');self._goal_kind_changed()
        self.goal_vars['metric'].set(g['metric']);self.goal_vars['frequency'].set(format(g['frequency_hz']/1e9,'.17g'))
        self.goal_vars['rule'].set(g.get('operator',g.get('direction')));value=g.get('limit',g.get('target',''))
        self.goal_vars['low'].set(str(value[0] if isinstance(value,list) else value));self.goal_vars['high'].set(str(value[1]) if isinstance(value,list) else '')

    def _goal_apply(self,update=False):
        try:
            selected=self.goal_tree.selection();kind=self.goal_vars['kind'].get();constraint=kind=='Constraint';prefix='c' if constraint else 'o'
            if update and (not selected or selected[0].split(':')[0]!=prefix):raise ValueError('Select a row of the same type to update.')
            metric=self.goal_vars['metric'].get();freq=_number(self.goal_vars['frequency'].get(),'Frequency (GHz)')*1e9
            get=lambda k:self.v[k].get()
            freqs=frequency_list(get('freq_values') if get('freq_mode')=='list' else get('freq_start'),get('freq_stop'),get('freq_mode'),get('freq_amount'))
            matches=[f for f in freqs if abs(f-freq)<=max(abs(f)*1e-12,1e-12)]
            if len(matches)!=1:raise ValueError('Choose a unique frequency from the EMX Settings samples.')
            freq=matches[0];rule=self.goal_vars['rule'].get();low=self.goal_vars['low'].get().strip();high=self.goal_vars['high'].get().strip()
            value=(low+':'+high) if constraint and rule=='between' else low
            line='%s, %.17g, %s' % (metric,freq,rule)
            if constraint or rule=='target':line+=', '+value
            widget=self.constraints if constraint else self.objectives;lines=self._goal_lines(widget)
            if update:
                index=int(selected[0].split(':')[1]);old=self._parse_goals(lines[index],constraint)[0];scale=old.get('scale',1.0)
                if scale!=1.0:line+=(', , ' if not constraint and rule!='target' else ', ')+str(scale)
            self._parse_goals(line,constraint)
            if update:lines[index]=line
            else:lines.append(line)
            widget.delete('1.0','end');widget.insert('1.0','\n'.join(lines));self._goal_sync_tree();self._refresh_chart_choices()
            self.goal_status.set('Saved. Frequency matches an EMX sample.')
        except (ValueError,IndexError) as exc:self.goal_status.set(str(exc))

    def _goal_remove(self):
        selected=self.goal_tree.selection()
        if not selected:self.goal_status.set('Select a row to remove.');return
        prefix,index=selected[0].split(':');widget=self.constraints if prefix=='c' else self.objectives;lines=self._goal_lines(widget);del lines[int(index)]
        widget.delete('1.0','end');widget.insert('1.0','\n'.join(lines));self._goal_sync_tree();self._refresh_chart_choices();self.goal_status.set('Row removed.')

    def _settings_card(self,parent,title):
        card=ttk.Frame(parent,style='Panel.TFrame');card.pack(fill='x',padx=4,pady=0)
        opened=tk.BooleanVar(value=True)
        body=ttk.Frame(card,padding=4);body.pack(fill='x')
        toggle=ttk.Checkbutton(card,text=title,variable=opened,command=lambda: body.pack(fill='x') if opened.get() else body.pack_forget())
        toggle.pack(before=body,fill='x')
        self.section_widgets[title]=dict(card=card,body=body,toggle=toggle,open=opened)
        return body

    def _build_results(self,settings):
        right=ttk.Frame(self.results_parent,style='Panel.TFrame');right.grid(row=1,column=1,sticky='nsew',padx=(4,8));right.columnconfigure(0,weight=1);right.rowconfigure(1,weight=6,minsize=220);right.rowconfigure(2,weight=1)
        controls=self._settings_card(settings,'Chart manager');self.chart_manager=controls
        toolbar=ttk.Frame(controls);toolbar.pack(fill='x')
        self.manager_open=tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar,text='Edit chart',variable=self.manager_open,command=self._toggle_manager).grid(row=0,column=0,sticky='w')
        self.chart_toolbar_buttons=[]
        for index,(label,command) in enumerate((('Add',self.add_chart),('Update',self.update_chart),('Delete',self.delete_chart),('Add goal pairs',self.add_all_pairs),('Reset view',self.reset_chart),('Export CSV',self.export_csv))):
            button=ttk.Button(toolbar,text=label,command=command);button.grid(row=1+index//2,column=index%2,sticky='ew',padx=0)
            self.chart_toolbar_buttons.append(button)
            if label=='Add goal pairs':self.pairs_button=button
        self.chart_selection=tk.StringVar(value='')
        self.chart_selector=ttk.Combobox(toolbar,textvariable=self.chart_selection,width=18,state='readonly')
        self.chart_selector.grid(row=4,column=0,columnspan=2,sticky='ew',padx=0)
        self.chart_selector.bind('<<ComboboxSelected>>',self._choose_chart)
        self.chart_editor=ttk.Frame(controls);self.chart_editor.pack(fill='x')
        editor=self.chart_editor
        editor.columnconfigure(1,weight=1);editor.columnconfigure(3,weight=1)
        self.chart_vars={key:tk.StringVar(value='') for key in ('x','y','x_frequency_hz','y_frequency_hz','grid_y','slice','slice_value','z','z_frequency_hz')}
        self.chart_combos={}
        fields=(('X','x'),('Y','y'),('X Hz','x_frequency_hz'),('Y Hz','y_frequency_hz'),('Grid Y','grid_y'),('Slice axis','slice'),('Slice value','slice_value'),('Z','z'),('Z Hz','z_frequency_hz'))
        for i,(label,key) in enumerate(fields):
            row=i//2; col=(i%2)*2
            ttk.Label(editor,text=label).grid(row=row,column=col,sticky='w')
            box=ttk.Combobox(editor,textvariable=self.chart_vars[key],width=11,state='readonly');box.grid(row=row,column=col+1,sticky='ew',padx=(0,4))
            self.chart_combos[key]=box
            box.bind('<<ComboboxSelected>>',lambda e:self._refresh_chart_choices())
        graph=ttk.Frame(right);graph.grid(row=1,column=0,sticky='nsew',pady=4);graph.rowconfigure(0,weight=1);graph.columnconfigure(0,weight=1)
        self.chart_notebook=ttk.Notebook(graph);self.chart_notebook.grid(row=0,column=0,sticky='nsew')
        self.chart_notebook.bind('<<NotebookTabChanged>>',self._chart_selected)
        actions=self._settings_card(settings,'Selected candidate')
        self.result_target=tk.StringVar(value='Select a completed candidate with an SNP.')
        ttk.Label(actions,textvariable=self.result_target,wraplength=300,style='Muted.TLabel').pack(fill='x')
        self.result_buttons=[]
        for label,action in (('Generate Layout','regenerate_layout'),('Generate Schematic + Symbol','fit_schematic'),('Open in SNPFit Studio','open_snpfit')):
            button=ttk.Button(actions,text=label,command=lambda a=action:self._start_result_action(a));button.pack(fill='x');self.result_buttons.append(button)
        self.result_cancel_button=ttk.Button(actions,text='Cancel result action',command=self._cancel_result_action,state='disabled');self.result_cancel_button.pack(fill='x')
        self.only_fit=tk.BooleanVar(value=False)
        ttk.Checkbutton(actions,text='Only Fit Comparison',variable=self.only_fit,command=self._toggle_fit_only).pack(anchor='w')
        table=ttk.Frame(right);table.grid(row=2,column=0,sticky='nsew');table.rowconfigure(2,weight=1);table.columnconfigure(0,weight=1)
        table_controls=self._settings_card(settings,'Frequency');self.frequency_controls=table_controls
        ttk.Label(table_controls,text='Sample frequency (Hz)').pack(side='left')
        self.sample_frequency=tk.StringVar(value='');self.frequency_combo=ttk.Combobox(table_controls,textvariable=self.sample_frequency,values=(),state='readonly',width=15);self.frequency_combo.pack(side='left',padx=4)
        self.frequency_combo.bind('<<ComboboxSelected>>',lambda e:self._refresh_results())
        status_legend=ttk.Frame(table);status_legend.grid(row=1,column=0,sticky='w')
        for index,(label,color,foreground) in enumerate((('Fail: failed / rejected / cancelled','#57333b','#fff0ef'),('Infeasible: constraints not met','#654526','#fff2df'),('Feasible: constraints met','#294b42','#ecfff8'),('Optimal: current feasible Pareto','#5a512b','#fff9d8'))):
            tk.Label(status_legend,text=label,bg=color,fg=foreground,font=('sans',8),padx=4).grid(row=index//2,column=index%2,sticky='w',padx=0,pady=0)
        self.results=ttk.Treeview(table,columns=('status','values','objectives','constraint','snp')+METRICS,show='tree headings',selectmode='browse',height=2)
        self.results.heading('#0',text='Candidate');self.results.column('#0',width=85,stretch=False)
        for name,title,width in [('status','Status',150),('values','Parameters',200),('objectives','Objectives',140),('constraint','Feasibility',85),('snp','SNP',160)]:
            self.results.heading(name,text=title);self.results.column(name,width=width,minwidth=65)
        for metric in METRICS:
            self.results.heading(metric,text=metric + (' (nH)' if metric in ('L','Lp','Ls') else ''),command=lambda m=metric:self._sort_results(m))
            self.results.column(metric,width=85,minwidth=65,stretch=False,anchor='e')
        self.results.grid(row=2,column=0,sticky='nsew');vs=ttk.Scrollbar(table,command=self.results.yview);vs.grid(row=2,column=1,sticky='ns');hs=ttk.Scrollbar(table,orient='horizontal',command=self.results.xview);hs.grid(row=3,column=0,sticky='ew');self.results.configure(yscrollcommand=vs.set,xscrollcommand=hs.set)
        self.results.tag_configure('warning',foreground='#f4cb93')
        self.results.tag_configure('fail',background='#57333b',foreground='#fff0ef')
        self.results.tag_configure('infeasible',background='#654526',foreground='#fff2df')
        self.results.tag_configure('feasible',background='#294b42',foreground='#ecfff8')
        self.results.tag_configure('optimal',background='#5a512b',foreground='#fff9d8')
        self.results.tag_configure('pending',background='#293b4b',foreground=INK)
        self.results.bind('<<TreeviewSelect>>',lambda e:self._selection_changed());self.results.bind('<Button-3>',self._popup)
        self._update_display_columns()
        self.menu=tk.Menu(self,tearoff=False);self.menu.add_command(label='Open EMX log',command=self.open_candidate_log);self.menu.add_command(label='Candidate details',command=self.candidate_details);self.menu.add_command(label='Copy SNP path',command=self.copy_snp);self.menu.add_command(label='Copy layout identity',command=self.copy_layout_identity)
        self.status=tk.StringVar(value='Ready');foot=ttk.Frame(self.results_parent,style='Panel.TFrame');foot.grid(row=2,column=0,columnspan=2,sticky='ew',padx=8,pady=4);foot.columnconfigure(0,weight=1)
        self.status_chip=fw.StatusChip(foot);self.status_chip.grid(row=0,column=0,sticky='w')
        bind_status(self.status,self.status_chip,lambda:self.is_running)
        self.progress=ttk.Progressbar(foot,mode='determinate',length=180);self.progress.grid(row=0,column=1,padx=4)
        ttk.Button(foot,text='Campaign log',command=self.show_log).grid(row=0,column=2)
        self.log_lines=[]
        self._result_buttons_update()

    def _toggle_advanced(self):
        if self.advanced_open.get():self.advanced.grid()
        else:self.advanced.grid_remove()

    def _browse_path(self, key):
        path=filedialog.askopenfilename(parent=self)
        if path:self.v[key].set(path)

    def _reset_auto_paths(self):
        for key in ('emx_executable','process_file'):self.v[key].set('')
        self._auto_paths={}
        self._discovery_request=None
        if self.source:self._discover_paths(self.source)

    def _discover_paths(self, source):
        identity=copy.deepcopy(source.get('identity') or {})
        technology=identity.get('technology')
        if not technology:
            self._discovery_generation+=1;self._discovery_request=None
            self.discovery_text.set('Path detection needs an attached technology.');return
        runtime=copy.deepcopy(source.get('runtime') or {})
        snapshot={key:self.v[key].get().strip() for key in ('emx_executable','process_file')}
        overrides={key:value for key,value in snapshot.items() if value and value != self._auto_paths.get(key)}
        request=(identity,runtime,overrides)
        if request == self._discovery_request:
            return
        self._discovery_request=request
        self._discovery_generation+=1; generation=self._discovery_generation
        self.discovery_text.set('Detecting EMX paths…')
        results=self._discovery_queue
        def worker():
            try:
                from emx_adapter import discover_config
                result=discover_config(technology,runtime,overrides)
                results.put((generation,snapshot,result,None))
            except Exception as exc:results.put((generation,snapshot,None,str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def _drain_discovery(self):
        while True:
            try:generation,snapshot,result,error=self._discovery_queue.get_nowait()
            except queue.Empty:break
            if generation != self._discovery_generation:continue
            if error:
                self._discovery_request=None
                self.discovery_text.set('Path detection: '+error);continue
            for key in ('emx_executable','process_file'):
                current=self.v[key].get().strip()
                if current == snapshot[key] and (not current or current == self._auto_paths.get(key)):
                    self.v[key].set(result.get(key,''));self._auto_paths[key]=result.get(key,'')
            self.discovery_text.set('Paths ready. Edit or browse to override.')

    def refresh_source(self):
        if self.is_running:return
        try:
            source=copy.deepcopy(self.get_source())
            if not isinstance(source,dict) or not isinstance(source.get('fields'),list):raise ValueError('Invalid source metadata.')
        except Exception as exc:
            self._discovery_generation+=1;self._discovery_request=None;self.source=None;self.source_text.set(str(exc));self.pin_text.set('Pins: apply and sync a layout.');self._render_fields();return
        old_model=self.source_model
        new_model=source.get('design',{}).get('model')
        if old_model and old_model != new_model:
            self.axes={};self.objectives.delete('1.0','end');self.constraints.delete('1.0','end');self._clear_charts()
        self.source=source;self.source_model=new_model
        identity=source.get('identity') or {}
        discovery_identity=(copy.deepcopy(identity),copy.deepcopy(source.get('runtime') or {}))
        if discovery_identity != self._discovery_identity:
            for key,old in self._auto_paths.items():
                if self.v[key].get().strip() == old:self.v[key].set('')
            self._auto_paths={}
            self._discovery_identity=discovery_identity
        contract=source.get('port_contract') or {}
        terminals=contract.get('terminals',[])
        self.pin_text.set('EMX pins: '+', '.join('%s (%s)' % (t.get('name','?'),t.get('kind','?')) for t in terminals) if terminals else 'Pins: apply and sync a layout.')
        self._discover_paths(source)
        workspace=source.get('identity',{}).get('workspace')
        if workspace:
            output=self.v['output'].get().strip()
            suggested=str(Path(workspace) / (Path(__file__).resolve().parent.name + '_runtime') / 'emx_campaigns')
            if not output or output == self.auto_output:
                self.v['output'].set(suggested)
            self.auto_output=suggested
        valid={f['name'] for f in source['fields']}
        self.axes={k:v for k,v in self.axes.items() if k in valid}
        design=source.get('design',{})
        metrics=source.get('definition',{}).get('emx_metrics') or []
        if metrics:
            self.goal_help.set('Goal frequency must match an EMX Settings sample. Inductance is in nH.')
            self.goal_widgets['metric'].configure(values=metrics)
            if self.goal_vars['metric'].get() not in metrics:self.goal_vars['metric'].set(metrics[0])
        self.source_text.set('Applied: %s / %s · %s' % (design.get('library',''),design.get('cell',''),new_model or ''))
        self._render_fields();self._estimate();self._update_display_columns();self._goal_sync_tree();self._refresh_chart_choices()

    def _render_fields(self):
        for child in self.field_tree.get_children():self.field_tree.delete(child)
        for field in (self.source or {}).get('fields',[]):
            name=field['name']
            self.field_tree.insert('', 'end', iid=name, text=field.get('label',name),values=(str(field.get('current')), 'Yes' if name in self.axes else ''))

    def _selected_field(self,event=None):
        ids=self.field_tree.selection()
        if not ids or not self.source:return
        name=ids[0];field=next((f for f in self.source['fields'] if f['name']==name),None)
        if not field:return
        axis=self.axes.get(name)
        defaults=dict(sampling='choices' if field.get('type') in ('bool','enum') or field.get('current') is None else 'linear',minimum=field.get('current',''),maximum=field.get('current',''),count=3,step=1,values='',group='')
        if field.get('type') in ('bool','boolean'):defaults['values']='false,true'
        elif field.get('type') in ('enum','choice','select','string','str') or field.get('options') or field.get('choices'):
            defaults['sampling']='choices'
            defaults['values']=','.join(str(x.get('id', x.get('value'))) if isinstance(x,dict) else str(x) for x in (field.get('options') or field.get('choices') or []))
        elif field.get('current') is None:defaults['values']='none'
        defaults.update(axis or {})
        if isinstance(defaults.get('values'),list):defaults['values']=','.join('none' if v is None else str(v).lower() if isinstance(v,bool) else str(v) for v in defaults['values'])
        for k,var in self.edit.items():var.set(str(defaults.get(k,'')))
        self.active_field=name

    def set_axis(self):
        if not self.source:return
        ids=self.field_tree.selection()
        if not ids:return
        try:
            fields={f['name']:f for f in self.source['fields']}
            proposed=copy.deepcopy(self.axes)
            for name in ids:proposed[name]=axis_from_editor(fields[name],{k:v.get() for k,v in self.edit.items()})
            from emx_search import SearchSpace
            SearchSpace(self.source['fields'],list(proposed.values()))
            self.axes=proposed;self._render_fields();self._estimate();self._refresh_chart_choices()
        except Exception as exc:messagebox.showerror('Invalid parameter range',str(exc),parent=self)

    def remove_axis(self):
        for name in self.field_tree.selection():self.axes.pop(name,None)
        self._render_fields();self._estimate();self._refresh_chart_choices()

    def _estimate(self):
        try:
            from emx_search import SearchSpace
            count=SearchSpace((self.source or {}).get('fields',[]),list(self.axes.values())).size
            self.estimate.set('Sweep: %s combinations' % count)
        except Exception as exc:self.estimate.set('Range error: %s' % exc)

    def _parse_goals(self, text, constraint=False):
        result=[]
        for number,line in enumerate(text.strip().splitlines(),1):
            if not line.strip():continue
            parts=[p.strip() for p in line.split(',')]
            if len(parts)<3 or len(parts)>5:raise ValueError('Goal line %s requires metric, Hz, direction/operator, optional value and scale.' % number)
            metric=parts[0]
            if metric not in ('L','Q','Lp','Ls','k','Qp','Qs'):raise ValueError('Unknown metric on line %s.' % number)
            allowed=(self.source or {}).get('definition',{}).get('emx_metrics')
            if allowed and metric not in allowed:raise ValueError('Metric %s is unavailable for this model; choose %s.' % (metric, ', '.join(allowed)))
            freq=_number(parts[1],'Goal frequency');op=parts[2].lower()
            if freq<=0:raise ValueError('Goal frequency must be positive.')
            if constraint:
                if op not in ('<=','>=','between') or len(parts)<4:raise ValueError('Constraint needs <=, >= or between and a limit.')
                limit=[_number(p,'Limit') for p in parts[3].split(':')] if op=='between' else _number(parts[3],'Limit')
                if op=='between' and (len(limit)!=2 or limit[0]>limit[1]):raise ValueError('Between requires ordered low:high limits.')
                row=dict(metric=metric,frequency_hz=freq,operator=op,limit=limit)
            else:
                if op not in ('minimize','maximize','target'):raise ValueError('Direction must be minimize, maximize or target.')
                row=dict(metric=metric,frequency_hz=freq,direction=op)
                if op=='target':
                    if len(parts)<4 or not parts[3]:raise ValueError('Target requires a value.')
                    row['target']=_number(parts[3],'Target')
            row['scale']=_number(parts[4],'Scale') if len(parts)>4 and parts[4] else 1.0
            if row['scale']<=0:raise ValueError('Scale must be positive.')
            result.append(row)
        return result

    def config(self):
        get=lambda k:self.v[k].get().strip()
        freqs=frequency_list(get('freq_values') if get('freq_mode') == 'list' else get('freq_start'),get('freq_stop'),get('freq_mode'),get('freq_amount'))
        objectives=self._parse_goals(self.objectives.get('1.0','end'))
        constraints=self._parse_goals(self.constraints.get('1.0','end'),True)
        if self.mode.get()=='optimize' and not objectives:raise ValueError('Optimization requires at least one objective.')
        for goal in objectives+constraints:
            frequency=goal['frequency_hz']
            if sum(abs(f-frequency)<=max(abs(frequency)*1e-12,1e-12) for f in freqs)!=1:
                raise ValueError('Goal frequency %s Hz is not a unique EMX Settings sample. Change the goal or EMX Settings.' % frequency)
        emx=dict(edge_um=_number(get('edge'),'Edge'),thickness_um=_number(get('thickness'),'Thickness'),via_merge_um=_number(get('via'),'Via merge'),three_d=self.three_d.get(),frequencies_hz=freqs,extra_options=get('extra'),options_text=get('extra'),emx_executable=get('emx_executable'),process_file=get('process_file'),parallel=_number(get('parallel'),'Threads',True),memory_mb=_number(get('memory'),'Memory',True),timeout_seconds=_number(get('timeout'),'Timeout'))
        optimizer=dict(algorithm=self.opt['algorithm'].get(),population=_number(self.opt['population'].get(),'Population',True),generations=_number(self.opt['generations'].get(),'Generations',True),seed=_number(self.opt['seed'].get(),'Seed',True))
        return dict(mode=self.mode.get(),axes=copy.deepcopy(list(self.axes.values())),emx=emx,objectives=objectives,constraints=constraints,optimizer=optimizer,concurrency=_number(get('concurrency'),'Concurrency',True),output_dir=get('output'),cleanup_intermediates=self.cleanup.get())

    def load_results(self):
        if self.is_running or self._result_worker or self._loading:return
        directory=filedialog.askdirectory(parent=self,title='Choose campaign results folder')
        if directory:self._load_results_from_dir(directory)

    def _load_results_from_dir(self,directory):
        if self.is_running or self._result_worker or self._loading:return
        self._loading=True;self._load_generation+=1;generation=self._load_generation
        current=copy.deepcopy(self.source);results=self._load_queue
        self.status.set('Reading campaign results…');self._result_buttons_update()
        def work():
            try:
                from result_data import load_campaign
                results.put((generation,directory,load_campaign(directory,current),None))
            except Exception as exc:results.put((generation,directory,None,str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def _drain_loaded_results(self):
        try:generation,directory,snapshot,error=self._load_queue.get_nowait()
        except queue.Empty:return
        if generation!=self._load_generation:return
        self._loading=False
        if error:
            self._result_buttons_update();self.status.set('Could not load campaign results.')
            messagebox.showerror('Load Results',error,parent=self);return
        self._result_revision+=1;self._hydrate_state={};self._clear_fit_tabs()
        self.rows={};self.pareto_ids=[];self.results.delete(*self.results.get_children())
        self.sample_frequency.set('');self.frequency_combo.configure(values=())
        self.campaign=SimpleNamespace(source=snapshot['source'],config=snapshot['config'],run_id='history-'+uuid.uuid4().hex,is_running=False,events=queue.Queue(),output_dir=str(directory))
        self.log_lines=['Loaded campaign %s from %s; metrics are reconstructed from retained SNP files.' % (snapshot.get('original_run_id'),directory)]
        for row in snapshot['rows']:self._add_row(row)
        self._refresh_results();self._refresh_chart_choices();self.update_plot();self._result_buttons_update()
        self.status.set('Loaded %s candidates from %s; reading SNP data.' % (len(snapshot['rows']),directory))

    def start(self):
        if self.is_running or self._result_worker or self._loading:return
        try:
            self.refresh_source()
            if self.source is None:raise ValueError(self.source_text.get())
            config=self.config()
            from emx_search import SearchSpace
            space=SearchSpace(self.source['fields'],config['axes'])
            if config['mode']=='optimize' and not space.dimensions:raise ValueError('Select at least one varying parameter.')
            if not config['output_dir']:raise ValueError('Choose an output folder.')
            from emx_campaign import Campaign
            campaign=Campaign(copy.deepcopy(self.source),copy.deepcopy(config))
            self._result_revision+=1;self._hydrate_state={}
            self._clear_fit_tabs()
            self.rows={};self.pareto_ids=[];self.results.delete(*self.results.get_children());self.log_lines=[];self.progress['value']=0
            self.sample_frequency.set('');self.frequency_combo.configure(values=());self._legend_signature=None;self.update_plot()
            self.last_completed=0;self.last_total=None
            self.progress.configure(mode='indeterminate');self.progress.start(80)
            self.campaign=campaign;campaign.start();self.start_btn.configure(state='disabled');self.stop_btn.configure(state='normal');self.status.set('Starting…')
            self._update_video_button()
        except Exception as exc:
            self.progress.stop();self.progress.configure(mode='determinate',value=0)
            messagebox.showerror('Cannot start EMX',str(exc),parent=self)

    def stop(self):
        if self.is_running:
            self.campaign.cancel();self.status.set('Cancellation requested; waiting for owned jobs…');self.stop_btn.configure(state='disabled')

    def request_close(self,callback):
        self._close_callback=callback
        if self.is_running:self.stop()
        if self._result_worker:self._cancel_result_action()
        if not self.is_running and not self._result_worker:
            self._close_callback=None;callback()

    def _poll(self):
        self._drain_discovery()
        self._drain_loaded_results()
        self._drain_hydrated()
        try:
            progress = self._video_progress.get_nowait()
        except queue.Empty:
            pass
        else:
            if self._video_active and not self._result_cancel.is_set():
                self.status.set(progress)
        if self._drain_result_action():
            return
        if self.campaign:
            for _ in range(100):
                try:event=self.campaign.events.get_nowait()
                except queue.Empty:break
                self._event(event)
            if not self.is_running:
                while True:
                    try:self._event(self.campaign.events.get_nowait())
                    except queue.Empty:break
                self.start_btn.configure(state='disabled' if self._result_worker or self._loading else 'normal');self.stop_btn.configure(state='disabled')
                if self._close_callback and not self._result_worker:
                    callback=self._close_callback;self._close_callback=None;callback();return
        self._update_video_button()
        self._after=self.after(100,self._poll)

    def _event(self,event):
        if event.get('run_id') != getattr(self.campaign, 'run_id', None):return
        typ=event.get('type');message=event.get('message','')
        if typ in ('log','state','error') and message:
            self.log_lines.append(str(message));self.status.set(str(message))
        if typ=='progress':
            total=event.get('total');completed=event.get('completed') or 0
            self.last_completed=completed
            if total:self.last_total=total
            if total:
                self.progress.stop();self.progress.configure(mode='determinate',maximum=total,value=completed)
                self.status.set('%s / %s candidates' % (completed,total))
            else:
                if str(self.progress['mode']) != 'indeterminate':self.progress.configure(mode='indeterminate');self.progress.start(80)
                self.status.set('%s candidates · generation %s' % (completed,event.get('generation',0)))
        if typ=='candidate':self._add_row(event['row'])
        if typ=='complete':
            self.progress.stop()
            rows=event.get('rows',[])
            for row in rows:self._add_row(row)
            counts=event.get('counts') or {key:sum(row.get('status')==key for row in rows) for key in ('completed','failed','rejected','cancelled')}
            processed=sum(counts.get(key,0) for key in ('completed','failed','rejected','cancelled'))
            state=event.get('status','Finished')
            maximum=max(1,processed) if state in ('completed','partial') else max(1,self.last_total or processed+1,processed)
            self.progress.configure(mode='determinate',maximum=maximum,value=processed)
            self.pareto_ids=list(event.get('pareto_ids',[]))
            self._refresh_results()
            self.status.set('%s · %s completed, %s failed, %s rejected, %s cancelled · %s' % (state,counts.get('completed',0),counts.get('failed',0),counts.get('rejected',0),counts.get('cancelled',0),event.get('output_dir') or ''))
            self.update_plot()

    def _add_row(self,row):
        ident=str(row['id']);row=copy.deepcopy(row)
        existing=self.rows.get(ident);signature=self._snp_signature(row)
        state=self._hydrate_state.get(ident)
        if row.get('status')=='completed':
            row['metrics']=copy.deepcopy(existing.get('metrics',[])) if existing and state and state[0]==signature else []
            if existing and state and state[0]==signature:
                if existing.get('data_error'):row['data_error']=existing['data_error']
                if existing.get('_data_pending'):row['_data_pending']=True
                for key in ('objective_values','feasible','violation'):
                    if key in existing:row[key]=copy.deepcopy(existing[key])
        self.rows[ident]=row
        self._queue_hydration(ident,signature)
        # The frontier is computed over all completed, feasible candidates so far.
        from emx_search import pareto
        try:
            self.pareto_ids=[str(r['id']) for r in pareto(list(self.rows.values()))]
        except (TypeError, ValueError):
            self.pareto_ids=[]
        self._refresh_results()
        self._schedule_plot()

    @staticmethod
    def _snp_signature(row):
        path=row.get('snp_path')
        try:
            stat=os.stat(path);file_sig=(stat.st_dev,stat.st_ino,stat.st_size,getattr(stat,'st_mtime_ns',stat.st_mtime))
        except (OSError,TypeError):file_sig=None
        return (path,file_sig,json.dumps(row.get('network') or {},sort_keys=True),row.get('job_dir'))

    def _queue_hydration(self,ident,signature):
        row=self.rows[ident]
        if row.get('status')!='completed':return
        state=self._hydrate_state.get(ident)
        if state and state[0]==signature:return
        self._hydrate_serial+=1;serial=self._hydrate_serial
        self._hydrate_state[ident]=(signature,serial)
        row['metrics']=[];row['objective_values']=[];row['feasible']=None;row['violation']=None;row.pop('data_error',None);row.pop('evaluation_error',None);row['_data_pending']=True
        self._hydrate_tasks.put((self._result_revision,ident,serial,signature,copy.deepcopy(row)))

    def _drain_hydrated(self):
        changed=False
        while True:
            try:revision,ident,serial,signature,row=self._hydrate_done.get_nowait()
            except queue.Empty:break
            if revision!=self._result_revision or self._hydrate_state.get(ident)!=(signature,serial) or ident not in self.rows:continue
            if self._snp_signature(self.rows[ident])!=signature:
                self._queue_hydration(ident,self._snp_signature(self.rows[ident]));continue
            self.rows[ident]['metrics']=row.get('metrics',[]);self.rows[ident].pop('_data_pending',None)
            if row.get('data_error'):self.rows[ident]['data_error']=row['data_error'];self.log_lines.append('%s: %s' % (ident,row['data_error']))
            else:
                self.rows[ident].pop('data_error',None)
                config=getattr(self.campaign,'config',{}) or {}
                if config.get('objectives') or config.get('constraints'):
                    from emx_search import evaluate_response
                    try:self.rows[ident].update(evaluate_response(row['metrics'],config.get('objectives',[]),config.get('constraints',[])))
                    except (ValueError,TypeError,KeyError) as exc:
                        self.rows[ident]['evaluation_error']='Objective evaluation failed: %s' % exc;self.log_lines.append('%s: %s' % (ident,self.rows[ident]['evaluation_error']))
                else:self.rows[ident].update(feasible=True,violation=0.0)
            changed=True
        if changed:
            from emx_search import pareto
            try:self.pareto_ids=[str(r['id']) for r in pareto(list(self.rows.values()))]
            except (TypeError,ValueError):self.pareto_ids=[]
            self._refresh_results();self._schedule_plot();self._result_buttons_update()

    def _sample_value(self, row, metric):
        try: frequency=float(self.sample_frequency.get())
        except ValueError: return None
        for sample in row.get('metrics') or []:
            if sample.get('frequency_hz') == frequency:
                value=sample.get(metric)
                if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value): return value
        return None

    def _update_display_columns(self):
        # Prefer the campaign's frozen source when results belong to a run.
        campaign_source=getattr(self.campaign,'source',None) if self.rows else None
        source=campaign_source or self.source or {}
        declared=(source.get('definition') or {}).get('emx_metrics') or []
        metrics=[m for m in METRICS if m in declared]
        if not metrics:
            observed={m for row in self.rows.values() for sample in row.get('metrics') or [] for m in METRICS if m in sample}
            metrics=[m for m in METRICS if m in observed]
        if not metrics: metrics=list(METRICS)
        self.results.configure(displaycolumns=('status',)+tuple(metrics)+('values','objectives','constraint','snp'))

    def _refresh_results(self):
        self._update_display_columns()
        frequencies=sorted({float(s['frequency_hz']) for r in self.rows.values() for s in r.get('metrics') or []
                            if isinstance(s.get('frequency_hz'),(int,float)) and not isinstance(s.get('frequency_hz'),bool) and math.isfinite(s['frequency_hz'])})
        labels=[repr(f) for f in frequencies]
        self.frequency_combo.configure(values=labels)
        if self.sample_frequency.get() not in labels: self.sample_frequency.set(labels[0] if labels else '')
        for ident,row in self.rows.items():
            values=row.get('values') or {}; text=', '.join('%s=%s' % x for x in values.items())
            warnings=row.get('warnings') or []
            status=row.get('status','') + (' (%s warnings)' % len(warnings) if warnings else '')
            if row.get('data_error'):status+=' · SNP unavailable'
            elif row.get('evaluation_error'):status+=' · Objective unavailable'
            elif row.get('_data_pending'):status+=' · Reading SNP'
            feasible=row.get('feasible')
            if row.get('status') in ('failed','rejected','cancelled'): state='fail'
            elif row.get('status') == 'completed' and feasible is False: state='infeasible'
            elif row.get('status') == 'completed' and feasible is True: state='optimal' if ident in self.pareto_ids else 'feasible'
            else: state='pending'
            metrics=[self._sample_value(row,m) for m in METRICS]
            vals=(status,text,', '.join(map(str,row.get('objective_values') or [])), 'Yes' if row.get('status') == 'completed' and feasible is True else 'No' if row.get('status') == 'completed' and feasible is False else '—',Path(row['snp_path']).name if row.get('snp_path') else '') + tuple('%.7g' % v if v is not None else '—' for v in metrics)
            tags=(state,'warning') if warnings else (state,)
            if self.results.exists(ident): self.results.item(ident,values=vals,tags=tags)
            else: self.results.insert('','end',iid=ident,text=ident,values=vals,tags=tags)
        self._apply_sort()

    def _sort_results(self, metric):
        self.sort_reverse = not self.sort_reverse if self.sort_column == metric else False
        self.sort_column=metric;self._apply_sort()

    def _apply_sort(self):
        if not self.sort_column: return
        present=[];missing=[]
        for ident in self.results.get_children():
            value=self._sample_value(self.rows[ident],self.sort_column)
            (missing if value is None else present).append((ident,value))
        present.sort(key=lambda item:item[1],reverse=self.sort_reverse)
        for index,(ident,_) in enumerate(present+missing): self.results.move(ident,'',index)

    def _chart_context(self):
        campaign_config=getattr(self.campaign,'config',None)
        if campaign_config:
            return (getattr(self.campaign,'source',None) or self.source or {},campaign_config)
        try: goals=self._parse_goals(self.objectives.get('1.0','end'))
        except ValueError: goals=[]
        try: frequencies=frequency_list(self.v['freq_values'].get() if self.v['freq_mode'].get() == 'list' else self.v['freq_start'].get(),self.v['freq_stop'].get(),self.v['freq_mode'].get(),self.v['freq_amount'].get())
        except ValueError: frequencies=[]
        return self.source or {},dict(mode=self.mode.get(),axes=list(self.axes.values()),objectives=goals,emx=dict(frequencies_hz=frequencies))

    def _refresh_chart_choices(self):
        from emx_charts import axis_options
        source,config=self._chart_context()
        try: options=axis_options(source,config,list(self.rows.values()))
        except (ValueError,KeyError,TypeError): return
        params=options.get('parameters',[]);metrics=options.get('metrics',[])
        objectives=options.get('objectives',[])
        mode=config.get('mode','sweep')
        frequencies=[str(f) for f in options.get('frequencies_hz',[])]
        choices=dict(x=metrics if mode=='optimize' else params,y=metrics,grid_y=params,slice=params,z=metrics,x_frequency_hz=frequencies,y_frequency_hz=frequencies,z_frequency_hz=frequencies)
        for key,index in (('grid_y',1),('slice',2)):
            if params and self.chart_vars[key].get() not in params:self.chart_vars[key].set(params[min(index,len(params)-1)])
        if mode == 'optimize' and objectives:
            for key,index in (('x',0),('y',1),('z',2)):
                if not self.chart_vars[key].get() and len(objectives)>index:
                    self.chart_vars[key].set(objectives[index]['metric'])
                    self.chart_vars[key+'_frequency_hz'].set(str(objectives[index].get('frequency_hz','')))
        axis=self.chart_vars['slice'].get()
        dimension=next((d for d in options.get('dimensions',[]) if axis in d.get('names',[])),{})
        choices['slice_value']=[str(v) for v in dimension.get('values',[])]
        three_sweep=mode=='sweep' and len(options.get('dimensions',[]))==3
        three_opt=mode=='optimize' and len(objectives)==3
        enabled=dict(x=True,y=True,x_frequency_hz=mode=='optimize',y_frequency_hz=True,grid_y=mode=='sweep' and len(options.get('dimensions',[])) in (2,3),slice=three_sweep,slice_value=three_sweep,z=three_opt,z_frequency_hz=three_opt)
        self.pairs_button.configure(state='normal' if mode=='optimize' else 'disabled')
        for key,values in choices.items():
            self.chart_combos[key].configure(values=values,state='readonly' if enabled[key] else 'disabled')
            if self.chart_vars[key].get() not in values:self.chart_vars[key].set(values[0] if values else '')
        if mode=='sweep':
            dimensions=options.get('dimensions',[]);x=self.chart_vars['x'].get()
            xdim=next((d for d in dimensions if x in d.get('names',[])),None)
            grid=self.chart_vars['grid_y'].get()
            if xdim and grid in xdim.get('names',[]):
                other=next((d['names'][0] for d in dimensions if d is not xdim and d.get('names')),None)
                if other:self.chart_vars['grid_y'].set(other)
            if len(dimensions)==3:
                grid=self.chart_vars['grid_y'].get()
                remaining=next((d for d in dimensions if x not in d.get('names',[]) and grid not in d.get('names',[])),None)
                if remaining and self.chart_vars['slice'].get() not in remaining['names']:
                    self.chart_vars['slice'].set(remaining['names'][0])
                    values=[str(v) for v in remaining.get('values',[])]
                    self.chart_combos['slice_value'].configure(values=values)
                    if self.chart_vars['slice_value'].get() not in values:self.chart_vars['slice_value'].set(values[0] if values else '')

    def _editor_spec(self):
        source,config=self._chart_context();spec=dict(mode=config.get('mode','sweep'))
        for key,var in self.chart_vars.items():
            value=var.get()
            if key.endswith('_frequency_hz'):
                if value:
                    try:value=float(value)
                    except ValueError:raise ValueError('Frequency must be numeric.')
                else:value=None
            spec[key]=value
        from emx_charts import axis_options
        options=axis_options(source,config,list(self.rows.values()))
        dimension=next((d for d in options['dimensions'] if spec.get('slice') in d['names']),{})
        spec['slice_value']=next((v for v in dimension.get('values',[]) if str(v)==spec['slice_value']),spec['slice_value'])
        if not spec['x'] or not spec['y']:raise ValueError('Choose X and Y before adding or updating a chart.')
        return spec

    def _toggle_manager(self):
        if self.manager_open.get():self.chart_editor.pack(fill='x')
        else:self.chart_editor.pack_forget()

    def _current_chart(self):
        tab=self.chart_notebook.select()
        return next((chart for chart in self.charts if str(chart['frame'])==tab),None)

    def _new_chart(self,spec):
        self._chart_counter+=1
        frame=ttk.Frame(self.chart_notebook);frame.rowconfigure(1,weight=1);frame.columnconfigure(0,weight=1)
        bar=ttk.Frame(frame);bar.grid(row=0,column=0,columnspan=2,sticky='ew')
        visible=tk.BooleanVar(value=spec.get('legend_visible',True))
        plot=Plot(frame,self.select_row);plot.grid(row=1,column=0,sticky='nsew')
        legend=ttk.Frame(frame);legend.grid(row=1,column=1,sticky='ns')
        listing=tk.Listbox(legend,width=20,height=3,bg=fw.PLOT_BG,fg=INK,selectbackground='#395b70',exportselection=False);listing.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(legend,command=listing.yview);scroll.pack(side='right',fill='y');listing.configure(yscrollcommand=scroll.set)
        chart=dict(frame=frame,plot=plot,legend=legend,listing=listing,visible=visible,spec=copy.deepcopy(spec),legend_ids=[],number=self._chart_counter)
        def toggle():
            chart['spec']['legend_visible']=visible.get()
            if visible.get():legend.grid()
            else:legend.grid_remove()
        ttk.Checkbutton(bar,text='Legend',variable=visible,command=toggle).pack(side='left')
        ttk.Label(bar,textvariable=plot.readout,style='Muted.TLabel').pack(side='left',fill='x',expand=True)
        chart['notes']=tk.StringVar(value='')
        ttk.Label(frame,textvariable=chart['notes'],style='Muted.TLabel',wraplength=500).grid(row=2,column=0,columnspan=2,sticky='w')
        def select(event):
            chosen=listing.curselection()
            if chosen and chosen[0]<len(chart['legend_ids']):
                ids=chart['legend_ids'][chosen[0]]
                if ids:self.select_row(ids[0])
        listing.bind('<<ListboxSelect>>',select)
        self.charts.append(chart);self.chart_notebook.add(frame,text='Chart %s' % self._chart_counter)
        if self.only_fit.get():
            self._pre_fit_tab_states[str(frame)]='normal';self.chart_notebook.tab(frame,state='hidden')
        toggle();self._sync_chart_selection()
        return chart

    def add_chart(self):
        try:
            self._refresh_chart_choices();spec=self._editor_spec()
            from emx_charts import build_chart
            source,config=self._chart_context();build_chart(spec,source,config,list(self.rows.values()),self.pareto_ids)
            chart=self._new_chart(spec);self.chart_notebook.select(chart['frame']);self.update_plot()
        except (ValueError,KeyError,TypeError) as exc:messagebox.showerror('Chart',str(exc),parent=self)

    def update_chart(self):
        chart=self._current_chart()
        if not chart:return
        try:
            spec=self._editor_spec()
            from emx_charts import build_chart
            source,config=self._chart_context();build_chart(spec,source,config,list(self.rows.values()),self.pareto_ids)
            spec['legend_visible']=chart['visible'].get();chart['spec']=spec;self._sync_chart_selection();self.update_plot()
        except (ValueError,KeyError,TypeError) as exc:messagebox.showerror('Chart',str(exc),parent=self)

    def delete_chart(self):
        chart=self._current_chart()
        if chart:self.charts.remove(chart);self.chart_notebook.forget(chart['frame']);chart['frame'].destroy();self._sync_chart_selection()

    def _clear_charts(self):
        for chart in self.charts:self.chart_notebook.forget(chart['frame']);chart['frame'].destroy()
        self.charts=[];self._sync_chart_selection()

    def add_all_pairs(self):
        from itertools import combinations
        from emx_charts import axis_options,build_chart
        source,config=self._chart_context()
        options=axis_options(source,config,list(self.rows.values()))
        goals=options.get('objectives',[]);metrics=options.get('metrics',[])
        frequencies=options.get('frequencies_hz',[])
        if config.get('mode')!='optimize' or not goals or not frequencies:
            messagebox.showerror('Chart','Optimization, a goal and a frequency are required.',parent=self);return
        goal_axes=list(dict.fromkeys((g['metric'],g.get('frequency_hz')) for g in goals))
        if len(goal_axes)>=2: axes=goal_axes
        else:
            try: frequency=float(self.chart_vars['y_frequency_hz'].get())
            except ValueError:frequency=frequencies[0]
            axes=[(metric,frequency) for metric in metrics]
        if len(axes)<2:
            messagebox.showerror('Chart','At least two metric and frequency axes are required.',parent=self);return
        for x,y in combinations(axes,2):
            spec=dict(mode='optimize',x=x[0],y=y[0],x_frequency_hz=x[1],y_frequency_hz=y[1])
            if len(goals)==3:
                color=next((axis for axis in axes if axis not in (x,y)),None)
                if color is None:continue
                spec.update(color=color[0],color_frequency_hz=color[1])
            try:build_chart(spec,source,config,list(self.rows.values()),self.pareto_ids)
            except ValueError:continue
            self._new_chart(spec)
        self.update_plot()

    def _sync_chart_selection(self):
        def axis(spec,key):
            frequency=spec.get(key+'_frequency_hz')
            return '%s @ %s Hz' % (spec.get(key,''),frequency) if spec.get('mode')=='optimize' and frequency is not None else str(spec.get(key,''))
        titles=['Chart %s: %s / %s' % (c['number'],axis(c['spec'],'x'),axis(c['spec'],'y')) for c in self.charts]
        self.chart_selector.configure(values=titles)
        current=self._current_chart()
        self.chart_selection.set(titles[self.charts.index(current)] if current else '')

    def _choose_chart(self,event=None):
        index=self.chart_selector.current()
        if 0 <= index < len(self.charts):self.chart_notebook.select(self.charts[index]['frame'])

    def _chart_selected(self,event=None):
        self._sync_chart_selection()
        chart=self._current_chart()
        if chart:
            self._refresh_chart_choices()
            for key,var in self.chart_vars.items():
                value=chart['spec'].get(key, chart['spec'].get({'z':'color','z_frequency_hz':'color_frequency_hz'}.get(key,'')))
                var.set(str(value) if key=='slice_value' and key in chart['spec'] else '' if value is None else str(value))

    def reset_chart(self):
        chart=self._current_chart()
        if chart:chart['plot'].reset()

    def _selection_changed(self):
        selected=self.results.selection();ident=selected[0] if selected else None
        if ident in self.rows:self._queue_hydration(ident,self._snp_signature(self.rows[ident]))
        self._result_buttons_update()
        for chart in self.charts:
            chart['plot'].highlight=ident;chart['plot'].draw();chart['listing'].selection_clear(0,'end')
            for index,ids in enumerate(chart['legend_ids']):
                if ident and ident in ids:chart['listing'].selection_set(index);chart['listing'].see(index);break

    @staticmethod
    def _fit_frequency_count(row):
        values=set()
        for sample in (row or {}).get('metrics') or []:
            frequency=sample.get('reported_frequency_hz',sample.get('frequency_hz'))
            if isinstance(frequency,(int,float)) and not isinstance(frequency,bool) and math.isfinite(frequency) and frequency>0:values.add(frequency)
        return len(values)

    def _result_buttons_update(self):
        selected=self.results.selection()
        row=self.rows.get(selected[0]) if selected else None
        eligible=bool(row and row.get('status')=='completed' and row.get('snp_path') and Path(row['snp_path']).is_file() and not row.get('data_error') and not row.get('_data_pending') and getattr(self.campaign,'source',None) and not self.campaign.source.get('offline'))
        identity=(row or {}).get('source') or {}
        fit_ready=eligible and self._fit_frequency_count(row)>=2
        if row:
            self.result_target.set('Candidate %s · Target %s / %s · SNP %s%s' % (row.get('id'),identity.get('library','?'),identity.get('cell','?'),row.get('snp_path') or 'unavailable',(' · '+row['data_error']) if row.get('data_error') else ''))
        else:self.result_target.set('Select a completed candidate with an existing SNP.')
        if eligible and not fit_ready:self.result_target.set(self.result_target.get()+' · Schematic fitting requires at least 2 distinct measured frequency points; SNPFit Studio can open this SNP for viewing.')
        for index,button in enumerate(self.result_buttons):button.configure(state='normal' if eligible and (index!=1 or fit_ready) and not self._result_worker and not self._loading else 'disabled')
        if hasattr(self,'start_btn') and not self.is_running:self.start_btn.configure(state='disabled' if self._result_worker or self._loading else 'normal')
        self.load_results_btn.configure(state='disabled' if self._result_worker or self._loading or self.is_running else 'normal')
        self.result_cancel_button.configure(state='normal' if self._result_worker else 'disabled')

    def _video_ready(self):
        campaign = self.campaign
        return bool(campaign and not self.is_running and not self._loading and
                    not self._result_worker and self.rows and
                    getattr(campaign, 'output_dir', None) and
                    campaign.config.get('mode') == 'optimize')

    def _update_video_button(self):
        ready = self._video_ready()
        self.video_btn.configure(text='Cancel Video' if self._video_active else 'Export Video',
                                 state='normal' if self._video_active or ready else 'disabled')
        path = self._portable_video_path() if ready else None
        self.video_play_btn.configure(state='normal' if path and path.is_file() else 'disabled')

    def _portable_video_path(self):
        directory = getattr(self.campaign, 'output_dir', None)
        if directory:
            return Path(directory) / 'optimization-history.png'

    def play_video(self):
        path = self._portable_video_path()
        if path is None:
            return
        try:
            from video_player import VideoPlayer
            VideoPlayer(self, path)
        except (ImportError, OSError, RuntimeError, ValueError, tk.TclError) as exc:
            messagebox.showerror('Play Video', str(exc), parent=self)

    def export_video(self):
        if self._video_active:
            self._cancel_result_action()
            return
        if not self._video_ready():
            return
        try:
            fps = _number(self.video_fps.get(), 'Video FPS', integer=True)
            if not 1 <= fps <= 60:
                raise ValueError('Video FPS must be an integer from 1 to 60.')
        except ValueError as exc:
            messagebox.showerror('Export Video', str(exc), parent=self)
            return

        campaign = self.campaign
        revision, run_id = self._result_revision, campaign.run_id
        rows = [dict(row, metrics=[]) for row in self.rows.values()]
        source, config = campaign.source, campaign.config
        current = self.source or {}
        if (current.get('schema') == 'flux.emx.source.v1' and
                current.get('identity') == source.get('identity') and
                current.get('design', {}).get('model') == source.get('design', {}).get('model') and
                source.get('schema') != 'flux.emx.source.v1'):
            source = current
        directory = campaign.output_dir
        cancel = self._result_cancel = threading.Event()
        self._video_active = True
        self.status.set('Preparing optimization video…')

        def progress(message):
            try:
                self._video_progress.put_nowait(message)
            except queue.Full:
                pass

        def work():
            result = None
            error = None
            try:
                from optimization_video import export_optimization
                result = export_optimization(rows, source, config, directory, fps,
                                             cancel_event=cancel, progress=progress)
            except InterruptedError:
                result = {'cancelled': True}
            except Exception as exc:
                error = str(exc)
            self._result_queue.put((revision, run_id, None, 'export_video', result, error))

        self._result_worker = threading.Thread(target=work, daemon=True)
        self._result_worker.start()
        self._result_buttons_update()
        self._update_video_button()

    def _start_result_action(self,action):
        selected=self.results.selection()
        if not selected or self._result_worker or self._loading:return
        ident=selected[0]
        if ident not in self.rows:return
        self._queue_hydration(ident,self._snp_signature(self.rows[ident]))
        row=copy.deepcopy(self.rows.get(ident) or {})
        source=copy.deepcopy(getattr(self.campaign,'source',None))
        if not source or row.get('status')!='completed' or row.get('data_error') or row.get('_data_pending') or not row.get('snp_path') or not Path(row['snp_path']).is_file():
            self._result_buttons_update();return
        if action=='fit_schematic' and self._fit_frequency_count(row)<2:
            self._result_buttons_update();return
        revision=self._result_revision;run_id=getattr(self.campaign,'run_id',None)
        output=(getattr(self.campaign,'config',{}) or {}).get('output_dir')
        cancel=threading.Event();self._result_cancel=cancel
        self.log_lines.append('%s: candidate %s · %s' % (action,ident,row['snp_path']))
        self.status.set('%s: candidate %s' % (action,ident))
        def work():
            try:
                import flux_results
                fn=getattr(flux_results,action)
                kwargs=dict(output_dir=output)
                if action!='open_snpfit':kwargs['cancel_event']=cancel
                receipt=fn(row,source,**kwargs)
                self._result_queue.put((revision,run_id,ident,action,receipt,None))
            except Exception as exc:self._result_queue.put((revision,run_id,ident,action,None,str(exc)))
        self._result_worker=threading.Thread(target=work,daemon=True);self._result_worker.start();self._result_buttons_update()

    def _cancel_result_action(self):
        if self._result_cancel:
            self._result_cancel.set();self.status.set('Cancellation requested; waiting for result action.')
            self.log_lines.append('Cancellation requested for result action.')

    def _drain_result_action(self):
        try:revision,run_id,ident,action,receipt,error=self._result_queue.get_nowait()
        except queue.Empty:return
        if action == 'export_video':
            self._video_active = False
        self._result_worker=None;self._result_cancel=None;self._result_buttons_update()
        if revision!=self._result_revision or run_id!=getattr(self.campaign,'run_id',None):return
        if action == 'export_video':
            if error:
                self.status.set('Video export failed.')
                self.log_lines.append('Video export failed. ' + error)
                if not self._close_callback:
                    messagebox.showerror('Export Video', error, parent=self)
            elif receipt.get('cancelled'):
                self.status.set('Video export cancelled.')
            else:
                message = 'Video saved · {layouts} layouts · {missing_layouts} status frames · {fps} fps · {path}'.format(**receipt)
                self.log_lines.append(message)
                self.status.set(message)
        elif error:
            self.log_lines.append('%s candidate %s failed: %s' % (action,ident,error));self.status.set('Result action failed; see Campaign log.')
            messagebox.showerror('Result action',error,parent=self)
        else:
            receipt=receipt or {};self.log_lines.append('%s candidate %s: %s' % (action,ident,receipt))
            self.status.set('%s completed for candidate %s · %s' % (action,ident,receipt.get('log_path','')))
            if action=='fit_schematic' and receipt.get('comparison'):self._show_fit_comparison(ident,receipt)
        if self._close_callback and not self.is_running:
            callback=self._close_callback;self._close_callback=None;callback()
            return True

    def _toggle_fit_only(self):
        for chart in self.charts:
            frame=chart['frame'];key=str(frame)
            if self.only_fit.get():
                if key not in self._pre_fit_tab_states:self._pre_fit_tab_states[key]=self.chart_notebook.tab(frame,'state')
                self.chart_notebook.tab(frame,state='hidden')
            else:self.chart_notebook.tab(frame,state=self._pre_fit_tab_states.pop(key,'normal'))
        if self.only_fit.get() and self._fit_tabs:self.chart_notebook.select(self._fit_tabs[-1])
        self._sync_chart_selection()

    def _clear_fit_tabs(self):
        for frame in self._fit_tabs:
            self.chart_notebook.forget(frame);frame.destroy()
        self._fit_tabs=[]

    def _show_fit_comparison(self,ident,receipt):
        comparison=receipt['comparison'];freq=comparison.get('frequency_hz') or []
        original=comparison.get('original') or {};fitted=comparison.get('fitted') or {}
        for metric in METRICS:
            if metric not in original or metric not in fitted:continue
            if len(original[metric])!=len(freq) or len(fitted[metric])!=len(freq):
                self.log_lines.append('Fit comparison %s omitted: frequency and value lengths differ.' % metric);continue
            frame=ttk.Frame(self.chart_notebook);frame.columnconfigure(0,weight=1);frame.rowconfigure(1,weight=1)
            bar=ttk.Frame(frame);bar.grid(row=0,column=0,sticky='ew')
            plot=Plot(frame,self.select_row);plot.grid(row=1,column=0,sticky='nsew')
            listing=tk.Listbox(frame,height=3,width=18,exportselection=False,bg=fw.PLOT_BG,fg=INK);listing.grid(row=1,column=1,sticky='ns')
            curves=[]
            for index,(name,values) in enumerate((('Original SNP',original[metric]),('Fit',fitted[metric]))):
                points=[]
                for x,y in zip(freq,values):
                    if isinstance(x,(int,float)) and isinstance(y,(int,float)) and math.isfinite(x) and math.isfinite(y):points.append((x,y,str(ident),index,SERIES_COLORS[index]))
                curves.append((name,points));listing.insert('end',name);listing.itemconfigure('end',foreground=SERIES_COLORS[index])
            def redraw(p=plot,c=curves,m=metric):p.set_points([point for _,points in c for point in points],'Frequency (Hz)',m + (' (nH)' if m in ('L','Lp','Ls') else ''))
            def delete_curve(c=curves,l=listing,draw=redraw):
                selected=l.curselection()
                if selected:
                    idx=selected[0];c.pop(idx);l.delete(idx);draw()
            ttk.Button(bar,text='Delete selected curve',style='Danger.TButton',command=delete_curve).pack(side='left')
            ttk.Label(bar,text='Candidate %s · %s' % (ident,metric)).pack(side='left')
            self._fit_tabs.append(frame);self.chart_notebook.add(frame,text='Fit %s %s' % (ident,metric));redraw()
        if self._fit_tabs:self.chart_notebook.select(self._fit_tabs[-1])
        self._toggle_fit_only()

    def _schedule_plot(self):
        if self._plot_after is None:self._plot_after=self.after(100,self._flush_plot)

    def _flush_plot(self):
        self._plot_after=None;self.update_plot()

    def select_row(self,ident):
        ident=str(ident)
        if self.results.exists(ident):self.results.selection_set(ident);self.results.see(ident)

    def update_plot(self):
        from emx_charts import build_chart
        for ident in list(self.rows):self._queue_hydration(ident,self._snp_signature(self.rows[ident]))
        source,config=self._chart_context()
        for chart in self.charts:
            plot=chart['plot'];listing=chart['listing'];points=[];legend=[]
            try:data=build_chart(chart['spec'],source,config,list(self.rows.values()),self.pareto_ids)
            except (ValueError,KeyError,TypeError) as exc:
                chart['data']={};chart['legend_ids']=[];listing.delete(0,'end');chart['notes'].set(str(exc))
                plot.chart_type='line';plot.surface_points=[];plot.triangles=[];plot.surface_axes=None;plot.color_values={};plot.color_range=None;plot.color_label=''
                plot.set_points([],'','');plot.readout.set(str(exc));continue
            chart['data']=data
            plot.surface_points=data.get('points',[]) if data.get('type')=='surface' else []
            plot.triangles=data.get('triangles',[]) if data.get('type')=='surface' else []
            plot.zlabel=data.get('z_label','')
            surface_axes=(data.get('x_label',''),data.get('y_label',''),plot.zlabel) if data.get('type')=='surface' else None
            if surface_axes != plot.surface_axes:
                plot.azimuth=-.7;plot.elevation=.5;plot.surface_zoom=1.;plot.manual_view=False
                plot.surface_axes=surface_axes
            if data.get('type')=='surface':
                plot.color_values={str(p['id']):p['z'] for p in plot.surface_points};plot.color_label=plot.zlabel
            series=data.get('series') or []
            if not series and data.get('points'):series=[dict(label='Measured',points=data['points'])]
            all_colors=[p.get('color') for series_item in series for p in series_item.get('points',[]) if isinstance(p.get('color'),(int,float)) and math.isfinite(p['color'])]
            lo=min(all_colors) if all_colors else 0;hi=max(all_colors) if all_colors else 1
            type_changed=plot.chart_type!=data.get('type','line')
            plot.x_grid=data.get('x_grid',[]);plot.y_grid=data.get('y_grid',[])
            plot.breaks=set();plot.chart_type=data.get('type','line');plot.top_margin=55 if plot.chart_type=='heatmap' else 20;plot.x_ticks=data.get('x_ticks') or [];plot.y_ticks=data.get('y_ticks') or [];plot.color_label=data.get('color_label') or '';plot.color_range=(lo,hi) if all_colors else None;plot.color_values=({str(p['id']):p['z'] for p in plot.surface_points} if plot.chart_type=='surface' else {str(p['id']):p['color'] for item in series for p in item.get('points',[]) if isinstance(p.get('color'),(int,float)) and math.isfinite(p['color'])});plot.color_label=plot.zlabel if plot.chart_type=='surface' else plot.color_label
            for index,item in enumerate(series):
                ids=[];color=SERIES_COLORS[index%len(SERIES_COLORS)]
                for point in item.get('points',[]):
                    ident=str(point['id']);ids.append(ident);shade=color
                    c=point.get('color')
                    if isinstance(c,(int,float)) and math.isfinite(c):
                        t=(c-lo)/(hi-lo) if hi!=lo else .5;shade=value_color(t)
                    if point.get('break_before'):plot.breaks.add(ident)
                    points.append((point['x'],point['y'],ident,index,shade))
                legend.append((item.get('label','Series %s' % (index+1)),ids,color))
            listing.delete(0,'end');chart['legend_ids']=[]
            for label,ids,color in legend:
                listing.insert('end',label);listing.itemconfigure('end',foreground=color);chart['legend_ids'].append(ids)
            if plot.chart_type=='heatmap':
                if not plot.x_grid:plot.x_grid=sorted(set(p[0] for p in points))
                if not plot.y_grid:plot.y_grid=sorted(set(p[1] for p in points))
                def grid_ticks(grid):
                    values=sorted(set(grid));indices=sorted(set(round(i*(len(values)-1)/4) for i in range(min(5,len(values)))))
                    return [(values[i],'%.5g' % values[i]) for i in indices]
                if not plot.x_ticks:plot.x_ticks=grid_ticks(plot.x_grid)
                if not plot.y_ticks:plot.y_ticks=grid_ticks(plot.y_grid)
            plot.set_points(points,data.get('x_label',''),data.get('y_label',''))
            if type_changed:plot.reset()
            notes=data.get('notes') or [];conflicts=data.get('conflicts') or []
            if all_colors:notes=list(notes)+['Color: %.5g to %.5g' % (lo,hi)]
            chart['notes'].set(' · '.join(map(str,notes+conflicts)))
            plot.readout.set('')
        self._selection_changed()

    def _objective_label(self, index):
        config=getattr(self.campaign,'config',{}) or {}
        goals=config.get('objectives') or []
        if index < 0 or index >= len(goals):return 'Objective %s' % (index+1)
        goal=goals[index];metric=str(goal.get('metric') or 'Objective %s' % (index+1))
        unit=' (nH)' if metric in ('L','Lp','Ls') else ''
        frequency=goal.get('frequency_hz')
        try:frequency='%.6g Hz' % float(frequency)
        except (TypeError,ValueError):frequency='? Hz'
        direction=goal.get('direction') or ''
        if direction == 'target':direction='target %s' % goal.get('target','?')
        return '%s%s · %s · %s' % (metric,unit,frequency,direction)

    def _popup(self,event):
        ident=self.results.identify_row(event.y)
        if not ident:return
        self.select_row(ident);row=self.rows.get(ident,{})
        self.menu.entryconfigure(0,state='normal' if row.get('log_path') else 'disabled')
        self.menu.entryconfigure(2,state='normal' if row.get('snp_path') else 'disabled')
        source=row.get('source') or {}
        self.menu.entryconfigure(3,state='normal' if all(source.get(k) for k in ('library','cell','view')) else 'disabled')
        self.menu.tk_popup(event.x_root,event.y_root)

    def candidate_details(self):
        ids=self.results.selection()
        if not ids:return
        row=self.rows[ids[0]];source=row.get('source') or {};design=row.get('design') or {}
        details=dict(candidate=row.get('id'),status=row.get('status'),
                     source={key:source.get(key) for key in ('library','cell','view','technology','workspace','cds_lib','library_path')},
                     parameters=design.get('parameters',{}),layers=design.get('layers',{}),structure=design.get('structure'),
                     warnings=row.get('warnings') or [],error=row.get('error'),errors=row.get('errors') or [],
                     metric_units={'frequency_hz':'Hz','L':'nH','Lp':'nH','Ls':'nH','Q':'dimensionless','Qp':'dimensionless','Qs':'dimensionless','k':'dimensionless'},
                     metrics=row.get('metrics') or [])
        self._log_window('Candidate details · '+str(ids[0]),json.dumps(details,ensure_ascii=False,indent=2,default=str),readonly=True)

    def open_candidate_log(self):
        selection=self.results.selection()
        if not selection:return
        path=self.rows[selection[0]].get('log_path')
        try:
            with open(path,'r',encoding='utf-8',errors='replace') as file:content=file.read()
            self._log_window('EMX log · '+path,content)
        except Exception as exc:messagebox.showerror('Cannot open log',str(exc),parent=self)

    def copy_snp(self):
        ids=self.results.selection()
        if ids:
            self.clipboard_clear();self.clipboard_append(self.rows[ids[0]].get('snp_path') or '')

    def copy_layout_identity(self):
        ids=self.results.selection()
        if ids:
            source=self.rows[ids[0]].get('source') or {}
            value='/'.join(str(source.get(k,'')) for k in ('library','cell','view'))
            self.clipboard_clear();self.clipboard_append(value)

    def show_log(self):self._log_window('Campaign log','\n'.join(self.log_lines))

    def _log_window(self,title,content,readonly=False):
        window=tk.Toplevel(self);window.title('Flux Studio — '+title);window.geometry('800x500');window.rowconfigure(1,weight=1);window.columnconfigure(0,weight=1)
        fw.apply(window,product='flux');fw.header(window,'Flux Studio',title).grid(row=0,column=0,columnspan=2,sticky='ew')
        text=tk.Text(window,bg=fw.PLOT_BG,fg=INK,insertbackground=INK,wrap='none');text.grid(row=1,column=0,sticky='nsew');text.insert('1.0',content)
        if readonly:text.configure(state='disabled')
        scroll=ttk.Scrollbar(window,command=text.yview);scroll.grid(row=1,column=1,sticky='ns');text.configure(yscrollcommand=scroll.set)
        ttk.Button(window,text='Save log',command=lambda:self._save_text(text.get('1.0','end-1c'))).grid(row=2,column=0,sticky='e')

    def _save_text(self,content):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.log')
        if path:
            with open(path,'w',encoding='utf-8') as file:file.write(content)

    def export_csv(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if not path:return
        with open(path,'w',encoding='utf-8',newline='') as file:
            writer=csv.writer(file)
            chart=self._current_chart()
            if chart:
                data=chart.get('data') or {};spec=chart['spec']
                writer.writerow(['candidate','x_label','x','y_label','y','z_label','z','color_metric','color_value','series','slice_axis','slice_value'])
                export_series=data.get('series') or ([dict(label='Measured',points=data.get('points',[]))] if data.get('points') else [])
                for item in export_series:
                    for point in item.get('points',[]):
                        writer.writerow([point.get('id'),data.get('x_label'),point.get('x'),data.get('y_label'),point.get('y'),data.get('z_label'),point.get('z'),data.get('color_label'),point.get('color'),item.get('label'),spec.get('slice',''),spec.get('slice_value','')])

    def choose_output(self):
        path=filedialog.askdirectory(parent=self)
        if path:self.v['output'].set(path)

    def save_settings(self):
        if not self.source:messagebox.showerror('Save settings','Sync an applied layout first.',parent=self);return
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.json',filetypes=[('JSON','*.json')])
        if not path:return
        values={k:v.get() for k,v in self.v.items()}
        for key,auto in self._auto_paths.items():
            if values[key].strip()==auto:values[key]=''
        data=dict(schema='flux.emx.ui.settings.v1',model=self.source['design']['model'],output_is_auto=bool(self.auto_output and self.v['output'].get().strip()==self.auto_output),values=values,axes=list(self.axes.values()),mode=self.mode.get(),objectives=self.objectives.get('1.0','end-1c'),constraints=self.constraints.get('1.0','end-1c'),optimizer={k:v.get() for k,v in self.opt.items()},three_d=self.three_d.get(),cleanup=self.cleanup.get(),charts=[copy.deepcopy(chart['spec']) for chart in self.charts])
        try:
            with open(path,'x',encoding='utf-8') as file:json.dump(data,file,indent=2,allow_nan=False)
        except FileExistsError:messagebox.showerror('Save settings','Choose a new file to avoid overwriting existing settings.',parent=self)
        except Exception as exc:messagebox.showerror('Save settings',str(exc),parent=self)

    def load_settings(self):
        if not self.source:messagebox.showerror('Load settings','Sync an applied layout first.',parent=self);return
        path=filedialog.askopenfilename(parent=self,filetypes=[('JSON','*.json')])
        if not path:return
        try:
            with open(path,'r',encoding='utf-8') as file:data=json.load(file)
            if data.get('schema')!='flux.emx.ui.settings.v1' or data.get('model')!=self.source['design']['model']:raise ValueError('Settings do not match the applied model.')
            from emx_search import SearchSpace
            axes=data.get('axes',[]);SearchSpace(self.source['fields'],axes)
            self._discovery_generation+=1
            self._discovery_request=None
            for key,auto in self._auto_paths.items():
                if key not in data.get('values',{}) and self.v[key].get().strip()==auto:self.v[key].set('')
            for key,value in data.get('values',{}).items():
                if key in self.v:self.v[key].set(value)
            if data.get('output_is_auto') is True and self.auto_output:
                self.v['output'].set(self.auto_output)
            for key,value in data.get('optimizer',{}).items():
                if key in self.opt:self.opt[key].set(value)
            if 'extra' not in data.get('values',{}): self.v['extra'].set('--3d=*' if data.get('three_d',False) else '--3d=-*')
            elif data.get('three_d',False) and '--3d' not in self.v['extra'].get(): self.v['extra'].set('--3d=* ' + self.v['extra'].get())
            self._auto_paths={}
            self._discover_paths(self.source)
            self.axes={a['name']:a for a in axes};self.mode.set(data.get('mode','sweep'));self.three_d.set(data.get('three_d',False));self.cleanup.set(data.get('cleanup',False))
            for widget,key in ((self.objectives,'objectives'),(self.constraints,'constraints')):widget.delete('1.0','end');widget.insert('1.0',data.get(key,''))
            self._goal_sync_tree()
            charts=data.get('charts',[])
            if not isinstance(charts,list) or any(not isinstance(chart,dict) for chart in charts):raise ValueError('Invalid chart settings.')
            self._clear_charts()
            for spec in charts:self._new_chart(spec)
            self._render_fields();self._estimate();self._refresh_chart_choices();self.update_plot()
        except Exception as exc:messagebox.showerror('Load settings',str(exc),parent=self)

    def destroy(self):
        if self.is_running or self._result_worker:
            self.request_close(self.destroy);return
        if self._after:
            self.after_cancel(self._after);self._after=None
        if self._plot_after:
            self.after_cancel(self._plot_after);self._plot_after=None
        self._hydrate_tasks.put(None)
        if self._external_settings:
            for widget in self._settings_widgets:widget.destroy()
        super().destroy()
