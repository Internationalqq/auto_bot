from autobot import web_ui


def test_tenders_is_main_quiet_entry(monkeypatch):
    monkeypatch.setattr(
        web_ui,
        "build_workflow_payload",
        lambda include_storage=False: {
            "counts": {"find_market_prices": 1},
            "tenders": [
                {
                    "tender_id": "1",
                    "title": "Документы",
                    "region": "Челябинская область",
                    "stage": "Подача заявок",
                    "publish_date": "17.08.2026",
                    "price_rub": 1000.0,
                    "has_downloads": True,
                    "has_estimate": True,
                    "has_estimate_html": True,
                    "has_market_sources": False,
                    "has_comparison": False,
                    "has_report_site": False,
                    "next_action": "find_market_prices",
                    "next_action_label": "Найти цены",
                    "is_ready": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        web_ui,
        "load_tender_metadata",
        lambda: {"1": {"url": "https://zakupki.gov.ru/?regNumber=1", "price_rub": 1000.0}},
    )

    client = web_ui.app.test_client()

    root = client.get("/")
    assert root.status_code == 302
    assert root.headers["Location"].endswith("/tenders")

    dash = client.get("/dashboard")
    assert dash.status_code == 302
    assert dash.headers["Location"].endswith("/tenders")

    resp = client.get("/tenders")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Тендеры" in html
    assert "Закупка № 1" in html
    assert "Найти цены" in html
    assert "tender-card" in html
    assert 'data-href="/merge-report/1/"' in html
    assert 'href="/merge-report/1/"' in html
    assert "tender-row" not in html
    assert "Сейчас ничего не выполняется" in html
    assert "Старый вид" not in html
