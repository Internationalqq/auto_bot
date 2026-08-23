from __future__ import annotations

import io
import json
import re
import zipfile

import pandas as pd
import pytest

from autobot import source_documents, web_ui


def _nested_archive_bytes() -> bytes:
    inner_buffer = io.BytesIO()
    with zipfile.ZipFile(inner_buffer, "w") as inner:
        inner.writestr("Сметы/1 - благоустройство - ЛСР.pdf", b"%PDF-test-estimate")
        inner.writestr("Сметы/ССР.xlsx", b"excel-test")
        inner.writestr("Сметы/проект.pdf.sig", b"signature")
    outer_buffer = io.BytesIO()
    with zipfile.ZipFile(outer_buffer, "w") as outer:
        outer.writestr("ПСД ВЫБОРКА ДЛЯ ТОРГОВ.zip", inner_buffer.getvalue())
    return outer_buffer.getvalue()


def test_source_files_group_repeated_downloads_and_keep_original_name(monkeypatch, tmp_path):
    tender_id = "12345678"
    folder = tmp_path / tender_id
    folder.mkdir()
    payload = b"same archive"
    (folder / "contract.docx.zip").write_bytes(payload)
    (folder / "contract.docx (2).zip").write_bytes(payload)
    (folder / "download_log.json").write_text(
        json.dumps(
            [
                {
                    "status": "ok",
                    "saved_name": "contract.docx (2).zip",
                    "original_name": "contract.docx.zip",
                    "url": "https://zakupki.gov.ru/file",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(source_documents, "DOWNLOADS_DIR", tmp_path)

    inventory = source_documents.list_tender_source_files(tender_id)

    assert inventory["count"] == 1
    assert inventory["physical_count"] == 2
    assert inventory["files"][0]["name"] == "contract.docx.zip"
    assert inventory["files"][0]["copies"] == 2
    assert inventory["files"][0]["can_preview"] is True


def test_source_file_resolution_rejects_path_traversal(monkeypatch, tmp_path):
    tender_id = "12345678"
    (tmp_path / tender_id).mkdir()
    monkeypatch.setattr(source_documents, "DOWNLOADS_DIR", tmp_path)
    token = source_documents.make_file_token("../secret.txt")

    with pytest.raises(ValueError):
        source_documents.resolve_tender_source_file(tender_id, token)


def test_repair_filename_decodes_eis_cp866_zip_name():
    expected = "ПСД ВЫБОРКА ДЛЯ ТОРГОВ.rar"
    garbled = expected.encode("cp866").decode("cp437")

    assert source_documents.repair_filename(garbled) == expected


def test_archive_preview_expands_nested_archive_and_marks_estimates(tmp_path):
    path = tmp_path / "source.zip"
    path.write_bytes(_nested_archive_bytes())

    preview = source_documents.build_source_file_preview(path)
    nested = preview["entries"][0]

    assert preview["kind"] == "archive"
    assert nested["short_name"] == "ПСД ВЫБОРКА ДЛЯ ТОРГОВ.zip"
    assert len(nested["children"]) == 3
    assert preview["estimate_count"] == 2
    estimate = next(item for item in preview["estimate_entries"] if "ЛСР" in item["name"])
    member = source_documents.read_archive_member(path, estimate["token"])
    assert member["name"] == "1 - благоустройство - ЛСР.pdf"
    assert member["data"] == b"%PDF-test-estimate"


def test_excel_preview_returns_rows(monkeypatch, tmp_path):
    path = tmp_path / "estimate.xlsx"
    pd.DataFrame([{"Работа": "Укладка плитки", "Цена": 300}]).to_excel(path, index=False)

    preview = source_documents.build_source_file_preview(path)

    assert preview["kind"] == "excel"
    assert preview["sheets"][0]["columns"] == ["Работа", "Цена"]
    assert preview["sheets"][0]["rows"][0] == ["Укладка плитки", "300"]


def test_tender_files_tab_and_safe_text_preview(monkeypatch, tmp_path):
    tender_id = "12345678"
    folder = tmp_path / tender_id
    folder.mkdir()
    path = folder / "notice.txt"
    path.write_text("<script>alert('x')</script>Текст документа", encoding="utf-8")
    token = source_documents.make_file_token(path.name)
    monkeypatch.setattr(source_documents, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(web_ui, "load_tender_metadata", lambda: {tender_id: {"title": "Тестовая закупка"}})
    monkeypatch.setattr(
        web_ui,
        "_tenders_items",
        lambda: ([{"tender_id": tender_id, "has_downloads": True, "status_label": "Документы скачаны"}], {}),
    )
    client = web_ui.app.test_client()

    tab_response = client.get(f"/tenders/{tender_id}?tab=files")
    preview_response = client.get(f"/tenders/{tender_id}/source-files/{token}/preview")
    download_response = client.get(f"/tenders/{tender_id}/source-files/{token}/download")

    assert tab_response.status_code == 200
    assert "Исходные файлы закупки" in tab_response.get_data(as_text=True)
    tab_html = tab_response.get_data(as_text=True)
    assert "notice.txt" in tab_html
    assert re.search(r'class="btn file-preview"[^>]*target=', tab_html) is None
    assert preview_response.status_code == 200
    assert "&lt;script&gt;" in preview_response.get_data(as_text=True)
    assert "<script>alert" not in preview_response.get_data(as_text=True)
    assert download_response.status_code == 200
    assert download_response.data == path.read_bytes()
    assert "attachment" in download_response.headers.get("Content-Disposition", "")


def test_archive_member_preview_and_download_routes(monkeypatch, tmp_path):
    tender_id = "12345678"
    folder = tmp_path / tender_id
    folder.mkdir()
    path = folder / "source.zip"
    path.write_bytes(_nested_archive_bytes())
    source_token = source_documents.make_file_token(path.name)
    preview = source_documents.build_source_file_preview(path)
    estimate = next(item for item in preview["estimate_entries"] if "ЛСР" in item["name"])
    monkeypatch.setattr(source_documents, "DOWNLOADS_DIR", tmp_path)
    client = web_ui.app.test_client()

    archive_response = client.get(f"/tenders/{tender_id}/source-files/{source_token}/preview")
    member_preview = client.get(
        f"/tenders/{tender_id}/source-files/{source_token}/members/{estimate['token']}/preview"
    )
    member_download = client.get(
        f"/tenders/{tender_id}/source-files/{source_token}/members/{estimate['token']}/download"
    )

    assert archive_response.status_code == 200
    archive_html = archive_response.get_data(as_text=True)
    assert "Сметные документы · 2" in archive_html
    assert "ПСД ВЫБОРКА ДЛЯ ТОРГОВ.zip" in archive_html
    assert 'target="_blank"' not in archive_html
    assert member_preview.status_code == 200
    assert member_preview.data == b"%PDF-test-estimate"
    assert member_download.status_code == 200
    assert member_download.data == b"%PDF-test-estimate"
    assert "attachment" in member_download.headers.get("Content-Disposition", "")
