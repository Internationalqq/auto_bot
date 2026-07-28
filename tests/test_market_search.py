from autobot.item_research import ItemResearchResult
from autobot.real_market_scraper import MarketOffer, _parse_avito_html


def test_parse_avito_html_reads_json_payload():
    page_html = """
    <html><body>
    <script type="application/ld+json">
    {
      "itemListElement": [
        {
          "url": "https:\\/\\/www.avito.ru\\/chelyabinsk\\/remont_i_stroitelstvo\\/beton_m300_1234567890",
          "name": "Бетон М300 с доставкой",
          "offers": {"price": "12 500 ₽"}
        }
      ]
    }
    </script>
    </body></html>
    """

    offers = _parse_avito_html(page_html, "https://www.avito.ru/all?q=бетон", max_results=5)

    assert len(offers) == 1
    assert offers[0].title == "Бетон М300 с доставкой"
    assert offers[0].price == 12500
    assert offers[0].url == "https://www.avito.ru/chelyabinsk/remont_i_stroitelstvo/beton_m300_1234567890"


def test_api_research_items_uses_configured_sources(monkeypatch):
    from autobot import item_research
    from autobot.web_ui import app

    captured: dict[str, object] = {}

    def fake_research_item(query: str, *, region: str = "", sources=None, max_results: int = 5):
        captured["query"] = query
        captured["region"] = region
        captured["sources"] = list(sources or [])
        captured["max_results"] = max_results
        return ItemResearchResult(
            query=query,
            region=region,
            sources=list(sources or []),
            offers=[
                MarketOffer(
                    source="Авито",
                    title="Бетон М300",
                    price=12500,
                    url="https://www.avito.ru/item_1234567890",
                    snippet="",
                )
            ],
            errors="",
        )

    monkeypatch.delenv("MARKET_SUMMARY_SOURCES", raising=False)
    monkeypatch.setenv("MARKET_SOURCES", "avito,web")
    monkeypatch.setattr(item_research, "research_item", fake_research_item)

    client = app.test_client()
    resp = client.post(
        "/api/research-items",
        json={"queries": "бетон м300", "city": "Челябинск"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert captured["sources"] == ["avito", "web"]
    assert data["results"][0]["offers"][0]["source"] == "Авито"
