from __future__ import annotations

import json
import time
from pathlib import Path

import autobot.real_market_scraper as market
import pandas as pd


def _offer(url: str = "https://supplier.example/item") -> market.MarketOffer:
    return market.MarketOffer(
        source="Интернет",
        title="Тротуарная плитка 200×100×60",
        price=1250.0,
        url=url,
    )


def test_search_cache_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(market, "_MARKET_CACHE_DIR", tmp_path)
    market._save_search_cache("web", "тротуарная плитка", "Ярославль", [_offer()])

    cached = market._load_search_cache("web", "тротуарная плитка", "Ярославль")

    assert cached is not None
    assert len(cached) == 1
    assert cached[0].price == 1250.0
    assert cached[0].url == "https://supplier.example/item"


def test_avito_cache_keeps_card_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(market, "_MARKET_CACHE_DIR", tmp_path)
    offer = market.MarketOffer(
        source="Авито",
        title="Песок строительный",
        price=900.0,
        url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/pesok_1234567890",
        published_at="сегодня в 10:15",
        location="Ярославль",
    )

    market._save_search_cache("avito", "песок", "Ярославль", [offer])
    cached = market._load_search_cache("avito", "песок", "Ярославль")

    assert cached is not None
    assert cached[0].published_at == "сегодня в 10:15"
    assert cached[0].location == "Ярославль"


def test_avito_block_is_persistent(tmp_path: Path, monkeypatch) -> None:
    guard_path = tmp_path / "avito_guard.json"
    monkeypatch.setattr(market, "_AVITO_GUARD_PATH", guard_path)
    monkeypatch.setattr(market, "_AVITO_BLOCK_COOLDOWN_SEC", 3600)
    monkeypatch.setattr(market, "_AVITO_BLOCKED_UNTIL", 0.0)

    market._block_avito("HTTP 429")

    monkeypatch.setattr(market, "_AVITO_BLOCKED_UNTIL", 0.0)
    assert guard_path.is_file()
    assert "Авито на паузе" in market._avito_guard_message()


def test_avito_ip_block_has_clear_message() -> None:
    html = "<html><body><h1>Доступ ограничен: проблема с IP</h1><p>Нажмите Продолжить для решения капчи</p></body></html>"

    assert market._avito_block_reason(html) == "Авито ограничил доступ по IP/VPN"


class _FakeLocator:
    def __init__(self, page, *, cards: bool = False) -> None:
        self.page = page
        self.cards = cards

    @property
    def first(self):
        return self

    def count(self) -> int:
        return self.page.counts[self.page.index] if self.cards else 0

    def is_visible(self) -> bool:
        return False


class _FakeScrollPage:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.index = 0

    def locator(self, selector: str):
        return _FakeLocator(self, cards=selector == market.AVITO_CARD_SELECTOR)

    def evaluate(self, _script: str) -> None:
        if self.index < len(self.counts) - 1:
            self.index += 1

    def wait_for_timeout(self, _delay_ms: int) -> None:
        return None

    def wait_for_load_state(self, _state: str, timeout: int) -> None:
        return None


def test_avito_scroll_logs_card_growth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(market, "_AVITO_LOG_PATH", tmp_path / "avito.jsonl")
    fetcher = market.AvitoBrowserFetcher(enabled=True)
    fetcher.scroll_delay_min_sec = 0
    fetcher.scroll_delay_max_sec = 0
    fetcher.scroll_target_cards = 20
    fetcher.max_scrolls = 10
    page = _FakeScrollPage([3, 8, 25])

    fetcher._scroll_search_results(page, "https://www.avito.ru/all?q=песок")

    assert fetcher.last_scroll_counts == [3, 8, 25]
    log_text = (tmp_path / "avito.jsonl").read_text(encoding="utf-8")
    assert '"cards": 3' in log_text
    assert '"cards": 25' in log_text


