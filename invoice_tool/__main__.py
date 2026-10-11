"""Run the native app, or process a folder from the command line."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地批量提取 PDF/图片/文字中的发票号码')
    parser.add_argument('--input',nargs='+',help='文件或文件夹（可多个）')
    parser.add_argument('--output',help='Excel 输出路径（.xlsx）')
    parser.add_argument('--report',help='可选 JSON 明细输出路径')
    parser.add_argument('--overwrite',action='store_true',help='允许替换已有输出文件')
    parser.add_argument('--single-invoice',action='store_true',help='快速模式：输入每页只有一张发票，疑难票回退整页')
    parser.add_argument('--workers',type=int,choices=(1,2),default=1,help='同时处理文件数：1（默认）或 2')
    parser.add_argument('--smoke-test',metavar='DIRECTORY',help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test:
        from invoice_tool.smoke import run_smoke
        return run_smoke(Path(args.smoke_test))
    if args.input:
        if not args.output:
            parser.error('批处理需要 --output 结果.xlsx')
        from invoice_tool.core import export_excel
        from invoice_tool.reader import InvoiceReader, collect_inputs
        output = Path(args.output).resolve()
        report = Path(args.report).resolve() if args.report else None
        if report == output:
            parser.error('Excel 与 JSON 明细必须使用不同的输出路径。')
        files = collect_inputs(args.input)
        if not files:
            parser.error('没有找到支持的文件；RAR/ZIP 请先解压。')
        source_paths = {p.resolve() for p in files}
        if output in source_paths or report in source_paths:
            parser.error('输出路径不能与输入原件相同。')
        for path in (output,report):
            if path and path.exists() and not args.overwrite:
                parser.error('输出文件已存在。换一个路径或使用 --overwrite。')
        reader = InvoiceReader(single_invoice=args.single_invoice)
        rows = []
        def consume(row):
            rows.append(row)
            if sys.stdout:
                print(f'{Path(row.source).name} / {row.page}: {row.invoice_number or "—"} [{row.status}]',flush=True)
        if args.workers == 2 and len(files) > 1:
            from invoice_tool.pipeline import ParallelReader
            with ParallelReader() as pool:
                for kind,value in pool.read(files,single_invoice=args.single_invoice):
                    if kind == 'row':
                        consume(value)
        else:
            for path in files:
                for row in reader.read(path):
                    consume(row)
        export_excel(rows,output)
        if report:
            report.write_text(json.dumps([asdict(r) for r in rows],ensure_ascii=False,indent=2),encoding='utf-8')
        if sys.stdout:
            print(f'已导出 {len(rows)} 条：{output}')
        return 0
    if args.output or args.report or args.single_invoice or args.workers != 1:
        parser.error('--output/--report 需要同时指定 --input。')
    from invoice_tool.gui import create_app
    root,_ = create_app()
    root.mainloop()
    return 0


def entry():
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        return main()
    except Exception as exc:
        message = '程序无法完成操作：\n'+str(exc)
        if sys.stderr:
            print(message,file=sys.stderr)
        if not any(arg in sys.argv for arg in ('--input','--smoke-test')) and sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'发票号码提取',0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(entry())
