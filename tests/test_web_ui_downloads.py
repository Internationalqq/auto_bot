from autobot import web_ui


def test_tender_download_routes_serve_existing_files(monkeypatch, tmp_path):
    tid = "123456"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    estimate_path = report_dir / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    market_path = report_dir / f"РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    svodka_path = report_dir / f"СВОДКА_РЫНОК_{tid}.xlsx"

    estimate_path.write_bytes(b"estimate-bytes")
    market_path.write_bytes(b"market-bytes")
    svodka_path.write_bytes(b"svodka-bytes")

    monkeypatch.setattr(web_ui, "REPORTS_DIR", report_dir)
    monkeypatch.setattr(web_ui, "load_tender_metadata", lambda: {tid: {"title": "Test Tender"}})

    client = web_ui.app.test_client()

    estimate_resp = client.get(f"/tenders/{tid}/estimate.xlsx")
    market_resp = client.get(f"/tenders/{tid}/market-sources.xlsx")
    svodka_resp = client.get(f"/tenders/{tid}/svodka.xlsx")

    assert estimate_resp.status_code == 200
    assert estimate_resp.data == b"estimate-bytes"
    assert "attachment" in estimate_resp.headers.get("Content-Disposition", "")

    assert market_resp.status_code == 200
    assert market_resp.data == b"market-bytes"

    assert svodka_resp.status_code == 200
    assert svodka_resp.data == b"svodka-bytes"


def test_estimate_detail_page_renders_switchable_tables(monkeypatch, tmp_path):
    import pandas as pd

    from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE

    estimate_id = "abc123def456"
    compare_path = tmp_path / "market_compare.xlsx"
    raw_path = tmp_path / "market_sources.xlsx"

    pd.DataFrame(
        [
            {
                COL_ITEM: "1",
                "Тип": "Материал",
                COL_NAME: "Бетон М300",
                COL_UNIT: "м3",
                COL_QTY: 10,
                COL_UNIT_PRICE: 5000,
                COL_SUM: 50000,
                "Рынок цены за ед. (итог)": "5 300; 5 450",
                "Медиана цена за ед. (рынок)": 5400,
                "Ошибка / статус": "",
            }
        ]
    ).to_excel(compare_path, index=False)
    pd.DataFrame(
        [
            {
                COL_ITEM: "1",
                "Тип": "Материал",
                COL_NAME: "Бетон М300",
                "Поисковый запрос рынка": "бетон м300 челябинск",
                "Цены за ед. (рынок, руб)": "5300; 5450",
                "Рыночные источники": "Авито; сайт поставщика",
                "Ошибка / статус": "",
            }
        ]
    ).to_excel(raw_path, index=False)

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
            "section": "",
            "source": "Excel",
            "type": "material",
            "type_label": "Материал",
        }
    ]

    monkeypatch.setattr(web_ui, "_load_estimate_meta", lambda _id: {"id": estimate_id, "title": "Тестовая смета", "original_filename": "test.xlsx", "created_at": "25.07.2026", "row_count": 1} if _id == estimate_id else None)
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _id: rows if _id == estimate_id else [])
    monkeypatch.setattr(web_ui, "_estimate_market_merged_path", lambda _id: compare_path)
    monkeypatch.setattr(web_ui, "_estimate_market_raw_path", lambda _id: raw_path)
    monkeypatch.setattr(web_ui, "_estimate_market_sections", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_ui, "_estimate_market_links", lambda *args, **kwargs: [])

    client = web_ui.app.test_client()
    resp = client.get(f"/estimates/{estimate_id}?table_view=compare")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-estimate-view-btn="estimate"' in html
    assert 'data-estimate-view-btn="compare"' in html
    assert 'data-estimate-view-btn="sources"' in html
    assert 'data-estimate-view-panel="compare"' in html
    assert "/estimates/abc123def456/market-compare.xlsx" in html
    assert "Бетон М300" in html
    assert "бетон м300 челябинск" in html


def test_estimates_page_renders_delete_action(monkeypatch, tmp_path):
    estimate_id = "feedbeef1234abcd"
    monkeypatch.setattr(
        web_ui,
        "_read_estimates_index",
        lambda: [
            {
                "id": estimate_id,
                "title": "Локальная смета",
                "original_filename": "local.xlsx",
                "created_at": "28.07.2026 12:00",
                "row_count": 2,
            }
        ],
    )
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_rows",
        lambda _id: [
            {"type": "material", "total": 1200.0},
            {"type": "work", "total": 800.0},
        ] if _id == estimate_id else [],
    )
    monkeypatch.setattr(web_ui, "_estimate_market_merged_path", lambda _id: tmp_path / "missing_compare.xlsx")
    monkeypatch.setattr(web_ui, "_estimate_market_raw_path", lambda _id: tmp_path / "missing_sources.xlsx")

    client = web_ui.app.test_client()
    resp = client.get("/estimates")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'data-estimate-card="{estimate_id}"' in html
    assert f'data-estimate-delete="{estimate_id}"' in html
    assert f'/estimates/{estimate_id}' in html
