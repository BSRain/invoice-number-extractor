"""Local amount review dialogs. Workers never access Tk widgets."""
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from .review import crop_region
from .review import display_region, recognize_region
from queue import Queue, Empty
from threading import Event, Thread


class ReviewScroll(ttk.Frame):
    """Keep review actions reachable on the supported 980 x 640 window."""
    def __init__(self,parent):
        super().__init__(parent)
        self.canvas=tk.Canvas(self,background='#F3F5F7',highlightthickness=0,width=460)
        bar=ttk.Scrollbar(self,orient='vertical',command=self.canvas.yview)
        bar.pack(side='right',fill='y')
        self.canvas.pack(side='left',fill='both',expand=True)
        self.canvas.configure(yscrollcommand=bar.set)
        self.content=ttk.Frame(self.canvas,padding=(12,0,0,0))
        self.item=self.canvas.create_window((0,0),window=self.content,anchor='nw')
        self.canvas.bind('<Configure>',self.resize)
        self.content.bind('<Configure>',self.resize)
        self.canvas.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(-int(e.delta/120),'units'))

    def resize(self,_=None):
        self.canvas.itemconfigure(self.item,width=max(1,self.canvas.winfo_width()),
                                  height=max(self.canvas.winfo_height(),self.content.winfo_reqheight()))
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def bind_focus(self):
        def visit(widget):
            widget.bind('<FocusIn>',self.reveal,add='+')
            for child in widget.winfo_children():
                visit(child)
        visit(self.content)

    def reveal(self,event):
        viewport=self.canvas.winfo_height()
        current=self.canvas.canvasy(0)
        top=event.widget.winfo_rooty()-self.canvas.winfo_rooty()+current
        bottom=top+event.widget.winfo_height()
        total=max(1,self.content.winfo_height())
        if top<current:
            self.canvas.yview_moveto(max(0,(top-8)/total))
        elif bottom>current+viewport:
            self.canvas.yview_moveto((bottom-viewport+8)/total)


def show_amount(parent, image, bounds):
    """Zoom a clean crop, leaving record values and the edit form untouched."""
    try:
        crop=crop_region(image,bounds)
    except ValueError as exc:
        messagebox.showinfo('金额区域不可用',str(exc),parent=parent)
        return
    dialog=tk.Toplevel(parent)
    dialog.title('金额放大 · 对照原件核对')
    dialog.geometry('820x360')
    dialog.transient(parent)
    dialog.grab_set()
    toolbar=ttk.Frame(dialog,padding=8)
    toolbar.pack(fill='x')
    factor=min(4,760/crop.width,230/crop.height)
    initial=factor
    frame=ttk.Frame(dialog)
    frame.pack(fill='both',expand=True,padx=8,pady=8)
    frame.rowconfigure(0,weight=1)
    frame.columnconfigure(0,weight=1)
    canvas=tk.Canvas(frame,background='white',highlightthickness=0)
    canvas.grid(row=0,column=0,sticky='nsew')
    vertical=ttk.Scrollbar(frame,orient='vertical',command=canvas.yview)
    vertical.grid(row=0,column=1,sticky='ns')
    horizontal=ttk.Scrollbar(frame,orient='horizontal',command=canvas.xview)
    horizontal.grid(row=1,column=0,sticky='ew')
    canvas.configure(xscrollcommand=horizontal.set,yscrollcommand=vertical.set)
    def render(scale):
        nonlocal factor
        factor=max(.1,min(8,4800/max(crop.size),scale))
        resized=crop.resize((max(1,int(crop.width*factor)),max(1,int(crop.height*factor))),Image.Resampling.LANCZOS)
        dialog.photo=ImageTk.PhotoImage(resized,master=dialog)
        canvas.delete('all')
        canvas.create_image(8,8,image=dialog.photo,anchor='nw')
        canvas.configure(scrollregion=(0,0,resized.width+16,resized.height+16))
    for label,action in [('放大 +',lambda:render(factor*1.5)),('缩小 −',lambda:render(factor/1.5)),
                         ('适合窗口',lambda:render(initial)),('关闭',dialog.destroy)]:
        ttk.Button(toolbar,text=label,command=action).pack(side='left',padx=4)
    dialog.bind('<Escape>',lambda _:dialog.destroy())
    render(factor)
    return dialog


