from __future__ import annotations

import unittest

from autobot.market_strategy import (
    assess_market_median_anomaly,
    assess_price_plausibility,
    build_search_plan,
    check_offer,
    classify_position,
    estimate_unit_multiplier,
    is_direct_source_url,
    normalize_unit,
    units_compatible,
)


class MarketStrategyTests(unittest.TestCase):
    def test_work_and_material_use_different_buckets(self) -> None:
        work = classify_position("Монтаж кабеля в лотках", "100 м")
        material = classify_position("Кабель силовой ВВГнг 3x2,5", "м")
        product = classify_position("Светильник светодиодный 40 Вт", "шт")
        self.assertEqual(work.bucket, "works")
        self.assertEqual(material.bucket, "materials")
        self.assertEqual(product.bucket, "materials")

    def test_aggregate_without_unit_is_not_auto_priced(self) -> None:
        plan = build_search_plan("Благоустройство основной территории", "")
        self.assertFalse(plan.can_auto_price)
        self.assertTrue(plan.position.needs_decomposition)

    def test_direct_source_rejects_search_result(self) -> None:
        self.assertFalse(is_direct_source_url("https://www.google.com/search?q=кабель"))
        self.assertTrue(is_direct_source_url("https://petrovich.ru/product/123456/"))

    def test_offer_needs_matching_unit(self) -> None:
        candidate = check_offer(
            name="Укладка тротуарной плитки",
            unit="м2",
            title="Укладка тротуарной плитки",
            snippet="Стоимость работ 1 200 ₽",
            url="https://example.org/services/plitka",
            price=1200,
            page_checked=True,
        )
        verified = check_offer(
            name="Укладка тротуарной плитки",
            unit="м2",
            title="Укладка тротуарной плитки",
            snippet="Стоимость работ 1 200 ₽ за м²",
            url="https://example.org/services/plitka",
            price=1200,
            page_checked=True,
        )
        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(verified.status, "verified")

    def test_offer_accepts_unit_confirmed_by_page_adapter(self) -> None:
        result = check_offer(
            name="Кабель ВВГнг 3х2,5",
            unit="м",
            title="Кабель ВВГнг 3х2,5",
            snippet="Цена подтверждена в карточке товара",
            url="https://supplier.example/product/vvgng-3x2-5",
            price=82,
            page_checked=True,
            source_unit="м",
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.matched_unit, "м")

    def test_dense_rock_crushed_stone_accepts_granite_but_not_gravel(self) -> None:
        common = {
            "name": "Щебень из плотных горных пород для строительных работ",
            "unit": "м3",
            "url": "https://supplier.example/scheben-20-40/",
            "price": 2400,
            "page_checked": True,
            "source_unit": "м3",
        }
        granite = check_offer(
            **common,
            title="Щебень гранитный (фр. 20-40)",
            snippet="Щебень гранитный 20-40 — 2400 руб. за м3",
        )
        gravel = check_offer(
            **common,
            title="Щебень гравийный (фр. 20-40)",
            snippet="Щебень гравийный 20-40 — 1500 руб. за м3",
        )
        self.assertEqual(granite.status, "verified")
        self.assertEqual(granite.matched_unit, "м3")
        self.assertEqual(gravel.status, "candidate")

    def test_fine_sand_requires_the_correct_fineness_module(self) -> None:
        common = {
            "name": "Песок природный для строительных работ | класс, мелкий",
            "unit": "м3",
            "price": 492,
            "page_checked": True,
            "source_unit": "м3",
        }
        specific = check_offer(
            **common,
            title="Песок с быстрой доставкой",
            snippet="Ярославль; песок; модуль крупности (мм): 1,5-2; 492 ₽ за м³",
            url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/pesok_8353321678",
        )
        too_fine = check_offer(
            **common,
            title="Песок с быстрой доставкой",
            snippet="Ярославль; песок; модуль крупности (мм): 1-1,5; 492 ₽ за м³",
            url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/pesok_8353321678",
        )
        mixed = check_offer(
            **common,
            title="Песок, ПГС с доставкой",
            snippet="Песок мелко- и крупнозернистый; ПГС; 550 ₽ за м³",
            url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/pesok_pgs_2603045420",
        )

        self.assertEqual(specific.status, "verified")
        self.assertEqual(specific.matched_unit, "м3")
        self.assertEqual(too_fine.status, "candidate")
        self.assertEqual(mixed.status, "candidate")

    def test_linear_and_running_metre_are_compatible(self) -> None:
        self.assertTrue(units_compatible("м", "пог. м"))
        self.assertFalse(units_compatible("м2", "пог. м"))

    def test_estimate_material_names_are_shortened_for_market_search(self) -> None:
        crushed_stone = build_search_plan(
            "Щебень из плотных горных пород для строительных работ",
            "м3",
            "ФСБЦ-02.2.05.04-2094",
        )
        concrete = build_search_plan(
            "Смеси бетонные тяжелого бетона (БСТ) на щебне из гравия, класс В15",
            "м3",
            "ФСБЦ-04.1.02.05-0002",
        )
        geotextile = build_search_plan(
            "Геополотно нетканое полиэфирное, иглопробивное",
            "м2",
            "ФСБЦ-01.7.06.01-1000",
        )

        self.assertIn("щебень строительный", crushed_stone.queries[0])
        self.assertIn("бетон В15 М200", concrete.queries[0])
        self.assertIn("геотекстиль нетканый иглопробивной", geotextile.queries[0])

    def test_search_queries_use_exact_price_marker_then_regional_fallback(self) -> None:
        plan = build_search_plan("Плитка керамическая", "кв. м", "ФСБЦ", "Материалы")

        self.assertIn('"плитка керамическая"', plan.queries[0].casefold())
        self.assertIn("₽/м²", plan.queries[0])
        self.assertIn("поставщик", plan.queries[1])

    def test_crushed_stone_fraction_is_kept_in_compact_query(self) -> None:
        plan = build_search_plan("Щебень строительный фракция 20-40", "м3", "ФСБЦ")
        self.assertIn("щебень фракции 20-40", plan.queries[0].casefold())

    def test_common_unit_aliases_are_canonical(self) -> None:
        aliases = {
            "пог. м": "пог.м",
            "погонный метр": "пог.м",
            "m2": "м2",
            "MTK": "м2",
            "m3": "м3",
            "MTQ": "м3",
            "MTR": "м",
        }
        for raw, expected in aliases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_unit(raw), expected)

    def test_normative_multiplier_without_space(self) -> None:
        self.assertEqual(estimate_unit_multiplier("Укладка плитки", "100м2"), 100.0)

    def test_trailing_thousand_is_used_for_price_plausibility(self) -> None:
        name = "Разработка грунта с погрузкой в траншеях 1000"
        self.assertEqual(estimate_unit_multiplier(name, "м3"), 1000.0)
        result = assess_price_plausibility(
            estimate_price=84_518,
            market_price=180,
            name=name,
            unit="м3",
        )
        self.assertEqual(result.status, "plausible")
        self.assertAlmostEqual(result.ratio or 0, 2.1297, places=3)

    def test_normative_block_is_recovered_from_row_math_after_pdf_ocr(self) -> None:
        multiplier = estimate_unit_multiplier(
            "Укладка тротуарной плитки",
            "м2",
            estimate_price=62_277.666594,
            quantity=2_306.5,
            total=1_436_434.38,
        )
        self.assertEqual(multiplier, 100.0)

        result = assess_price_plausibility(
            estimate_price=62_277.666594,
            market_price=800,
            name="Укладка тротуарной плитки",
            unit="м2",
            quantity=2_306.5,
            total=1_436_434.38,
        )
        self.assertEqual(result.multiplier, 100.0)
        self.assertEqual(result.status, "plausible")
        self.assertAlmostEqual(result.ratio or 0, 1.2846, places=3)

    def test_arbitrary_row_ratio_is_not_treated_as_a_normative_block(self) -> None:
        multiplier = estimate_unit_multiplier(
            "Монтаж оборудования",
            "шт",
            estimate_price=12_345,
            quantity=7,
            total=20_000,
        )
        self.assertEqual(multiplier, 1.0)

    def test_extreme_price_is_sent_to_review(self) -> None:
        result = assess_price_plausibility(
            estimate_price=18.23,
            market_price=189,
            name="Нанесение дорожной разметки",
            unit="м2",
        )
        self.assertEqual(result.status, "extreme")
        self.assertGreater(result.ratio or 0, 10)

    def test_threefold_peer_market_outlier_is_sent_to_review(self) -> None:
        normal = assess_market_median_anomaly(2500, [1000, 1100, 900], threshold=3)
        outlier = assess_market_median_anomaly(3500, [1000, 1100, 900], threshold=3)

        self.assertEqual(normal.status, "plausible")
        self.assertEqual(outlier.status, "review")
        self.assertEqual(outlier.median, 1000)

    def test_distance_up_to_ten_is_not_a_price_block(self) -> None:
        self.assertEqual(estimate_unit_multiplier("Перемещение грунта до 10", "м"), 1.0)

    def test_road_marking_work_does_not_search_for_paint(self) -> None:
        plan = build_search_plan(
            "Нанесение горизонтальной дорожной разметки",
            "м2",
            "ГЭСН27-09-001-01",
        )

        self.assertEqual(plan.position.slug, "work")
        self.assertIn("нанесение дорожной разметки", plan.queries[0].casefold())
        self.assertNotIn("краска", plan.queries[0].casefold())

    def test_ocr_geogrid_name_uses_clean_commercial_query(self) -> None:
        plan = build_search_plan("Георешетка композитная Е И ОО 496,95488", "м2", "ФСБЦ")
        self.assertIn("георешетка композитная", plan.queries[0].casefold())
        self.assertNotIn("496", plan.queries[0])


if __name__ == "__main__":
    unittest.main()
