import io
from pathlib import Path
import tempfile
import zipfile

import pandas as pd
import pytest

from autobot import document_preview_worker as worker, paths, source_documents as documents, web_ui
from autobot.atomic_output import output_lock


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def private_scratch(monkeypatch, tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch))
    monkeypatch.setattr(paths, 'DATA_DIR', tmp_path / 'data')
    yield
    assert not list(scratch.glob('autobot-preview-*'))


def test_preview_rejects_unsafe_archive_then_accepts_next_request(tmp_path):
    path = tmp_path / 'docs.zip'
    payload = zipped([('../bad.txt', b'bad'), ('good.txt', b'good')])
    path.write_bytes(payload)
    with pytest.raises(worker.PreviewRejected, match='путь'):
        documents.build_source_file_preview(path)
    assert path.read_bytes() == payload
    path.write_bytes(zipped([('good.txt', b'good')]))
    result = documents.build_source_file_preview(path)
    assert result['total'] == 1
    with pytest.raises(FileNotFoundError):
        documents.read_archive_member(path, documents.make_archive_member_token(['absent.txt']))


def test_nested_archives_share_member_budget(tmp_path):
    inner = zipped([(f'{i}.txt', b'x') for i in range(300)])
    path = tmp_path / 'docs.zip'
    path.write_bytes(zipped([('a.zip', inner), ('b.zip', inner)]))
    result = documents.build_source_file_preview(path)
    assert len(result['entries'][0]['children']) == 300
    assert result['entries'][1]['nested_error'] and not result['entries'][1]['children']
    assert result['truncated']


def test_root_count_limit_and_click_depth_do_not_reset(tmp_path):
    path = tmp_path / 'docs.zip'
    path.write_bytes(zipped([(f'{i}.txt', b'x') for i in range(501)]))
    with pytest.raises(worker.PreviewRejected, match='количество файлов'):
        documents.build_source_file_preview(path)
    result = documents.build_source_bytes_preview(zipped([('end.txt', b'x')]), 'nested.zip', ['a.zip','b.zip','c.zip'])
    assert result['kind'] == 'unavailable' and 'глубина' in result['message']


def test_timeout_releases_lock_and_removes_scratch(monkeypatch, tmp_path):
    path = tmp_path / 'docs.zip'
    path.write_bytes(zipped([('a.txt', b'x')]))
    with monkeypatch.context() as patch:
        patch.setattr(worker, 'TIMEOUT', .00001)
        with pytest.raises(worker.PreviewRejected, match='Время предпросмотра'):
            documents.build_source_file_preview(path)
    assert documents.build_source_file_preview(path)['total'] == 1


def test_parallel_reader_is_bounded_and_does_not_queue_web_requests(tmp_path):
    path = tmp_path / 'docs.zip'
    path.write_bytes(zipped([('a.txt', b'x')]))
    with output_lock(paths.DATA_DIR / 'document-preview'):
        with pytest.raises(worker.PreviewRejected, match='другой документ'):
            documents.build_source_file_preview(path)


def test_pdf_page_is_rendered_in_a_bounded_process_with_original_unchanged(tmp_path):
    import pymupdf
    path=tmp_path/'source.pdf'
    with pymupdf.open() as document:
        for number in range(2):
            page=document.new_page();page.insert_text((40,60),'Page '+str(number+1))
        document.save(path)
    before=path.read_bytes()
    result=worker.run_reader('pdf-page',path=path,page=2)
    assert result['page']==2 and result['pages']==2 and result['data'].startswith(b'\x89PNG\r\n\x1a\n')
    assert result['width']*result['height']<=3000000 and len(result['data'])<=worker.RESULT_LIMIT
    assert path.read_bytes()==before
    with pytest.raises(worker.PreviewRejected,match='Страница недоступна'):worker.run_reader('pdf-page',path=path,page=3)
    for invalid in [0,251,True,'1']:
        with pytest.raises(worker.PreviewRejected,match='от 1 до 250'):worker.run_reader('pdf-page',path=path,page=invalid)


def test_broken_pdf_does_not_block_the_next_preview(tmp_path):
    path=tmp_path/'bad.pdf';path.write_bytes(b'broken PDF')
    with pytest.raises(worker.PreviewRejected):worker.run_reader('pdf-page',path=path)
    assert path.read_bytes()==b'broken PDF'
    path=tmp_path/'good.zip';path.write_bytes(zipped([('readme.txt',b'OK')]))
    assert documents.build_source_file_preview(path)['total']==1


def test_excel_marks_row_and_column_truncation_and_preserves_cells(tmp_path):
    path = tmp_path / 'estimate.xlsx'
    pd.DataFrame([{f'Колонка {i}': f'строка {j}' for i in range(32)} for j in range(102)]).to_excel(path, index=False)
    result = documents.build_source_file_preview(path)
    assert result['truncated'] and len(result['sheets'][0]['columns']) == 30
    assert len(result['sheets'][0]['rows']) == 100
    assert result['sheets'][0]['rows'][99][0] == 'строка 99'


def test_text_and_docx_outputs_are_bounded(tmp_path):
    path = tmp_path / 'large.txt'
    path.write_bytes(b'x' * (3 * 1024 * 1024))
    result = documents.build_source_file_preview(path)
    assert result['truncated'] and len(result['text']) == 2 * 1024 * 1024
    path.write_bytes(b'A' + ('я' * (1024 * 1024 + 10)).encode('utf-8'))
    result = documents.build_source_file_preview(path)
    assert result['truncated'] and result['text'].startswith('Aяяяя')
    assert set(result['text']) == {'A', 'я'}
    path = tmp_path / 'compressed.docx'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', b'x' * 1_000_000)
    with pytest.raises(worker.PreviewRejected, match='Текст документа'):
        documents.build_source_file_preview(path)


def test_limit_page_preserves_original_download(monkeypatch, tmp_path):
    tid = '12345678'
    folder = tmp_path / tid
    folder.mkdir()
    original = zipped([('big.txt', b'original')])
    path = folder / 'docs.zip'
    path.write_bytes(original)
    token = documents.make_file_token(path.name)
    member = documents.make_archive_member_token(['big.txt'])
    monkeypatch.setattr(documents, 'DOWNLOADS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'read_archive_member', lambda *args: (_ for _ in ()).throw(worker.PreviewRejected('Размер превышен')))
    client = web_ui.app.test_client()
    for action in ('preview','download'):
        response = client.get(f'/tenders/{tid}/source-files/{token}/members/{member}/{action}')
        assert response.status_code == 422
        assert 'Размер превышен' in response.get_data(as_text=True)
        assert f'/tenders/{tid}/source-files/{token}/download' in response.get_data(as_text=True)
    assert client.get(f'/tenders/{tid}/source-files/{token}/download').data == original


def test_unsupported_member_preview_links_to_its_own_download(monkeypatch, tmp_path):
    tid = '12345678'
    folder = tmp_path / tid
    folder.mkdir()
    path = folder / 'docs.zip'
    path.write_bytes(zipped([('file.unknown', b'data')]))
    monkeypatch.setattr(documents, 'DOWNLOADS_DIR', tmp_path)
    token, member = documents.make_file_token(path.name), documents.make_archive_member_token(['file.unknown'])
    base = f'/tenders/{tid}/source-files/{token}/members/{member}'
    body = web_ui.app.test_client().get(base + '/preview').get_data(as_text=True)
    assert body.count(base + '/download') == 2
