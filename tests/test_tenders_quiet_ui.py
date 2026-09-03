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
    monkeypatch.setattr(web_ui, "_read_estimates_index", lambda: [])
    client = web_ui.app.test_client()

    estimates_html = client.get("/estimates").get_data(as_text=True)
    tenders_template = (web_ui.REPO_ROOT / "autobot" / "templates" / "tenders.html").read_text(encoding="utf-8")
    shared_styles = (web_ui.REPO_ROOT / "autobot" / "static" / "autobot-ui.css").read_text(encoding="utf-8")
    tender_styles = (web_ui.REPO_ROOT / "autobot" / "static" / "tenders.css").read_text(encoding="utf-8")
    detail_styles = (web_ui.REPO_ROOT / "autobot" / "static" / "tender_detail.css").read_text(encoding="utf-8")

    for html in (estimates_html, tenders_template):
        nav = html.split('<nav class="topnav"', 1)[1].split("</nav>", 1)[0]
        assert nav.index('href="/estimates"') < nav.index('href="/tenders"')
        assert nav.count("topnav-primary") == 2
    assert 'href="/estimates" aria-current="page"' in estimates_html
    assert 'href="/tenders" aria-current="page"' in tenders_template
    assert ".topnav a.topnav-primary" in shared_styles
    assert ".topnav a.topnav-primary" in tender_styles
    assert ".topnav a.topnav-primary" in detail_styles
