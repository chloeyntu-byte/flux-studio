"""Small Tk UI: choose one plugin, enter fresh tolerances, inspect and deliver."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from .network import read_touchstone
from .workflow import diagnose, fit_file, json_write, runtime_root


def _field(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


class App:
    def __init__(self, root, runtime=None):
        from .models import list_models
        self.root = root
        self.runtime = runtime_root(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.models = list(list_models())
        if isinstance(self.models[0], str):
            from .models import get_model
            self.models = [get_model(m) for m in self.models]
        self.labels = [_field(m, 'label', 'name', 'model_id', 'id') for m in self.models]
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.result = None
        self.source = None
        self.token = 0
        root.title('SNPFit · 无源等效原理图')
        root.geometry('1060x790')
        root.minsize(850, 640)
        self.v = {k: tk.StringVar() for k in (
            'file','model','ports','fmin','fmax','l','q','workspace','library','cell')}
        self.v['library'].set('SNPFit_models')
        self.v['cell'].set('passive_fit')
        self.v['fmin'].set('1')
        self.v['fmax'].set('50')
        self.confirm_ports = tk.BooleanVar(value=False)
        self.short_unused = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='选择 SNP 和明确的模型，然后填写本次误差。')
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(0, minsize=110)
        outer.rowconfigure(8, weight=1)
        ttk.Label(outer, text='SNPFit', font=('sans',18,'bold')).grid(row=0,column=0,sticky='w')
        ttk.Label(outer, text='SNP → 指定模型拟合 → 可编辑 schematic / symbol').grid(row=0,column=1,sticky='w')
        self.entry(outer,1,'SNP 文件','file', lambda: self.browse_snp())
        ttk.Label(outer,text='模型').grid(row=2,column=0,sticky='w',pady=5)
        self.modelbox=ttk.Combobox(outer,textvariable=self.v['model'],values=self.labels,state='readonly')
        self.modelbox.grid(row=2,column=1,columnspan=2,sticky='ew')
        self.modelbox.bind('<<ComboboxSelected>>',self.model_changed)
        setup=ttk.LabelFrame(outer,text='本次拟合',padding=8)
        setup.grid(row=3,column=0,columnspan=3,sticky='ew',pady=6)
        setup.columnconfigure(1,weight=1)
        self.role_label=ttk.Label(setup,text='先选择模型',wraplength=880)
        self.role_label.grid(row=0,column=0,columnspan=6,sticky='w')
        ttk.Label(setup,text='文件端口编号').grid(row=1,column=0,sticky='w')
        ttk.Entry(setup,textvariable=self.v['ports'],width=24).grid(row=1,column=1,sticky='ew',padx=6)
        ttk.Checkbutton(setup,text='未选端口短路到参考地',variable=self.short_unused).grid(row=1,column=2,columnspan=3,sticky='w')
        ttk.Checkbutton(setup,text='确认端口顺序、正负端和上述端接',variable=self.confirm_ports).grid(row=2,column=0,columnspan=6,sticky='w',pady=4)
        fields=[('起始 GHz','fmin'),('终止 GHz','fmax'),('L 最大误差 %','l'),('Q 最大误差 %','q')]
        inputs=ttk.Frame(setup)
        inputs.grid(row=3,column=0,columnspan=6,sticky='ew')
        for i,(label,key) in enumerate(fields):
            inputs.columnconfigure(i,weight=1,uniform='fit_inputs')
            box=ttk.Frame(inputs)
            box.grid(row=0,column=i,sticky='ew',padx=(0,12))
            ttk.Label(box,text=label).pack(anchor='w')
            ttk.Entry(box,textvariable=self.v[key],width=15).pack(fill='x')
        ttk.Label(setup,text='误差每次必填，范围 5–20%；四类元件各 ≤20。频段可修改，每次重新拟合结构和数值。').grid(row=4,column=0,columnspan=6,sticky='w',pady=(7,0))
        self.strategy_text=tk.StringVar(value='策略：填写本次 L/Q 后自动选择')
        ttk.Label(setup,textvariable=self.strategy_text).grid(row=5,column=0,columnspan=6,sticky='w',pady=(3,0))
        for key in ('l','q'):self.v[key].trace_add('write',self.update_strategy_preview)
        bar=ttk.Frame(outer)
        bar.grid(row=4,column=0,columnspan=3,sticky='ew',pady=4)
        self.fit_button=ttk.Button(bar,text='开始拟合',command=self.start_fit)
        self.fit_button.pack(side='left')
        self.stop_button=ttk.Button(bar,text='取消',command=self.cancel.set,state='disabled')
        self.stop_button.pack(side='left',padx=8)
        ttk.Button(bar,text='环境诊断',command=self.doctor).pack(side='left')
        ttk.Button(bar,text='复制结果路径',command=self.show_path).pack(side='right')
        self.progress=ttk.Progressbar(outer,mode='indeterminate')
        self.progress.grid(row=5,column=0,columnspan=3,sticky='ew',pady=3)
        ttk.Label(outer,textvariable=self.status,wraplength=970).grid(row=6,column=0,columnspan=3,sticky='w',pady=3)
        self.tabs=ttk.Notebook(outer)
        self.tabs.grid(row=8,column=0,columnspan=3,sticky='nsew',pady=6)
        self.result_frame=ttk.Frame(self.tabs)
        self.tabs.add(self.result_frame,text='曲线与误差')
        self.summary=ScrolledText(self.result_frame,height=8,wrap='word')
        self.summary.pack(fill='x',expand=False)
        self.plot_frame=None
        native=ttk.Frame(self.tabs,padding=10)
        self.tabs.add(native,text='生成原理图')
        native.columnconfigure(1,weight=1)
        self.entry(native,0,'Virtuoso 工作区','workspace',lambda:self.choose_dir('workspace'))
        self.entry(native,1,'目标库名','library')
        self.entry(native,2,'新 Cell 名','cell')
        self.native_button=ttk.Button(native,text='生成并回放验收',command=self.start_native,state='disabled')
        self.native_button.grid(row=3,column=0,columnspan=3,sticky='w',pady=12)
        ttk.Label(native,text='同名 cell 不覆盖。只在拟合达标后生成；原生连通、网表与回放结果分别记录。',wraplength=850).grid(row=4,column=0,columnspan=3,sticky='w')
        self.native_text=ScrolledText(native,height=10,wrap='word')
        self.native_text.grid(row=5,column=0,columnspan=3,sticky='nsew',pady=6)
        native.rowconfigure(5,weight=1)
        logs=ttk.Frame(self.tabs)
        self.tabs.add(logs,text='日志 / 设置')
        ttk.Label(logs,text='独立 NumPy 拟合；生成原理图时使用已有 Virtuoso / Spectre。').grid(row=0,column=0,columnspan=3,sticky='w',pady=6)
        actions=ttk.Frame(logs)
        actions.grid(row=1,column=0,columnspan=3,sticky='ew')
        ttk.Button(actions,text='复制日志',command=self.copy_log).pack(side='left')
        ttk.Button(actions,text='保存日志',command=self.save_log).pack(side='left',padx=8)
        self.log=ScrolledText(logs,height=12,wrap='word')
        self.log.grid(row=2,column=0,columnspan=3,sticky='nsew')
        logs.columnconfigure(1,weight=1);logs.rowconfigure(2,weight=1)
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.load_settings()
        root.after(100,self.poll)

    def entry(self,parent,row,label,key,browse=None):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',pady=4,padx=(0,10))
        ttk.Entry(parent,textvariable=self.v[key]).grid(row=row,column=1,sticky='ew')
        if browse:
            ttk.Button(parent,text='选择…',command=browse).grid(row=row,column=2,padx=(6,0))

    def choose_dir(self,key):
        p=filedialog.askdirectory(parent=self.root)
        if p:
            self.v[key].set(p)
            if key=='workspace':
                from .delivery import library_mappings
                for name,path in library_mappings(p).items():
                    try:
                        if Path(path).resolve().is_relative_to(Path(p).resolve()):
                            self.v['library'].set(name);break
                    except (OSError,ValueError):pass

    def choose_file(self,key):
        p=filedialog.askopenfilename(parent=self.root)
        if p:self.v[key].set(p)

    def browse_snp(self):
        p=filedialog.askopenfilename(parent=self.root,filetypes=[('Touchstone','*.s*p *.ts'),('All','*')])
        if not p:return
        try:
            self.source=read_touchstone(p)
            self.v['file'].set(p)
            self.confirm_ports.set(False)
            self.v['l'].set('');self.v['q'].set('')
            self.status.set(f'{self.source.nports} 端口，{len(self.source.freq)} 点，数据范围 {self.source.freq[0]/1e9:g}–{self.source.freq[-1]/1e9:g} GHz。请选择模型并确认映射。')
            self.model_changed()
        except Exception as exc:messagebox.showerror('SNP 读取失败',str(exc),parent=self.root)

    def selected_model(self):
        if self.v['model'].get() not in self.labels:
            raise ValueError('请选择一个明确的模型。')
        return self.models[self.labels.index(self.v['model'].get())]

    def model_changed(self,event=None):
        self.confirm_ports.set(False)
        try:
            model=self.selected_model()
        except ValueError:return
        roles=_field(model,'port_roles','ports',default=[])
        n=_field(model,'nports','num_ports',default=len(roles))
        if not self.v['ports'].get() or len(self.v['ports'].get().split(','))!=n:
            self.v['ports'].set(','.join(str(i+1) for i in range(n)))
        explicit=_field(model,'explicit_reference',default=True)
        self.role_label.configure(text='对应顺序：'+', '.join(roles)+'；'+('显式 GND 引脚' if explicit else '内部参考地，无独立 GND 引脚')+'；tap 开路'+('，差分共模匹配' if _field(model,'mode')=='dm' else ''))

    def update_strategy_preview(self,*args):
        try:
            l,q=float(self.v['l'].get()),float(self.v['q'].get())
            if not (5<=l<=20 and 5<=q<=20):raise ValueError()
            from .lpriority import select_strategy
            self.strategy_text.set('策略：'+('L优先（自动）' if select_strategy(l,q)=='l_priority' else '联合拟合（自动）'))
        except ValueError:self.strategy_text.set('策略：填写本次 L/Q 后自动选择')

    def set_busy(self,value):
        self.busy=value
        self.fit_button.configure(state='disabled' if value else 'normal')
        self.stop_button.configure(state='normal' if value else 'disabled')
        self.native_button.configure(state='normal' if not value and self.result and self.result.get('passed') else 'disabled')
        if value:self.progress.start(80)
        else:self.progress.stop()

    def worker(self,fn,kind):
        self.cancel=threading.Event();self.token+=1;token=self.token
        self.stop_button.configure(command=self.cancel.set)
        self.set_busy(True)
        def run():
            try:self.events.put((token,kind,fn()))
            except Exception as exc:self.events.put((token,'error',(str(exc),traceback.format_exc())))
        threading.Thread(target=run,daemon=False).start()

    def start_fit(self):
        if self.busy:return
        try:
            m=self.selected_model()
            if not self.confirm_ports.get():raise ValueError('请先确认端口映射和端接。')
            l=float(self.v['l'].get());q=float(self.v['q'].get())
            if not 5<=l<=20 or not 5<=q<=20:raise ValueError('本次 L/Q 误差必须在 5–20% 之间。')
            order=[int(v.strip())-1 for v in self.v['ports'].get().split(',')]
            kwargs=dict(snp=self.v['file'].get(),model_id=_field(m,'model_id','id'),
                        port_order=order,fmin=float(self.v['fmin'].get())*1e9,
                        fmax=float(self.v['fmax'].get())*1e9,l_pct=l,q_pct=q,
                        runtime=self.runtime,
                        short_unused=self.short_unused.get())
        except Exception as exc:
            messagebox.showerror('检查本次输入',str(exc),parent=self.root);return
        self.result=None
        self.v['l'].set('');self.v['q'].set('')
        from .lpriority import select_strategy
        self.status.set(('正在以 L优先 策略拟合；' if select_strategy(l,q)=='l_priority' else '正在联合拟合；')+'本次误差已固定，下一次需要重新填写。')
        self.worker(lambda:fit_file(**kwargs,cancel=self.cancel,
                                   progress=lambda msg:self.events.put((self.token,'log',msg))),'fit')

    def start_native(self):
        if self.busy or not self.result:return
        try:
            workspace=self.v['workspace'].get()
            if not Path(workspace,'cds.lib').is_file():raise ValueError('请选择包含 cds.lib 的真实工作区。')
            from .delivery import deliver
            snapshot=self.result.copy()
            library,cell=self.v['library'].get(),self.v['cell'].get()
        except Exception as exc:messagebox.showerror('原生生成准备失败',str(exc),parent=self.root);return
        self.worker(lambda:deliver(snapshot,workspace,library,cell,cancel=self.cancel,
                                  progress=lambda m:self.events.put((self.token,'log',m))),'native')

    def poll(self):
        try:
            while True:
                token,kind,value=self.events.get_nowait()
                if token!=self.token:continue
                if kind=='log':self.append_log(value);self.status.set(str(value))
                elif kind=='error':
                    self.set_busy(False);self.append_log(value[1]);self.status.set('本次失败：'+value[0])
                    messagebox.showerror('本次操作未完成',value[0],parent=self.root)
                elif kind=='fit':
                    self.result=value;self.set_busy(False)
                    self.status.set('本次已取消，已保留当前证据。' if value.get('delivery_status')=='CANCELLED' else ('数值达标，可生成原理图。' if value.get('passed') else '未达标：已保留最佳候选和超差报告。'))
                    self.show_result(value)
                elif kind=='native':
                    self.result=value;self.set_busy(False)
                    self.status.set('原理图已生成，回放验收通过。' if value.get('native_verified') else '原生验收未通过，请查看报告。')
                    native=value.get('native',{})
                    self.native_text.delete('1.0','end')
                    self.native_text.insert('end',('回放验收通过\n' if value.get('native_verified') else '回放未达标\n')+
                        '库 / Cell：'+str(native.get('library'))+' / '+str(native.get('cell'))+'\n'+
                        '库路径：'+str(native.get('library_path'))+'\n'+
                        '元件数：'+str(native.get('component_counts'))+'\n'+
                        '新库如未映射，在工作区 cds.lib 添加：\n'+str(native.get('mapping_line'))+'\n')
        except queue.Empty:pass
        self.root.after(100,self.poll)

    def show_result(self,result):
        self.summary.delete('1.0','end')
        metrics=result.get('metrics') or {}
        text=['数值达标' if result.get('passed') else '未达标',
              '自动策略：'+('L优先' if result.get('strategy',result.get('request',{}).get('strategy'))=='l_priority' else '联合拟合'),
              '元件数：'+str(metrics.get('component_counts',{}))]
        for name,errors in metrics.get('branches',{}).items():
            pieces=[]
            for key in ('l','q'):
                error=errors[key]
                pct=error.get('max_relative_pct')
                pieces.append(f'{key.upper()} 最大 {pct:.4g}% @ {error["worst_frequency_hz"]/1e9:.6g} GHz' if pct is not None else key.upper()+' 见绝对误差')
            text.append(name+'：'+'；'.join(pieces))
        text.append('完整误差与结果目录：'+result['run_dir'])
        self.summary.insert('end','\n'.join(text))
        try:
            from .plots import plot_result
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            fig=plot_result(result)
            if self.plot_frame:self.plot_frame.destroy()
            self.plot_frame=ttk.Frame(self.result_frame)
            self.plot_frame.pack(fill='both',expand=True)
            self.summary.configure(height=5)
            canvas=FigureCanvasTkAgg(fig,master=self.plot_frame)
            canvas.draw();canvas.get_tk_widget().pack(fill='both',expand=True)
            NavigationToolbar2Tk(canvas,self.plot_frame).update()
        except Exception as exc:self.append_log('曲线显示：'+str(exc))

    def append_log(self,message):
        bottom=self.log.yview()[1]>=.98
        self.log.insert('end',str(message)+'\n')
        if bottom:self.log.see('end')

    def doctor(self):
        try:
            result=diagnose()
            self.append_log(json.dumps(result,ensure_ascii=False,indent=2));self.tabs.select(2)
        except Exception as exc:messagebox.showerror('诊断',str(exc),parent=self.root)

    def show_path(self):
        path=self.result.get('run_dir') if self.result else str(self.runtime)
        self.root.clipboard_clear();self.root.clipboard_append(path)
        messagebox.showinfo('结果目录（已复制）',path,parent=self.root)

    def copy_log(self):
        self.root.clipboard_clear();self.root.clipboard_append(self.log.get('1.0','end-1c'))

    def save_log(self):
        p=filedialog.asksaveasfilename(parent=self.root,defaultextension='.log')
        if p:Path(p).write_text(self.log.get('1.0','end-1c'),encoding='utf-8')

    def load_settings(self):
        p=self.runtime/'settings.json'
        if p.is_file():
            try:
                data=json.loads(p.read_text(encoding='utf-8'))
                for k in ('workspace','library'):
                    if isinstance(data.get(k),str):self.v[k].set(data[k])
            except Exception as exc:self.append_log('设置读取失败：'+str(exc))

    def close(self):
        if self.busy:
            self.cancel.set();self.status.set('正在停止本次作业，结束后可关闭。');return
        settings=self.runtime/'settings.json'
        if settings.is_file():
            import datetime as dt
            import shutil
            backup=settings.with_name(settings.name+'.bak-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
            if not backup.exists():shutil.copy2(settings,backup)
        json_write(settings,{'schema':2,**{k:self.v[k].get() for k in ('workspace','library')}})
        self.root.destroy()


def main(runtime=None):
    root=tk.Tk();App(root,runtime);root.mainloop()
