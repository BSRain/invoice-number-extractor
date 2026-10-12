"""Conservative RMB gross-total extraction; never infer totals from item prices."""
from decimal import Decimal
from dataclasses import replace
import re
import unicodedata
from .uppercase import uppercase_money


TOTAL = re.compile(r'价\s*税\s*合\s*计|total\s+incl\.?\s*(?:tax|VAT)', re.I)
SMALL = re.compile(r'[（(]?小\s*写[）)]?')
# Consume malformed tokens whole so that 1,23.00 cannot become 23.00.
TOKEN = re.compile(r'(?<![\w.,¥￥+\-])(?:-\s*)?(?:[¥￥]|CNY\s*)?\s*(?:-\s*)?\d[\dA-Za-z,.]*(?![\w.,])', re.I)
VALUE = re.compile(r'-?(?:\d{1,12}|\d{1,3}(?:,\d{3}){1,3})(?:\.\d{2})?')


def money(value):
    text = unicodedata.normalize('NFKC', value).replace('−','-').strip()
    text = re.sub(r'^(-?)\s*(?:¥|CNY)\s*', r'\1', text, flags=re.I).replace(' ', '')
    if not VALUE.fullmatch(text):
        raise ValueError('金额须为人民币数字，最多 12 位整数、小数为两位；可填写负数或留空。')
    return format(Decimal(text.replace(',', '')), '.2f')


def _values(text):
    text = unicodedata.normalize('NFKC', text).replace('−','-')
    if '$' in text or re.search(r'\b(?:USD|EUR|HKD)\b', text, re.I):
        return []
    values = []
    for match in TOKEN.finditer(text):
        token = match.group().strip()
        # Unmarked integers often come from dates, accounts or missing decimals.
        if '.' not in token and not re.search(r'¥|CNY', token, re.I):
            continue
        try:
            values.append(money(token))
        except ValueError:
            pass
    return values


def amount_boxes(boxes):
    """Join only nearby fragments of known labels, never adjacent price columns."""
    parts = {'价','税','合','计','价税','税合','合计','小','写'}
    fragments = sorted((b for b in boxes if b.text.strip(' （）()') in parts),
                       key=lambda b: b.bbox[0])
    joined = []
    for first in fragments:
        text = first.text.strip(' （）()')
        bounds, score = first.bbox, first.confidence
        for other in fragments:
            gap = other.bbox[0]-bounds[2]
            if not (0 <= gap <= max(first.height,other.height)*.7 and
                    abs(first.cy-other.cy) <= min(first.height,other.height)*.4):
                continue
            extended = text+other.text.strip(' （）()')
            if not any(label.startswith(extended) for label in ('价税合计','小写')):
                continue
            text = extended
            bounds = (bounds[0],min(bounds[1],other.bbox[1]),other.bbox[2],max(bounds[3],other.bbox[3]))
            score = min(score,other.confidence)
            if text in ('价税合计','小写'):
                joined.append(replace(first,text=text,bbox=bounds,confidence=score))
                break
    return list(boxes)+joined


def _near_values(region, label):
    eligible = [b for b in region if not re.search(r'税额|税率|不含税|单价|小计|银行|账号',b.text)]
    inline = [b for b in eligible if b is label or
              (b.bbox[0] >= label.bbox[0]-.025 and
               -.3*min(b.height,label.height) <= b.cy-label.cy <= max(b.height,label.height)*.85)]
    values = [(value,b) for b in inline for value in _values(b.text)]
    if not values:
        # A wrapped value must be directly below its label, not an adjacent column.
        below = [b for b in eligible if abs(b.bbox[0]-label.bbox[0]) <= .12 and
                 0 < b.cy-label.cy <= max(b.height,label.height)*2]
        values = [(value,b) for b in below for value in _values(b.text)]
    if not values:
        return []
    distance = min(abs(b.cy-label.cy) for _,b in values)
    return [(value,b) for value,b in values
            if abs(b.cy-label.cy) <= distance+.25*min(b.height,label.height)]


