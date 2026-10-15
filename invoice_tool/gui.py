"""Native Windows batch UI; only the main thread touches Tk widgets."""
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import confirm_record, export_excel, mark_duplicates
from .reader import InvoiceReader, SUPPORTED, collect_inputs, parse_text, preview_image
from .pipeline import ParallelReader
from .review import FILTERS, visible_indices, next_review_index, crop_region

BG, INK, MUTED, ACCENT = '#F3F5F7', '#18334D', '#546579', '#176B57'


class InvoiceApp:
    def __init__(self, root):
        self.root = root
        self.rows = []
        self.paths_seen = set()
        self.queue = Queue(maxsize=128)
        self.stop = Event()
        self.reader = InvoiceReader(self.stop)
        self.parallel_reader = ParallelReader()
        self.busy = False
        self.dirty = False
        self.selected = None
        self.preview = None
        self._region_pending = None
        self._photo = self._crop_photo = None
        self._render_timer = None
        self.number = tk.StringVar()
        self.code = tk.StringVar()
        self.amount = tk.StringVar()
        self.review_filter = tk.StringVar(value='全部记录')
        self._filter_applied = '全部记录'
        self.single_invoice = tk.BooleanVar(value=False)
        self.parallel = tk.BooleanVar(value=False)
        self.caption = tk.StringVar(value='选择一条结果，查看原件并核对号码。')
        self.reason = tk.StringVar()
        self.summary = tk.StringVar(value='暂无结果')
        self.progress_text = tk.StringVar(value='拖入文件或文件夹，也可以点击“添加文件”。')
        from .task_ui import TaskController
        self.tasks=TaskController(self)
        root.title('发票号码与金额提取 · 本地版')
        width = min(1360,max(980,root.winfo_screenwidth()-80))
        height = min(900,max(640,root.winfo_screenheight()-100))
        root.geometry(f'{width}x{height}+40+40')
        root.minsize(980,640)
        root.configure(bg=BG)
        self.build_ui()
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.bind('<Control-o>',lambda _: self.choose_files())
        root.bind('<Control-s>',lambda _: self.export())
        root.bind('<Control-Shift-S>',lambda _:self.tasks.save())
        root.bind('<Control-Shift-O>',lambda _:self.tasks.open())
        root.bind('<Control-Return>',lambda _: self.confirm())
        root.after(120,self.poll)

    def build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10),background=BG,foreground=INK)
        style.configure('TButton',padding=(12,8))
        style.configure('Primary.TButton',background=ACCENT,foreground='white')
        style.map('Primary.TButton',background=[('active','#115440'),('disabled','#879E95')])
        style.configure('Treeview',background='white',fieldbackground='white',rowheight=34)
        style.configure('Treeview.Heading',background='#E5EBF0',padding=8,font=('Microsoft YaHei UI',10,'bold'))
        style.map('Treeview',background=[('selected','#D5E7F2')],foreground=[('selected',INK)])
        header = ttk.Frame(self.root,padding=(20,16,20,8))
        header.pack(fill='x')
        ttk.Label(header,text='发票号码与金额提取',font=('Microsoft YaHei UI',22,'bold')).pack(side='left')
        ttk.Label(header,text='PDF / 图片 / 文字  ·  本机离线处理',foreground=MUTED).pack(side='right')
        toolbar = ttk.Frame(self.root,padding=(20,8))
        toolbar.pack(fill='x')
        self.import_buttons = []
        for text,command in [('添加文件',self.choose_files),('添加文件夹',self.choose_folder),('粘贴文字',self.paste)]:
            button = ttk.Button(toolbar,text=text,command=command)
            button.pack(side='left',padx=(0,8))
            self.import_buttons.append(button)
        self.export_button = ttk.Button(toolbar,text='导出全部 Excel',style='Primary.TButton',command=self.export)
        self.export_button.pack(side='right')
        self.clear_button = ttk.Button(toolbar,text='清空结果',command=self.clear)
        self.clear_button.pack(side='right',padx=8)
        self.stop_button = ttk.Button(toolbar,text='停止识别',command=self.cancel,state='disabled')
        self.stop_button.pack(side='right')
        self.tasks.build(self.root)
        options = ttk.Frame(self.root,padding=(20,0,20,4))
        options.pack(fill='x')
        self.fast_check = ttk.Checkbutton(options,text='快速模式（每页只有一张发票）',
                                          variable=self.single_invoice)
        self.fast_check.pack(side='left')
        ttk.Label(options,text='同页多张票请保持未勾选；快速模式遇疑难结果会回退整页。',
                  foreground=MUTED).pack(side='left',padx=16)
        parallel_options = ttk.Frame(self.root,padding=(20,0,20,4))
        parallel_options.pack(fill='x')
        self.parallel_check = ttk.Checkbutton(parallel_options,text='并行处理（两个任务，单份 PDF 按页）',variable=self.parallel)
        self.parallel_check.pack(side='left')
        ttk.Label(parallel_options,text='适合多页或多份扫描件；首次加载两个模型会增加耗时和内存。',
                  foreground=MUTED).pack(side='left',padx=16)
        panes = ttk.Panedwindow(self.root,orient='horizontal')
        panes.pack(fill='both',expand=True,padx=20,pady=8)
        left = ttk.Frame(panes)
        from .review_dialog import ReviewScroll
        self.review_panel=ReviewScroll(panes)
        right=self.review_panel.content
        panes.add(left,weight=3)
        panes.add(self.review_panel,weight=2)
        result_toolbar = ttk.Frame(left)
        result_toolbar.pack(fill='x',pady=(0,8))
        ttk.Label(result_toolbar,text='识别结果',font=('Microsoft YaHei UI',12,'bold')).pack(side='left')
        ttk.Label(result_toolbar,text='筛选').pack(side='left',padx=(12,4))
        self.filter_entry = ttk.Combobox(result_toolbar,textvariable=self.review_filter,
                                         values=FILTERS,state='readonly',width=13)
        self.filter_entry.pack(side='left')
        self.filter_entry.bind('<<ComboboxSelected>>',self.apply_filter)
        table = ttk.Frame(left)
        table.pack(fill='both',expand=True)
        table.rowconfigure(0,weight=1)
        table.columnconfigure(0,weight=1)
        columns = [('file','来源文件',160),('page','页',35),('number','发票号码',205),
                   ('amount','价税合计',105),('amount_state','金额状态',88),
                   ('state','号码状态',88),('duplicate','重复于',75)]
        self.tree = ttk.Treeview(table,columns=[c[0] for c in columns],show='headings',selectmode='browse')
        for name,title,width in columns:
            self.tree.heading(name,text=title)
            self.tree.column(name,width=width,minwidth=40,stretch=name == 'file')
        self.tree.grid(row=0,column=0,sticky='nsew')
        vertical = ttk.Scrollbar(table,orient='vertical',command=self.tree.yview)
        vertical.grid(row=0,column=1,sticky='ns')
        horizontal = ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.tree.tag_configure('待复核',background='#FFF4DB')
        self.tree.tag_configure('未识别',background='#FDEBEC')
        self.tree.tag_configure('失败',background='#FDEBEC')
        self.tree.tag_configure('已确认',background='#E8F4ED')
        self.tree.bind('<<TreeviewSelect>>',self.select)
        summary_label=ttk.Label(left,textvariable=self.summary,foreground=MUTED,wraplength=500)
        summary_label.pack(side='bottom',fill='x',pady=8,before=table)
        left.bind('<Configure>',lambda event:summary_label.configure(wraplength=max(150,event.width-12)))
        ttk.Label(right,text='原件与区域预览',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w',pady=(0,8))
        self.canvas = tk.Canvas(right,background='white',highlightthickness=1,highlightbackground='#D6DEE5',height=80)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',self.schedule_render)
        self.canvas.bind('<Button-1>',self.open_original)
        caption_label = ttk.Label(right,textvariable=self.caption,wraplength=460,foreground=MUTED)
        caption_label.pack(fill='x',pady=4)
        self.region_tabs = ttk.Notebook(right,height=66)
        self.region_tabs.pack(fill='x')
        number_tab,amount_tab=ttk.Frame(self.region_tabs),ttk.Frame(self.region_tabs)
        self.region_tabs.add(number_tab,text='号码区域')
        self.region_tabs.add(amount_tab,text='金额区域')
        self.crop_canvas = tk.Canvas(number_tab,background='white',height=66,highlightthickness=1,highlightbackground='#D6DEE5')
        self.crop_canvas.pack(fill='x')
        self.crop_canvas.bind('<Button-1>',self.open_original)
        self.amount_canvas = tk.Canvas(amount_tab,background='white',height=66,highlightthickness=1,highlightbackground='#D6DEE5')
        self.amount_canvas.pack(fill='x')
        self.amount_canvas.bind('<Button-1>',self.zoom_amount)
        self.amount_canvas.bind('<Configure>',self.schedule_render)
        preview_actions=ttk.Frame(right)
        preview_actions.pack(fill='x',pady=2)
        ttk.Button(preview_actions,text='放大金额',command=self.zoom_amount).pack(side='left')
        self.region_button=ttk.Button(preview_actions,text='框选金额重读',command=self.reread_region)
        self.region_button.pack(side='left',padx=8)
        form = ttk.Frame(right,padding=(0,4))
        form.pack(fill='x')
        form.columnconfigure(1,weight=1)
        ttk.Label(form,text='发票号码').grid(row=0,column=0,sticky='w',padx=(0,12),pady=4)
        self.number_entry = ttk.Combobox(form,textvariable=self.number,font=('Consolas',13))
        self.number_entry.grid(row=0,column=1,sticky='ew',pady=4)
        ttk.Label(form,text='发票代码').grid(row=1,column=0,sticky='w',padx=(0,12),pady=4)
        ttk.Entry(form,textvariable=self.code,font=('Consolas',12)).grid(row=1,column=1,sticky='ew',pady=4)
        help_label = ttk.Label(form,text='传统发票可填写代码；20 位数电票留空。',foreground=MUTED,wraplength=460)
        help_label.grid(row=2,column=0,columnspan=2,sticky='w',pady=2)
        ttk.Label(form,text='价税合计').grid(row=3,column=0,sticky='w',padx=(0,12),pady=4)
        self.amount_entry = ttk.Combobox(form,textvariable=self.amount,font=('Consolas',12))
        self.amount_entry.grid(row=3,column=1,sticky='ew',pady=4)
        amount_help=ttk.Label(form,text='含税人民币金额；可填负数，未确定时留空。',foreground=MUTED,wraplength=460)
        amount_help.grid(row=4,column=0,columnspan=2,sticky='w',pady=2)
        actions = ttk.Frame(right)
        actions.pack(fill='x')
        actions.columnconfigure((0,1),weight=1)
        ttk.Button(actions,text='保存并确认',command=self.confirm,style='Primary.TButton').grid(row=0,column=0,sticky='ew',padx=(0,4))
        ttk.Button(actions,text='确认并下一条',command=lambda:self.confirm(advance=True)).grid(row=0,column=1,sticky='ew')
        ttk.Button(actions,text='下一条待复核',command=self.next_review).grid(row=1,column=0,columnspan=2,sticky='ew',pady=(4,0))
        reason_label = ttk.Label(right,textvariable=self.reason,wraplength=460,foreground=MUTED)
        reason_label.pack(fill='x',pady=(4,0))
        right.bind('<Configure>',lambda event: [label.configure(wraplength=max(100,event.width-20))
                                                for label in (caption_label,help_label,amount_help,reason_label)],add='+')
        self.review_panel.bind_focus()
        progress = ttk.Frame(self.root,padding=(20,4,20,12))
        progress.pack(side='bottom',fill='x',before=panes)
        ttk.Label(progress,textvariable=self.progress_text,foreground=MUTED,wraplength=900).pack(anchor='w')
        self.progress = ttk.Progressbar(progress,mode='determinate')
        self.progress.pack(fill='x',pady=(8,0))
        self.render()

    def choose_files(self):
        paths = filedialog.askopenfilenames(title='选择发票',filetypes=[
            ('发票文件','*.pdf *.PDF *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.txt'),('所有文件','*.*')])
        if paths:
            self.import_paths(paths)

    def choose_folder(self):
        path = filedialog.askdirectory(title='选择解压后的发票文件夹（包含子目录）')
        if path:
            self.import_paths([path])

    def import_paths(self, paths):
        if self.busy:
            messagebox.showinfo('正在识别','本批处理完毕后可继续添加文件。',parent=self.root)
            return
        files = [p for p in collect_inputs(paths) if str(p).casefold() not in self.paths_seen]
        if not files:
            messagebox.showinfo('没有新文件','支持 PDF、图片和 TXT。RAR/ZIP 请先解压；已导入的文件不会重复添加。',parent=self.root)
            return
        self.stop = Event()
        self.reader.stop = self.stop
        self.reader.single_invoice = self.single_invoice.get()
        parallel = self.parallel.get() and (len(files)>1 or files[0].suffix.lower() == '.pdf')
        single_invoice = self.single_invoice.get()
        self.set_busy(True)
        self.progress.configure(maximum=len(files),value=0)
        def work():
            # Imports are sequential (busy guard); retain the expensive OCR
            # sessions for the next batch instead of loading models again.
            reader = self.reader
            completed = []
            try:
                if parallel:
                    for kind,value in self.parallel_reader.read(files,self.stop,single_invoice):
                        if kind == 'complete':
                            completed.append(str(value).casefold())
                        else:
                            self.queue.put((kind,value))
                    self.queue.put(('done',completed))
                    return
                for index,path in enumerate(files):
                    if self.stop.is_set():
                        break
                    self.queue.put(('progress',(index,len(files),path.name)))
                    for row in reader.read(path):
                        self.queue.put(('row',row))
                    if not self.stop.is_set():
                        completed.append(str(path).casefold())
                self.queue.put(('done',completed))
            except Exception as exc:
                self.queue.put(('error',str(exc)))
        Thread(target=work,daemon=True).start()

    def set_busy(self, busy):
        self.busy = busy
        self.fast_check.configure(state='disabled' if busy else 'normal')
        self.parallel_check.configure(state='disabled' if busy else 'normal')
        for button in self.import_buttons+[self.clear_button]:
            button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        self.export_button.configure(state='disabled' if busy else 'normal')
        self.region_button.configure(state='disabled' if busy else 'normal')
        self.tasks.set_busy(busy)

    def poll(self):
        new_rows = []
        try:
            while True:
                kind,data = self.queue.get_nowait()
                if kind == 'row':
                    new_rows.append(data)
                elif kind == 'progress':
                    index,count,name = data
                    self.progress.configure(maximum=count,value=index)
                    self.progress_text.set(f'正在处理 {index+1}/{count}：{name}')
                elif kind == 'done':
                    self.paths_seen.update(data)
                    self.tasks.changed()
                    self.set_busy(False)
                    self.progress.configure(value=len(data) if self.stop.is_set() else self.progress['maximum'])
                    self.progress_text.set('已停止，已读取结果可导出。' if self.stop.is_set() else '识别完成。选择结果核对，修改后点击“保存并确认”。')
                elif kind == 'error':
                    self.set_busy(False)
                    self.progress_text.set('处理异常，已读取结果仍可导出。')
                    messagebox.showerror('处理异常',data,parent=self.root)
        except Empty:
            pass
        if new_rows:
            self.add_rows(new_rows)
        if self.tasks.poll():
            return
        self.root.after(120,self.poll)

    def add_rows(self, rows):
        self.rows.extend(rows)
        self.dirty = True
        self.tasks.changed()
        self.refresh()
        indices=visible_indices(self.rows,self.review_filter.get())
        if self.selected is None and indices:
            self.tree.selection_set(str(indices[0]))
            self.tree.focus(str(indices[0]))

    def refresh(self):
        mark_duplicates(self.rows)
        visible=set(visible_indices(self.rows,self.review_filter.get()))
        for index,row in enumerate(self.rows):
            values = (Path(row.source).name,row.page,row.invoice_number or '—',
                      row.amount or '—',row.amount_status,row.status,
                      '第 '+row.duplicate_of+' 条' if row.duplicate_of else '')
            if self.tree.exists(str(index)):
                self.tree.item(str(index),values=values,tags=(row.status,))
            else:
                self.tree.insert('','end',iid=str(index),values=values,tags=(row.status,))
            if index in visible:
                self.tree.move(str(index),'','end')
            else:
                self.tree.detach(str(index))
        review = sum(r.status not in ('已识别','已确认') for r in self.rows)
        duplicates = sum(bool(r.duplicate_of) for r in self.rows)
        amount_review = sum(r.amount_status not in ('已识别','已确认') for r in self.rows)
        self.summary.set(f'显示 {len(visible)}/{len(self.rows)} 条 · 号码待处理 {review} · 金额待处理 {amount_review} · 重复 {duplicates}')

    def apply_filter(self, _=None):
        if self.form_changed():
            self.review_filter.set(self._filter_applied)
            messagebox.showinfo('修改尚未保存','请先保存并确认当前修改，再切换筛选。',parent=self.root)
            return
        self._filter_applied=self.review_filter.get()
        self.tasks.changed()
        self.refresh()
        indices=visible_indices(self.rows,self.review_filter.get())
        if self.selected not in indices:
            self.clear_detail()
            if indices:
                self.tree.selection_set(str(indices[0]))
                self.tree.focus(str(indices[0]))

    def clear_detail(self):
        self.tree.selection_remove(*self.tree.selection())
        self.selected=None
        self.preview=None
        self._region_pending=None
        for variable in (self.number,self.code,self.amount,self.reason):
            variable.set('')
        self.number_entry.configure(values=())
        self.amount_entry.configure(values=())
        self.caption.set('当前筛选下没有选中记录。导出仍包含全部结果。')
        self.render()

    def zoom_amount(self, _=None):
        if self.preview is None or self.selected is None:
            messagebox.showinfo('没有可用票面','请先选择有原件预览的记录。',parent=self.root)
            return
        bounds=self.rows[self.selected].amount_bbox
        if not bounds:
            messagebox.showinfo('未定位金额','尚未定位金额区域，可使用“框选金额重读”。',parent=self.root)
            return
        from .review_dialog import show_amount
        return show_amount(self.root,self.preview,bounds)

    def reread_region(self):
        if self.busy:
            return
        if self.selected is None or self.preview is None:
            messagebox.showinfo('没有可用票面','请先选择有原件预览的记录。',parent=self.root)
            return
        from .review_dialog import RegionDialog
        try:
            row=self.rows[self.selected]
            image=preview_image(row,max_side=2400)
            if image is None:
                raise ValueError('该记录没有可识别的原件图片。')
        except Exception as exc:
            messagebox.showerror('无法读取原件',str(exc),parent=self.root)
            return
        return RegionDialog(self,image,row)

    def accept_region_candidate(self, row, candidates, chosen):
        # Adoption edits only the form. Saving is the explicit commit boundary.
        if self.selected is None or self.rows[self.selected] is not row:
            return False
        self._region_pending=(row,candidates,chosen)
        self.amount_entry.configure(values=list(dict.fromkeys(row.amount_candidates+
                                                [item['value'] for item in candidates])))
        self.amount.set(chosen['value'])
        self.tasks.changed()
        self.reason.set(row.reason+'\n框选重读候选已填入，请对照原件后保存并确认。')
        return True

    def select(self, _=None):
        selected = self.tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if index == self.selected and self.form_changed():
            return
        if self.selected is not None and self.selected != index and self.form_changed():
            choice = messagebox.askyesnocancel('修改尚未保存','保存当前号码及金额修改并确认后，切换到下一条？',parent=self.root)
            if choice is True:
                try:
                    self.save_form()
                    self.refresh()
                except ValueError as exc:
                    messagebox.showerror('字段格式不正确',str(exc),parent=self.root)
                    choice = None
            if choice is None:
                self.tree.selection_set(str(self.selected))
                return
        if self.selected!=index:
            self.tasks.changed()
        self.selected = index
        self._region_pending = None
        row = self.rows[self.selected]
        self.number.set(row.invoice_number)
        self.code.set(row.invoice_code)
        self.amount.set(row.amount)
        self.amount_entry.configure(values=row.amount_candidates)
        self.number_entry.configure(values=row.candidates)
        self.reason.set(row.reason+'\n金额：'+row.amount_reason)
        self.caption.set(f'{Path(row.source).name}  ·  第 {row.page} 页')
        try:
            self.preview = preview_image(row)
        except Exception as exc:
            self.preview = None
            self.reason.set(row.reason+'\n金额：'+row.amount_reason+'\n无法预览：'+str(exc))
        if self.preview is not None:
            self.caption.set(self.caption.get()+'  ·  点击预览打开原件')
        self.render()

    def open_original(self, _=None):
        if self.selected is None:
            return
        path = Path(self.rows[self.selected].source)
        if not path.is_absolute() or path.suffix.lower() not in SUPPORTED:
            messagebox.showinfo('没有原件文件','该记录没有可打开的原件文件。粘贴文字可在界面中核对。',parent=self.root)
            return
        try:
            if not path.is_file():
                messagebox.showerror('原件不存在','原件文件已移动或删除，请重新添加文件。\n'+str(path),parent=self.root)
                return
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror('无法打开原件','请检查文件访问权限及 Windows 默认打开程序。\n'+str(exc),parent=self.root)

    def schedule_render(self, _=None):
        if self._render_timer:
            self.root.after_cancel(self._render_timer)
        self._render_timer = self.root.after(100,self.render)

    def render(self):
        from PIL import Image, ImageDraw, ImageTk
        self._render_timer = None
        self.canvas.delete('all')
        self.crop_canvas.delete('all')
        self.amount_canvas.delete('all')
        self.amount_canvas.configure(cursor='')
        self.canvas.configure(cursor='hand2' if self.preview is not None else '')
        self.crop_canvas.configure(cursor='')
        if self.preview is None:
            self.canvas.create_text(max(self.canvas.winfo_width(),300)//2,110,
                                    text='选择结果查看票面\n\n支持 PDF、图片和文字',fill=MUTED,
                                    font=('Microsoft YaHei UI',11),justify='center')
            if self.selected is not None:
                self.crop_canvas.create_text(12,18,anchor='nw',text=self.rows[self.selected].evidence[:70],fill=INK)
            return
        image = self.preview.copy()
        row = self.rows[self.selected]
        bounds = None
        if row.amount_bbox:
            w,h = image.size
            x0,y0,x1,y1 = row.amount_bbox
            ImageDraw.Draw(image).rectangle((int(x0*w),int(y0*h),int(x1*w),int(y1*h)),
                                            outline=ACCENT,width=3)
            try:
                padding=.012
                area=(max(0,x0-padding),max(0,y0-padding),min(1,x1+padding),min(1,y1+padding))
                amount_crop=crop_region(self.preview,area)
                scale=min(max(100,self.amount_canvas.winfo_width()-16)/amount_crop.width,
                          54/amount_crop.height,3)
                amount_crop=amount_crop.resize((max(1,int(amount_crop.width*scale)),max(1,int(amount_crop.height*scale))),Image.Resampling.LANCZOS)
                self._amount_photo=ImageTk.PhotoImage(amount_crop,master=self.root)
                self.amount_canvas.create_image(self.amount_canvas.winfo_width()/2,33,image=self._amount_photo)
                self.amount_canvas.configure(cursor='hand2')
            except ValueError:
                self.amount_canvas.create_text(12,20,anchor='nw',text='金额区域不可用，请框选重读。',fill=MUTED)
        else:
            self.amount_canvas.create_text(12,20,anchor='nw',text='未定位金额，可框选重读。',fill=MUTED)
        if row.bbox:
            w,h = image.size
            x0,y0,x1,y1 = row.bbox
            bounds = (max(0,int(x0*w)-12),max(0,int(y0*h)-10),min(w,int(x1*w)+12),min(h,int(y1*h)+10))
            ImageDraw.Draw(image).rectangle(bounds,outline='#B22E38',width=3)
        image.thumbnail((max(100,self.canvas.winfo_width()-16),max(100,self.canvas.winfo_height()-16)),Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image,master=self.root)
        self.canvas.create_image(self.canvas.winfo_width()/2,self.canvas.winfo_height()/2,image=self._photo)
        if bounds and bounds[2]>bounds[0] and bounds[3]>bounds[1]:
            crop = self.preview.crop(bounds)
            factor = min(max(100,self.crop_canvas.winfo_width()-16)/crop.width,54/crop.height,3)
            crop = crop.resize((max(1,int(crop.width*factor)),max(1,int(crop.height*factor))),Image.Resampling.LANCZOS)
            self._crop_photo = ImageTk.PhotoImage(crop,master=self.root)
            self.crop_canvas.create_image(self.crop_canvas.winfo_width()/2,33,image=self._crop_photo)
            self.crop_canvas.configure(cursor='hand2')

    def save_form(self):
        """One atomic confirmation path for buttons and save-before-switch."""
        confirm_record(self.rows[self.selected],self.number.get(),self.code.get(),self.amount.get())
        if self._region_pending:
            row,candidates,chosen=self._region_pending
            if self.rows[self.selected] is row and row.amount==chosen['value']:
                row.amount_candidates=list(dict.fromkeys(row.amount_candidates+
                                                        [item['value'] for item in candidates]))
                row.amount_bbox=chosen['bbox']
                row.amount_reason='人工框选重读后核对并确认。'
            self._region_pending=None
        self.dirty = True
        self.tasks.changed()
        self.number.set(self.rows[self.selected].invoice_number)
        self.code.set(self.rows[self.selected].invoice_code)
        self.amount.set(self.rows[self.selected].amount)

    def confirm(self, advance=False):
        if self.selected is None:
            return
        try:
            self.save_form()
        except ValueError as exc:
            messagebox.showerror('字段格式不正确',str(exc),parent=self.root)
            return
        self.refresh()
        if advance or self.selected not in visible_indices(self.rows,self.review_filter.get()):
            if not self.next_review():
                self.clear_detail()
        else:
            self.select()
        return True

    def next_review(self):
        index=next_review_index(self.rows,self.selected,self.review_filter.get())
        if index is not None:
            self.tree.selection_set(str(index))
            self.tree.focus(str(index))
            self.tree.see(str(index))
            return True
        self.progress_text.set('当前筛选下没有待处理记录。导出仍包含全部结果。')
        return False

    def paste(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('粘贴发票文字或号码')
        dialog.geometry('660x430')
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog,text='可粘贴发票全文，或每行一个号码。纯号码需要人工确认。',padding=12).pack(anchor='w')
        text = tk.Text(dialog,font=('Microsoft YaHei UI',11),wrap='word',undo=True)
        text.pack(fill='both',expand=True,padx=12,pady=8)
        text.focus_set()
        def submit():
            try:
                rows = parse_text(text.get('1.0','end'))
            except ValueError as exc:
                messagebox.showerror('无法读取',str(exc),parent=dialog)
                return
            self.add_rows(rows)
            dialog.destroy()
        ttk.Button(dialog,text='读取文字',command=submit,style='Primary.TButton').pack(anchor='e',padx=12,pady=12)

    def export(self):
        if not self.rows:
            messagebox.showinfo('暂无结果','请先添加发票。',parent=self.root)
            return
        if self.form_changed():
            messagebox.showinfo('修改尚未保存','请先点击“保存并确认”，再导出。',parent=self.root)
            return
        path = filedialog.asksaveasfilename(title='导出全部识别结果',defaultextension='.xlsx',
                                          initialfile='发票号码.xlsx',filetypes=[('Excel 工作簿','*.xlsx')])
        if not path:
            return
        try:
            export_excel(self.rows,path)
        except Exception as exc:
            messagebox.showerror('导出失败','请检查文件是否正在 Excel 中打开。\n'+str(exc),parent=self.root)
            return
        self.dirty = False
        self.progress_text.set('已导出：'+path)

    def cancel(self):
        self.stop.set()
        self.stop_button.configure(state='disabled')
        self.progress_text.set('正在停止，当前 OCR 完成后保留已有结果。')

    def clear(self):
        if (self.dirty or self.form_changed()) and not messagebox.askyesno('清空结果','结果尚未导出，仍要清空吗？',parent=self.root):
            return
        for index in range(len(self.rows)):
            self.tree.delete(str(index))
        self.rows.clear()
        self.paths_seen.clear()
        self.review_filter.set('全部记录')
        self._filter_applied='全部记录'
        self.selected = None
        self.preview = None
        self._region_pending = None
        self.dirty = False
        self.number.set('')
        self.code.set('')
        self.amount.set('')
        self.reason.set('')
        self.caption.set('选择一条结果，查看原件并核对号码。')
        self.progress.configure(value=0)
        self.refresh()
        self.render()
        self.tasks.reset()

    def close(self):
        if self.busy:
            messagebox.showinfo('正在识别','请先停止识别，保留结果后再退出。',parent=self.root)
            return
        if self.tasks.needs_warning() and not messagebox.askyesno('退出程序','当前进度尚未保存到任务或导出，仍要退出吗？',parent=self.root):
            return
        self.tasks.finish(self.finish_close)

    def finish_close(self):
        self.tasks.writer.close()
        self.parallel_reader.close()
        self.root.destroy()

    def form_changed(self):
        if self.selected is None:
            return False
        row = self.rows[self.selected]
        return bool(self._region_pending) or (self.number.get(),self.code.get(),self.amount.get()) != (
            row.invoice_number,row.invoice_code,row.amount)


def create_app():
    import sys
    if sys.platform == 'win32':
        import ctypes
        # Use physical pixels for Tk, preview and screenshots on scaled Windows displays.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    from tkinterdnd2 import DND_FILES, TkinterDnD
    root = TkinterDnD.Tk()
    app = InvoiceApp(root)
    root.drop_target_register(DND_FILES)
    def dropped(event):
        app.import_paths(root.tk.splitlist(event.data))
        return event.action
    root.dnd_bind('<<Drop>>',dropped)
    return root,app
