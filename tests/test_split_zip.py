import io
import json
from pathlib import Path
import struct
import zipfile

import pytest

from autobot import archive_extraction as ae
from autobot.document_download import DownloadRejected, inspect_document
from autobot.split_zip import SplitZipRejected, open_zip, volume_paths


TID = '32615872880'


def split_fixture(folder, entries=None, cuts=(120,), legacy=False):
    entries = entries or [('first.xlsx', b'first' * 60), ('second.xlsx', b'second' * 60)]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, content in entries:
            archive.writestr(name, content)
    raw = bytearray(b'PK\x07\x08' + stream.getvalue())
    end_pos = len(raw) - 22
    central_size, central_old = struct.unpack_from('<2L', raw, end_pos + 12)
    central = central_old + 4
    assert all(0 < cut < central for cut in cuts)
    starts = (0, *cuts)
    cursor = central
    for _ in entries:
        name_len, extra_len, comment_len = struct.unpack_from('<3H', raw, cursor + 28)
        absolute = struct.unpack_from('<L', raw, cursor + 42)[0] + 4
        disk = max(i for i, start in enumerate(starts) if start <= absolute)
        struct.pack_into('<H', raw, cursor + 34, disk)
        struct.pack_into('<L', raw, cursor + 42, absolute - starts[disk])
        cursor += 46 + name_len + extra_len + comment_len
    assert cursor == central + central_size
    struct.pack_into('<2H', raw, end_pos + 4, len(cuts), len(cuts))
    struct.pack_into('<L', raw, end_pos + 16, central - starts[-1])
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (start, stop) in enumerate(zip(starts, (*cuts, len(raw)))):
        extension = f'.z{index+1:02}' if index < len(cuts) else '.zip'
        path = folder / ((f'doc_{index+3}' if legacy else 'documents') + extension)
        path.write_bytes(raw[start:stop])
        paths.append(path)
    return paths


def extract(tmp_path, paths, **limits):
    scratch = tmp_path / ('scratch' + str(len(list(tmp_path.glob('scratch*')))))
    scratch.mkdir()
    return ae.extract_in_worker([paths[-1]], tmp_path / 'extracted', scratch, ae.Limits(**limits), paths)


@pytest.mark.parametrize('legacy,cuts', [(False, (120,)), (False, (120, 500)), (True, (120,))])
def test_all_volumes_crc_offsets_on_later_disk_and_cache(tmp_path, monkeypatch, legacy, cuts):
    paths = split_fixture(tmp_path / 'downloads' / TID, cuts=cuts, legacy=legacy)
    original = [p.read_bytes() for p in paths]
    result = extract(tmp_path, paths)
    assert result['failed_count'] == 0, result
    row = result['archives'][0]
    assert [Path(f['path']).read_bytes() for f in row['files']] == [b'first' * 60, b'second' * 60]
    assert [f['name'] for f in row['source_files']] == [p.name for p in paths]
    monkeypatch.setattr(ae, '_unpack', lambda *a: pytest.fail('verified cache must be reused'))
    again = extract(tmp_path, paths)
    assert again['archives'][0]['files'] == row['files']
    assert [p.read_bytes() for p in paths] == original


