"""Versioned, bounded local task files; JSON only, with atomic replacement."""
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import hmac
import json
import math
import os
import re
import tempfile

from .core import InvoiceRecord
from .amounts import money
from .review import FILTERS

MAX_BYTES=32*1024*1024
MAX_ROWS=50000
STATES={'已识别','已确认','待复核','未识别','失败'}


@dataclass
class TaskState:
    rows: list[InvoiceRecord] = field(default_factory=list)
    completed_paths: set[str] = field(default_factory=set)
    review_filter: str = '全部记录'
    selected: int | None = None
    draft: dict | None = None
    single_invoice: bool = False
    parallel: bool = False
    saved_at: str = ''
    recovery_of: str | None = None


def _require(condition, message='任务内容无效或版本不兼容。'):
    if not condition:
        raise ValueError(message)


def _text(value):
    _require(isinstance(value,str))
    return value


def _source(value):
    _text(value)
    _require('\0' not in value and not value.startswith(('\\\\','//')) and '://' not in value,
             '任务只支持本机文件引用，不接受网络或 URL 路径。')
    if Path(value).suffix.lower() in {'.pdf','.png','.jpg','.jpeg','.bmp','.tif','.tiff','.txt','.invtask'}:
        _require(Path(value).is_absolute(),'任务中的文件引用必须使用本机绝对路径。')
    return value


def _bbox(value):
    if value is None:
        return None
    _require(isinstance(value,(list,tuple)) and len(value)==4)
    _require(all(type(v) in (int,float) and math.isfinite(v) and 0<=v<=1 for v in value))
    _require(value[0]<value[2] and value[1]<value[3])
    return tuple(value)


def _strings(value):
    _require(isinstance(value,list) and len(value)<=1000 and all(isinstance(v,str) for v in value))
    return value


def _candidate(item):
    _require(isinstance(item,dict) and set(item)=={'value','confidence','bbox'})
    value=_text(item['value'])
    _require(money(value)==value)
    score=item['confidence']
    _require(type(score) in (int,float) and math.isfinite(score) and 0<=score<=1)
    bounds=_bbox(item['bbox'])
    _require(bounds is not None)
    return dict(value=value,confidence=score,bbox=bounds)


def _record(item):
    _require(isinstance(item,dict) and set(item)=={f.name for f in fields(InvoiceRecord)})
    item=dict(item)
    for name in ('source','invoice_number','invoice_code','method','status','reason','duplicate_of',
                 'evidence','amount','amount_status','amount_reason'):
        _text(item[name])
    _source(item['source'])
    _require(type(item['page']) is int and 1<=item['page']<=1000000)
    _require(type(item['rotation']) is int and item['rotation'] in (0,90,180,270))
    _require(item['status'] in STATES and item['amount_status'] in STATES-{'失败'})
    _require(item['method'] in ('pdf','ocr','text'))
    _require(not item['invoice_number'] or re.fullmatch(r'(?:[0-9]{8}|[0-9]{20})',item['invoice_number']))
    _require(not item['invoice_code'] or re.fullmatch(r'(?:[0-9]{10}|[0-9]{12})',item['invoice_code']))
    _require(not item['amount'] or money(item['amount'])==item['amount'])
    for name in ('candidates','amount_candidates'):
        _strings(item[name])
    for value in item['amount_candidates']:
        _require(money(value)==value)
    for name in ('bbox','amount_bbox'):
        item[name]=_bbox(item[name])
    return InvoiceRecord(**item)


