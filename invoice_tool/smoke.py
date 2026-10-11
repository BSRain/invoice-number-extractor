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
        from .gui import create_app, filedialog, messagebox
        from .reader import parse_text
        with fitz.open() as raster:
            page = raster.new_page(width=800,height=500)
            page.insert_text((260,45),'VAT INVOICE',fontsize=22)
            page.insert_text((65,85),'3100214130',fontsize=20)
            page.insert_text((485,85),'Invoice No: 00123456',fontsize=20)
            for i in range(5):
                page.insert_text((65,170+i*40),'Invoice item description and quantity',fontsize=18)
            pix = page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
            image = Image.frombytes('RGB',(pix.width,pix.height),pix.samples).rotate(270,expand=True)
            image.save(folder/'旋转扫描件.png')
        pdf = folder/'电子发票 示例.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=800,height=500)
            page.insert_text((260,45),'ELECTRONIC INVOICE',fontsize=22)
            page.insert_text((370,85),'Invoice No: 26000000000000000001',fontsize=16)
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
        app.add_rows(parse_text('发票号码：00123456\n发票代码：3100214130'))
        app.add_rows(parse_text('00000001'))
        assert app.rows[2].duplicate_of == '2'
        app.tree.selection_set('3')
        root.update()
        app.number.set('00000002')
        app.code.set('3100214130')
        app.confirm()
        assert app.rows[3].invoice_number == '00000002'
        assert app.rows[3].status == '已确认'
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
            book.close()
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
        assert len(detection_calls) == 1
        app.reader.fast_header = previous_header
        assert app.reader.cache_hits == 1
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
                  'duplicate marking','Excel text values','all-record Excel summary',
                  'OCR model reuse across batches','content cache for copied files',
                  'fast-mode reused detection with one detector call and rotated preview'],
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
            root.destroy()
