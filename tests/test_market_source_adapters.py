from __future__ import annotations

import unittest

from autobot.market_source_adapters import inspect_source_page


class MarketSourceAdapterTests(unittest.TestCase):
    def test_work_price_is_taken_from_matching_line_not_page_minimum(self) -> None:
        page = """
        <h1>Укладка тротуарной плитки</h1>
        <h3>Стоимость укладки и подготовки основания (без материалов)</h3>
        <ol>
          <li>Укладка тротуарной плитки на готовое основание — от 450 руб./м<sup>2</sup></li>
          <li>Погрузка и вывоз мусора — от 150 руб./м<sup>2</sup></li>
        </ol>
        """
        result = inspect_source_page(
            page,
            "https://contractor.example/prices",
            name="Укладка тротуарной плитки",
            target_unit="м2",
            position_bucket="works",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 450)
        self.assertEqual(result.price_scope, "work_only")

    def test_material_price_from_page_metadata(self) -> None:
        page = """
        <html><head>
        <title>Кабель ВВГнг(А) 3х2,5 по цене от 82 ₽ за метр</title>
        <meta name="description" content="Кабель ВВГнг(А) 3х2,5. Цена 82 рублей за метр">
        </head><body><h1>Кабель ВВГнг(А) 3х2,5</h1></body></html>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/products/vvgnga-3x2-5",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 82)
        self.assertEqual(result.unit, "м")

    def test_category_and_antibot_are_not_prices(self) -> None:
        category = inspect_source_page(
            "<html><title>Кабели от 100 руб.</title></html>",
            "https://shop.example/catalog/cables",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        blocked = inspect_source_page(
            "<html><script src='https://abc.servicepipe.tech/loader.js'></script><div id='id_spinner'></div></html>",
            "https://www.vseinstrumenti.ru/product/123/",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        self.assertEqual(category.status, "listing")
        self.assertEqual(blocked.status, "blocked")
        self.assertFalse(category.accepted)
        self.assertFalse(blocked.accepted)

    def test_turnkey_work_with_materials_is_rejected(self) -> None:
        page = """
        <h1>Укладка плитки под ключ</h1>
        <h3>Цена с материалами</h3>
        <p>Укладка тротуарной плитки под ключ — 2400 руб./м2, материалы включены.</p>
        """
        result = inspect_source_page(
            page,
            "https://contractor.example/turnkey",
            name="Укладка тротуарной плитки",
            target_unit="м2",
            position_bucket="works",
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "scope-unknown")

    def test_compound_paragraph_keeps_price_with_its_own_line(self) -> None:
        page = """
        <h1>Укладка тротуарной плитки</h1>
        <h3>Цена укладки тротуарной плитки и подготовка основания</h3>
        <p>
          Подготовка песчаного основания — от 450 руб./м<sup>2</sup><br>
          Подготовка бетонного основания — от 600 руб./м<sup>2</sup><br>
          Укладка тротуарной плитки на готовое основание — от 500 руб./м<sup>2</sup>
        </p>
        """
        result = inspect_source_page(
            page,
            "https://contractor.example/prices",
            name="Укладка тротуарной плитки",
            target_unit="м2",
            position_bucket="works",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 500)
        self.assertIn("Укладка тротуарной плитки", result.evidence)

    def test_page_heading_does_not_match_an_unrelated_cheaper_line(self) -> None:
        page = """
        <h1>Стоимость укладки тротуарной плитки</h1>
        <ul>
          <li>Укладка геотекстиля — 50 руб./м²</li>
          <li>Укладка тротуарной плитки — 800 руб./м²</li>
        </ul>
        """
        result = inspect_source_page(
            page,
            "https://contractor.example/prices/plitka",
            name="Укладка тротуарной плитки",
            target_unit="м2",
            position_bucket="works",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 800)
        self.assertIn("тротуарной плитки", result.evidence.casefold())

    def test_work_table_inherits_unit_from_header(self) -> None:
        page = """
        <h1>Укладка тротуарной плитки</h1>
        <table>
          <thead><tr><th>Тип работ</th><th>Цена за м²</th></tr></thead>
          <tbody><tr><td>Укладка плитки на готовое основание</td><td>от 300 ₽</td></tr></tbody>
        </table>
        """
        result = inspect_source_page(
            page,
            "https://contractor.example/prices",
            name="Укладка тротуарной плитки",
            target_unit="м2",
            position_bucket="works",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 300)
        self.assertEqual(result.unit, "м2")

    def test_material_product_price_block_accepts_linear_metre(self) -> None:
        page = """
        <h1>Кабель ВВГнг 3х2,5</h1>
        <div class="product__pr-price-new"><span class="price-wrapper">96 ₽ / пог. м</span></div>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/product/vvgng-3x2-5",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 96)
        self.assertEqual(result.unit, "пог.м")

    def test_bulk_material_accepts_rubles_per_cube(self) -> None:
        page = """
        <h1>Щебень строительный</h1>
        <p>Щебень с доставкой — от 750 руб/куб</p>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/materials/scheben",
            name="Щебень строительный",
            target_unit="м3",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 750)
        self.assertEqual(result.unit, "м3")

    def test_jsonld_non_ruble_offer_is_rejected(self) -> None:
        page = """
        <script type="application/ld+json">
        {"@type":"Product","name":"Кабель ВВГнг 3х2,5","offers":{"@type":"Offer","price":"82","priceCurrency":"USD","unitText":"м"}}
        </script>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/product/cable",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.facts_found, 0)

    def test_schema_microdata_is_preferred_and_reported(self) -> None:
        page = """
        <div itemscope itemtype="https://schema.org/Product">
          <span itemprop="name">Кабель ВВГнг 3х2,5</span>
          <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
            <meta itemprop="priceCurrency" content="RUB">
            <meta itemprop="unitText" content="м">
            <meta itemprop="price" content="96.50">
          </div>
        </div>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/product/cable",
            name="Кабель ВВГнг 3х2,5",
            target_unit="м",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 96.5)
        self.assertEqual(result.extractor, "microdata")

    def test_delivery_fee_is_not_bound_to_product_unit_price(self) -> None:
        page = """
        <h1>Тротуарная плитка Старый город</h1>
        <p>Стоимость доставки: 500 ₽</p>
        <p>Тротуарная плитка Старый город — 1 500 ₽/м²</p>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/product/plitka",
            name="Тротуарная плитка Старый город",
            target_unit="м2",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 1500)

    def test_neighbouring_fraction_price_is_not_used(self) -> None:
        page = """
        <h1>Щебень фракции 20-40</h1>
        <ul>
          <li>Щебень фракции 5-40 — 2 300 ₽/м³</li>
          <li>Щебень фракции 20-40 — 2 650 ₽/м³</li>
        </ul>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/shcheben/20-40/",
            name="Щебень фракции 20-40",
            target_unit="м3",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 2650)

    def test_material_table_binds_cube_price_to_its_column(self) -> None:
        page = """
        <h1>Щебень гранитный в Ярославле</h1>
        <table>
          <thead><tr><td>Материал</td><td>Цена за м3 (куб)</td><td>Цена за тонну</td></tr></thead>
          <tbody>
            <tr itemscope itemtype="http://schema.org/Product">
              <td itemprop="name">Щебень гранитный (фр. 20-40)</td>
              <td itemprop="description">От 2400 руб</td>
              <td itemprop="offers"><meta itemprop="price" content="1500">От 1500 руб</td>
            </tr>
          </tbody>
        </table>
        """
        result = inspect_source_page(
            page,
            "https://supplier.example/scheben-granitniy/",
            name="Щебень гранитный 20-40",
            target_unit="м3",
            position_bucket="materials",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.price, 2400)
        self.assertEqual(result.unit, "м3")
        self.assertEqual(result.extractor, "table-row")


if __name__ == "__main__":
    unittest.main()
