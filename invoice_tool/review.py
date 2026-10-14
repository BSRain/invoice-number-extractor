"""Review filters and coordinates in the upright preview's normalized space."""
import math
from .amounts import _values

FILTERS = ('全部记录','号码待处理','金额待处理','任一待处理','重复记录')
ACCEPTED = ('已识别','已确认')


def visible_indices(rows, name):
    if name not in FILTERS:
        raise ValueError('未知筛选条件。')
    def matches(row):
        number = row.status not in ACCEPTED
        amount = row.amount_status not in ACCEPTED
        return {'全部记录':True, '号码待处理':number, '金额待处理':amount,
                '任一待处理':number or amount, '重复记录':bool(row.duplicate_of)}[name]
    return [index for index,row in enumerate(rows) if matches(row)]


def next_review_index(rows, current, name):
    indices = visible_indices(rows,name)
    start = current+1 if current is not None else 0
    order = [i for i in indices if i>=start]+[i for i in indices if i<start]
    return next((i for i in order if rows[i].status not in ACCEPTED or
                 rows[i].amount_status not in ACCEPTED),None)


def crop_region(image, bounds):
    if len(bounds)!=4 or not all(math.isfinite(v) and 0<=v<=1 for v in bounds):
        raise ValueError('请选择票面内的金额区域。')
    x0,y0,x1,y1=bounds
    width,height=image.size
    pixels=(int(x0*width),int(y0*height),int(x1*width),int(y1*height))
    if pixels[2]-pixels[0]<4 or pixels[3]-pixels[1]<4:
        raise ValueError('选区太小，请重新框选。')
    return image.crop(pixels)


def display_region(start, end, image_rect):
    left,top,width,height=image_rect
    if width<=0 or height<=0 or not (left<=start[0]<=left+width and top<=start[1]<=top+height):
        raise ValueError('请从票面内开始框选。')
    ax,ay=(start[0]-left)/width,(start[1]-top)/height
    bx,by=max(0,min(1,(end[0]-left)/width)),max(0,min(1,(end[1]-top)/height))
    bounds=min(ax,bx),min(ay,by),max(ax,bx),max(ay,by)
    if bounds[2]-bounds[0]<.002 or bounds[3]-bounds[1]<.002:
        raise ValueError('请拖动鼠标框选金额，或取消。')
    return bounds


def region_candidates(boxes, bounds):
    """Manual selection supplies context; every value still requires review."""
    x0,y0,x1,y1=bounds
    found={}
    for box in boxes:
        if not all(math.isfinite(v) and 0<=v<=1 for v in box.bbox):
            continue
        if box.bbox[2]<=box.bbox[0] or box.bbox[3]<=box.bbox[1]:
            continue
        for value in _values(box.text):
            mapped=(x0+box.bbox[0]*(x1-x0),y0+box.bbox[1]*(y1-y0),
                    x0+box.bbox[2]*(x1-x0),y0+box.bbox[3]*(y1-y0))
            if value not in found or box.confidence>found[value]['confidence']:
                found[value]={'value':value,'confidence':box.confidence,'bbox':mapped}
    return list(found.values())


def recognize_region(reader, image, bounds):
    """One bounded OCR call; cancellation produces no adoptable candidates."""
    if reader.stop.is_set():
        return []
    crop=crop_region(image,bounds)
    scale=min(2,2400/max(crop.size))
    if scale!=1:
        from PIL import Image
        crop=crop.resize((max(1,int(crop.width*scale)),max(1,int(crop.height*scale))),Image.Resampling.LANCZOS)
    boxes=reader.ocr_boxes(crop)
    return [] if reader.stop.is_set() else region_candidates(boxes,bounds)