def merge_amounts(row, other):
    """Keep conflicting evidence and leave number/source/manual fields untouched."""
    candidates = list(dict.fromkeys(row.amount_candidates+other.amount_candidates))
    if len(candidates) > 1:
        row.amount, row.amount_status = '', '待复核'
        row.amount_candidates = candidates
        row.amount_reason = '金额重读与原结果存在不同候选，请对照原件确认。'
    elif other.amount_status == '已识别' or (not row.amount_candidates and other.amount_candidates):
        for name in ('amount','amount_status','amount_reason','amount_candidates','amount_bbox'):
            setattr(row,name,getattr(other,name))


def attach_amounts(rows, boxes, method):
    """Assign total labels to invoice regions; conflicts remain review candidates."""
    boxes = amount_boxes(boxes)
    anchors = [(r, r.bbox) for r in rows if r.bbox is not None]
    if not anchors and len(rows) == 1:
        anchors = [(rows[0], (0, 0, 1, 0))]
    for row in rows:
        row.amount = ''
        row.amount_status = '未识别'
        row.amount_candidates = []
        row.amount_reason = '未找到可靠的价税合计（小写），请对照原件补录。'
        row.amount_bbox = None
    if not anchors:
        return rows

    def owner(b):
        # Stacked invoices end at the next number header. Headers at the same
        # height form a horizontal band, split midway between their centres.
        above = [(r, a) for r, a in anchors if a[1] <= b.cy+.03]
        if not above:
            return None
        top = max(a[1] for _, a in above)
        band = [(r, a) for r, a in above if abs(a[1]-top) <= .04]
        return min(band, key=lambda pair: abs((pair[1][0]+pair[1][2])/2-
                                              (b.bbox[0]+b.bbox[2])/2))[0]

    for row, _ in anchors:
        region = [b for b in boxes if owner(b) is row]
        totals = [b for b in region if TOTAL.search(b.text)]
        upper = {value for total in totals for b in region
                 if (method != 'ocr' or min(total.confidence,b.confidence)>=.96)
                 and abs(b.cy-total.cy) <= max(.04,total.height*2)
                 for value in [uppercase_money(b.text)] if value is not None}
        matches = []
        for total in totals or [None]:
            small = [b for b in region if SMALL.search(b.text) and (total is None or
                     -.02 <= b.cy-total.cy <= max(.06, total.height*2))]
            if total is not None and not small and re.search(r'大\s*写',total.text):
                continue
            labels = small or ([total] if total is not None else [])
            for label in labels:
                for value,b in _near_values(region,label):
                    context = total is not None and (method != 'ocr' or
                              (total.confidence >= .96 and label.confidence >= .85))
                    matches.append((value,b.confidence,b,context))
        candidates = list(dict.fromkeys(value for value, _, _, _ in matches))
        row.amount_candidates = candidates
        if len(candidates) == 1:
            best = max(matches, key=lambda item: (item[3],item[1]))
            row.amount_bbox = best[2].bbox
            verified = upper == {candidates[0]}
            mismatch = bool(upper) and not verified
            if best[3] and not mismatch and (method != 'ocr' or best[1] >= .96 or
                                             (verified and best[1]>=.85)):
                row.amount, row.amount_status = candidates[0], '已识别'
                row.amount_reason = ('票面大写与小写金额一致，已交叉核对。' if verified
                                     else '价税合计标签与小写金额位置匹配。')
            else:
                row.amount_status = '待复核'
                row.amount_reason = ('票面大写与小写金额不一致，请对照原件确认。' if mismatch
                                     else '金额 OCR 分数偏低，请核对后确认。' if best[3]
                                     else '总额标签缺失或不够清晰，金额仅作候选，请核对后确认。')
        elif candidates:
            row.amount_status = '待复核'
            row.amount_reason = '价税合计区域存在不同金额候选，请对照原件选择。'
    for row, anchor in anchors:
        peers = [r for r, a in anchors if abs(a[1]-anchor[1]) <= .04]
        if len(peers) > 1 and any(not r.amount_candidates for r in peers) and row.amount:
            row.amount = ''
            row.amount_status = '待复核'
            row.amount_reason = '同页并排多票的金额归属不明确，请对照原件确认。'
    return rows
