"""Bounded disposable reader for document previews and archive members."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

INPUT_LIMIT = 128 * 1024 * 1024
MEMBER_LIMIT = 32 * 1024 * 1024
RESULT_LIMIT = 8 * 1024 * 1024
TIMEOUT = 25


class PreviewRejected(ValueError):
    pass


def run_reader(operation, *, path=None, data=None, filename='', member_token='', chain=None):
    from autobot.atomic_output import output_lock
    from autobot.paths import DATA_DIR
    from autobot.archive_extraction import _stop_process

    if path is not None:
        path = Path(path).absolute()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > INPUT_LIMIT:
            raise PreviewRejected('Исходный файл превышает лимит просмотра или недоступен. Скачайте оригинал.')
    if data is not None and len(data) > MEMBER_LIMIT:
        raise PreviewRejected('Файл превышает лимит просмотра 32 МиБ. Скачайте исходный архив.')
    try:
        with output_lock(DATA_DIR / 'document-preview', timeout=.1):
            with tempfile.TemporaryDirectory(prefix='autobot-preview-') as folder:
                scratch = Path(folder).resolve()
                assert scratch.parent == Path(tempfile.gettempdir()).resolve()
                if data is not None:
                    path = scratch / 'input.bin'
                    path.write_bytes(data)
                request = {'operation': operation, 'path': str(path), 'filename': filename,
                           'member_token': member_token, 'chain': chain or []}
                (scratch / 'request.json').write_text(json.dumps(request), encoding='utf-8')
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith(
                    ('PMBI_', 'OPENAI_', 'TELEGRAM_', 'CLERK_', 'SMTP_', 'RESEND_', 'MARKET_'))}
                env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(scratch)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                    start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                try:
                    proc.wait(timeout=TIMEOUT)
                    result_path = scratch / 'result.json'
                    if proc.returncode or not result_path.is_file() or result_path.stat().st_size > RESULT_LIMIT:
                        _stop_process(proc)
                        raise PreviewRejected('Обработчик просмотра прерван. Скачайте исходный файл и откройте его на компьютере.')
                    result = json.loads(result_path.read_text(encoding='utf-8'))
                    if result.get('error'):
                        if result.get('missing'):
                            raise FileNotFoundError('Файл внутри архива не найден')
                        raise PreviewRejected(result['error'])
                    if result.pop('has_data', False):
                        output = scratch / 'member.bin'
                        if not output.is_file() or output.stat().st_size > MEMBER_LIMIT:
                            raise PreviewRejected('Размер файла превышает лимит просмотра. Скачайте исходный архив.')
                        result['data'] = output.read_bytes()
                    return result
                except subprocess.TimeoutExpired:
                    _stop_process(proc)
                    raise PreviewRejected('Время предпросмотра истекло. Скачайте оригинал или повторите попытку.') from None
                finally:
                    if proc.poll() is None:
                        _stop_process(proc)
    except TimeoutError:
        raise PreviewRejected('Сейчас обрабатывается другой документ. Повторите просмотр через несколько секунд.') from None


def worker(folder):
    # Keep imports after limits; NumPy uses one thread to bound address space.
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MEMBER_LIMIT, MEMBER_LIMIT))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from autobot import source_documents as documents
    from autobot.archive_extraction import ArchiveRejected
    request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
    path = Path(request['path'])
    try:
        if request['operation'] == 'member':
            result = documents._read_archive_member(path, request['member_token'])
            (folder / 'member.bin').write_bytes(result.pop('data'))
            result['has_data'] = True
        elif request['operation'] == 'bytes':
            result = documents._build_source_bytes_preview(path.read_bytes(), request['filename'], request['chain'])
        elif request['operation'] == 'file':
            result = documents._build_source_file_preview(path)
        else:
            raise ValueError('Unknown reader operation')
    except FileNotFoundError:
        result = {'error': 'Файл внутри архива не найден', 'missing': True}
    except (ArchiveRejected, documents.PreviewRejected) as error:
        result = {'error': str(error)}
    except Exception:
        result = {'error': 'Не удалось прочитать документ. Файл сохранён; скачайте оригинал для проверки.'}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    worker(Path(sys.argv[1]))
