from __future__ import annotations

import json

from autobot.tender_detail import _display_unit, _parse_bundle, _verdict


def _bundle(price: float) -> str:
    return json.dumps(
        [
            {
                "source": "Интернет",
                "title": "Нанесение дорожной разметки 189 руб. за м2",
                "price": price,
                "url": "https://supplier.example/services/marking",
                "verification": "verified",
                "verification_reason": "Совпали позиция и единица",
                "evidence": "Цена нанесения разметки — 189 руб. за м2",
            }
        ],
        ensure_ascii=False,
    )


def test_existing_extreme_offer_is_not_displayed_as_verified() -> None:
    sources = _parse_bundle(
        _bundle(189),
        estimate_price=18.23,
        name="Нанесение дорожной разметки",
        unit="м2",
    )

    assert len(sources) == 1
    assert sources[0]["verified"] is False
    assert sources[0]["plausibility"] == "extreme"
    assert "Аномальный масштаб" in sources[0]["reason"]


def test_market_and_estimate_are_displayed_in_same_block() -> None:
    sources = _parse_bundle(
        _bundle(180),
        estimate_price=84_518,
        name="Разработка грунта с погрузкой в траншеях 1000",
        unit="м3",
    )

    assert _display_unit("Разработка грунта с погрузкой в траншеях 1000", "м3") == "1000 м³"
    assert sources[0]["comparison_price"] == 180_000
    assert sources[0]["verified"] is True


def test_verdict_uses_a_wider_real_market_band() -> None:
    assert _verdict(1_000, 1_200)[0] == "Сопоставимо со сметой"
    assert _verdict(1_000, 2_000)[0] == "Выше сметы"
