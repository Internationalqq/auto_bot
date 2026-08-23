from __future__ import annotations

import json
import unittest

from autobot.report_merge_html import _rows_from_bundle_or_fallback


class ReportMarketVerificationTests(unittest.TestCase):
    def test_candidates_do_not_appear_as_confirmed_sources(self) -> None:
        bundle = json.dumps(
            [
                {
                    "title": "Confirmed supplier",
                    "price": 1500,
                    "url": "https://supplier.example/item",
                    "verification": "verified",
                },
                {
                    "title": "Unverified listing",
                    "price": 350,
                    "url": "https://listing.example/item",
                    "verification": "candidate",
                    "verification_reason": "unit mismatch",
                },
            ]
        )

        rows = _rows_from_bundle_or_fallback(
            bundle_json=bundle,
            qty_scale=1.0,
            fallback_prices_text="1500; 350",
            fallback_phones_text="",
            fallback_urls_text="https://supplier.example/item; https://listing.example/item",
        )

        self.assertEqual([row["title"] for row in rows], ["Confirmed supplier"])

    def test_candidate_only_bundle_does_not_use_legacy_fallback(self) -> None:
        bundle = json.dumps(
            [
                {
                    "title": "Unverified listing",
                    "price": 350,
                    "url": "https://listing.example/item",
                    "verification": "candidate",
                }
            ]
        )

        rows = _rows_from_bundle_or_fallback(
            bundle_json=bundle,
            qty_scale=1.0,
            fallback_prices_text="350",
            fallback_phones_text="",
            fallback_urls_text="https://listing.example/item",
        )

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