def _state(payload):
    _require(isinstance(payload,dict) and set(payload)=={f.name for f in fields(TaskState)})
    _require(isinstance(payload['rows'],list) and len(payload['rows'])<=MAX_ROWS)
    rows=[_record(row) for row in payload['rows']]
    paths=payload['completed_paths']
    _require(isinstance(paths,list) and len(paths)<=MAX_ROWS and all(isinstance(p,str) for p in paths))
    _require(set(paths)<={row.source.casefold() for row in rows})
    for path in paths:
        _source(path)
    _require(payload['review_filter'] in FILTERS)
    for name in ('single_invoice','parallel'):
        _require(type(payload[name]) is bool)
    selected=payload['selected']
    _require(selected is None or type(selected) is int and 0<=selected<len(rows))
    for index,row in enumerate(rows,1):
        _require(not row.duplicate_of or row.duplicate_of.isdigit() and 1<=int(row.duplicate_of)<index)
    draft=payload['draft']
    if draft is not None:
        _require(isinstance(draft,dict) and set(draft)=={'index','number','code','amount','region'})
        _require(type(draft['index']) is int and draft['index']==selected)
        draft=dict(draft)
        for name in ('number','code','amount'):
            _text(draft[name])  # Unconfirmed input intentionally need not be valid money/numbers.
        region=draft['region']
        if region is not None:
            _require(isinstance(region,dict) and set(region)=={'candidates','chosen'})
            _require(isinstance(region['candidates'],list) and 0<len(region['candidates'])<=1000)
            candidates=[_candidate(item) for item in region['candidates']]
            chosen=_candidate(region['chosen'])
            _require(chosen in candidates)
            draft['region']={'candidates':candidates,'chosen':chosen}
    timestamp=_text(payload['saved_at'])
    try:
        _require(datetime.fromisoformat(timestamp).tzinfo is not None)
    except (ValueError,TypeError):
        raise ValueError('任务保存时间无效。') from None
    recovery_of=payload['recovery_of']
    if recovery_of is not None:
        _source(recovery_of)
        _require(Path(recovery_of).is_absolute() and Path(recovery_of).suffix.lower()=='.invtask')
    return TaskState(rows,set(paths),payload['review_filter'],selected,draft,
                     payload['single_invoice'],payload['parallel'],timestamp,recovery_of)


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def encode_task(state):
    payload=asdict(state)
    payload['completed_paths']=sorted(state.completed_paths)
    payload['saved_at']=datetime.now(timezone.utc).isoformat()
    _state(payload)
    digest=hashlib.sha256(_canonical(payload)).hexdigest()
    result=_canonical({'format':'invoice-task','version':1,'payload':payload,'sha256':digest})
    _require(len(result)<=MAX_BYTES,'任务超过 32 MiB，请分批保存。')
    return result


def _unique(pairs):
    result={}
    for key,value in pairs:
        _require(key not in result,'任务含重复字段。')
        result[key]=value
    return result


def decode_task(data):
    _require(len(data)<=MAX_BYTES,'任务超过 32 MiB，无法打开。')
    try:
        def invalid_constant(_):
            raise ValueError('任务含非法数值。')
        wrapper=json.loads(data.decode('utf-8-sig'),object_pairs_hook=_unique,parse_constant=invalid_constant)
        _require(isinstance(wrapper,dict) and set(wrapper)=={'format','version','payload','sha256'})
        _require(wrapper['format']=='invoice-task' and type(wrapper['version']) is int and wrapper['version']==1)
        _require(isinstance(wrapper['sha256'],str) and re.fullmatch('[0-9a-f]{64}',wrapper['sha256']))
        digest=hashlib.sha256(_canonical(wrapper['payload'])).hexdigest()
        _require(hmac.compare_digest(digest,wrapper['sha256']),'任务校验失败，文件可能损坏。')
        return _state(wrapper['payload'])
    except (UnicodeError,TypeError,KeyError,OverflowError,RecursionError) as exc:
        raise ValueError('任务文件损坏或格式不兼容。') from exc


def save_task(path,state):
    path=Path(path).absolute()
    _source(str(path))
    _require(path.suffix.lower()=='.invtask','任务文件请使用 .invtask 扩展名。')
    data=encode_task(state)
    descriptor,temporary=tempfile.mkstemp(prefix='.invoice-task-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_task(path):
    with Path(path).open('rb') as stream:
        return decode_task(stream.read(MAX_BYTES+1))
