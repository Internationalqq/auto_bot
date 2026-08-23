from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import autobot.real_market_scraper as market


class AvitoSafetyBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = Path(self.tmp.name) / "avito_guard.json"
        self.log = Path(self.tmp.name) / "avito.jsonl"
        self.patchers = [
            patch.object(market, "_AVITO_GUARD_PATH", self.guard),
            patch.object(market, "_AVITO_LOG_PATH", self.log),
            patch.object(market, "_AVITO_BLOCKED_UNTIL", 0.0),
            patch.object(market, "_AVITO_LAST_REQUEST_AT", 0.0),
            patch.object(market, "_AVITO_REQUEST_COUNT", 0),
            patch.dict(
                os.environ,
                {
                    "MARKET_AVITO_MAX_REQUESTS_PER_RUN": "2",
                    "MARKET_AVITO_MAX_REQUESTS_PER_DAY": "1",
                    "MARKET_AVITO_MIN_INTERVAL_SEC": "30",
                },
                clear=False,
            ),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_daily_budget_is_persistent_and_stops_second_navigation(self) -> None:
        self.assertEqual(market._before_avito_request(), "")

        state = json.loads(self.guard.read_text(encoding="utf-8"))
        self.assertEqual(state["daily_requests"], 1)
        self.assertEqual(state["daily_date"], time.strftime("%Y-%m-%d"))

        status = market.avito_guard_status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["daily_remaining"], 0)
        self.assertIn("Авито на паузе", market._before_avito_request())

    def test_success_resets_repeated_block_counter(self) -> None:
        self.guard.write_text(
            json.dumps({"consecutive_blocks": 2, "reason": "HTTP 429"}),
            encoding="utf-8",
        )

        market._record_avito_page_success(cards=17, query="щебень 20-40")

        state = json.loads(self.guard.read_text(encoding="utf-8"))
        self.assertEqual(state["consecutive_blocks"], 0)
        self.assertEqual(state["last_success_cards"], 17)
        self.assertEqual(state["last_success_query"], "щебень 20-40")

    def test_browser_defaults_keep_scroll_budget_small(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MARKET_AVITO_MAX_SCROLLS": "2",
                "MARKET_AVITO_SCROLL_TARGET_CARDS": "25",
                "MARKET_AVITO_SCROLL_DELAY_MIN_SEC": "5",
                "MARKET_AVITO_SCROLL_DELAY_MAX_SEC": "9",
            },
            clear=False,
        ):
            fetcher = market.AvitoBrowserFetcher(enabled=False)

        self.assertEqual(fetcher.max_scrolls, 2)
        self.assertEqual(fetcher.scroll_target_cards, 25)
        self.assertEqual(fetcher.scroll_delay_min_sec, 5.0)
        self.assertEqual(fetcher.scroll_delay_max_sec, 9.0)

    def test_search_index_reads_snippet_without_opening_avito(self) -> None:
        rss = """<?xml version="1.0"?><rss><channel><item>
        <title>Щебень фракции 20-40 — 1 800 ₽</title>
        <link>https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/scheben_1234567890</link>
        <description>Щебень фракции 20-40, цена 1 800 ₽ за м3, Ярославль</description>
        </item></channel></rss>"""
        requested: list[str] = []

        def fake_get(url: str, *args, **kwargs) -> str:
            requested.append(url)
            return rss if "bing.com" in url else "<html></html>"

        with (
            patch.object(market, "_MARKET_CACHE_DIR", Path(self.tmp.name) / "cache"),
            patch.object(market, "_session_get", side_effect=fake_get),
        ):
            offers = market.search_avito_index("щебень фракция 20-40", region="Ярославль", max_results=3)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].verification, "candidate")
        self.assertEqual(offers[0].price, 1800.0)
        self.assertEqual(offers[0].rejection_code, "indexed_snippet_only")
        self.assertTrue(requested)
        self.assertTrue(all("avito.ru" not in url.split("?", 1)[0] for url in requested))

    def test_guarded_direct_search_falls_back_to_index(self) -> None:
        indexed = market.MarketOffer(
            source="Авито · поисковый индекс",
            title="Щебень 20-40",
            price=1800.0,
            url="https://www.avito.ru/yaroslavl/item_123",
            verification="candidate",
        )
        with (
            patch.dict(os.environ, {"MARKET_AVITO_FORCE_REFRESH": "1"}, clear=False),
            patch.object(market, "_before_avito_request", return_value="Авито на паузе"),
            patch.object(market, "search_avito_index", return_value=[indexed]),
        ):
            offers, error = market.search_avito("щебень 20-40", max_results=3)

        self.assertEqual(offers, [indexed])
        self.assertIn("без открытия Авито", error)


if __name__ == "__main__":
    unittest.main()
