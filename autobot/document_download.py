"""Stream one source document in a disposable process with a hard deadline."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.message import Message

MAX_BYTES = 128 * 1024 * 1024
TIMEOUT = 90


class DownloadRejected(ValueError):
    pass


def inspect_document(path, headers, expected_extension=''):
    disposition = Message()
    disposition['Content-Disposition'] = headers.get('content-disposition', '')
    expected_extension = Path(disposition.get_filename() or '').suffix.lower() or expected_extension
    with Path(path).open('rb') as stream:
        head = stream.read(4096)
    if not head:
        raise DownloadRejected('Источник вернул пустой файл.')
    content_type = str(headers.get('content-type', '')).lower()
    text = head.decode('utf-8-sig', errors='ignore').lstrip().lower()
    if 'text/html' in content_type or 'application/xhtml' in content_type or any(
        marker in text[:2048] for marker in ('<!doctype html', '<html', '<head', '<body')):
        raise DownloadRejected('Источник вернул страницу сайта вместо документа. Возможно, нужна проверка доступа в ЕИС.')
    if head.startswith(b'PK') or expected_extension in {'.zip', '.xlsx', '.xlsm', '.docx'}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if len(names) > 100_000:
                    raise DownloadRejected('В документе слишком много элементов.')
                if 'xl/workbook.xml' in names:
                    return '.xlsm' if 'xl/vbaProject.bin' in names or expected_extension == '.xlsm' else '.xlsx'
                if 'word/document.xml' in names:
                    return '.docx'
                return '.zip'
        except zipfile.BadZipFile:
            raise DownloadRejected('ZIP/Office-файл повреждён или загрузился не полностью.') from None
    if head.startswith(b'Rar!\x1a\x07'):
        return '.rar'
    if expected_extension == '.rar':
        raise DownloadRejected('Вместо RAR получен файл другого формата.')
    if head.startswith(b'%PDF-'):
        return '.pdf'
    if head.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '.7z'
    if head.startswith(b'{\\rtf'):
        return '.rtf'
    return ''


def stream_response(response, path, *, expected_extension='', limit=MAX_BYTES):
    response.raise_for_status()
    if response.status_code != 200:
        raise DownloadRejected('Источник не вернул целый документ (HTTP ' + str(response.status_code) + ').')
    headers = {key: response.headers.get(key, '') for key in
               ('content-type', 'content-disposition', 'content-length', 'content-encoding')}
    try:
        declared = int(headers['content-length']) if headers['content-length'] else None
    except ValueError:
        raise DownloadRejected('Источник передал некорректный размер файла.') from None
    if declared is not None and (declared < 0 or declared > limit):
        raise DownloadRejected('Размер документа превышает лимит 128 МиБ.')
    size, digest = 0, hashlib.sha256()
    with Path(path).open('xb') as target:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                raise DownloadRejected('Размер документа превышает лимит 128 МиБ.')
            target.write(chunk)
            digest.update(chunk)
    if declared is not None and headers['content-encoding'].lower() in {'', 'identity'} and declared != size:
        raise DownloadRejected('Документ загрузился не полностью: размер не совпадает с ответом источника.')
    extension = inspect_document(path, headers, expected_extension)
    return {'headers': headers, 'size_bytes': size, 'sha256': digest.hexdigest(), 'extension': extension}


def fetch_document(url, destination, *, headers=None, cookies=None, verify=True, timeout=None):
    from autobot.archive_extraction import _stop_process

    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise DownloadRejected('Путь документа недоступен.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='autobot-download-') as folder:
        scratch = Path(folder).resolve()
        assert scratch.parent == Path(tempfile.gettempdir()).resolve()
        request = {'url': url, 'headers': headers or {}, 'cookies': cookies or {}, 'verify': verify,
                   'extension': destination.suffix.lower()}
        request_path = scratch / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        os.chmod(request_path, 0o600)
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith(
            ('PMBI_', 'OPENAI_', 'TELEGRAM_', 'CLERK_', 'SMTP_', 'RESEND_', 'MARKET_'))}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(scratch)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            process.wait(timeout=TIMEOUT if timeout is None else min(TIMEOUT, max(.001, timeout)))
            metadata = scratch / 'result.json'
            if process.returncode or not metadata.is_file() or metadata.stat().st_size > 64 * 1024:
                _stop_process(process)
                raise DownloadRejected('Загрузка документа прервана. Повторите попытку.')
            result = json.loads(metadata.read_text(encoding='utf-8'))
            if result.get('error'):
                raise DownloadRejected(result['error'])
            payload = scratch / 'payload.bin'
            if not payload.is_file() or payload.stat().st_size != result['size_bytes'] or payload.stat().st_size > MAX_BYTES:
                raise DownloadRejected('Не удалось проверить загруженный документ.')
            descriptor, name = tempfile.mkstemp(prefix='.autobot-incoming-', suffix='.part', dir=destination.parent)
            os.close(descriptor)
            temporary = Path(name)
            try:
                shutil.copyfile(payload, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return result
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise DownloadRejected('Время загрузки документа истекло. Повторите попытку позже.') from None
        finally:
            if process.poll() is None:
                _stop_process(process)


def worker(folder):
    import threading
    watchdog = threading.Timer(TIMEOUT + 5, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (80, 80))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    import requests
    request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
    try:
        with requests.get(request['url'], headers=request['headers'], cookies=request['cookies'],
                          verify=request['verify'], stream=True, timeout=(15, 20)) as response:
            result = stream_response(response, folder / 'payload.bin', expected_extension=request['extension'])
    except DownloadRejected as error:
        result = {'error': str(error)}
    except requests.RequestException:
        result = {'error': 'Источник недоступен или оборвал загрузку документа.'}
    except Exception:
        result = {'error': 'Не удалось сохранить документ. Повторите загрузку.'}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    worker(Path(sys.argv[1]))
