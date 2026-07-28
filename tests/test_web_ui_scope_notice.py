from autobot import web_ui


def test_estimate_detail_page_explains_scope_mismatch(monkeypatch, tmp_path):
    estimate_id = "abc123def4567890"
    rows = [
        {
            "idx": 1,
            "name": "Бетон М300",
            "unit": "м3",
            "qty": 10.0,
            "unit_price": 5000.0,
            "total": 50000.0,
            "item_no": "1",
            "sheet": "Лист 1",
            "excel_row": 2,
            "section": "Раздел 1",
            "source": "Excel",
            "type": "material",
            "type_label": "Материал",
        }
    ]

    monkeypatch.setattr(
        web_ui,
        "_load_estimate_meta",
        lambda _id: {
            "id": estimate_id,
            "title": "Тестовая смета",
            "original_filename": "test.xlsx",
            "created_at": "25.07.2026",
            "row_count": 1,
            "market_selected_types": ["service"],
        }
        if _id == estimate_id
        else None,
    )
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _id: rows if _id == estimate_id else [])
    monkeypatch.setattr(web_ui, "_estimate_market_merged_path", lambda _id: tmp_path / "missing_compare.xlsx")
    monkeypatch.setattr(web_ui, "_estimate_market_raw_path", lambda _id: tmp_path / "missing_sources.xlsx")
    monkeypatch.setattr(web_ui, "_estimate_market_sections", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_ui, "_estimate_market_links", lambda *args, **kwargs: [])

    client = web_ui.app.test_client()
    resp = client.get(f"/estimates/{estimate_id}?types=material&table_view=compare")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "РЫНОК НЕ СОБРАН ДЛЯ ЭТОГО ТИПА" in html
    assert "Сейчас в файле рынка есть только: Услуги." in html
