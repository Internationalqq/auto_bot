"""Bounded split ZIP adapter using public ZIP fields; original volumes stay intact.

Only ZIP32 with its entire central directory in the last volume is supported.
The disposable copy has disk-relative offsets translated to ordinary ZIP offsets;
zipfile still performs local-header, overlap, decompression and CRC validation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import struct
import tempfile
import zipfile


class SplitZipRejected(ValueError):
    code = 'multipart'


def part_number(name):
    match = re.fullmatch(r'\.z(\d{2})', Path(name).suffix, re.I)
    return int(match[1]) if match and 1 <= int(match[1]) <= 99 else None


@dataclass(frozen=True)
class End:
    disk: int
    central_disk: int
    disk_count: int
    count: int
    size: int
    offset: int
    position: int


def read_end(source):
    """Read at most one ZIP comment plus EOCD, without loading member data."""
    with Path(source).open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or position + 22 > len(tail):
        return None
    fields = struct.unpack_from('<4s4H2LH', tail, position)
    if position + 22 + fields[-1] != len(tail):
        return None
    return End(*fields[1:-1], size - len(tail) + position)


def _candidates(final):
    # A current bundle excludes old volumes retained in the downloads folder.
    if final.parent.parent.name == 'downloads' and re.fullmatch(r'\d{8,25}', final.parent.name):
        from autobot.document_bundle import current_files
        return current_files(final.parent.parent, final.parent.name)
    found = []
    for path in final.parent.iterdir():
        if len(found) >= 2000:
            raise SplitZipRejected('Слишком много файлов для определения томов ZIP.')
        found.append(path)
    return found


def volume_paths(final, candidates=None):
    final = Path(final).absolute()
    if final.suffix.lower() != '.zip':
        return [final]
    end = read_end(final)
    if end is None or (end.disk == 0 and end.central_disk == 0):
        return [final]
    if (not 1 <= end.disk <= 99 or end.central_disk != end.disk
            or end.disk_count != end.count or end.count == 65535
            or end.size == 0xffffffff or end.offset == 0xffffffff):
        raise SplitZipRejected('Этот вариант многотомного ZIP (ZIP64 или разделённый каталог) нужно открыть вручную.')
    pool = [Path(p).absolute() for p in (_candidates(final) if candidates is None else candidates)]
    pool = [p for p in pool if p.parent == final.parent and not p.name.startswith('.')]
    if final not in pool:
        raise SplitZipRejected('Этот ZIP отсутствует в текущем комплекте документов. Повторите скачивание из ЕИС.')
    parts = [p for p in pool if part_number(p.name) and p.stem.casefold() == final.stem.casefold()]
    if not parts and re.fullmatch(r'doc_\d+', final.stem, re.I):
        # Historical downloader discarded original stems. Never guess between
        # multiple final volumes or independently named sets.
        all_parts = [p for p in pool if part_number(p.name)]
        finals = [p for p in pool if p.suffix.lower() == '.zip']
        if len(finals) == 1 and all(re.fullmatch(r'doc_\d+', p.stem, re.I) for p in all_parts):
            parts = all_parts
    grouped = {}
    for part in parts:
        grouped.setdefault(part_number(part.name), []).append(part)
    expected = set(range(1, end.disk + 1))
    if set(grouped) != expected or any(len(values) != 1 for values in grouped.values()):
        raise SplitZipRejected('Не найден однозначный полный комплект томов ZIP (.z01 … .zip). Повторите скачивание всех документов из ЕИС.')
    return [grouped[number][0] for number in sorted(expected)] + [final]


def _central_directory(final, end, sizes, budget):
    if (end.count > budget.limits.members or end.size > 8 * 1024 * 1024
            or end.offset + end.size != end.position):
        raise SplitZipRejected('Каталог многотомного ZIP слишком большой или имеет неподдерживаемую структуру.')
    with final.open('rb') as source:
        source.seek(end.offset)
        data = bytearray(source.read(end.size))
    if len(data) != end.size:
        raise SplitZipRejected('Каталог последнего тома ZIP прочитан не полностью.')
    prefixes, total = [], 0
    for size in sizes:
        prefixes.append(total)
        total += size
    cursor = 0
    for _ in range(end.count):
        budget.check_time()
        if cursor + 46 > len(data) or data[cursor:cursor+4] != b'PK\x01\x02':
            raise SplitZipRejected('Некорректный каталог многотомного ZIP.')
        name_size, extra_size, comment_size, disk = struct.unpack_from('<4H', data, cursor + 28)
        offset = struct.unpack_from('<L', data, cursor + 42)[0]
        stop = cursor + 46 + name_size + extra_size + comment_size
        if (stop > len(data) or disk >= len(sizes) or offset >= sizes[disk]
                or prefixes[disk] + offset >= prefixes[-1] + end.offset):
            raise SplitZipRejected('Некорректная ссылка на том ZIP.')
        extra, extra_end = cursor + 46 + name_size, cursor + 46 + name_size + extra_size
        while extra < extra_end:
            if extra + 4 > extra_end:
                raise SplitZipRejected('Некорректные дополнительные поля ZIP.')
            kind, length = struct.unpack_from('<2H', data, extra)
            if kind == 1:
                raise SplitZipRejected('Многотомный ZIP64 нужно открыть вручную.')
            extra += 4 + length
            if extra > extra_end:
                raise SplitZipRejected('Некорректные дополнительные поля ZIP.')
        if 0xffffffff in struct.unpack_from('<2L', data, cursor + 20):
            raise SplitZipRejected('Многотомный ZIP64 нужно открыть вручную.')
        struct.pack_into('<H', data, cursor + 34, 0)
        struct.pack_into('<L', data, cursor + 42, prefixes[disk] + offset)
        cursor = stop
    if cursor != len(data):
        raise SplitZipRejected('Количество файлов не совпадает с каталогом многотомного ZIP.')
    return data, prefixes[-1]


@contextmanager
def open_zip(source, budget, *, volumes=None):
    if hasattr(source, 'read'):
        with zipfile.ZipFile(source) as archive:
            yield archive
        return
    paths = volume_paths(source) if volumes is None else volumes
    if len(paths) == 1:
        with zipfile.ZipFile(source) as archive:
            yield archive
        return
    from autobot.archive_extraction import _assert_plain_directory
    sizes = []
    for path in paths:
        _assert_plain_directory(path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise SplitZipRejected('Один из томов ZIP недоступен или пуст.')
        sizes.append(path.stat().st_size)
    if sum(sizes) > budget.limits.input_bytes:
        raise SplitZipRejected('Общий размер томов ZIP превышает лимит исходного архива.')
    end = read_end(paths[-1])
    if (end is None or end.disk != len(paths) - 1 or end.central_disk != end.disk
            or end.disk_count != end.count):
        raise SplitZipRejected('Состав томов ZIP изменился; повторите скачивание.')
    central, prefix = _central_directory(paths[-1], end, sizes, budget)
    # TemporaryFile is removed on close, including process termination. No
    # executable archive tool receives a destination or chooses output paths.
    with tempfile.TemporaryFile() as merged:
        for path, size in zip(paths, sizes):
            copied = 0
            with path.open('rb') as stream:
                while block := stream.read(64 * 1024):
                    budget.check_time()
                    copied += len(block)
                    if copied > size:
                        raise SplitZipRejected('Том ZIP изменился во время чтения.')
                    merged.write(block)
            if copied != size:
                raise SplitZipRejected('Том ZIP прочитан не полностью.')
        merged.seek(prefix + end.offset)
        merged.write(central)
        merged.seek(prefix + end.position + 4)
        merged.write(struct.pack('<2H', 0, 0))
        merged.seek(prefix + end.position + 16)
        merged.write(struct.pack('<L', prefix + end.offset))
        merged.seek(0)
        with zipfile.ZipFile(merged) as archive:
            yield archive
