from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autobot import real_market_scraper as market


def _candidate(url: str, title: str, snippet: str) -> market.MarketOffer:
    return market.MarketOffer(
        source="Интернет",
        title=title,
        price=float(market._parse_price(f"{title} {snippet}") or 0),
        url=url,
        snippet=snippet,
        discovery_engine="test",
    )


class UniversalMarketSearchTests(unittest.TestCase):
    def test_yahoo_parser_decodes_direct_supplier_url(self) -> None:
        page = """
        <div class="dd algo algo-sr">
          <a href="https://r.search.yahoo.com/x/RU=https%3A%2F%2Fsupplier.ru%2Fscheben-20-40/RK=2/RS=test">
            <h3>Купить щебень 20-40, цена 1 900 руб. за м3</h3>
          </a>
          <p>Щебень фракции 20-40 с доставкой, цена 1 900 ₽/м³.</p>
        </div>
        """

        offers = market._parse_yahoo_html(page, max_results=3)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].url, "https://supplier.ru/scheben-20-40")
        self.assertEqual(offers[0].price, 1900)
        self.assertEqual(offers[0].discovery_engine, "Yahoo")

    def test_web_search_combines_bing_with_an_independent_engine(self) -> None:
        rss = """
        <rss><channel><item>
          <title>Щебень 20-40 — 1800 руб. за м3</title>
          <link>https://supplier.example/catalog/scheben-20-40</link>
          <description>Щебень строительный 20-40, цена 1800 руб. за м3</description>
        </item></channel></rss>
        """
        independent = _candidate(
            "https://second-supplier.example/product/scheben-20-40",
            "Щебень гранитный 20-40 — 1950 руб. за м3",
            "Прайс на щебень 20-40: 1950 руб. за м3",
        )
        independent.discovery_engine = "DDGS/brave"
        with (
            patch.dict("os.environ", {"MARKET_SEARCH_PREFERRED_DOMAINS": "0"}, clear=False),
            patch.object(market, "_session_get", return_value=rss),
            patch.object(market, "_search_web_searx", return_value=([], "")),
            patch.object(market, "_search_web_ddgs", return_value=([independent], "")),
        ):
            offers = market.search_web("щебень 20-40 цена ₽/м3", max_results=2)

        self.assertEqual(len(offers), 2)
        self.assertEqual({offer.discovery_engine for offer in offers}, {"Bing RSS", "DDGS/brave"})

    def test_prefilter_blocks_marketplaces_and_informational_noise(self) -> None:
        offers = [
            _candidate(
                "https://www.avito.ru/all?q=plitka",
                "Плитка керамическая 1 500 ₽/м²",
                "Объявления о продаже плитки керамической",
            ),
            _candidate(
                "https://example.ru/blog/kak-vybrat-plitku",
                "Как выбрать плитку керамическую",
                "Инструкция по выбору без прайса",
            ),
            _candidate(
                "https://supplier.ru/product/ceramic-tile",
                "Плитка керамическая — 1 450 ₽/м²",
                "Карточка товара, цена 1 450 руб. за м2",
            ),
            _candidate(
                "https://supplier.ru/category/ceramic-tile",
                "Плитка керамическая — 1 430 ₽/м²",
                "Каталог плитки керамической, цена 1 430 руб. за м2",
            ),
        ]

        selected = market._relevant_search_offers(
            offers,
            "плитка керамическая цена ₽/м²",
            max_results=5,
            write_log=False,
        )

        self.assertEqual([offer.url for offer in selected], ["https://supplier.ru/product/ceramic-tile"])
        self.assertGreater(selected[0].discovery_score, 0)
        self.assertIn("цена есть", selected[0].discovery_reason)

    def test_prefilter_logs_machine_readable_rejection_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "market-search.jsonl"
            offer = _candidate(
                "https://www.ozon.ru/product/123",
                "Плитка керамическая 900 ₽/м²",
                "Цена плитки керамической 900 руб. за м2",
            )
            with patch.object(market, "_MARKET_SEARCH_LOG_PATH", log_path):
                selected = market._relevant_search_offers(
                    [offer],
                    "плитка керамическая цена ₽/м²",
                    max_results=3,
                )

            self.assertEqual(selected, [])
            event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertFalse(event["accepted"])
            self.assertEqual(event["reason_code"], "blocked_domain")
            self.assertEqual(event["url"], offer.url)

    def test_peer_market_outlier_stays_visible_but_leaves_verified_prices(self) -> None:
        offers = [
            market.MarketOffer("Интернет", "Плитка", price, f"https://supplier{index}.ru/item", verification="verified", confidence=0.9)
            for index, price in enumerate((950, 1000, 1050, 4000), start=1)
        ]

        checked = market._apply_market_consensus_guard(offers)

        self.assertEqual([offer.verification for offer in checked[:3]], ["verified", "verified", "verified"])
        self.assertEqual(checked[3].verification, "candidate")
        self.assertEqual(checked[3].rejection_code, "market_outlier")
        self.assertEqual(checked[3].consensus_median, 1025)
        self.assertIn("медианы", checked[3].verification_reason)

    def test_bulk_and_geotextile_queries_prioritize_specialized_domains(self) -> None:
        bulk = market._preferred_domains_for_query("щебень гранитный фракция 20-40")
        geotextile = market._preferred_domains_for_query("геополотно нетканое 300 г м2")

        self.assertEqual(bulk[:5], ("postavka76.ru", "samosval76.ru", "pesko.ru", "smit76.ru", "renta76.ru"))
        self.assertEqual(geotextile[:3], ("tentisib.ru", "tdagro.ru", "tstn.ru"))
        self.assertIn("petrovich.ru", bulk)

    def test_preferred_supplier_ranks_above_random_domain(self) -> None:
        preferred = _candidate(
            "https://postavka76.ru/catalog/scheben-20-40",
            "Щебень 20-40 — 1 800 ₽/м3",
            "Щебень фракции 20-40 с доставкой",
        )
        random_site = _candidate(
            "https://random-supplier.ru/catalog/scheben-20-40",
            "Щебень 20-40 — 1 800 ₽/м3",
            "Щебень фракции 20-40 с доставкой",
        )

        selected = market._relevant_search_offers(
            [random_site, preferred],
            "щебень фракция 20-40 цена ₽/м3",
            max_results=2,
            write_log=False,
        )

        self.assertEqual(selected[0].url, preferred.url)
        self.assertIn("приоритетный поставщик", selected[0].discovery_reason)


if __name__ == "__main__":
    unittest.main()
