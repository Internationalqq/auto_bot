"""Bounded, transactional extraction of downloaded documents; no extractall()."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autobot.split_zip import SplitZipRejected, open_zip, part_number, volume_paths

ARCHIVE_EXTENSIONS = {'.zip', '.rar', '.7z'}
POLICY_VERSION = 2


@dataclass(frozen=True)
class Limits:
    depth: int = 5
    archives: int = 64
    members: int = 2000
    member_bytes: int = 128 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    input_bytes: int = 128 * 1024 * 1024
    ratio: int = 500
    seconds: float = 120


class ArchiveRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def safe_member_path(name: str) -> PurePosixPath:
    """Use the same portable names on Linux and Windows; never repair a path."""
    normalized = unicodedata.normalize('NFC', name.replace('\\', '/'))
    parts = normalized.rstrip('/').split('/')
    if (not name or len(normalized) > 2048 or normalized.startswith('/')
            or any(ord(c) < 32 for c in normalized)
            or any(c in normalized for c in ':<>"|?*')
            or any(not p or p in {'.', '..'} or len(p) > 240 or p.endswith((' ', '.'))
                   or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p)
                   for p in parts)):
        raise ArchiveRejected('unsafe_path', 'В архиве есть недопустимый путь файла')
    return PurePosixPath(*parts)


def validate_member(info, limits: Limits) -> tuple[PurePosixPath, bool, int]:
    if isinstance(info, zipfile.ZipInfo):
        safe_member_path(info.orig_filename)
    path = safe_member_path(str(info.filename))
    directory = info.is_dir()
    if isinstance(info, zipfile.ZipInfo):
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ArchiveRejected('link', 'Ссылки и специальные файлы в архиве не поддерживаются')
        if info.flag_bits & 1:
            raise ArchiveRejected('encrypted', 'Архив защищён паролем')
        if info.volume:
            raise ArchiveRejected('multipart', 'Для этого ZIP нужны остальные тома; откройте полный комплект исходников.')
    else:
        if (info.is_symlink() or getattr(info, 'file_redir', None) is not None
                or stat.S_IFMT(int(getattr(info, 'mode', 0) or 0)) not in {0, stat.S_IFREG, stat.S_IFDIR}):
            raise ArchiveRejected('link', 'Ссылки в архиве не поддерживаются')
        if info.needs_password():
            raise ArchiveRejected('encrypted', 'Архив защищён паролем')
        if getattr(info, 'volume', 0):
            raise ArchiveRejected('multipart', 'Многотомный архив нужно открыть вручную')
    size = int(info.file_size or 0)
    compressed = int(info.compress_size or 0)
    if size < 0 or compressed < 0 or size > limits.member_bytes:
        raise ArchiveRejected('member_size', 'Файл в архиве превышает допустимый размер')
    if directory and size:
        raise ArchiveRejected('invalid_archive', 'Некорректный размер папки в архиве')
    if size > max(1, compressed) * limits.ratio:
        raise ArchiveRejected('ratio', 'Слишком высокая степень сжатия файла в архиве')
    return path, directory, size


def open_archive(source, name: str, *, budget=None, volumes=None):
    extension = Path(name).suffix.lower()
    if extension == '.zip':
        return open_zip(source, budget or Budget(Limits()), volumes=volumes)
    if extension == '.rar':
        import rarfile
        # rarfile selects the installed unrar/unar/7z reader. Only its stream
        # API is used: the external tool never chooses filesystem destinations.
        if os.name == 'nt':
            for program in (Path('C:/Program Files/7-Zip/7z.exe'),
                            Path('C:/Program Files (x86)/7-Zip/7z.exe'),
                            Path.home() / 'AppData/Local/Programs/7-Zip/7z.exe'):
                if program.is_file():
                    rarfile.SEVENZIP_TOOL = str(program)
                    break
            for program in (Path('C:/Program Files/WinRAR/UnRAR.exe'),
                            Path('C:/Program Files (x86)/WinRAR/UnRAR.exe')):
                if program.is_file():
                    rarfile.UNRAR_TOOL = str(program)
                    break
        return rarfile.RarFile(source)
    raise ArchiveRejected('unsupported', 'Формат 7z пока не поддерживается; скачайте архив и откройте вручную')


class Budget:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.deadline = time.monotonic() + limits.seconds
        self.archives = self.members = self.bytes = 0

    def check_time(self):
        if time.monotonic() >= self.deadline:
            raise ArchiveRejected('timeout', 'Истекло время распаковки документов')

    def reserve(self, infos):
        self.check_time()
        self.archives += 1
        if self.archives > self.limits.archives:
            raise ArchiveRejected('archive_count', 'Превышено количество вложенных архивов')
        self.members += len(infos)
        if self.members > self.limits.members:
            raise ArchiveRejected('member_count', 'Превышено количество файлов в архивах')
        entries = []
        kinds = {}
        for info in infos:
            path, directory, size = validate_member(info, self.limits)
            key = str(path).casefold()
            if key in kinds:
                raise ArchiveRejected('duplicate_path', 'В архиве повторяются имена файлов или папок')
            kinds[key] = directory
            entries.append((info, path, directory, size))
            self.bytes += size
            if self.bytes > self.limits.total_bytes:
                raise ArchiveRejected('total_size', 'Превышен общий размер распакованных документов')
        for key in kinds:
            if any(kinds.get(str(parent)) is False for parent in PurePosixPath(key).parents):
                raise ArchiveRejected('duplicate_path', 'Файл и папка в архиве используют один путь')
        return entries


def read_member_stream(source, destination, size: int, budget: Budget) -> str:
    digest = hashlib.sha256()
    read = 0
    while True:
        budget.check_time()
        block = source.read(min(64 * 1024, size - read + 1))
        if not block:
            break
        read += len(block)
        if read > size or read > budget.limits.member_bytes:
            raise ArchiveRejected('member_size', 'Фактический размер файла больше объявленного в архиве')
        destination.write(block)
        digest.update(block)
    if read != size:
        raise ArchiveRejected('invalid_archive', 'Файл в архиве прочитан не полностью')
    return digest.hexdigest()


def _hash_file(path: Path, max_bytes: int, budget: Budget) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ArchiveRejected('input_size', 'Исходный файл недоступен или превышает допустимый размер')
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as source:
        while block := source.read(64 * 1024):
            budget.check_time()
            count += len(block)
            if count > max_bytes:
                raise ArchiveRejected('input_size', 'Исходный файл изменился при чтении')
            digest.update(block)
    return digest.hexdigest()


def _unpack(archive_path: Path, destination: Path, chain: list[str], depth: int,
            budget: Budget, files: list[dict], root: Path, volumes=None):
    if depth > budget.limits.depth:
        raise ArchiveRejected('depth', 'Превышена допустимая глубина вложенных архивов')
    budget.check_time()
    with open_archive(archive_path, archive_path.name, budget=budget, volumes=volumes) as archive:
        entries = budget.reserve(archive.infolist())
        for info, relative, directory, size in entries:
            target = destination / str(relative)
            if os.name == 'nt' and len(str(target.absolute())) >= 248:
                raise ArchiveRejected('path_length', 'Путь распакованного файла слишком длинный для Windows; откройте архив вручную')
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('xb') as output:
                sha256 = read_member_stream(source, output, size, budget)
            member_chain = [*chain, str(info.filename)]
            files.append({'path': target.relative_to(root).as_posix(), 'size': size,
                          'sha256': sha256, 'chain': member_chain})
            if target.suffix.lower() in ARCHIVE_EXTENSIONS:
                # A separate namespace prevents collision with a real folder
                # having the same name as a nested archive.
                nested = root / 'nested' / (str(len(files)) + '-' + target.stem[:60])
                _unpack(target, nested, member_chain, depth + 1, budget, files, root)


def _assert_plain_directory(path: Path):
    """Reject pre-existing links, including links in any ancestor."""
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ArchiveRejected('unsafe_cache', 'Каталог распаковки содержит ссылку')
        try:
            if getattr(part.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ArchiveRejected('unsafe_cache', 'Каталог распаковки содержит точку перенаправления Windows')
        except FileNotFoundError:
            pass


def _read_cache(cache: Path, source_hash: str, limits: Limits, budget: Budget) -> dict | None:
    manifest = cache / 'manifest.json'
    if not manifest.exists():
        return None
    _assert_plain_directory(cache)
    if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise ArchiveRejected('unsafe_cache', 'Некорректный журнал распаковки')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if (data.get('source_sha256') != source_hash or data.get('policy') != POLICY_VERSION
            or data.get('limits') != asdict(limits)):
        raise ArchiveRejected('unsafe_cache', 'Журнал распаковки не соответствует исходному архиву')
    counts = data['counts']
    if any(type(counts.get(k)) is not int or counts[k] < 0 for k in ('archives', 'members', 'bytes')):
        raise ArchiveRejected('unsafe_cache', 'Некорректный журнал распаковки')
    if (budget.archives + counts['archives'] > limits.archives
            or budget.members + counts['members'] > limits.members
            or budget.bytes + counts['bytes'] > limits.total_bytes):
        raise ArchiveRejected('total_size', 'Общий объём документов превышает ограничения распаковки')
    files = data['files']
    if len(files) > counts['members'] or sum(f['size'] for f in files) != counts['bytes']:
        raise ArchiveRejected('unsafe_cache', 'Некорректный список распакованных файлов')
    for entry in files:
        candidate = cache / str(safe_member_path(entry['path']))
        _assert_plain_directory(candidate)
        if (not candidate.is_file() or candidate.stat().st_size != entry['size']
                or _hash_file(candidate, limits.member_bytes, budget) != entry['sha256']):
            raise ArchiveRejected('unsafe_cache', 'Распакованный файл изменён; исходный архив сохранён')
    budget.archives += counts['archives']
    budget.members += counts['members']
    budget.bytes += counts['bytes']
    return data


def _error(exc: Exception) -> dict:
    if isinstance(exc, (ArchiveRejected, SplitZipRejected)):
        return {'code': exc.code, 'message': str(exc)}
    kind = type(exc).__name__
    messages = {'BadZipFile': 'Архив ZIP повреждён', 'BadRarFile': 'Архив RAR повреждён',
                'RarCannotExec': 'На сервере недоступен обработчик RAR',
                'PasswordRequired': 'Архив защищён паролем',
                'NeedFirstVolume': 'Нужна первая часть многотомного архива'}
    return {'code': 'read_error', 'message': messages.get(kind, f'Не удалось распаковать архив ({kind})')}


def _source_identity(paths, budget):
    sizes = []
    for path in paths:
        _assert_plain_directory(path)
        if not path.is_file():
            raise ArchiveRejected('source_changed', 'Один из исходных томов недоступен; повторите скачивание')
        sizes.append(path.stat().st_size)
    if sum(sizes) > budget.limits.input_bytes:
        raise ArchiveRejected('input_size', 'Общий размер исходного архива превышает допустимый размер')
    rows = [{'name': path.name, 'size': size, 'sha256': _hash_file(path, budget.limits.input_bytes, budget)}
            for path, size in zip(paths, sizes)]
    identity = rows[0]['sha256'] if len(rows) == 1 else hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return identity, rows


def extract_in_worker(archives: list[Path], extracted_base: Path, scratch: Path, limits: Limits,
                      source_files=None) -> dict:
    budget = Budget(limits)
    results = []
    used_volumes = set()
    if len(archives) > limits.archives:
        raise ArchiveRejected('archive_count', 'Превышено количество исходных архивов')
    for source in archives:
        row = {'archive': source.name, 'source_path': str(source), 'status': 'failed', 'files': []}
        results.append(row)
        try:
            _assert_plain_directory(source)
            volumes = volume_paths(source, source_files)
            used_volumes.update(p.absolute() for p in volumes)
            sha256, source_rows = _source_identity(volumes, budget)
            # The original stem remains the owner of its derived cache, so the
            # existing source-version trash/restore workflow still works.
            owner = extracted_base / str(safe_member_path(source.parent.name)) / str(safe_member_path(source.stem))
            _assert_plain_directory(owner)
            key = hashlib.sha256(json.dumps([POLICY_VERSION, source.name, sha256, asdict(limits)], sort_keys=True).encode()).hexdigest()
            cache = owner / ('verified-' + key[:24])
            _assert_plain_directory(cache)
            try:
                data = _read_cache(cache, sha256, limits, budget)
                if data is None and cache.exists():
                    raise ArchiveRejected('unsafe_cache', 'Нет журнала завершённой распаковки')
            except (ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, ArchiveRejected) and exc.code != 'unsafe_cache':
                    raise
                # Preserve changed derived files for review, then reconstruct
                # this exact version from the unchanged source archive.
                _assert_plain_directory(cache)
                quarantine = cache.with_name('invalid-' + key[:24] + '-' + uuid.uuid4().hex[:8])
                if not all(p.resolve().is_relative_to(extracted_base.resolve()) for p in (cache, quarantine)):
                    raise ArchiveRejected('unsafe_cache', 'Каталог распаковки вне рабочей папки')
                os.rename(cache, quarantine)
                row['cache_rebuilt'] = True
                data = None
            if data is None:
                if cache.exists():
                    raise ArchiveRejected('unsafe_cache', 'Незавершённый каталог распаковки; исходный архив сохранён')
                work = scratch / str(len(results))
                work.mkdir()
                counts_before = (budget.archives, budget.members, budget.bytes)
                files: list[dict] = []
                _unpack(source, work / 'files', [source.name], 1, budget, files, work, volumes)
                if _source_identity(volumes, budget)[0] != sha256:
                    raise ArchiveRejected('source_changed', 'Архив изменился во время распаковки; повторите разбор')
                if os.name == 'nt' and any(len(str(cache / f['path'])) >= 248 for f in files):
                    raise ArchiveRejected('path_length', 'Путь распакованного файла слишком длинный для Windows; откройте архив вручную')
                data = {'policy': POLICY_VERSION, 'source_sha256': sha256, 'source_files': source_rows, 'files': files,
                        'limits': asdict(limits), 'counts': dict(zip(('archives', 'members', 'bytes'),
                            (budget.archives - counts_before[0], budget.members - counts_before[1], budget.bytes - counts_before[2])))}
                (work / 'manifest.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                owner.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(work, cache)
                except OSError:
                    # Concurrent identical parsing can publish first. Only use
                    # its cache after the same complete validation.
                    duplicate_budget = Budget(limits)
                    data = _read_cache(cache, sha256, limits, duplicate_budget)
                    if data is None:
                        raise
            if _source_identity(volumes, budget)[0] != sha256:
                raise ArchiveRejected('source_changed', 'Исходные тома изменились во время проверки; повторите разбор')
            row.update(status='complete', source_sha256=sha256, source_files=source_rows,
                       files=[{**f, 'path': str(cache / f['path'])} for f in data['files']])
        except Exception as exc:
            row.update(_error(exc))
    results.extend(_orphan_parts(source_files, used_volumes))
    return {'policy': POLICY_VERSION, 'limits': asdict(limits), 'archives': results,
            'failed_count': sum(r['status'] != 'complete' for r in results)}


def _orphan_parts(source_files, used_volumes=()):
    return [{'archive': Path(p).name, 'status': 'failed', 'files': [], 'code': 'multipart',
             'message': 'Для тома ZIP не найден соответствующий завершающий .zip в текущем комплекте. Повторите скачивание всех документов.'}
            for p in (source_files or []) if part_number(p) and Path(p).absolute() not in used_volumes]


def _stop_process(proc):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()
    proc.wait(timeout=10)


def extract_documents(archives: list[Path], extracted_base: Path, *, limits: Limits | None = None,
                      source_files=None) -> dict:
    """Run the whole tender budget in a child, including external RAR readers."""
    limits = limits or Limits()
    paths = list(dict.fromkeys(Path(p).absolute() for p in archives))
    base = Path(extracted_base).absolute()
    _assert_plain_directory(base)
    base.mkdir(parents=True, exist_ok=True)
    if not paths:
        orphans = _orphan_parts(source_files)
        return {'policy': POLICY_VERSION, 'limits': asdict(limits), 'archives': orphans, 'failed_count': len(orphans)}
    if len(paths) > limits.archives:
        return {'policy': POLICY_VERSION, 'limits': asdict(limits), 'archives': [], 'failed_count': len(paths),
                'code': 'archive_count', 'message': 'Превышено количество исходных архивов'}
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=base) as name:
        scratch = Path(name)
        request = {'archives': [str(p) for p in paths], 'base': str(base),
                   'scratch': str(scratch), 'limits': asdict(limits),
                   'source_files': [str(Path(p).absolute()) for p in source_files] if source_files is not None else None}
        (scratch / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        command = [sys.executable, str(Path(__file__).resolve()), str(scratch / 'request.json')]
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            proc.wait(timeout=limits.seconds + 2)
            result_path = scratch / 'result.json'
            if proc.returncode != 0 or not result_path.is_file() or result_path.stat().st_size > 4 * 1024 * 1024:
                raise ArchiveRejected('worker_failed', 'Обработчик архивов завершился с ошибкой; исходники сохранены')
            return json.loads(result_path.read_text(encoding='utf-8'))
        except subprocess.TimeoutExpired:
            _stop_process(proc)
            error = ArchiveRejected('timeout', 'Истекло время распаковки документов; исходники сохранены')
        except Exception as exc:
            if proc.poll() is None:
                _stop_process(proc)
            error = exc
        return {'policy': POLICY_VERSION, 'limits': asdict(limits), 'failed_count': len(paths),
                'archives': [{'archive': p.name, 'status': 'failed', 'files': [], **_error(error)} for p in paths]}


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if os.name != 'nt':
        import resource
        # Applies to the reader and its subprocesses. No pandas/browser imports.
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    result = extract_in_worker([Path(p) for p in request['archives']], Path(request['base']),
                               Path(request['scratch']), Limits(**request['limits']), request.get('source_files'))
    (Path(request['scratch']) / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