def test_probe_export_contains_required_avito_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(market, "REPORTS_DIR", tmp_path)
    offer = market.MarketOffer(
        source="Авито",
        title="Щебень 20–40",
        price=1800.0,
        url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/scheben_1234567890",
        published_at="вчера в 18:10",
        location="Ярославль",
    )

    csv_path, xlsx_path = market._export_probe_offers("щебень 20 40", [offer])

    assert csv_path.is_file()
    assert xlsx_path.is_file()
    exported = pd.read_csv(csv_path)
    assert exported.loc[0, "Название"] == "Щебень 20–40"
    assert exported.loc[0, "Дата публикации"] == "вчера в 18:10"
    assert exported.loc[0, "Локация"] == "Ярославль"


def test_web_result_skips_avito(monkeypatch) -> None:
    calls = {"web": 0, "avito": 0}

    def fake_web(query: str, *, region: str = "", max_results: int = 3):
        calls["web"] += 1
        return [_offer()]

    def fake_avito(*args, **kwargs):
        calls["avito"] += 1
        return [], ""

    monkeypatch.setattr(market, "_load_search_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(market, "_save_search_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(market, "search_web", fake_web)
    monkeypatch.setattr(market, "search_avito", fake_avito)

    offers, error = market.search_market(
        "тротуарная плитка",
        sources=["web", "avito"],
        max_results=3,
    )

    assert error == ""
    assert len(offers) == 1
    assert calls == {"web": 1, "avito": 0}


class _FakeAvitoBrowser:
    enabled = True
    last_error = ""

    def __init__(self, html: str, offers: list[market.MarketOffer] | None = None) -> None:
        self.html = html
        self.offers = offers or []
        self.urls: list[str] = []

    def fetch(self, url: str) -> str:
        self.urls.append(url)
        return self.html

    def current_avito_offers(self, *, base_url: str, max_results: int):
        return self.offers[:max_results]


def test_avito_search_is_playwright_only(tmp_path: Path, monkeypatch) -> None:
    browser_offer = market.MarketOffer(
        source="Авито",
        title="Тротуарная плитка 200×100×60",
        price=1250.0,
        url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/plitka_1234567890",
    )
    browser = _FakeAvitoBrowser("<html><body>Авито</body></html>", [browser_offer])
    monkeypatch.setattr(market, "_MARKET_CACHE_DIR", tmp_path)
    monkeypatch.setattr(market, "_AVITO_GUARD_PATH", tmp_path / "guard.json")
    monkeypatch.setattr(market, "_AVITO_REQUEST_COUNT", 0)
    monkeypatch.setattr(market, "_AVITO_LAST_REQUEST_AT", 0.0)
    monkeypatch.setattr(market, "_AVITO_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(market, "_session_get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("requests must not be used for Avito")))

    offers, error = market.search_avito(
        "тротуарная плитка",
        region="Ярославль",
        max_results=3,
        browser_fetcher=browser,
    )

    assert error == ""
    assert [offer.url for offer in offers] == [browser_offer.url]
    assert len(browser.urls) == 1
    assert browser.urls[0].startswith("https://www.avito.ru/all?")


def test_avito_offer_page_is_checked_in_same_browser(tmp_path: Path, monkeypatch) -> None:
    page_html = """
    <html><head>
      <script type="application/ld+json">
        {"@type":"Product","name":"Тротуарная плитка 200×100×60","offers":{"@type":"Offer","price":"1250","priceCurrency":"RUB","unitText":"м2"}}
      </script>
    </head><body>Тротуарная плитка 200×100×60, 1 250 ₽ за м2</body></html>
    """
    browser = _FakeAvitoBrowser(page_html)
    monkeypatch.setattr(market, "_AVITO_GUARD_PATH", tmp_path / "guard.json")
    monkeypatch.setattr(market, "_AVITO_REQUEST_COUNT", 0)
    monkeypatch.setattr(market, "_AVITO_LAST_REQUEST_AT", 0.0)
    monkeypatch.setattr(market, "_AVITO_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(market, "_session_get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("requests must not be used for Avito")))
    plan = market.build_search_plan("Тротуарная плитка 200×100×60", "м2", "ФССЦ", "Материалы", "")
    offer = market.MarketOffer(
        source="Авито",
        title="Тротуарная плитка 200×100×60",
        price=1250.0,
        url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/plitka_1234567890",
    )

    checked = market._verify_offers(
        pd.Series({market.COL_NAME: "Тротуарная плитка 200×100×60", "Ед. изм.": "м2"}),
        [offer],
        plan,
        browser_fetcher=browser,
    )

    assert len(checked) == 1
    assert checked[0].page_checked is True
    assert checked[0].verification == "verified"
    assert checked[0].price == 1250.0
    assert browser.urls == [offer.url]


