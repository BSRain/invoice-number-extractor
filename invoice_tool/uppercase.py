"""Strict financial Chinese amounts, used only to cross-check printed digits."""
from decimal import Decimal
import re
import unicodedata

DIGITS = dict(zip('零壹贰叁肆伍陆柒捌玖',range(10)))
UNITS = {'拾':10,'佰':100,'仟':1000}


def section(text):
    if text == '零':
        return 0
    value, digit, last, zero = 0, None, 10000, False
    for char in text:
        if char == '零':
            if digit is not None or zero or not value:
                raise ValueError()
            zero = True
        elif char in DIGITS:
            if digit is not None:
                raise ValueError()
            digit = DIGITS[char]
        elif char in UNITS:
            unit = UNITS[char]
            if not digit or unit >= last or (last != 10000 and last/unit>10 and not zero):
                raise ValueError()
            value += digit*unit
            digit, last, zero = None, unit, False
        else:
            raise ValueError()
    if zero and digit is None:
        raise ValueError()
    if digit is not None and last>10 and last!=10000 and not zero:
        raise ValueError()
    return value+(digit or 0)


def uppercase_money(text):
    """Return None for incomplete, malformed or ambiguous OCR spelling."""
    text = re.sub(r'\s+','',unicodedata.normalize('NFKC',text))
    # Include invalid leading number/unit spellings so a damaged prefix cannot
    # be silently skipped and a valid-looking suffix treated as the full total.
    start = re.search(r'[负零壹贰叁肆伍陆柒捌玖拾佰仟万亿一二三四五六七八九十百千0-9]',text)
    if not start:
        return None
    text = text[start.start():]
    match = re.fullmatch(r'(负?)([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)[元圆](.*)',text)
    if not match:
        return None
    try:
        integer, value, previous = match[2], 0, False
        for name, unit in (('亿',100000000),('万',10000)):
            if name in integer:
                part,integer = integer.split(name,1)
                leading_zero = part.startswith('零')
                if previous and leading_zero:
                    part = part[1:]
                group = section(part)
                if not 0 < group < 10000 or (previous and group<1000 and not leading_zero):
                    return None
                value += group*unit
                previous = True
        if integer:
            leading_zero = integer.startswith('零')
            if previous and integer == '零':
                return None
            group = section(integer[1:] if previous and leading_zero else integer)
            if previous and group<1000 and not leading_zero:
                return None
            value += group
        if value >= 10**12:
            return None
        suffix = match[3]
        if suffix in ('整','正'):
            cents = 0
        else:
            fraction = re.fullmatch(r'(?:([壹贰叁肆伍陆柒捌玖])角(?:零?([壹贰叁肆伍陆柒捌玖])分)?|零?([壹贰叁肆伍陆柒捌玖])分)',suffix)
            if not fraction:
                return None
            cents = DIGITS.get(fraction[1],0)*10+DIGITS.get(fraction[2] or fraction[3],0)
        amount = Decimal(value)+Decimal(cents)/100
        return format(-amount if match[1] else amount,'.2f')
    except ValueError:
        return None
