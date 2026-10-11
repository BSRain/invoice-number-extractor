"""Read invoice originals without modifying them. OCR is lazy and entirely local."""
from pathlib import Path
from collections import OrderedDict
from copy import deepcopy
import hashlib
import math
import os
import re
from threading import Event

from .core import NUMBER_LABEL, InvoiceRecord, TextBox, extract_invoices, normalize, numbers

SUPPORTED = {'.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.txt'}
MAX_DECODE_PIXELS = 30_000_000
MAX_OCR_SIDE = 2400
MAX_CACHE_RECORDS = 200
MAX_CACHE_FILES = 32
FAST_BAND_MIN_WIDTH = 1400


def collect_inputs(paths):
    found = {}
    for raw in paths:
        path = Path(raw).resolve()
        if path.is_dir():
            items = sorted(path.rglob('*'))
        else:
            items = [path]
        for item in items:
            if item.is_file() and item.suffix.lower() in SUPPORTED and not item.is_symlink():
                found[str(item).casefold()] = item
    return list(found.values())


def parse_text(text, source='粘贴文字'):
    if len(text) > 100_000:
        raise ValueError('文字超过 10 万字，请分批粘贴。')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError('请输入发票文字或号码。')
    # Bare-number lists have no context, so each complete line is a review row.
    if all(re.fullmatch(r'[\d \t]+', normalize(line)) for line in lines):
        rows = []
        for i, line in enumerate(lines,1):
            values = numbers(line)
            rows.append(InvoiceRecord(source=source, page=i, method='text',
                                      invoice_number=values[0] if len(values) == 1 else '',
                                      candidates=values, status='待复核' if values else '未识别',
                                      reason='纯号码缺少票面上下文，请确认。', evidence=line))
        return rows
    step = 1/max(len(lines),20)
    boxes = [TextBox(line, (.5, i*step, .99, (i+.6)*step)) for i,line in enumerate(lines)]
    rows = extract_invoices(boxes, 'text')
    for row in rows:
        row.source = source
    return rows


