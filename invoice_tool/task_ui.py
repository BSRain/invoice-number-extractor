"""Main-thread task state and explicit, opt-in local recovery."""
from copy import deepcopy
from pathlib import Path
from queue import Empty
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from .task_file import TaskState, save_task, load_task
from .recovery import RecoveryWriter
from .review import visible_indices


class TaskController:
    def __init__(self,app):
        self.app=app
        self.writer=RecoveryWriter()
        self.enabled=tk.BooleanVar(value=False)
        self.status=tk.StringVar(value='任务未保存 · 自动恢复关闭')
        self.generation=0
        self.loading=False
        self.reset()
        for variable in (app.number,app.code,app.amount):
            variable.trace_add('write',self.edit_changed)
        for variable in (app.single_invoice,app.parallel):
            variable.trace_add('write',lambda *_:self.changed())

    def build(self,parent):
        bar=ttk.Frame(parent,padding=(20,0,20,4))
        bar.pack(fill='x')
        self.buttons=[]
        for text,command in [('打开任务 / 恢复',self.open),('保存任务',self.save),
                             ('任务另存为',lambda:self.save(save_as=True))]:
            button=ttk.Button(bar,text=text,command=command)
            button.pack(side='left',padx=(0,8))
            self.buttons.append(button)
        self.check=ttk.Checkbutton(bar,text='自动恢复（选择目录）',variable=self.enabled,command=self.toggle)
        self.check.pack(side='left')
        label=ttk.Label(parent,textvariable=self.status,padding=(20,0,20,3),foreground='#546579',wraplength=900)
        label.pack(fill='x')
        parent.bind('<Configure>',lambda e:label.configure(wraplength=max(400,e.width-40)),add='+')

    def set_busy(self,busy):
        for widget in self.buttons+[self.check]:
            widget.configure(state='disabled' if busy else 'normal')

    def edit_key(self):
        a=self.app
        return a.selected,a.number.get(),a.code.get(),a.amount.get()

    def edit_changed(self,*_):
        key=self.edit_key()
        if key!=self.last_edit:
            self.last_edit=key
            self.changed()

    def reset(self):
        self.generation+=1
        self.writer.discard_pending()
        self.enabled.set(False)
        self.path=None
        self.recovery_path=None
        self.revision=0
        self.saved_revision=None
        self.recovered_revision=None
        self.submitted_revision=None
        self.due=None
        self.closing=None
        self.last_edit=self.edit_key()
        self.status.set('任务未保存 · 自动恢复关闭')

    def changed(self):
        if self.loading:
            return
        self.revision+=1
        name=self.path.name if self.path else '任务未保存'
        self.status.set(name+' · 有改动'+(' · 自动恢复已启用' if self.enabled.get() else ''))
        if self.enabled.get() and self.due is None:
            self.due=time.monotonic()+5

    def snapshot(self,recovery=False):
        a=self.app
        draft=None
        if a.selected is not None:
            region=None
            if a._region_pending:
                _,candidates,chosen=a._region_pending
                region=deepcopy(dict(candidates=candidates,chosen=chosen))
            draft=dict(index=a.selected,number=a.number.get(),code=a.code.get(),amount=a.amount.get(),region=region)
        return TaskState(rows=deepcopy(a.rows),completed_paths=set(a.paths_seen),
                         review_filter=a.review_filter.get(),selected=a.selected,draft=draft,
                         single_invoice=a.single_invoice.get(),parallel=a.parallel.get(),
                         recovery_of=str(self.path) if recovery and self.path else None,is_recovery=recovery)

    def needs_warning(self):
        if self.saved_revision==self.revision:
            return False
        return self.app.dirty or self.app.form_changed() or self.path is not None

    def save(self,path=None,save_as=False):
        if self.app.busy:
            return False
        path=path or (None if save_as else self.path)
        if path is None:
            path=filedialog.asksaveasfilename(parent=self.app.root,title='保存核对任务（保留未确认草稿）',
                defaultextension='.invtask',initialfile='发票核对任务.invtask',filetypes=[('发票核对任务','*.invtask')])
        if not path:
            return False
        try:
            save_task(path,self.snapshot())
        except Exception as exc:
            messagebox.showerror('保存任务失败',str(exc),parent=self.app.root)
            return False
        self.path=Path(path).absolute()
        self.saved_revision=self.revision
        self.status.set('任务已保存：'+str(self.path))
        # The recovery metadata should follow a new Save As path as well.
        if self.enabled.get():
            self.submitted_revision=None
            self.due=time.monotonic()
        return True

    def open(self,path=None):
        if self.app.busy:
            return False
        path=path or filedialog.askopenfilename(parent=self.app.root,title='打开任务或自动恢复副本',
                                               filetypes=[('发票核对任务','*.invtask')])
        if not path:
            return False
        try:
            state=load_task(path)
        except Exception as exc:
            messagebox.showerror('无法打开任务',str(exc),parent=self.app.root)
            return False
        if self.needs_warning():
            choice=messagebox.askyesnocancel('切换任务','保存当前任务后再打开？\n选择“否”将放弃当前未保存的进度。',parent=self.app.root)
            if choice is None or choice is True and not self.save():
                return False
            # Saving may have replaced the very file selected for opening.
            if choice is True and self.path==Path(path).absolute():
                try:
                    state=load_task(path)
                except Exception as exc:
                    messagebox.showerror('无法打开任务',str(exc),parent=self.app.root)
                    return False
        self.apply(state,path)
        return True

    def apply(self,state,path):
        a=self.app
        self.loading=True
        try:
            self.reset()
            for index in range(len(a.rows)):
                a.tree.delete(str(index))
            a.rows=state.rows
            a.paths_seen=state.completed_paths
            a.selected=None
            a._region_pending=None
            a.single_invoice.set(state.single_invoice)
            a.parallel.set(state.parallel)
            chosen_filter=state.review_filter
            if state.draft and state.selected not in visible_indices(a.rows,chosen_filter):
                chosen_filter='全部记录'
            a.review_filter.set(chosen_filter)
            a._filter_applied=chosen_filter
            a.clear_detail()
            a.refresh()
            if state.selected is not None and state.selected in visible_indices(a.rows,chosen_filter):
                a.tree.selection_set(str(state.selected))
                a.tree.focus(str(state.selected))
                a.tree.see(str(state.selected))
                a.select()
                if state.draft:
                    draft=state.draft
                    a.number.set(draft['number'])
                    a.code.set(draft['code'])
                    a.amount.set(draft['amount'])
                    if draft['region']:
                        region=draft['region']
                        a.accept_region_candidate(a.rows[a.selected],region['candidates'],region['chosen'])
                        a.amount.set(draft['amount'])
            a.dirty=True
            self.path=None if state.is_recovery else Path(path).absolute()
            self.saved_revision=None if state.is_recovery else self.revision
            self.status.set(('恢复副本已打开，请另存任务' if state.is_recovery else '任务已打开')+
                            ' · 来源 '+Path(path).name+
                            (' · 原任务 '+Path(state.recovery_of).name if state.recovery_of else '')+
                            ' · 保存时间 '+state.saved_at+' · 自动恢复关闭')
            a.progress_text.set('任务已恢复，未确认输入仍是草稿；原件缺失时可继续核对、保存和导出。')
            a.progress.configure(value=0)
        finally:
            self.last_edit=self.edit_key()
            self.loading=False

    def toggle(self):
        self.generation+=1
        self.writer.discard_pending()
        self.recovered_revision=self.submitted_revision=None
        self.due=None
        if not self.enabled.get():
            self.status.set('自动恢复已关闭，已有副本保留。')
            return
        folder=filedialog.askdirectory(parent=self.app.root,title='选择本机自动恢复目录（包含业务字段和原件路径）')
        if not folder:
            self.enabled.set(False)
            return
        folder=Path(folder).absolute()
        if str(folder).startswith(('\\\\','//')):
            self.enabled.set(False)
            messagebox.showerror('目录不支持','请选择本机目录。',parent=self.app.root)
            return
        self.recovery_path=folder/('发票自动恢复-'+uuid4().hex+'.invtask')
        self.status.set('自动恢复已启用：'+str(self.recovery_path))
        self.submit_recovery()

    def submit_recovery(self):
        if not self.enabled.get() or self.recovery_path is None:
            return
        self.writer.submit(self.recovery_path,self.snapshot(recovery=True),self.generation,self.revision)
        self.submitted_revision=self.revision
        self.due=None

    def poll(self):
        try:
            while True:
                generation,revision,error=self.writer.results.get_nowait()
                if generation!=self.generation:
                    continue
                if error:
                    self.enabled.set(False)
                    self.due=None
                    self.status.set('自动恢复失败，已有成功副本保留：'+error)
                    if self.closing:
                        self.closing=None
                        self.app.root.deiconify()
                        messagebox.showerror('自动恢复失败','退出已取消，请先保存任务。\n'+error,parent=self.app.root)
                else:
                    self.recovered_revision=revision
                    self.status.set(('自动恢复已保存' if revision==self.revision else '自动恢复已保存较早进度')+
                                    '：'+str(self.recovery_path))
                    if self.closing and revision==self.closing[0]:
                        callback=self.closing[1]
                        self.closing=None
                        callback()
                        return True
        except Empty:
            pass
        if self.enabled.get() and self.due is not None and time.monotonic()>=self.due:
            self.submit_recovery()
        return False

    def finish(self,callback):
        if self.enabled.get() and self.recovered_revision!=self.revision:
            self.closing=(self.revision,callback)
            self.submit_recovery()
            self.app.root.withdraw()
        else:
            callback()
