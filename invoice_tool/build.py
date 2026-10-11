"""Build a self-contained Windows folder and ZIP with offline OCR models."""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT/'invoice_tool'


def workspace_path(relative):
    path = (ROOT/relative).resolve()
    if path == ROOT or not path.is_relative_to(ROOT):
        raise ValueError('Build output must stay inside this workspace.')
    return path


def main():
    if sys.platform != 'win32':
        raise SystemExit('Please build on Windows x64.')
    dist = workspace_path('dist')
    output = workspace_path('dist/InvoiceReader')
    work = workspace_path('build/invoice-reader')
    release = workspace_path('releases')
    # PyInstaller replaces only its named output. Validate it before --noconfirm.
    output.parent.mkdir(exist_ok=True)
    work.mkdir(parents=True,exist_ok=True)
    release.mkdir(exist_ok=True)
    local_temp = workspace_path('.tools/invoice-temp')
    local_temp.mkdir(parents=True,exist_ok=True)
    environment = os.environ.copy()
    environment.update(TEMP=str(local_temp),TMP=str(local_temp))
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed',
                    '--name','InvoiceReader','--paths',str(ROOT),'--distpath',str(dist),
                    '--workpath',str(work),'--specpath',str(work),
                    '--collect-all','rapidocr_onnxruntime','--collect-all','tkinterdnd2',
                    str(SOURCE/'launcher.py')],cwd=ROOT,env=environment,check=True)
    matching_source = output/'source'/'invoice_tool'
    matching_source.mkdir(parents=True,exist_ok=True)
    for path in SOURCE.rglob('*'):
        if path.is_file() and path.suffix in {'.py','.md','.txt'} and '__pycache__' not in path.parts:
            target = matching_source/path.relative_to(SOURCE)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
    notices = []
    for distribution in metadata.distributions():
        name = distribution.metadata['Name']
        if name.lower() == 'pip':
            continue
        notices.append(f'{name} {distribution.version}')
        for item in distribution.files or []:
            if '..' in Path(str(item)).parts:
                continue
            if not any('license' in part.lower() or part.lower().startswith(('copying','notice'))
                       for part in Path(str(item)).parts):
                continue
            origin = Path(distribution.locate_file(item))
            if origin.is_file():
                target = output/'licenses'/name/Path(str(item))
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(origin,target)
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license,output/'licenses'/'Python-LICENSE.txt')
    (output/'THIRD_PARTY_NOTICES.txt').write_text(
        '\n'.join(sorted(notices))+'\n\nLicense texts are under licenses/.\n'
        'PyMuPDF: AGPL / commercial dual licensing. https://pymupdf.readthedocs.io/\n'
        'RapidOCR / bundled OCR models: https://github.com/RapidAI/RapidOCR\n',encoding='utf-8')
    shutil.copy2(SOURCE/'README.md',output/'使用说明.md')
    (output/'请先阅读.txt').write_text(
        '发票号码提取工具 1.7.0\n\n'
        '完整解压后双击 InvoiceReader.exe，保留旁边的 _internal 文件夹。\n'
        'RAR/ZIP 发票包请先解压，再将文件夹拖入窗口。\n'
        '在本机离线识别 PDF、图片和文字；核对号码后导出 Excel。\n'
        '使用说明：使用说明.md；源代码：source/invoice_tool；许可：licenses。\n'
        '已在本机验证，未在全新未安装 Python 的 Windows 电脑验证。\n',encoding='utf-8')
    manifest = {str(p.relative_to(output)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in output.rglob('*.onnx')}
    if len(manifest) < 3:
        raise RuntimeError('The package is missing offline OCR models.')
    (output/'OCR_MODEL_CHECKSUMS.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    archive = release/'发票号码提取工具-1.7.0-Windows.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                bundle.write(path,Path('发票号码提取工具')/path.relative_to(output))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='utf-8')
    print(str(archive))
    print('SHA-256:',checksum)


if __name__ == '__main__':
    main()
