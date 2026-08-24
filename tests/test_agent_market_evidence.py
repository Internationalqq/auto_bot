from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import autobot.real_market_scraper as market


class AgentMarketEvidenceTests(unittest.TestCase):
    def test_agent_block_unit_is_normalized_to_base_market_unit(self) -> None:
        self.assertEqual(market._agent_unit_multiplier("100 м"), 100)
        self.assertEqual(market._agent_unit_multiplier("1000 м²"), 1000)
        self.assertEqual(65000 / market._agent_unit_multiplier("100 м"), 650)

    def test_agent_roll_price_is_converted_only_from_explicit_dimensions(self) -> None:
        multiplier, unit, note = market._agent_offer_unit_conversion(
            "упаковка",
            target_unit="м2",
            title="Геотекстиль иглопробивной Неотекс 2x25м",
            evidence="Цена 3 050 ₽ за упаковку",
        )
        self.assertEqual(multiplier, 50)
        self.assertEqual(unit, "м2")
        self.assertIn("50 м²", note)

        density_only = market._agent_offer_unit_conversion(
            "упаковка",
            target_unit="м2",
            title="Геотекстиль 200 г/м²",
            evidence="Цена 6 500 ₽ за упаковку",
        )
        self.assertEqual(density_only[0], 1)
        self.assertNotEqual(density_only[1], "м2")

        mass = market._agent_offer_unit_conversion("килограмм", target_unit="т")
        self.assertEqual(mass[:2], (0.001, "т"))
        self.assertEqual(60 / mass[0], 60000)

        incompatible_block = market._agent_offer_unit_conversion(
            "100 м",
            target_unit="шт",
            title="Лента сигнальная ЛСЭ-300 100 м",
        )
        self.assertEqual(incompatible_block[0], 1)
        self.assertEqual(incompatible_block[1], "м")

    def test_agent_evidence_price_is_not_replaced_by_another_page_row(self) -> None:
        page = """
        <h1>Песок в Ярославле</h1>
        <div>Песок речной — 400 руб. за м3</div>
        <div>Песок карьерный строительный — 650 руб. за м3</div>
        """
        row = pd.Series(
            {
                market.COL_NAME: "Песок строительный карьерный",
                "Ед. изм.": "м3",
                "basis_code": "ФСБЦ-02.3",
                "Раздел": "Материалы",
                market.COL_UNIT_PRICE: 676.21,
                market.COL_QTY: 10,
                market.COL_SUM: 6762.1,
            }
        )
        evidence = "Песок карьерный строительный — 650 руб. за м3"
        offer = market.MarketOffer(
            source="Hermes Agent",
            title="Песок карьерный строительный",
            price=650,
            url="https://supplier.example/pesok/",
            snippet=evidence,
            evidence=evidence,
            matched_unit="м3",
            adapter="hermes-browser-agent",
            agent_price=650,
            agent_unit="м3",
            agent_evidence=evidence,
        )
        plan = market.build_search_plan(row[market.COL_NAME], row["Ед. изм."], row["basis_code"], row["Раздел"])

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch.object(market, "_SOURCE_PAGE_CACHE_DIR", Path(tempdir)),
                patch.object(market, "_fetch_source_page", return_value=(page, "", "http")),
            ):
                checked = market._verify_offers(row, [offer], plan)

        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0].price, 650)
        self.assertTrue(checked[0].page_checked)
        self.assertEqual(checked[0].extractor, "hermes-evidence-confirmed")

    def test_import_converts_price_per_hundred_metres_before_plausibility(self) -> None:
        tender_id = "12345678"
        name = "Установка бетонного бордюра"
        captured: dict[str, market.MarketOffer] = {}
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            estimate_path = root / "estimate.xlsx"
            output_path = root / "market.xlsx"
            pd.DataFrame(
                [{
                    market.COL_NAME: name,
                    "Ед. изм.": "м",
                    market.COL_QTY: 755,
                    market.COL_UNIT_PRICE: 29238.67,
                    market.COL_SUM: 220751.97,
                    "basis_code": "ГЭСН27",
                    "Раздел": "Бордюры",
                }, {
                    market.COL_NAME: name + ",",
                    "Ед. изм.": "м",
                    market.COL_QTY: 100,
                    market.COL_UNIT_PRICE: 29238.67,
                    market.COL_SUM: 2923867,
                    "basis_code": "ГЭСН27",
                    "Раздел": "Бордюры",
                }]
            ).to_excel(estimate_path, index=False)

            def fake_verify(_row, offers, _plan, **_kwargs):
                captured["offer"] = offers[0]
                offers[0].verification = "verified"
                offers[0].page_checked = True
                offers[0].verification_reason = "Цена подтверждена"
                return offers

            with (
                patch.object(market, "estimate_path_for_tender", return_value=estimate_path),
                patch.object(market, "output_path_for_tender", return_value=output_path),
                patch.object(market, "load_tender_metadata", return_value={tender_id: {"region": "Ярославская область"}}),
                patch.object(market, "_verify_offers", side_effect=fake_verify),
                patch.object(market, "_store_verified_offers_in_index", return_value=0),
            ):
                imported = market.import_agent_market_result(
                    tender_id,
                    {
                        "name": name,
                        "queries": ["установка бордюра цена"],
                        "equivalent_positions": [{"position_key": "border-2", "name": name + ","}],
                    },
                    {"offers": [{
                        "title": "Установка бетонного бордюра",
                        "price": 65000,
                        "unit": "100 м",
                        "url": "https://contractor.example/prices/borders/",
                        "evidence": "Установка бордюра — 650 руб. за погонный метр",
                    }]},
                )
                output_row_count = len(pd.read_excel(output_path))

        self.assertEqual(captured["offer"].price, 650)
        self.assertEqual(captured["offer"].agent_price, 65000)
        self.assertEqual(imported["offer_outcomes"][0]["price"], 650)
        self.assertEqual(imported["offer_outcomes"][0]["raw_price"], 65000)
        self.assertEqual(imported["equivalent_positions_updated"], 1)
        self.assertEqual(output_row_count, 2)

    def test_direct_source_probe_requires_page_price_and_matching_unit(self) -> None:
        tender_id = "12345678"
        name = "Песок природный для строительных работ | класс, мелкий"
        page = "<h1>Карьерный песок</h1><div>Песок строительный мелкий — 300 ₽/м³</div>"
        with tempfile.TemporaryDirectory() as tempdir:
            estimate_path = Path(tempdir) / "estimate.xlsx"
            pd.DataFrame([{
                market.COL_NAME: name,
                "Ед. изм.": "м3",
                market.COL_QTY: 10,
                market.COL_UNIT_PRICE: 450,
                market.COL_SUM: 4500,
                "basis_code": "ФСБЦ",
                "Раздел": "Материалы",
            }]).to_excel(estimate_path, index=False)
            with (
                patch.object(market, "estimate_path_for_tender", return_value=estimate_path),
                patch.object(market, "_fetch_source_page", return_value=(page, "", "http")),
            ):
                result = market.probe_agent_market_start_urls(
                    tender_id,
                    {
                        "position_key": "sand-1",
                        "name": name,
                        "region": "Ярославская область",
                        "start_urls": ["https://supplier.example/sand/"],
                    },
                )

        self.assertTrue(result["_autobot_direct_probe"])
        self.assertEqual(result["offers"][0]["price"], 300)
        self.assertEqual(result["offers"][0]["unit"], "м3")


if __name__ == "__main__":
    unittest.main()