class RegionDialog:
    """A modal, cancelable selection; only explicit adoption edits the form."""
    def __init__(self, app, image, row):
        self.app,self.image,self.row=app,image,row
        self.bounds=self.start_point=None
        self.busy=self.cancelled=False
        self.candidates=[]
        self.results=Queue(maxsize=1)
        self.poll_timer=None
        self.window=tk.Toplevel(app.root)
        self.window.title(f'框选金额重读 · 第 {row.page} 页 · {row.source}')
        self.window.geometry(f'{min(1080,app.root.winfo_screenwidth()-80)}x{min(800,app.root.winfo_screenheight()-100)}')
        self.window.minsize(640,480)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.cancel)
        self.window.bind('<Escape>',lambda _:self.cancel())
        self.window.bind('<Control-Return>',lambda _:self.read())
        ttk.Label(self.window,text='拖框选择价税合计的小写金额。只读取选区，所有结果均需人工核对；取消不修改记录。',
                  wraplength=900,padding=8).pack(fill='x')
        self.canvas=tk.Canvas(self.window,background='#E5EBF0',highlightthickness=0,cursor='crosshair')
        self.canvas.pack(fill='both',expand=True,padx=8)
        self.canvas.bind('<Configure>',self.draw)
        self.canvas.bind('<ButtonPress-1>',self.press)
        self.canvas.bind('<B1-Motion>',self.drag)
        self.canvas.bind('<ButtonRelease-1>',self.release)
        self.status=tk.StringVar(value='先框选金额，再点击“识别选区”。')
        ttk.Label(self.window,textvariable=self.status,wraplength=900,padding=8).pack(fill='x')
        controls=ttk.Frame(self.window,padding=8)
        controls.pack(fill='x')
        self.read_button=ttk.Button(controls,text='识别选区',command=self.read)
        self.read_button.pack(side='left')
        self.choice=ttk.Combobox(controls,state='readonly',width=34)
        self.choice.pack(side='left',padx=8,fill='x',expand=True)
        self.use_button=ttk.Button(controls,text='填入所选候选',command=self.adopt,state='disabled')
        self.use_button.pack(side='left')
        self.cancel_button=ttk.Button(controls,text='取消',command=self.cancel)
        self.cancel_button.pack(side='left',padx=8)
        self.app.set_busy(True)
        self.draw_timer=self.window.after(30,self.draw)

    def draw(self, _=None):
        resized=self.image.copy()
        resized.thumbnail((max(1,self.canvas.winfo_width()-16),max(1,self.canvas.winfo_height()-16)),Image.Resampling.LANCZOS)
        self.photo=ImageTk.PhotoImage(resized,master=self.window)
        left=(self.canvas.winfo_width()-resized.width)/2
        top=(self.canvas.winfo_height()-resized.height)/2
        self.image_rect=left,top,resized.width,resized.height
        self.canvas.delete('all')
        self.canvas.create_image(left,top,image=self.photo,anchor='nw')
        if self.bounds:
            x0,y0,x1,y1=self.bounds
            self.canvas.create_rectangle(left+x0*resized.width,top+y0*resized.height,
                                         left+x1*resized.width,top+y1*resized.height,
                                         outline='#176B57',width=3,tags='selection')

    def press(self, event):
        if self.busy:
            return
        left,top,width,height=self.image_rect
        self.start_point=(event.x,event.y) if left<=event.x<=left+width and top<=event.y<=top+height else None
        if self.start_point:
            self.bounds=None
            self.candidates=[]
            self.choice.set('')
            self.choice.configure(values=())
            self.use_button.configure(state='disabled')
            self.draw()

    def drag(self, event):
        if self.busy or self.start_point is None:
            return
        self.canvas.delete('selection')
        left,top,width,height=self.image_rect
        x=max(left,min(left+width,event.x))
        y=max(top,min(top+height,event.y))
        self.canvas.create_rectangle(*self.start_point,x,y,outline='#176B57',width=3,tags='selection')

    def release(self, event):
        if self.busy or self.start_point is None:
            return
        try:
            self.bounds=display_region(self.start_point,(event.x,event.y),self.image_rect)
            crop_region(self.image,self.bounds)
            self.status.set('选区已准备，点击“识别选区”；可重新拖框。')
        except ValueError as exc:
            self.bounds=None
            self.status.set(str(exc))
        self.start_point=None
        self.draw()

    def read(self):
        if self.busy:
            return
        if self.bounds is None:
            self.status.set('请先在票面内拖动鼠标框选金额。')
            return
        self.busy=True
        self.candidates=[]
        self.choice.set('')
        self.choice.configure(values=())
        self.use_button.configure(state='disabled')
        self.read_button.configure(state='disabled')
        self.app.stop=Event()
        self.app.reader.stop=self.app.stop
        image,bounds,reader=self.image,self.bounds,self.app.reader
        self.status.set('正在识别选区，可取消；当前推理结束后停止。')
        def work():
            try:
                result=recognize_region(reader,image,bounds)
                self.results.put((result,None))
            except Exception as exc:
                self.results.put(([],str(exc)))
        Thread(target=work,daemon=True).start()
        self.poll_timer=self.window.after(80,self.poll)

    def poll(self):
        try:
            candidates,error=self.results.get_nowait()
        except Empty:
            self.poll_timer=self.window.after(80,self.poll)
            return
        self.busy=False
        if self.cancelled:
            self.finish()
            return
        self.read_button.configure(state='normal')
        self.candidates=candidates
        self.choice.configure(values=[f"{item['value']}  ·  OCR {item['confidence']:.1%}" for item in candidates])
        if candidates:
            # Deliberately require an explicit choice, including a single result.
            self.status.set(f'找到 {len(candidates)} 个候选。请选择并对照票面，填入后还需保存并确认。')
            self.use_button.configure(state='normal')
        else:
            self.status.set('重读失败：'+error if error else '未读取到有效人民币数字金额，请重新框选或手动填写。')

    def adopt(self):
        index=self.choice.current()
        if self.busy or not 0<=index<len(self.candidates):
            self.status.set('请先从列表中选择要核对的候选金额。')
            return
        if self.app.accept_region_candidate(self.row,self.candidates,self.candidates[index]):
            self.finish()

    def cancel(self):
        if self.busy:
            self.cancelled=True
            self.app.stop.set()
            self.cancel_button.configure(state='disabled')
            self.status.set('已请求取消，当前推理结束后关闭；记录不会改变。')
        else:
            self.finish()

    def finish(self):
        for timer in (self.draw_timer,self.poll_timer):
            if timer is not None:
                self.window.after_cancel(timer)
        self.app.set_busy(False)
        self.window.grab_release()
        self.window.destroy()
