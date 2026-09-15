import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import zipfile
import zlib

import pytest

from autobot import archive_extraction as ae


TID = '0171200001926000664'


def zip_bytes(entries, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def original(tmp_path, data, name='documents.zip'):
    path = tmp_path / 'downloads' / TID / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def extract(tmp_path, paths, **limits):
    scratch = tmp_path / ('scratch-' + str(len(list(tmp_path.glob('scratch-*')))))
    scratch.mkdir()
    return ae.extract_in_worker(paths, tmp_path / 'extracted', scratch, ae.Limits(**limits))


def test_nested_zip_provenance_cache_and_same_named_estimates(tmp_path, monkeypatch):
    nested = zip_bytes([('ЛСР.xlsx', b'second')])
    source = original(tmp_path, zip_bytes([('ЛСР.xlsx', b'first'), ('nested.zip', nested)]))
    first = extract(tmp_path, [source])
    files = first['archives'][0]['files']
    assert first['failed_count'] == 0, first
    estimates = [f for f in files if f['path'].endswith('.xlsx')]
    assert len(estimates) == 2
    assert {tuple(f['chain']) for f in estimates} == {('documents.zip', 'ЛСР.xlsx'), ('documents.zip', 'nested.zip', 'ЛСР.xlsx')}
    assert {Path(f['path']).read_bytes() for f in estimates} == {b'first', b'second'}
    monkeypatch.setattr(ae, '_unpack', lambda *a: pytest.fail('complete cache should be reused'))
    second = extract(tmp_path, [source])
    assert second['archives'][0]['files'] == files


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/escape', 'C:escape', 'a\\..\\escape',
                                  '//server/share', 'a/./b', 'a//b', 'a:NDS', 'NUL.txt', 'COM1',
                                  'a. /b', 'a./b', 'a\\\\b', 'a\x01b', 'a?b'])
def test_unsafe_names_never_publish_even_earlier_good_member(tmp_path, name):
    source = original(tmp_path, zip_bytes([('good.xlsx', b'good'), (name, b'bad')]))
    result = extract(tmp_path, [source])
    assert result['archives'][0]['code'] == 'unsafe_path'
    assert result['archives'][0]['files'] == []
    assert not (tmp_path / 'extracted').exists()
    assert source.read_bytes().startswith(b'PK')


@pytest.mark.parametrize('entries', [[('Same.xlsx', b'a'), ('same.xlsx', b'b')],
                                     [('dir', b'a'), ('dir/inside.xlsx', b'b')]])
def test_colliding_paths_are_not_silently_overwritten(tmp_path, entries):
    source = original(tmp_path, zip_bytes(entries))
    assert extract(tmp_path, [source])['archives'][0]['code'] == 'duplicate_path'


def test_zip_symlink_is_rejected(tmp_path):
    link = zipfile.ZipInfo('link.xlsx')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    source = original(tmp_path, zip_bytes([(link, b'../../outside')]))
    assert extract(tmp_path, [source])['archives'][0]['code'] == 'link'


def test_zip_nul_name_is_rejected_before_python_truncates_it():
    info = zipfile.ZipInfo('first\x00hidden')
    with pytest.raises(ae.ArchiveRejected) as error:
        ae.validate_member(info, ae.Limits())
    assert error.value.code == 'unsafe_path'


def test_existing_directory_link_cannot_redirect_extraction(tmp_path):
    source = original(tmp_path, zip_bytes([('file.txt', b'content')]))
    outside = tmp_path / 'outside'
    outside.mkdir()
    owner = tmp_path / 'extracted' / TID
    owner.parent.mkdir()
    try:
        owner.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('OS does not permit directory symlink creation')
    result = extract(tmp_path, [source])
    assert result['archives'][0]['code'] == 'unsafe_cache'
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('limits,entries,code', [
    ({'member_bytes': 3}, [('a', b'abcd')], 'member_size'),
    ({'members': 1}, [('a', b'a'), ('b', b'b')], 'member_count'),
    ({'total_bytes': 3}, [('a', b'ab'), ('b', b'cd')], 'total_size'),
])
def test_size_and_count_rejections_are_transactional(tmp_path, limits, entries, code):
    source = original(tmp_path, zip_bytes(entries))
    result = extract(tmp_path, [source], **limits)
    assert result['archives'][0]['code'] == code
    assert not result['archives'][0]['files']


def test_compression_bomb_stopped_before_streaming(tmp_path):
    source = original(tmp_path, zip_bytes([('bomb.txt', b'0' * 1000000)], zipfile.ZIP_DEFLATED))
    assert extract(tmp_path, [source])['archives'][0]['code'] == 'ratio'


def test_nesting_and_total_budget_cannot_reset_per_archive(tmp_path):
    source = original(tmp_path, zip_bytes([('inner.zip', zip_bytes([('a', b'a')]))]))
    result = extract(tmp_path, [source], depth=1)
    assert result['archives'][0]['code'] == 'depth'
    assert not result['archives'][0]['files']
    a = original(tmp_path, zip_bytes([('a', b'ab')]), 'a.zip')
    b = original(tmp_path, zip_bytes([('b', b'ab')]), 'b.zip')
    result = extract(tmp_path, [a, b], total_bytes=3)
    assert [r['status'] for r in result['archives']] == ['complete', 'failed']
    assert result['archives'][1]['code'] == 'total_size'
    # Cached roots are charged against the same total on the next run.
    result = extract(tmp_path, [a, b], total_bytes=3)
    assert result['archives'][1]['code'] == 'total_size'


