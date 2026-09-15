"""Finite, local-only Excel/PDF parsing; never writes live tender reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import zipfile


@dataclass(frozen=True)
class ParseLimits:
    seconds: float = 300
    files: int = 100
    file_bytes: int = 128 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    sheets: int = 100
    sheet_rows: int = 50_000
    columns: int = 256
    cells: int = 2_000_000
    pdf_pages: int = 250
    page_pixels: int = 25_000_000
    rows: int = 50_000


RESULT_BYTES = 16 * 1024 * 1024


class EstimateParseRejected(ValueError):
    pass


def snapshot(paths, limits=None):
    limits = limits or ParseLimits()
    paths = list(dict.fromkeys(Path(p).absolute() for p in paths))
    if len(paths) > limits.files * 3:
        raise EstimateParseRejected('Слишком много исходных документов для одного разбора.')
    result, total = [], 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise EstimateParseRejected('Исходный документ недоступен: ' + path.name)
        size = path.stat().st_size
        total += size
        if size > limits.file_bytes or total > limits.total_bytes:
            raise EstimateParseRejected('Превышен допустимый размер документов для разбора.')
        digest, read = hashlib.sha256(), 0
        with path.open('rb') as source:
            for block in iter(lambda: source.read(64 * 1024), b''):
                read += len(block)
                if read > size:
                    raise EstimateParseRejected('Документ изменился при чтении: ' + path.name)
                digest.update(block)
        if read != size:
            raise EstimateParseRejected('Документ изменился при чтении: ' + path.name)
        result.append({'path': str(path), 'size': size, 'sha256': digest.hexdigest()})
    return result


def validate_snapshot(expected, limits=None):
    if snapshot([row['path'] for row in expected], limits) != expected:
        raise EstimateParseRejected('Документы изменились во время разбора. Предыдущий отчёт сохранён; повторите разбор.')


def read_excel_bounded(path, limits):
    import pandas as pd
    if Path(path).suffix.lower() in {'.xlsx', '.xlsm'}:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (len(infos) > 100_000 or sum(i.file_size for i in infos) > 256 * 1024 * 1024
                    or any(i.file_size > 64 * 1024 * 1024 or i.file_size > max(1, i.compress_size) * 500 for i in infos)):
                raise EstimateParseRejected('Excel превышает ограничения внутреннего размера.')
    frames, cells = {}, 0
    with pd.ExcelFile(path) as book:
        if len(book.sheet_names) > limits.sheets:
            raise EstimateParseRejected('В Excel слишком много листов.')
        for name in book.sheet_names:
            frame = book.parse(name, header=None, nrows=limits.sheet_rows + 1)
            cells += frame.size
            if len(frame) > limits.sheet_rows or len(frame.columns) > limits.columns or cells > limits.cells:
                raise EstimateParseRejected('Excel превышает лимит строк, колонок или ячеек; неполный результат не сохраняется.')
            frames[str(name)] = frame
    return frames


def inspect_pdf(path, limits):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    with fitz.open(path) as document:
        if document.needs_pass:
            raise EstimateParseRejected('PDF защищён паролем.')
        if len(document) > limits.pdf_pages:
            raise EstimateParseRejected('В PDF слишком много страниц для одного разбора.')
        if not len(document):
            raise EstimateParseRejected('PDF не содержит страниц.')
        for page in document:
            if page.rect.width * page.rect.height * (300 / 72) ** 2 > limits.page_pixels:
                raise EstimateParseRejected('Размер страницы PDF превышает предел распознавания.')


def parse_files(excel_files, pdf_files, tender, limits):
    from autobot import main
    if len(excel_files) + len(pdf_files) > limits.files:
        raise EstimateParseRejected('Слишком много сметных файлов для одного разбора.')
    before = snapshot([*excel_files, *pdf_files], limits)
    rows, totals, documents = [], {}, []
    for kind, files in (('excel', excel_files), ('pdf', pdf_files)):
        for raw_path in files:
            path = Path(raw_path)
            try:
                if kind == 'excel':
                    part = main.extract_rows_from_excel(path, tender, strict=True, limits=limits)
                    skipped = main.should_skip_object_estimate_file(path)
                else:
                    inspect_pdf(path, limits)
                    part = main.extract_rows_from_pdf(path, tender, strict=True)
                    skipped = False
                    total = main.extract_pdf_estimate_total(path)
                    if total is not None:
                        totals[str(path)] = total
                if kind == 'pdf' and not part:
                    raise EstimateParseRejected('В выбранной PDF-смете не удалось распознать позиции.')
                rows.extend(part)
                if len(rows) > limits.rows:
                    raise EstimateParseRejected('Распознано слишком много строк; результат требует отдельного разбора.')
                documents.append({'source_file': str(path), 'kind': kind, 'rows': len(part),
                                  'missing_quantity_rows': sum(row.get('qty') is None for row in part),
                                  'missing_unit_rows': sum(not str(row.get('unit') or '').strip() for row in part),
                                  'missing_amount_rows': sum(row.get('price_from_estimate_rub') is None for row in part),
                                  'state': 'skipped' if skipped else 'fallback' if any(
                                      row.get('extract_source') == 'PDF fallback' for row in part) else 'parsed' if part else 'unrecognized'})
            except EstimateParseRejected as error:
                raise EstimateParseRejected(path.name + ': ' + str(error)) from None
            except Exception as error:
                raise EstimateParseRejected(path.name + ': не удалось прочитать файл (' + type(error).__name__ + ').') from None
    validate_snapshot(before, limits)
    if not rows:
        raise EstimateParseRejected('В документах не найдены позиции сметы. Предыдущий отчёт сохранён.')
    return {'rows': rows, 'official_totals': totals, 'documents': documents, 'sources': before}


def run_parser(excel_files, pdf_files, tender, *, limits=None):
    from autobot.archive_extraction import _stop_process
    limits = limits or ParseLimits()
    # File hashing and parsers run in the same bounded child. The publisher
    # additionally verifies its full original/extracted source snapshot.
    with tempfile.TemporaryDirectory(prefix='autobot-estimate-') as name:
        folder = Path(name)
        request = {'excel': [str(Path(p).absolute()) for p in excel_files],
                   'pdf': [str(Path(p).absolute()) for p in pdf_files],
                   'tender': asdict(tender), 'limits': asdict(limits)}
        (folder / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith(
            ('PMBI_', 'OPENAI_', 'TELEGRAM_', 'CLERK_', 'SMTP_', 'RESEND_', 'MARKET_'))}
        env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(folder)], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            try:
                process.wait(timeout=limits.seconds)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                raise EstimateParseRejected('Время разбора сметы истекло. Предыдущий отчёт сохранён; повторите разбор или проверьте документы.') from None
            output = folder / 'result.json'
            if process.returncode or not output.is_file() or output.stat().st_size > RESULT_BYTES:
                raise EstimateParseRejected('Обработчик сметы прерван или превысил лимит. Предыдущий отчёт сохранён.')
            result = json.loads(output.read_text(encoding='utf-8'))
            if result.get('error'):
                raise EstimateParseRejected(result['error'])
            if not isinstance(result.get('rows'), list) or not result['rows'] or len(result['rows']) > limits.rows:
                raise EstimateParseRejected('Обработчик не вернул проверяемые строки сметы.')
            return result
        finally:
            if process.poll() is None:
                _stop_process(process)


def worker(folder):
    request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
    limits = ParseLimits(**request['limits'])
    # Parent termination must not leave an unbounded OCR process behind.
    def watchdog():
        time.sleep(limits.seconds + 5)
        if os.name != 'nt':
            import signal
            os.killpg(os.getpgrp(), signal.SIGKILL)
        else:
            try:
                subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except Exception:
                pass
        os._exit(124)
    threading.Thread(target=watchdog, daemon=True).start()
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (int(limits.seconds) + 5,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # The parser has no integration work. main's normal CLI dotenv loading
    # must not restore credentials removed from this child's environment.
    try:
        import dotenv
        dotenv.load_dotenv = lambda *args, **kwargs: False
    except ImportError:
        pass
    from autobot.main import Tender
    try:
        result = parse_files(request['excel'], request['pdf'], Tender(**request['tender']), limits)
        content = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(content.encode('utf-8')) > RESULT_BYTES:
            raise EstimateParseRejected('Результат разбора превышает допустимый размер; прежний отчёт сохранён.')
    except Exception as error:
        message = str(error) if isinstance(error, EstimateParseRejected) else 'Не удалось завершить разбор сметы (' + type(error).__name__ + ').'
        content = json.dumps({'error': message}, ensure_ascii=False)
    (folder / 'result.json').write_text(content, encoding='utf-8')


if __name__ == '__main__':
    # Import this module under its package name once so exception classes used
    # by strict parsers and the worker are identical.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from autobot.estimate_parse_worker import worker as execute
    execute(Path(sys.argv[1]))
