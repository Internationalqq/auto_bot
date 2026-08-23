from autobot import web_ui


def test_estimate_crm_payload_recovers_zero_price_from_total(monkeypatch):
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_rows",
        lambda _id: [
            {
                "name": "Монтаж мелких конструкций",
                "unit": "т",
                "qty": 0.002,
                "unit_price": 0,
                "total": 265.37,
                "item_no": "59",
                "basis_code": "ГЭСН09-03-039-06",
                "type": "work",
            }
        ],
    )

    materials = web_ui._estimate_materials_for_crm("abc123")

    assert len(materials) == 1
    assert materials[0]["planned_qty"] == 0.002
    assert materials[0]["planned_price"] == 132685.0
    assert materials[0]["planned_total"] == 265.37


def test_tender_crm_payload_keeps_file_section_and_position_type(monkeypatch, tmp_path):
    import pandas as pd

    from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE

    tender_id = "0123456789012345678"
    report_path = tmp_path / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"
    report_path.touch()
    frame = pd.DataFrame(
        [
            {
                "Файл ЛСР": r"data\estimate\01 - heating.xlsx",
                "Раздел": "Раздел 1. Отопление",
                COL_ITEM: "5",
                COL_NAME: "Монтаж трубопровода отопления",
                COL_UNIT: "м",
                COL_QTY: 12,
                COL_UNIT_PRICE: 1000,
                COL_SUM: 12000,
                "basis_code": "ГЭСН16-02-001-01",
            },
            {
                "Файл ЛСР": r"data\estimate\02 - materials.xlsx",
                "Раздел": "Раздел 2. Материалы",
                COL_ITEM: "7",
                COL_NAME: "Труба стальная оцинкованная",
                COL_UNIT: "м",
                COL_QTY: 20,
                COL_UNIT_PRICE: 500,
                COL_SUM: 10000,
                "basis_code": "ФСБЦ-22.2.02.01-0001",
            },
        ]
    )
    monkeypatch.setattr(web_ui, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(web_ui.pd, "read_excel", lambda _path: frame)
    monkeypatch.setattr(web_ui, "load_tender_metadata", lambda: {tender_id: {"region": "Челябинская область"}})

    items = web_ui._tender_estimate_materials_for_crm(tender_id)

    assert [item["estimate_file_name"] for item in items] == ["01 - heating.xlsx", "02 - materials.xlsx"]
    assert [item["section_title"] for item in items] == ["Раздел 1. Отопление", "Раздел 2. Материалы"]
    assert [item["item_kind"] for item in items] == ["work", "material"]
    assert len({item["source_item_key"] for item in items}) == 2


def test_crm_projects_picker_api(monkeypatch):
    monkeypatch.setattr(
        web_ui,
        "crm_projects_for_picker",
        lambda: [{"id": 17, "title": "Школа", "contract_no": "44-ФЗ-1", "address": "Челябинск"}],
    )

    response = web_ui.app.test_client().get("/api/crm/projects")

    assert response.status_code == 200
    assert response.get_json()["projects"][0]["id"] == 17


def test_tender_export_adds_estimates_to_selected_existing_project(monkeypatch):
    import requests

    tender_id = "0123456789012345678"
    calls = []

    class Response:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return Response(payload={"projects": [{"id": 42, "title": "Школа", "contract_no": "OLD"}]})

        def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            if url.endswith("/api/auth/login"):
                return Response(payload={"ok": True})
            if url.endswith("/api/projects/42/estimate-import"):
                return Response(
                    201,
                    {
                        "imported": 2,
                        "estimateSources": 2,
                        "items": [{"id": 1}, {"id": 2}],
                    },
                )
            raise AssertionError(f"Unexpected POST {url}")

    materials = [
        {"title": "Работа", "planned_qty": 1, "estimate_file_name": "01.xlsx"},
        {"title": "Материал", "planned_qty": 2, "estimate_file_name": "02.xlsx"},
    ]
    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(web_ui, "_crm_credentials", lambda: ("director", "secret"))
    monkeypatch.setattr(web_ui, "_crm_base_url", lambda: "http://crm")
    monkeypatch.setattr(web_ui, "load_tender_metadata", lambda: {tender_id: {"title": "Ремонт школы"}})
    monkeypatch.setattr(
        web_ui,
        "_build_crm_project_payload",
        lambda _tid: ({"title": "Ремонт школы", "contract_no": tender_id}, materials),
    )

    result = web_ui.export_tender_to_crm(tender_id, project_id=42)

    assert result["project_id"] == 42
    assert result["added_to_existing"] is True
    assert result["materials_sent"] == 2
    import_call = next(call for call in calls if call[1].endswith("/estimate-import"))
    assert import_call[2]["json"]["source"]["sourceType"] == "tender"
    assert import_call[2]["json"]["replace_source"] is True
    assert import_call[2]["json"]["items"] == materials
    assert not any(call[1] == "http://crm/api/projects" and call[0] == "POST" for call in calls)


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