def test_corrupt_cache_is_preserved_and_rebuilt(tmp_path):
    source = original(tmp_path, zip_bytes([('ЛСР.xlsx', b'correct')]))
    first = extract(tmp_path, [source])
    file = Path(first['archives'][0]['files'][0]['path'])
    file.write_bytes(b'changed')
    second = extract(tmp_path, [source])
    assert second['failed_count'] == 0
    assert second['archives'][0]['cache_rebuilt'] is True
    assert file.read_bytes() == b'correct'
    copies = list((tmp_path / 'extracted' / TID / 'documents').glob('invalid-*/files/ЛСР.xlsx'))
    assert len(copies) == 1 and copies[0].read_bytes() == b'changed'


def test_changed_source_cannot_use_previous_version(tmp_path):
    source = original(tmp_path, zip_bytes([('ЛСР.xlsx', b'old')]))
    old = extract(tmp_path, [source])['archives'][0]['files'][0]
    source.write_bytes(zip_bytes([('ЛСР.xlsx', b'new')]))
    new = extract(tmp_path, [source])['archives'][0]['files'][0]
    assert old['path'] != new['path']
    assert Path(old['path']).read_bytes() == b'old'
    assert Path(new['path']).read_bytes() == b'new'


def test_crc_failure_never_publishes_partial_root(tmp_path):
    data = zip_bytes([('good.xlsx', b'first'), ('broken.xlsx', b'UNIQUE_CONTENT')])
    source = original(tmp_path, data.replace(b'UNIQUE_CONTENT', b'WRONG__CONTENT', 1))
    result = extract(tmp_path, [source])
    assert result['failed_count'] == 1
    assert result['archives'][0]['files'] == []
    assert not (tmp_path / 'extracted').exists()


def test_actual_size_is_bounded_even_if_metadata_lies():
    with pytest.raises(ae.ArchiveRejected, match='Фактический размер'):
        ae.read_member_stream(io.BytesIO(b'123456'), io.BytesIO(), 3, ae.Budget(ae.Limits()))


def test_unsupported_format_has_actionable_reason(tmp_path):
    source = original(tmp_path, b'7z?', 'estimate.7z')
    result = extract(tmp_path, [source])
    assert result['archives'][0]['code'] == 'unsupported'
    assert 'вручную' in result['archives'][0]['message']


def test_real_worker_reopens_completed_output_and_cleans_scratch(tmp_path):
    source = original(tmp_path, zip_bytes([('ЛСР.xlsx', b'excel')]))
    base = tmp_path / 'extracted'
    first = ae.extract_documents([source], base)
    assert first['failed_count'] == 0, first
    assert Path(first['archives'][0]['files'][0]['path']).read_bytes() == b'excel'
    second = ae.extract_documents([source], base)
    assert second['archives'][0]['files'] == first['archives'][0]['files']
    assert not list(base.glob('.extract-*'))


def test_stuck_worker_is_terminated_without_partial_output(tmp_path, monkeypatch):
    source = original(tmp_path, zip_bytes([('ЛСР.xlsx', b'excel')]))
    real_popen = subprocess.Popen
    children = []
    def stuck(command, **kwargs):
        proc = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(ae.subprocess, 'Popen', stuck)
    # Avoid intercepting the Windows taskkill subprocess itself.
    monkeypatch.setattr(ae, '_stop_process', lambda p: (p.kill(), p.wait(timeout=5)))
    result = ae.extract_documents([source], tmp_path / 'extracted', limits=ae.Limits(seconds=0.01))
    assert result['archives'][0]['code'] == 'timeout'
    assert children[0].poll() is not None
    assert not list((tmp_path / 'extracted').glob('.extract-*'))


def test_main_only_seeds_current_original_archives(tmp_path):
    from autobot.main import archive_seeds_for_tender
    source = original(tmp_path, b'zip')
    old = tmp_path / 'extracted' / TID / 'old' / 'nested.zip'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old')
    assert archive_seeds_for_tender(source.parent, old.parent) == [source]
    source.unlink()
    assert archive_seeds_for_tender(source.parent, old.parent) == []


def test_plain_rar_stream_and_password_link_guards(tmp_path):
    # Small RAR3 stored-file fixture, generated from our own content.
    def header(body):
        return struct.pack('<H', zlib.crc32(body) & 0xffff) + body
    name, data = b'estimate.txt', b'our test estimate'
    signature = b'Rar!\x1a\x07\x00'
    main = header(struct.pack('<BHHHI', 0x73, 0, 13, 0, 0))
    file = header(struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(name), len(data), len(data),
                              3, zlib.crc32(data), 0, 20, 0x30, len(name), 0o100644) + name)
    end = header(struct.pack('<BHH', 0x7b, 0, 7))
    source = original(tmp_path, signature + main + file + data + end, 'docs.rar')
    result = ae.extract_documents([source], tmp_path / 'extracted')
    assert result['failed_count'] == 0, result
    assert Path(result['archives'][0]['files'][0]['path']).read_bytes() == data
    import rarfile
    info = rarfile.Rar5FileInfo()
    info.filename = 'hardlink'
    info.file_redir = (rarfile.RAR5_XREDIR_HARD_LINK, 0, 'target')
    with pytest.raises(ae.ArchiveRejected) as error:
        ae.validate_member(info, ae.Limits())
    assert error.value.code == 'link'
