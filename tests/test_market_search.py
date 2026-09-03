from autobot.item_research import ItemResearchResult
from autobot.real_market_scraper import MarketOffer, _parse_avito_html, _parse_bing_rss


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


def test_parse_bing_rss_reads_direct_source_cards():
    page_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Укладка тротуарной плитки — 1 500 руб. за м2</title>
        <link>https://contractor.example/prices/plitka</link>
        <description>Прайс на укладку тротуарной плитки: 1 500 ₽ за м².</description>
      </item>
    </channel></rss>
    """

    offers = _parse_bing_rss(page_xml, max_results=3)

    assert len(offers) == 1
    assert offers[0].url == "https://contractor.example/prices/plitka"
    assert offers[0].price == 1500


def test_api_research_items_uses_configured_sources(monkeypatch):
    from autobot import item_research
    from autobot.web_ui import app

    captured: dict[str, object] = {}

    def fake_research_item(
        query: str,
        *,
        unit: str = "",
        region: str = "",
        sources=None,
        max_results: int = 5,
    ):
        captured["query"] = query
        captured["unit"] = unit
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
