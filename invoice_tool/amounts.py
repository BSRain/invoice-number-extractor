"""Conservative RMB gross-total extraction; never infer totals from item prices."""
from decimal import Decimal
import re
import unicodedata


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


def attach_amounts(rows, boxes, method):
    """Assign total labels to invoice regions; conflicts remain review candidates."""
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
        matches = []
        for total in totals:
            small = [b for b in region if SMALL.search(b.text)
                     and -.02 <= b.cy-total.cy <= max(.06, total.height*2)]
            if not small and re.search(r'大\s*写',total.text):
                continue
            labels = small or [total]
            for label in labels:
                nearby_values = []
                for b in region:
                    if b is label or (b.bbox[0] >= label.bbox[0]-.025 and
                                      -.3*min(b.height,label.height) <= b.cy-label.cy <=
                                      max(b.height, label.height)*.85):
                        for value in _values(b.text):
                            nearby_values.append((value, min(total.confidence, label.confidence, b.confidence), b))
                if nearby_values:
                    distance = min(abs(b.cy-label.cy) for _,_,b in nearby_values)
                    matches.extend(item for item in nearby_values if abs(item[2].cy-label.cy)
                                   <= distance+.25*min(item[2].height,label.height))
        candidates = list(dict.fromkeys(value for value, _, _ in matches))
        row.amount_candidates = candidates
        if len(candidates) == 1:
            best = max(matches, key=lambda item: item[1])
            row.amount_bbox = best[2].bbox
            if method != 'ocr' or best[1] >= .96:
                row.amount, row.amount_status = candidates[0], '已识别'
                row.amount_reason = '价税合计标签与小写金额位置匹配。'
            else:
                row.amount_status = '待复核'
                row.amount_reason = '金额 OCR 分数偏低，请核对后确认。'
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
