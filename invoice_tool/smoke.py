"""Explicit, opt-in portable verification using synthetic invoices only."""
from dataclasses import asdict
import json
from pathlib import Path
import time


def run_smoke(folder):
    folder = folder.resolve()
    folder.mkdir(parents=True,exist_ok=True)
    root = None
    try:
        import pymupdf as fitz
        from PIL import Image, ImageGrab
        from openpyxl import load_workbook
        from unittest.mock import patch
        from .gui import create_app, filedialog, messagebox
        from .reader import parse_text
        with fitz.open() as raster:
            page = raster.new_page(width=800,height=500)
            page.insert_text((260,45),'VAT INVOICE',fontsize=22)
            page.insert_text((65,85),'3100214130',fontsize=20)
            page.insert_text((485,85),'Invoice No: 00123456',fontsize=20)
            for i in range(5):
                page.insert_text((65,170+i*40),'Invoice item description and quantity',fontsize=18)
            page.insert_text((400,430),'Total incl. tax: CNY 113.00',fontsize=20)
            pix = page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
            image = Image.frombytes('RGB',(pix.width,pix.height),pix.samples).rotate(270,expand=True)
            image.save(folder/'旋转扫描件.png')
        pdf = folder/'电子发票 示例.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=800,height=500)
            page.insert_text((260,45),'ELECTRONIC INVOICE',fontsize=22)
            page.insert_text((370,85),'Invoice No: 26000000000000000001',fontsize=16)
            page.insert_text((400,430),'Total incl. tax: CNY 950.00',fontsize=16)
            doc.save(pdf)
        root,app = create_app()
        root.attributes('-topmost',True)
        app.import_paths([pdf,folder/'旋转扫描件.png'])
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        if app.busy:
            raise RuntimeError('Batch verification timed out.')
        assert len(app.rows) == 2, [asdict(r) for r in app.rows]
        assert app.rows[0].invoice_number == '26000000000000000001'
        assert app.rows[1].invoice_number == '00123456'
        assert app.rows[1].invoice_code == '3100214130'
        assert app.rows[1].rotation == 90
        assert [(r.amount,r.amount_status) for r in app.rows] == [('950.00','已识别'),('113.00','已识别')]
        # Exercise actual Tk click bindings; intercept only the Windows viewer
        # boundary so the opt-in smoke test doesn't launch external programs.
        for index in (0,1):
            app.tree.selection_set(str(index))
            root.update()
            app.render()
            assert app.canvas.cget('cursor') == 'hand2'
            assert app.crop_canvas.cget('cursor') == 'hand2'
            app.amount.set('999.99')
            before = asdict(app.rows[index])
            with patch('invoice_tool.gui.os.startfile') as launch:
                for canvas in (app.canvas,app.crop_canvas):
                    canvas.event_generate('<Button-1>',x=20,y=20)
                    root.update()
                assert launch.call_count == 2
                assert all(call.args == (app.rows[index].source,) for call in launch.call_args_list)
            assert app.amount.get() == '999.99' and asdict(app.rows[index]) == before
            assert app.selected == index and app.form_changed()
            app.amount.set(app.rows[index].amount)
        app.add_rows(parse_text('发票号码：00123456\n发票代码：3100214130'))
        app.add_rows(parse_text('00000001'))
        assert app.rows[2].duplicate_of == '2'
        app.tree.selection_set('0')
        root.update()
        app.next_review()
        root.update()
        assert app.selected == 2  # Number accepted, amount missing.
        app.tree.selection_set('3')
        root.update()
        app.number.set('00000002')
        app.code.set('3100214130')
        app.amount.set('-123.45')
        app.confirm()
        assert app.rows[3].invoice_number == '00000002'
        assert app.rows[3].status == '已确认'
        assert (app.rows[3].amount,app.rows[3].amount_status) == ('-123.45','已确认')
        assert not app.form_changed()
        exported = folder/'验证结果.xlsx'
        previous = filedialog.asksaveasfilename
        filedialog.asksaveasfilename = lambda **kwargs: str(exported)
        try:
            app.export()
        finally:
            filedialog.asksaveasfilename = previous
        with exported.open('rb') as stream:
            book = load_workbook(stream)
            sheet = book['发票号码']
            assert sheet['C2'].value == '26000000000000000001'
            assert sheet['C3'].value == '00123456'
            assert sheet['C5'].value == '00000002'
            assert sheet['C2'].data_type == 's'
            assert book.active.title == '全部发票号'
            assert book.active['B2'].value == '26000000000000000001'
            assert book.active.max_row == len(app.rows)+1
            assert book.active['I5'].value == -123.45
            assert book.active['I5'].data_type == 'n'
            assert sheet['K2'].value == 950
            book.close()
        # Amount-only edits must be guarded by both row switching and export.
        app.amount.set('-999.01')
        prompt = messagebox.askyesnocancel
        notice = messagebox.showinfo
        save_dialog = filedialog.asksaveasfilename
        notices = []
        messagebox.askyesnocancel = lambda *args,**kwargs: None
        messagebox.showinfo = lambda *args,**kwargs: notices.append(args[0])
        filedialog.asksaveasfilename = lambda **kwargs: (_ for _ in ()).throw(AssertionError('unsaved export'))
        try:
            app.tree.selection_set('1')
            root.update()
            assert app.selected == 3 and app.amount.get() == '-999.01'
            app.export()
            assert notices == ['修改尚未保存']
        finally:
            messagebox.askyesnocancel = prompt
            messagebox.showinfo = notice
            filedialog.asksaveasfilename = save_dialog
        app.amount.set('-123.45')
        # Switching rows must preserve an unconfirmed edit when the user cancels.
        app.number.set('00000003')
        previous_prompt = messagebox.askyesnocancel
        messagebox.askyesnocancel = lambda *args,**kwargs: None
        try:
            app.tree.selection_set('1')
            root.update()
            assert app.selected == 3
            assert app.number.get() == '00000003'
        finally:
            messagebox.askyesnocancel = previous_prompt
        messagebox.askyesnocancel = lambda *args,**kwargs: True
        try:
            app.tree.selection_set('1')
            root.update()
            assert app.rows[3].invoice_number == '00000003'
            assert app.selected == 1
        finally:
            messagebox.askyesnocancel = previous_prompt
        # A later batch must reuse the loaded model and get a fresh stop event.
        engine = app.reader._engine
        assert engine is not None
        app.stop.set()
        second = folder/'第二批扫描件.png'
        second.write_bytes((folder/'旋转扫描件.png').read_bytes())
        app.import_paths([second])
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        assert not app.busy
        assert not app.reader.stop.is_set()
        assert app.reader._engine is engine
        assert len(app.rows) == 5
        assert app.rows[-1].invoice_number == '00123456'
        assert app.reader.cache_hits == 1
        # Mode is part of the content cache key; fast mode must really OCR.
        app.single_invoice.set(True)
        fast_file = folder/'快速模式扫描件.png'
        fast_file.write_bytes(second.read_bytes())
        previous_header = app.reader.fast_header
        header_attempts = []
        def checked_header(image):
            result = previous_header(image)
            header_attempts.append(result)
            return result
        app.reader.fast_header = checked_header
        previous_detector = engine.text_det
        detection_calls = []
        def counted_detector(image):
            detection_calls.append(1)
            return previous_detector(image)
        engine.text_det = counted_detector
        app.import_paths([fast_file])
        assert str(app.fast_check['state']) == 'disabled'
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        engine.text_det = previous_detector
        assert not app.busy
        assert len(app.rows) == 6
        assert app.rows[-1].invoice_number == '00123456'
        assert app.rows[-1].invoice_code == '3100214130'
        assert app.rows[-1].rotation == 90
        # The narrower original-resolution crop now passes the same .96 gate.
        # Verify the real fast path, not a cached result or a lowered threshold.
        assert len(header_attempts) == 1
        assert header_attempts[0] is not None
        assert header_attempts[0][0].status == '已识别'
        assert app.rows[-1].reason.startswith('快速模式：复用检测框。')
        assert len(detection_calls) == 2  # Header detection plus the new amount region.
        assert app.rows[-1].amount == '113.00'
        app.reader.fast_header = previous_header
        assert app.reader.cache_hits == 1
        # Real independent OCR workers, ordered duplicates, and process reuse.
        app.parallel.set(True)
        parallel_files = [folder/f'并行扫描件{i}.png' for i in range(2)]
        for index,path in enumerate(parallel_files):
            # Keep different image content so neither result can be cached.
            with Image.open(second) as image:
                image.putpixel((0,0),(index,0,0))
                image.save(path)
        app.import_paths(parallel_files)
        assert str(app.parallel_check['state']) == 'disabled'
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        assert not app.busy
        assert len(app.rows) == 8
        assert [r.source for r in app.rows[-2:]] == list(map(str,parallel_files))
        assert all((r.invoice_number,r.invoice_code,r.rotation,r.duplicate_of)
                   == ('00123456','3100214130',90,'2') for r in app.rows[-2:])
        worker_pids = app.parallel_reader.pids
        assert len(set(worker_pids)) == 2
        next_files = [folder/f'并行第二批{i}.png' for i in range(2)]
        hits_before = app.parallel_reader.cache_hits
        for source,target in zip(parallel_files[::-1],next_files):
            target.write_bytes(source.read_bytes())
        app.import_paths(next_files)
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        assert not app.busy
        assert app.parallel_reader.pids == worker_pids
        assert app.parallel_reader.cache_hits == hits_before+2
        assert len(app.rows) == 10
        assert all(r.invoice_number == '00123456' for r in app.rows[-2:])
        # One mixed multi-page PDF: original page numbers, native text, real
        # rotated OCR in each process, then actual range-cache reuse for a copy.
        mixed_pdf = folder/'单份多页并行.pdf'
        with fitz.open() as doc:
            for index in range(4):
                if index%2 == 0:
                    page = doc.new_page(width=800,height=500)
                    page.insert_text((370,85),'Invoice No: 26000000000000000001',fontsize=16)
                    page.insert_text((400,430),'Total incl. tax: CNY 950.00',fontsize=16)
                else:
                    page = doc.new_page(width=500,height=800)
                    page.insert_image(page.rect,filename=str(second))
            doc.save(mixed_pdf)
        app.import_paths([mixed_pdf])
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        assert not app.busy
        assert app.parallel_reader.pids == worker_pids
        assert len(app.rows) == 14
        page_rows = app.rows[-4:]
        assert [r.page for r in page_rows] == [1,2,3,4]
        assert [r.invoice_number for r in page_rows] == ['26000000000000000001','00123456']*2
        assert [r.method for r in page_rows] == ['pdf','ocr']*2
        assert [r.rotation for r in page_rows] == [0,90]*2
        assert [r.duplicate_of for r in page_rows] == ['1','2']*2
        assert [r.amount for r in page_rows] == ['950.00','113.00']*2
        assert str(mixed_pdf).casefold() in app.paths_seen
        assert app.progress['maximum'] == 2
        assert app.progress['value'] == 2
        mixed_copy = folder/'多页并行副本.pdf'
        mixed_copy.write_bytes(mixed_pdf.read_bytes())
        hits_before = app.parallel_reader.cache_hits
        app.import_paths([mixed_copy])
        deadline = time.monotonic()+120
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(.03)
        root.update()
        assert not app.busy
        assert len(app.rows) == 18
        assert app.parallel_reader.cache_hits == hits_before+2
        assert [r.source for r in app.rows[-4:]] == [str(mixed_copy)]*4
        assert [r.page for r in app.rows[-4:]] == [1,2,3,4]
        assert [r.amount for r in app.rows[-4:]] == ['950.00','113.00']*2
        from .core import export_excel
        export_excel(app.rows,exported)
        book = load_workbook(exported)
        assert all(sheet.max_row == 19 for sheet in book)
        assert book.active['B19'].value == '00123456'
        assert book.active['B19'].data_type == 's'
        book.close()
        # Native number text with a bitmap total must bypass the native-only
        # preflight and supplement the amount in both serial and page workers.
        hybrid = folder/'文字号码与图片金额.pdf'
        with fitz.open() as amount_doc:
            page = amount_doc.new_page(width=800,height=500)
            page.insert_text((400,430),'Total incl. tax: CNY 113.00',fontsize=20)
            bitmap = page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False).tobytes('png')
        with fitz.open() as doc:
            for index in range(3):
                page = doc.new_page(width=800,height=500)
                page.insert_image(page.rect,stream=bitmap)
                page.insert_text((370,85),'Invoice No: '+str(26000000000000000001+index),fontsize=16)
            doc.save(hybrid)
        serial = list(app.reader.read(hybrid))
        parallel = [value for kind,value in app.parallel_reader.read([hybrid]) if kind=='row']
        for rows in (serial,parallel):
            assert len(rows)==3 and [r.page for r in rows]==[1,2,3]
            assert [r.invoice_number for r in rows]==[str(26000000000000000001+i) for i in range(3)]
            assert all((r.method,r.status,r.amount,r.amount_status)==('pdf','已识别','113.00','已识别') for r in rows)
        app.tree.selection_set('1')
        app.tree.focus('1')
        app.tree.see('1')
        root.update()
        app.select()
        root.update()
        app.render()
        root.update()
        # Capture this HWND only; desktop coordinates can capture another app
        # when a foreground/fullscreen window obscures the test window.
        # Windows can transiently refuse PrintWindow just after a Tk redraw.
        # Retry only this application's own frame/client, never the desktop.
        for attempt in range(3):
            root.update()
            handle = root.winfo_id() if attempt == 1 else int(root.frame(),0)
            try:
                ImageGrab.grab(window=handle).save(folder/'界面验证.png')
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(.15)
        report = {'ok':True,'checks':['native PDF','real offline OCR','90 degree rotation',
                  'GUI background processing','manual correction','unsaved edit protection',
                  'original and number-crop clicks invoke default viewer for PDF and image; edits preserved',
                  'duplicate marking','Excel text values','all-record Excel summary',
                  'OCR model reuse across batches','content cache for copied files',
                  'fast-mode header reuse plus gross-total OCR and rotated preview',
                  'independent amount status, manual negative correction, numeric Excel totals',
                  'two real OCR processes, ordered results, reuse and all-row Excel',
                  'reordered copies hit actual worker caches across batches',
                  'single mixed PDF page-parallel OCR, original page order and range cache',
                  'native number plus bitmap total: independent amount OCR in serial and page workers',
                  'page progress and whole-file completion; 18 rows in both Excel sheets'],
                  'records':[asdict(r) for r in app.rows]}
        (folder/'smoke-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0
    except Exception as exc:
        import traceback
        report = {'ok':False,'error':str(exc),'traceback':traceback.format_exc()}
        (folder/'smoke-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return 1
    finally:
        if root is not None:
            app.parallel_reader.close()
            root.destroy()