def test_changed_first_volume_does_not_reuse_cache_or_publish_partial(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    success = extract(tmp_path, paths)
    old_output = Path(success['archives'][0]['files'][0]['path'])
    data = bytearray(paths[0].read_bytes())
    data[60] ^= 1
    paths[0].write_bytes(data)
    failed = extract(tmp_path, paths)
    assert failed['failed_count'] == 1 and failed['archives'][0]['files'] == []
    assert 'повреждён' in failed['archives'][0]['message']
    assert old_output.read_bytes() == b'first' * 60


def test_volume_changed_during_cache_validation_is_rejected(tmp_path, monkeypatch):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    assert extract(tmp_path, paths)['failed_count'] == 0
    original = ae._read_cache
    def change(*args):
        result = original(*args)
        data = bytearray(paths[0].read_bytes())
        data[60] ^= 1
        paths[0].write_bytes(data)
        return result
    monkeypatch.setattr(ae, '_read_cache', change)
    result = extract(tmp_path, paths)
    assert result['archives'][0]['code'] == 'source_changed'
    assert not result['archives'][0]['files']


def test_missing_volume_stale_volume_and_ambiguous_legacy_are_rejected(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID, legacy=True)
    # An excluded old part physically exists but cannot complete the current set.
    with pytest.raises(SplitZipRejected, match='полный комплект'):
        volume_paths(paths[-1], [paths[-1]])
    extra = paths[-1].with_name('another.zip')
    extra.write_bytes(b'other')
    with pytest.raises(SplitZipRejected, match='полный комплект'):
        volume_paths(paths[-1], [*paths, extra])
    paths[0].unlink()
    assert extract(tmp_path, paths)['failed_count'] == 1


def test_auto_discovery_obeys_current_document_manifest(tmp_path):
    from autobot.source_file_versions import sha256_file
    paths = split_fixture(tmp_path / 'downloads' / TID)
    reports = tmp_path / 'reports'
    reports.mkdir()
    manifest = {'schema_version': 1, 'tender_id': TID, 'state': 'complete', 'files': [
        {'saved_name': paths[-1].name, 'size_bytes': paths[-1].stat().st_size, 'sha256': sha256_file(paths[-1])}]}
    (reports / f'DOCUMENTS_{TID}.json').write_text(json.dumps(manifest))
    with pytest.raises(SplitZipRejected, match='полный комплект'):
        volume_paths(paths[-1])


def test_combined_input_limit_and_member_count_apply_before_merge(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    size = max(p.stat().st_size for p in paths)
    assert extract(tmp_path, paths, input_bytes=size)['archives'][0]['code'] == 'input_size'
    assert extract(tmp_path, paths, members=1)['failed_count'] == 1


@pytest.mark.parametrize('position,value,code', [(4, 2, 'multipart'), (6, 0, 'multipart'), (8, 1, 'multipart')])
def test_missing_disk_split_directory_inconsistent_counts(tmp_path, position, value, code):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    data = bytearray(paths[-1].read_bytes())
    struct.pack_into('<H', data, len(data)-22+position, value)
    paths[-1].write_bytes(data)
    assert extract(tmp_path, paths)['archives'][0]['code'] == code


def test_split_zip_still_rejects_unsafe_members_and_nested_bombs(tmp_path):
    unsafe = split_fixture(tmp_path / 'unsafe', entries=[('../escape.xlsx', b'a' * 300)])
    assert extract(tmp_path, unsafe)['archives'][0]['code'] == 'unsafe_path'
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('large.xlsx', b'0' * 100_000)
    paths = split_fixture(tmp_path / 'nested', entries=[('nested.zip', nested.getvalue())], cuts=(60,))
    assert extract(tmp_path, paths, ratio=100)['archives'][0]['code'] == 'ratio'


def test_stream_download_accepts_volume_without_standalone_directory(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    assert inspect_document(paths[0], {'content-disposition': 'attachment; filename="documents.z01"'}) == '.z01'
    assert inspect_document(paths[0], {}) == '.z01'
    assert inspect_document(paths[-1], {}, '.zip') == '.zip'
    paths[0].write_bytes(b'<html>access check</html>')
    with pytest.raises(DownloadRejected, match='страницу сайта'):
        inspect_document(paths[0], {}, '.z01')
    paths[0].write_bytes(b'arbitrary bytes')
    with pytest.raises(DownloadRejected, match='Первый том'):
        inspect_document(paths[0], {}, '.z01')


def test_real_disposable_worker_and_recovery_after_missing_part(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    result = ae.extract_documents([paths[-1]], tmp_path / 'extracted', source_files=[paths[-1]])
    assert result['failed_count'] == 1
    recovered = ae.extract_documents([paths[-1]], tmp_path / 'extracted', source_files=paths)
    assert recovered['failed_count'] == 0, recovered
    assert not list((tmp_path / 'extracted').glob('.extract-*'))


def test_orphan_first_volume_blocks_even_without_final_archive_seed(tmp_path):
    paths = split_fixture(tmp_path / 'downloads' / TID)
    extra = paths[0].with_name('other.z01')
    extra.write_bytes(paths[0].read_bytes())
    for result in (ae.extract_documents([], tmp_path / 'extracted', source_files=[paths[0]]),
                   ae.extract_documents([paths[-1]], tmp_path / 'extracted', source_files=[*paths, extra])):
        assert result['failed_count'] >= 1
        assert any(row['code'] == 'multipart' for row in result['archives'] if row['status'] == 'failed')


def test_preview_uses_same_volumes_and_its_smaller_budget(tmp_path):
    from autobot.source_documents import _archive_preview, _read_archive_member, make_archive_member_token
    paths = split_fixture(tmp_path / 'downloads' / TID)
    preview = _archive_preview(paths[-1], paths[-1].name)
    assert preview['total'] == 2
    member = _read_archive_member(paths[-1], make_archive_member_token(['first.xlsx']))
    assert member['data'] == b'first' * 60
    with pytest.raises(SplitZipRejected, match='Общий размер'):
        with open_zip(paths[-1], ae.Budget(ae.Limits(input_bytes=700))):
            pytest.fail('over-limit input must not open')


def test_worker_preserves_precise_missing_volume_reason(tmp_path, monkeypatch):
    from autobot import document_preview_worker, paths as settings
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    paths = split_fixture(tmp_path / 'downloads' / TID)
    paths[0].unlink()
    with pytest.raises(document_preview_worker.PreviewRejected, match='полный комплект'):
        document_preview_worker.run_reader('file', path=paths[-1])


def test_preview_combined_limit_is_checked_before_temporary_file(tmp_path, monkeypatch):
    from dataclasses import replace
    from autobot import source_documents, split_zip
    paths = split_fixture(tmp_path / 'downloads' / TID)
    monkeypatch.setattr(source_documents, '_PREVIEW_ARCHIVE_LIMITS', replace(source_documents._PREVIEW_ARCHIVE_LIMITS, input_bytes=700))
    monkeypatch.setattr(split_zip.tempfile, 'TemporaryFile', lambda: pytest.fail('must reject before merging'))
    with pytest.raises(SplitZipRejected, match='Общий размер'):
        source_documents._archive_preview(paths[-1], paths[-1].name)


def test_document_discovery_includes_named_parts_and_eis_223_downloads():
    from autobot.main import collect_doc_links
    class Link:
        def __init__(self, href, text):
            self.href, self.text = href, text
        def get_attribute(self, name):
            return self.href
        def text_content(self, **kwargs):
            return self.text
    links = [Link('/223/purchase/public/download/download.html?id=test', 'Приложение'),
             Link('https://zakupki.gov.ru/document?id=part', 'Комплект.z01 (50 МБ)')]
    class Page:
        def locator(self, selector):
            return self
        def count(self):
            return len(links)
        def nth(self, index):
            return links[index]
        def content(self):
            return ''
    assert {url for _, url in collect_doc_links(Page())} == {
        'https://zakupki.gov.ru/223/purchase/public/download/download.html?id=test',
        'https://zakupki.gov.ru/document?id=part'}