def page_image(page, max_side=MAX_OCR_SIDE):
    import pymupdf as fitz
    from PIL import Image
    scale = min(220/72, max_side/max(page.rect.width, page.rect.height))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('PDF 页面尺寸无效。')
    pix = page.get_pixmap(matrix=fitz.Matrix(scale,scale), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes('RGB',(pix.width,pix.height),pix.samples)


def image_frame(path, index=0, max_side=MAX_OCR_SIDE):
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        image.seek(index)
        if image.width*image.height > MAX_DECODE_PIXELS:
            raise ValueError('图片超过 3000 万像素，请先缩小图片。')
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((max_side,max_side))
        return image.copy()


def preview_image(row, max_side=1800):
    path = Path(row.source)
    if path.suffix.lower() == '.pdf':
        import pymupdf as fitz
        with fitz.open(path) as doc:
            image = page_image(doc[row.page-1],max_side)
    elif path.suffix.lower() in SUPPORTED-{'.txt'}:
        image = image_frame(path,row.page-1,max_side)
    else:
        return None
    # OCR boxes are in the orientation used for recognition.
    return image.rotate(row.rotation, expand=True) if row.rotation else image


class InvoiceReader:
    def __init__(self, stop=None, single_invoice=False):
        self.stop = stop if stop is not None else Event()
        self.single_invoice = single_invoice
        self._engine = None
        self._cache = OrderedDict()
        self.cache_hits = 0

    def native_boxes(self, page):
        import pymupdf as fitz
        rect = page.rect
        boxes = []
        for block in page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)['blocks']:
            for line in block.get('lines',[]):
                text = ''.join(span['text'] for span in line['spans']).strip()
                if not text:
                    continue
                bbox = fitz.Rect(line['bbox'])*page.rotation_matrix
                boxes.append(TextBox(text,(bbox.x0/rect.width,bbox.y0/rect.height,
                                           bbox.x1/rect.width,bbox.y1/rect.height)))
        return boxes

    def engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR(intra_op_num_threads=min(4,os.cpu_count() or 2), inter_op_num_threads=1,
                                    max_side_len=MAX_OCR_SIDE, det_limit_side_len=1200,
                                    det_limit_type='max', text_score=.5)
        return self._engine

    def ocr_boxes(self, image):
        import numpy as np
        result, _ = self.engine()(np.asarray(image))
        width,height = image.size
        boxes = []
        for polygon,text,confidence in result or []:
            xs,ys = zip(*polygon)
            boxes.append(TextBox(text,(min(xs)/width,min(ys)/height,
                                       max(xs)/width,max(ys)/height),float(confidence)))
        return boxes

    def fast_header(self, image):
        """Opt-in single-invoice page: detect orientation, then OCR its header.

        Empty, ambiguous, low-score or incomplete traditional fields fall back
        to the standard full-page reader. Coordinates remain full-page based.
        """
        import cv2
        import numpy as np
        polygons, _ = self.engine()(np.asarray(image),use_cls=False,use_rec=False)
        if self.stop.is_set() or not polygons:
            return None
        horizontal = vertical = 0
        for polygon in polygons:
            xs,ys = zip(*polygon)
            width,height = max(xs)-min(xs),max(ys)-min(ys)
            horizontal += width > height*1.5
            vertical += height > width*1.5
        angle = 90 if vertical > horizontal else 0
        oriented = image.rotate(angle,expand=True) if angle else image
        width,height = oriented.size
        # RapidOCR 1.4.4 components share the already loaded models. Only this
        # adapter uses the pinned component API; failure preserves the old path.
        try:
            limit = .25 if width > FAST_BAND_MIN_WIDTH else .4
            reused = self.recognize_detected_boxes(oriented,polygons,angle,limit)
            rows = self.accept_header(reused,angle,'复用检测框')
            if rows is not None or self.stop.is_set():
                return rows
            incomplete = extract_invoices(reused,'ocr')
            if (any(NUMBER_LABEL.search(normalize(b.text)) for b in reused)
                    and len(incomplete) == 1 and incomplete[0].invoice_number):
                return None
        except (AttributeError,TypeError,ValueError,RuntimeError,cv2.error):
            pass
        bottom = max(1,int(height*.4))
        header = oriented.crop((0,0,width,bottom))
        if width > FAST_BAND_MIN_WIDTH:
            # Keep original pixels; a narrower first pass saves recognition
            # work without degrading small digits. Failed fields use the
            # original 40% header, then the standard full-page fallback.
            band_bottom = max(1,int(height*.25))
            boxes = self.header_boxes(oriented.crop((0,0,width,band_bottom)),0,band_bottom,height)
            rows = self.accept_header(boxes,angle,'号码区域高清识别')
            if rows is not None or self.stop.is_set():
                return rows
            incomplete = extract_invoices(boxes,'ocr')
            labelled = any(NUMBER_LABEL.search(normalize(b.text)) for b in boxes)
            if labelled and len(incomplete) == 1 and incomplete[0].invoice_number:
                # A complete but unreliable value needs the full-page context.
                # Do not add a second header attempt to already difficult pages.
                return None
        boxes = self.header_boxes(header,0,bottom,height)
        return self.accept_header(boxes,angle,'票头识别')

    def recognize_detected_boxes(self,image,polygons,angle,limit):
        """Recognize original-resolution header crops without another detector.

        Polygon coordinates from RapidOCR are in the original page space.
        Map them to the same upright space as the preview before cropping.
        Keep classification and its 180-degree correction; never guess digits.
        """
        import numpy as np
        if self.stop.is_set():
            return []
        width,height = image.size
        selected = []
        for raw in polygons:
            points = np.asarray(raw,dtype=np.float32)
            if points.shape != (4,2) or not np.isfinite(points).all():
                raise ValueError('Invalid detected text polygon')
            if angle == 90:
                points = np.column_stack((points[:,1],height-points[:,0]))[[1,2,3,0]]
            elif angle != 0:
                raise ValueError('Unsupported detection rotation')
            x0,y0 = points.min(axis=0)
            x1,y1 = points.max(axis=0)
            if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1-x0 < 1 or y1-y0 < 1:
                raise ValueError('Detected text polygon out of page bounds')
            if y1 <= int(height*limit):
                selected.append(points)
        if not selected:
            return []
        if len(selected) > 128:
            raise ValueError('Too many header text polygons')
        engine = self.engine()
        crops = engine.get_crop_img_list(np.asarray(image),selected)
        if len(crops) != len(selected):
            raise ValueError('Text crop count mismatch')
        if self.stop.is_set():
            return []
        classified = engine.text_cls(crops)
        if not isinstance(classified,tuple) or len(classified) != 3:
            raise ValueError('Unsupported classification component result')
        crops,_,_ = classified
        if len(crops) != len(selected):
            raise ValueError('Classification count mismatch')
        if self.stop.is_set():
            return []
        recognized = engine.text_rec(crops)
        if not isinstance(recognized,tuple) or len(recognized) != 2:
            raise ValueError('Unsupported recognition component result')
        values,_ = recognized
        if len(values) != len(selected):
            raise ValueError('Recognition count mismatch')
        boxes = []
        for points,(text,score) in zip(selected,values):
            score = float(score)
            if not isinstance(text,str) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('Invalid text recognition result')
            if score < .5:
                continue
            x0,y0 = points.min(axis=0)
            x1,y1 = points.max(axis=0)
            boxes.append(TextBox(text,(float(x0/width),float(y0/height),
                                       float(x1/width),float(y1/height)),score))
        return [] if self.stop.is_set() else boxes

    def header_boxes(self,image,top,bottom,page_height):
        return [TextBox(b.text,(b.bbox[0],(top+b.bbox[1]*(bottom-top))/page_height,
                               b.bbox[2],(top+b.bbox[3]*(bottom-top))/page_height),b.confidence)
                for b in self.ocr_boxes(image)]

    def accept_header(self,boxes,angle,stage):
        rows = extract_invoices(boxes,'ocr')
        if self.stop.is_set():
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        if row.status != '已识别' or not row.invoice_number:
            return None
        if len(row.invoice_number) == 8 and not row.invoice_code:
            return None
        row.rotation = angle
        row.reason = f'快速模式：{stage}。'+row.reason
        return rows

    def recognize_image(self, image):
        if self.single_invoice and not self.stop.is_set():
            try:
                rows = self.fast_header(image)
            except (ValueError,RuntimeError):
                rows = None
            if rows is not None:
                return rows
        best_rows = []
        best_rank = (-1,-1,-1,-1,-1)
        for angle in (0,90,270,180):
            if self.stop.is_set():
                return []
            oriented = image.rotate(angle,expand=True) if angle else image
            boxes = self.ocr_boxes(oriented)
            rows = extract_invoices(boxes,'ocr')
            for row in rows:
                row.rotation = angle
            horizontal = sum(b.bbox[2]-b.bbox[0] >= b.bbox[3]-b.bbox[1]
                             for b in boxes if len(b.text) >= 4)
            vertical = sum(b.bbox[3]-b.bbox[1] > (b.bbox[2]-b.bbox[0])*1.5
                           for b in boxes if len(b.text) >= 4)
            upright = horizontal >= vertical
            top_labels = sum(b.cy < .5 and bool(NUMBER_LABEL.search(normalize(b.text)))
                             for b in boxes)
            rank = (int(upright), top_labels, sum(r.status == '已识别' for r in rows),
                    sum(bool(r.invoice_number) for r in rows),
                    sum(len(r.candidates) for r in rows))
            if rank > best_rank:
                best_rank,best_rows = rank,rows
            # Rotation corrects orientation, not blurred digits. Keep review status
            # when a clear header and all number regions already have a value.
            readable_header = any(b.cy < .5 and b.confidence >= .8
                                  and NUMBER_LABEL.search(normalize(b.text)) for b in boxes)
            if upright and top_labels and (all(r.status == '已识别' for r in rows)
                                          or (readable_header and horizontal >= max(3,vertical*3)
                                              and all(r.invoice_number for r in rows))):
                break
        return best_rows

    @staticmethod
    def fingerprint(path, stop=None):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024),b''):
                if stop is not None and stop.is_set():
                    raise InterruptedError('读取已停止。')
                digest.update(chunk)
        return digest.hexdigest()

    def read(self, raw_path):
        """Bounded in-memory content cache; edits and modes get different keys."""
        if self.stop.is_set():
            return
        path = Path(raw_path).resolve()
        if path.suffix.lower() not in SUPPORTED:
            yield from self._read_uncached(path)
            return
        try:
            key = (path.suffix.lower(),self.fingerprint(path,self.stop),self.single_invoice)
        except InterruptedError:
            return
        except OSError:
            yield from self._read_uncached(path)
            return
        if key in self._cache:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            for original in self._cache[key]:
                if self.stop.is_set():
                    break
                row = deepcopy(original)
                row.source = str(path)
                yield row
            return
        saved = []
        cacheable = True
        for row in self._read_uncached(path):
            # Copy before yielding: GUI edits must never alter cached OCR data.
            if cacheable and len(saved) == MAX_CACHE_RECORDS:
                saved.clear()
                cacheable = False
            if cacheable:
                saved.append(deepcopy(row))
            yield row
        if self.stop.is_set() or not cacheable or not saved:
            return
        if any(r.status == '失败' for r in saved):
            return
        try:
            if self.fingerprint(path,self.stop) != key[1]:
                return
        except OSError:
            return
        self._cache[key] = saved
        while (len(self._cache) > MAX_CACHE_FILES
               or sum(len(rows) for rows in self._cache.values()) > MAX_CACHE_RECORDS):
            self._cache.popitem(last=False)

    def _read_uncached(self, raw_path):
        import pymupdf as fitz
        from PIL import Image
        path = Path(raw_path).resolve()
        try:
            if path.suffix.lower() == '.txt':
                data = path.read_bytes()
                if len(data) > 1_000_000:
                    raise ValueError('文本文件过大，请分批处理。')
                try:
                    text = data.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = data.decode('gb18030')
                for row in parse_text(text,str(path)):
                    if self.stop.is_set():
                        break
                    yield row
                return
            if path.suffix.lower() == '.pdf':
                with fitz.open(path) as doc:
                    if doc.needs_pass:
                        raise ValueError('PDF 有密码，请先解锁后导入。')
                    for index,page in enumerate(doc):
                        if self.stop.is_set():
                            break
                        try:
                            rows = extract_invoices(self.native_boxes(page),'pdf')
                            if not all(r.status == '已识别' for r in rows):
                                native_rows = rows
                                rows = self.recognize_image(page_image(page))
                                if rows and not any(r.invoice_number or r.candidates for r in rows):
                                    if any(r.invoice_number or r.candidates for r in native_rows):
                                        rows = native_rows
                            for row in rows:
                                row.source,row.page = str(path),index+1
                                yield row
                        except Exception as exc:
                            yield self.failure(path,index+1,exc)
            elif path.suffix.lower() in SUPPORTED-{'.pdf','.txt'}:
                with Image.open(path) as source:
                    count = getattr(source,'n_frames',1)
                for index in range(count):
                    if self.stop.is_set():
                        break
                    try:
                        for row in self.recognize_image(image_frame(path,index)):
                            row.source,row.page = str(path),index+1
                            yield row
                    except Exception as exc:
                        yield self.failure(path,index+1,exc)
            else:
                raise ValueError('不支持此文件类型；RAR/ZIP 请先解压再导入文件夹。')
        except Exception as exc:
            yield self.failure(path,1,exc)

    @staticmethod
    def failure(path,page,exc):
        return InvoiceRecord(source=str(path),page=page,status='失败',
                             reason=f'{type(exc).__name__}: {exc}')
