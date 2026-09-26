from autobot import web_ui


def test_estimates_are_default_but_tenders_remain_available(monkeypatch):
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
    assert root.headers["Location"].endswith("/estimates")

    dash = client.get("/dashboard")
    assert dash.status_code == 302
    assert dash.headers["Location"].endswith("/estimates")

    resp = client.get("/tenders")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Тендеры" in html
    assert "Закупка № 1" in html
    assert "Найти цены" in html
    assert "tender-card" in html
    assert 'data-href="/tenders/1"' in html
    assert 'href="/tenders/1"' in html
    assert "tender-row" not in html
    assert "Сейчас ничего не выполняется" in html
    assert "Старый вид" not in html


def test_primary_navigation_puts_estimates_first(monkeypatch):
    from bs4 import BeautifulSoup
    monkeypatch.setattr(web_ui, "_read_estimates_index", lambda: [])
    client = web_ui.app.test_client()

    estimates_html = client.get("/estimates").get_data(as_text=True)
    # Navigation is rendered from one component; test destinations and selection,
    # not duplicated markup or the old decorative primary-button classes.
    with web_ui.app.test_request_context():
        template = web_ui.app.jinja_env.get_template("workspace_nav.html")
        tenders_html = template.module.workspace_nav("tenders")
    for html, active in ((estimates_html, "/estimates"), (tenders_html, "/tenders")):
        nav = BeautifulSoup(html, "html.parser").select_one('nav[aria-label="Разделы AutoBot"]')
        assert [link['href'] for link in nav.select('a')] == [
            '/estimates', '/tenders', '/tenders/suppliers', '/research'
        ]
        assert [link['href'] for link in nav.select('[aria-current="page"]')] == [active]
