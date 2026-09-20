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
                    "extractor": "price-block",
                    "title": "Щебень строительный 20-40 — 2 800 руб/м3",
                    "price": 2800,
                    "url": "https://supplier.example/catalog/crushed-stone-20-40",
                    "confidence": 0.9,
                    "matched_unit": "м3",
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
                    "matched_unit": "м3",
                    "observed_at": old,
                }
            ],
        )
        self.assertEqual(index.lookup_verified_offers(name="Песок строительный", unit="м3"), [])

    def test_unknown_date_is_not_backfilled_as_today(self) -> None:
        stored = index.record_verified_offers(tender_id='123', name='Песок строительный', unit='м3', offers=[{
            'verification': 'verified', 'price': 900, 'url': 'https://supplier.example/sand',
            'matched_unit': 'м3', 'observed_at': 'unknown',
        }])
        self.assertEqual(stored, 0)
        self.assertEqual(index.lookup_verified_offers(name='Песок строительный', unit='м3'), [])

    def test_regional_cache_keeps_distinct_quotes_and_source_conditions(self) -> None:
        for region, price in [('Ярославль', 900), ('Миасс', 1200)]:
            stored = index.record_verified_offers(tender_id='123', name='Песок строительный', unit='м3', region=region,
                offers=[{'verification': 'verified', 'source': 'Поставщик', 'title': 'Песок строительный', 'extractor':'price-block',
                         'price': price, 'url': 'https://supplier.example/sand', 'matched_unit': 'м3',
                         'observed_at': datetime.now(timezone.utc).isoformat(), 'location': region, 'search_region': region,
                         'evidence': f'Песок строительный — {price} руб/м3', 'price_scope': 'без доставки'}])
            self.assertEqual(stored, 1)
        local = index.lookup_verified_offers(name='Песок строительный', unit='м3', region='Ярославль')
        self.assertEqual([r['price'] for r in local], [900])
        self.assertEqual(local[0]['location'], 'Ярославль')
        self.assertEqual(local[0]['price_scope'], 'без доставки')
        self.assertIn('900', local[0]['evidence'])
        self.assertEqual(index.lookup_verified_offers(name='Песок строительный', unit='м3', region='Москва'), [])

    def test_recording_an_old_offer_does_not_relabel_its_region(self) -> None:
        offer = {'verification': 'verified', 'price': 900, 'url': 'https://supplier.example/sand',
                 'matched_unit':'м3', 'observed_at':datetime.now(timezone.utc).isoformat(),
                 'search_region':'Миасс', 'region_evidence':'Доставка по Миассу'}
        self.assertEqual(index.record_verified_offers(tender_id='123', name='Песок строительный', unit='м3',
                                                     region='Ярославль', offers=[offer]), 0)

    def test_replayed_old_observation_cannot_replace_newer_index_price(self) -> None:
        now = time.time()
        offer = {'verification':'verified', 'price':1000, 'url':'https://supplier.example/sand', 'extractor':'price-block',
                 'matched_unit':'м3', 'observed_at':now, 'evidence':'Песок строительный 1000 руб/м3'}
        self.assertEqual(index.record_verified_offers(tender_id='123', name='Песок строительный', unit='м3', offers=[offer]), 1)
        old = dict(offer, price=900, observed_at=now-600, evidence='Песок строительный 900 руб/м3')
        self.assertEqual(index.record_verified_offers(tender_id='456', name='Песок строительный', unit='м3', offers=[old]), 0)
        self.assertEqual(index.lookup_verified_offers(name='Песок строительный', unit='м3')[0]['price'], 1000)

    def test_weighted_median_prefers_trusted_cluster(self) -> None:
        value = index.weighted_median([(800, 0.9), (820, 0.8), (250, 0.1)])
        self.assertEqual(value, 800)

    def test_origin_survives_index_reuse_and_legacy_origin_requires_recheck(self) -> None:
        import json
        offer={'verification':'verified','price':1000,'url':'https://supplier.example/sand','matched_unit':'м3',
               'observed_at':time.time(),'evidence':'Песок строительный 1000 руб/м3','extractor':'price-block'}
        args=dict(tender_id='123',name='Песок строительный',unit='м3')
        self.assertEqual(index.record_verified_offers(**args,offers=[dict(offer,extractor='metadata')]),0)
        self.assertEqual(index.record_verified_offers(**args,offers=[offer]),1)
        saved=index.lookup_verified_offers(name=args['name'],unit=args['unit'])[0]
        self.assertEqual(saved['extractor'],'price-block')
        audit=index.REPO_ROOT/saved['audit_record_path']
        legacy=json.loads(audit.read_text(encoding='utf-8'));legacy.pop('extractor')
        audit.write_text(json.dumps(legacy,ensure_ascii=False),encoding='utf-8')
        self.assertEqual(index.lookup_verified_offers(name=args['name'],unit=args['unit']),[])

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