def test_saved_extreme_price_is_downgraded_without_network() -> None:
    bundle = [
        {
            "source": "Интернет",
            "title": "Нанесение разметки",
            "price": 189,
            "url": "https://supplier.example/services/marking",
            "verification": "verified",
            "confidence": 0.9,
        }
    ]
    frame = pd.DataFrame(
        [
            {
                market.COL_NAME: "Нанесение дорожной разметки",
                market.COL_UNIT_PRICE: 18.23,
                "Ед. изм.": "м2",
                "Цена-сайт-телефон (json)": json.dumps(bundle, ensure_ascii=False),
                "Проверенных источников": 1,
                "Цены за ед. (рынок, руб)": "189",
                "Медиана цена за ед. (рынок)": 189,
            }
        ]
    )

    checked, changed = market._revalidate_previous(frame)

    saved = json.loads(checked.iloc[0]["Цена-сайт-телефон (json)"])[0]
    assert changed == 1
    assert saved["verification"] == "candidate"
    assert saved["plausibility"] == "extreme"
    assert checked.iloc[0]["Проверенных источников"] == 0
    assert checked.iloc[0]["Непроверенных кандидатов"] == 1


def test_avito_guard_status_is_read_only_and_reports_remaining(tmp_path: Path, monkeypatch) -> None:
    guard_path = tmp_path / "avito_guard.json"
    blocked_until = time.time() + 7200
    guard_path.write_text(
        json.dumps(
            {
                "blocked_until": blocked_until,
                "reason": "Авито ограничил доступ по IP/VPN",
                "last_request_at": 123.5,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = guard_path.read_bytes()
    monkeypatch.setattr(market, "_AVITO_GUARD_PATH", guard_path)
    monkeypatch.setattr(market, "_AVITO_BLOCKED_UNTIL", 0.0)

    status = market.avito_guard_status()

    assert status["blocked"] is True
    assert 7100 <= int(status["remaining_seconds"]) <= 7200
    assert status["reason"] == "Авито ограничил доступ по IP/VPN"
    assert guard_path.read_bytes() == before


def test_avito_collect_only_never_opens_listing_page(monkeypatch) -> None:
    name = "Тротуарная плитка 200×100×60"
    plan = market.build_search_plan(name, "м2", "ФССЦ", "Материалы", "")
    offer = market.MarketOffer(
        source="Авито",
        title=name,
        price=1250.0,
        url="https://www.avito.ru/yaroslavl/remont_i_stroitelstvo/plitka_1234567890",
        snippet="Тротуарная плитка 200×100×60, цена 1250 ₽ за м2, Ярославль",
        location="Ярославль",
        published_at="сегодня",
    )
    monkeypatch.setattr(
        market,
        "_enrich_offer_from_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("listing page must stay closed")),
    )

    checked = market._verify_offers(
        pd.Series({market.COL_NAME: name, "Ед. изм.": "м2", "basis_code": "ФССЦ", "Раздел": "Материалы", market.COL_UNIT_PRICE: 1200}),
        [offer],
        plan,
        avito_collect_only=True,
    )

    assert len(checked) == 1
    assert checked[0].page_checked is False
    assert checked[0].verification == "candidate"
    assert "без открытия объявления" in checked[0].verification_reason


def test_verified_market_keys_only_returns_rows_with_verified_sources() -> None:
    frame = pd.DataFrame(
        [
            {market.COL_NAME: "Песок строительный", "Проверенных источников": 1},
            {market.COL_NAME: "Щебень 20-40", "Проверенных источников": 0},
        ]
    )

    assert market._verified_market_keys(frame) == {market._norm_key("Песок строительный")}


def test_avito_only_pass_can_restore_non_avito_evidence() -> None:
    name = "Щебень 20-40"
    bundle = json.dumps(
        [
            {
                "source": "Интернет",
                "title": "Щебень гранитный 20-40",
                "price": 1800,
                "url": "https://supplier.example/scheben",
                "verification": "candidate",
            },
            {
                "source": "Авито",
                "title": "Старое объявление",
                "price": 1500,
                "url": "https://www.avito.ru/item/123",
                "verification": "candidate",
            },
        ],
        ensure_ascii=False,
    )
    frame = pd.DataFrame([{market.COL_NAME: name, "Цена-сайт-телефон (json)": bundle}])

    restored = market._saved_offers_for_key(frame, market._norm_key(name))
    non_avito = [offer for offer in restored if "avito.ru" not in offer.url]

    assert len(restored) == 2
    assert len(non_avito) == 1
    assert non_avito[0].url == "https://supplier.example/scheben"


def test_avito_safe_mode_only_stops_on_access_failure() -> None:
    assert market._avito_safe_error_is_fatal("Авито ограничил доступ по IP/VPN") is True
    assert market._avito_safe_error_is_fatal("Авито Playwright: TimeoutError") is True
    assert market._avito_safe_error_is_fatal(
        "Авито Playwright: страница получена, но пригодных объявлений с ценой не найдено"
    ) is False


def test_source_page_cache_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(market, "_SOURCE_PAGE_CACHE_DIR", tmp_path)
    url = "https://supplier.example/product"

    market._save_source_page_cache(url, page_html="<html>Цена 1200 руб.</html>", method="playwright")
    cached = market._load_source_page_cache(url)

    assert cached is not None
    assert cached[0] == "<html>Цена 1200 руб.</html>"
    assert cached[1] == ""
    assert cached[2] == "playwright"


def test_source_page_uses_limited_browser_after_http_error(tmp_path: Path, monkeypatch) -> None:
    class _SourceBrowser:
        enabled = True
        last_error = ""

        def fetch_source_page(self, url: str) -> str:
            assert url == "https://supplier.example/product"
            return "<html><body>Щебень 20-40 — 1800 руб. за м3</body></html>"

    monkeypatch.setattr(market, "_SOURCE_PAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(market, "_session_get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 403")))

    page, error, method = market._fetch_source_page(
        "https://supplier.example/product",
        timeout=5,
        browser_fetcher=_SourceBrowser(),
    )

    assert "1800 руб" in page
    assert error == ""
    assert method == "playwright"


def test_bing_rss_is_combined_with_independent_search(monkeypatch) -> None:
    rss = """
    <rss><channel><item>
      <title>Щебень 20-40 — 1800 руб. за м3</title>
      <link>https://supplier.example/catalog/scheben-20-40</link>
      <description>Купить щебень 20-40 с доставкой, цена 1800 руб. за м3</description>
    </item></channel></rss>
    """
    monkeypatch.setattr(market, "_session_get", lambda url, timeout=25: rss)
    independent = market.MarketOffer(
        source="Интернет",
        title="Щебень гранитный 20-40 — 1950 руб. за м3",
        price=1950,
        url="https://second-supplier.example/product/scheben-20-40",
        snippet="Щебень 20-40, прайс 1950 руб. за м3",
        discovery_engine="DDGS/brave",
    )
    monkeypatch.setattr(market, "_search_web_ddgs", lambda *args, **kwargs: ([independent], ""))
    monkeypatch.setattr(market, "_search_web_searx", lambda *args, **kwargs: ([], ""))

    offers = market.search_web("щебень 20-40 за м3", max_results=2)

    assert len(offers) == 2
    assert {offer.url for offer in offers} == {
        "https://supplier.example/catalog/scheben-20-40",
        "https://second-supplier.example/product/scheben-20-40",
    }
    assert {offer.discovery_engine for offer in offers} == {"Bing RSS", "DDGS/brave"}


def test_offer_dedupe_ignores_search_tracking_parameters() -> None:
    first = _offer("https://supplier.example/item?utm_source=google&srsltid=abc")
    second = _offer("https://supplier.example/item?srsltid=def")
    second.price = 1200

    offers = market._dedupe_and_sort([first, second], max_results=5)

    assert len(offers) == 1
    assert offers[0].url == "https://supplier.example/item"
    assert offers[0].price == 1200
