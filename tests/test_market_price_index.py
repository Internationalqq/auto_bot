from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import autobot.market_price_index as index


class MarketPriceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = (index.REPO_ROOT, index.INDEX_ROOT, index.INDEX_DB, index.AUDIT_ROOT)
        root = Path(tempfile.mkdtemp(prefix="market-index-test-"))
        index.REPO_ROOT = root
        index.INDEX_ROOT = root / "data" / "market_index"
        index.INDEX_DB = index.INDEX_ROOT / "market.sqlite3"
        index.AUDIT_ROOT = index.INDEX_ROOT / "audit"

    def tearDown(self) -> None:
        index.REPO_ROOT, index.INDEX_ROOT, index.INDEX_DB, index.AUDIT_ROOT = self.original

    def test_verified_offer_is_reused_with_audit_snapshot(self) -> None:
        stored = index.record_verified_offers(
            tender_id="123",
            name="Щебень строительный фракция 20-40",
            unit="м3",
            basis_code="ФСБЦ-02.2.05",
            offers=[
                {
                    "verification": "verified",
                    "source": "Поставщик",
                    "title": "Щебень строительный 20-40 — 2 800 руб/м3",
                    "price": 2800,
                    "url": "https://supplier.example/catalog/crushed-stone-20-40",
                    "confidence": 0.9,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "page_html": "<html><body>Щебень 20-40, цена 2800 руб/м3</body></html>",
                }
            ],
        )
        self.assertEqual(stored, 1)
        offers = index.lookup_verified_offers(
            name="Щебень для строительных работ фракция 20-40",
            unit="м3",
            basis_code="ФСБЦ-02.2.05",
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0]["audit_record_path"])
        self.assertTrue((index.REPO_ROOT / offers[0]["audit_record_path"]).is_file())
        self.assertTrue((index.REPO_ROOT / offers[0]["snapshot_path"]).is_file())

    def test_expired_offer_is_not_reused(self) -> None:
        old = datetime.fromtimestamp(time.time() - 90 * 86400, tz=timezone.utc).isoformat()
        index.record_verified_offers(
            tender_id="123",
            name="Песок строительный",
            unit="м3",
            offers=[
                {
                    "verification": "verified",
                    "source": "Поставщик",
                    "title": "Песок строительный",
                    "price": 900,
                    "url": "https://supplier.example/sand",
                    "confidence": 0.9,
                    "observed_at": old,
                }
            ],
        )
        self.assertEqual(index.lookup_verified_offers(name="Песок строительный", unit="м3"), [])

    def test_weighted_median_prefers_trusted_cluster(self) -> None:
        value = index.weighted_median([(800, 0.9), (820, 0.8), (250, 0.1)])
        self.assertEqual(value, 800)

    def test_parser_degradation_is_detected_against_previous_run(self) -> None:
        index.record_parser_run(
            tender_id="1", sources=["web"], total_rows=10, processed_rows=10,
            rows_with_offers=9, verified_rows=7, candidate_rows=2, error_rows=1, duration_sec=3,
        )
        current = index.record_parser_run(
            tender_id="2", sources=["web"], total_rows=10, processed_rows=10,
            rows_with_offers=4, verified_rows=2, candidate_rows=2, error_rows=6, duration_sec=3,
        )
        self.assertTrue(current["degraded"])
        self.assertEqual(current["baseline_rate"], 0.9)


if __name__ == "__main__":
    unittest.main()
