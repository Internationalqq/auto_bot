import json

import pytest

from autobot.market_strategy import build_search_plan, check_offer, market_query_name
from autobot.market_requirements import technical_conflict
from autobot.market_price_index import build_price_identity
from autobot.market_contract import offers_for_row


@pytest.mark.parametrize('name,unit,kind,fragments', [
    ('Щебень из плотных горных пород фракция 20-40 мм', 'м3', 'material', ['20-40', 'плотных']),
    ('Камень бортовой бетонный БР 100.30.15', 'шт', 'material', ['БР 100.30.15']),
    ('Кабель силовой ВВГнг-LS (3х2,5 мм2)', 'м', 'material', ['ВВГнг-LS', '3х2,5']),
    ('Штукатурка гипсовая КНАУФ Ротбанд (30 кг)', 'шт', 'material', ['Ротбанд', '30 кг']),
    ('Пена монтажная 750 мл', 'шт', 'material', ['750 мл']),
    ('Георешетка полиэтиленовая высотой 150 мм', 'м2', 'material', ['полиэтиленовая', '150 мм']),
    ('Монтаж кабеля ВВГнг-LS 3х2,5 в лотках', '100 м', 'work', ['ВВГнг-LS', '3х2,5']),
    ('Аренда экскаватора 20 т', 'маш.-ч', 'service', ['20 т']),
    ('Штукатурка стен гипсовым раствором', '100 м2', 'work', ['стен']),
    ('Щит с монтажной панелью 800х600х250 IP54', 'шт', 'product', ['800х600х250', 'IP54']),
])
def test_request_keeps_identity_and_routes_to_the_correct_market(name, unit, kind, fragments):
    plan = build_search_plan(name, unit, region='Ярославль')
    assert plan.position.slug == kind
    assert plan.can_auto_price
    assert plan.requirements['original_name'] == name
    assert plan.requirements['original_unit'] == unit
    assert plan.requirements['can_search']
    assert not plan.requirements['issues']
    json.dumps(plan.requirements, ensure_ascii=False, allow_nan=False)
    for query in plan.queries:
        assert 'Ярославль' in query
        assert all(fragment.casefold() in query.casefold() for fragment in fragments)


def test_section_does_not_turn_a_product_into_installation_or_transport():
    for section in ('Монтажные работы', 'Доставка и погрузка'):
        assert build_search_plan('Кабель ВВГнг-LS 3х2,5', 'м', section=section).position.bucket == 'materials'
    assert build_search_plan('Штукатурка гипсовая', 'м2', basis_code='ГЭСН15').position.slug == 'work'


@pytest.mark.parametrize('unit', ['', '1.0', '—'])
def test_unknown_unit_is_a_visible_problem_and_never_assumed_to_be_a_piece(unit):
    plan = build_search_plan('Кабель ВВГнг-LS 3х2,5', unit)
    assert not plan.can_auto_price and not plan.requirements['can_search']
    assert not plan.requirements['normalized_unit']
    assert plan.requirements['issues']


def test_generic_material_does_not_acquire_unwritten_properties():
    assert 'гранит' not in market_query_name('Щебень из плотных горных пород фракция 20-40 мм')
    assert 'композит' not in market_query_name('Георешетка полиэтиленовая высотой 150 мм')
    assert 'иглопробив' not in market_query_name('Геотекстиль тканый 200 г/м2')


def test_technical_requirement_at_end_of_long_title_survives_shortening():
    name = 'Кабель ' + 'описание ' * 20 + 'ВВГнг-LS (3х2,5 мм2)'
    query = market_query_name(name)
    assert '3х2,5' in query and 'ВВГнг-LS' in query


@pytest.mark.parametrize('wanted,found', [
    ('Кабель ВВГнг-LS 3х2,5', 'Кабель ВВГнг-LS 3х1,5'),
    ('Кабель ВВГнг-LS 3х2,5', 'Кабель NYM 3х2,5'),
    ('Камень бортовой БР 100.30.15', 'Камень бортовой БР 100.20.8'),
    ('Щит 800х600х250 IP54', 'Щит 800х600х250 IP31'),
    ('Геотекстиль 300 г/м2', 'Геотекстиль 200 г/м2'),
    ('Штукатурка Ротбанд 30 кг', 'Штукатурка Ротбанд 25 кг'),
    ('Штукатурка Кнауф Ротбанд 30 кг', 'Штукатурка Церезит 30 кг'),
])
def test_wrong_variant_never_passes_due_to_shared_product_words(wanted, found):
    assert technical_conflict(wanted, found)
    result = check_offer(name=wanted, unit='шт', title=found, snippet=found + ' 1200 ₽/шт',
                         price=1200, url='https://supplier.example/product/123', page_checked=True,
                         source_unit='шт')
    assert result.status != 'verified'


def test_missing_dimensions_need_evidence_but_typographic_variants_match():
    assert technical_conflict('Кабель ВВГнг-LS 3х2,5', 'Кабель ВВГнг-LS')
    assert not technical_conflict('Кабель ВВГнг-LS 3х2,5', 'Кабель ВВГнг-LS 3 x 2.5')
    assert not technical_conflict('Кабель ВВГнг(А)-LS 3х2,5', 'Кабель ВВГнг(A)-LS 3x2.5')
    assert not technical_conflict('Кабель ВВГнг(А) 3х2,5', 'Кабель ВВГнг(A) 3x2.5')
    assert not technical_conflict('Штукатурка Кнауф Ротбанд 30 кг', 'Штукатурка Knauf Rotband 30 кг')
    assert not technical_conflict('Геотекстиль 300 г/м²', 'Геотекстиль 300 г/м2')
    assert technical_conflict('Кабель ВВГнг(А)-LS 3х2,5', 'Кабель ВВГнг(А) 3х2,5')
    assert 'БР' not in market_query_name('Камень бортовой гранитный БВ 100.30.15')
    assert 'бетонный' not in market_query_name('Камень бортовой гранитный БВ 100.30.15')


def test_different_required_sizes_have_different_reusable_price_identity():
    left = build_price_identity('Камень бортовой БР 100.30.15', 'шт', region='Ярославль')
    right = build_price_identity('Камень бортовой БР 100.20.8', 'шт', region='Ярославль')
    assert left.normalized_key != right.normalized_key


def test_electronic_models_cannot_match_another_device_in_same_catalogue():
    assert technical_conflict('Коммутатор TFortis SWU-16T','Коммутатор TFortis SWU-8T')
    assert not technical_conflict('Коммутатор TFortis SWU-16T','TFortis SWU-16T, 104100 руб/шт')


def test_article_discovery_is_short_but_original_requirements_stay_binding():
    name='Монтажная коробка Dahua DH-PFA136, размеры 110х34 мм'
    plan=build_search_plan(name,'шт',region='Ярославская область')
    assert plan.queries[0]=='"DH-PFA136" купить цена в рублях'
    assert 'Ярославская область' in plan.queries[1]
    assert any(s['value']=='110х34' for s in plan.requirements['specifications'])
    assert technical_conflict(name,'Dahua DH-PFA136, размеры 100х30 мм')
    lamp=build_search_plan('А777164 Светильник Spot-05-AF-54Вт, длинное описание AF0100003205','шт')
    assert lamp.queries[0]=='"AF0100003205" купить цена в рублях'
    assert technical_conflict('Клеммник WAGO 222-413','Клеммник WAGO 222-412')
    assert technical_conflict('Труба EKF tpndg-50','Труба EKF tpndg-25')
    assert technical_conflict('Камера Dahua DH-IPC-HDBW3441FP-AS-0280B-S2','Dahua DH-IPC-HDBW3441FP-AS-0360B-S2')


def test_exact_model_does_not_require_generic_product_words():
    result = check_offer(name='IP-камера Dahua DH-IPC-HDBW3441FP-AS-0280B-S2', unit='шт',
                         title='Dahua DH-IPC-HDBW3441FP-AS-0280B-S2', snippet='22021,80 руб/шт',
                         price=22021.8, url='https://supplier.example/product/123', page_checked=True, source_unit='шт')
    assert result.status == 'verified'
    assert technical_conflict('Лента сигнальная ЛСЭ-300', 'Лента сигнальная ЛСЭ-150')


def test_native_product_and_normative_adjustment_have_separate_search_routes():
    assert build_search_plan('Бордюрный камень 100.20.8 Тиманфайа', 'м').position.slug == 'material'
    assert build_search_plan('Монтажная коробка Dahua DH-PFA136', 'шт').position.slug == 'product'
    plan = build_search_plan('За каждые последующие 500 м испытания кабеля добавлять к норме', '100 м', basis_code='ГЭСНп01')
    assert plan.position.slug == 'aggregate' and not plan.can_auto_price


def test_fineness_module_does_not_supply_an_unwritten_sand_class():
    assert not technical_conflict('Песок мелкий', 'Песок, модуль крупности 1,5–2,0')
    assert technical_conflict('Песок I класса мелкий', 'Песок, модуль крупности 1,5–2,0')
    assert technical_conflict('Песок мелкий', 'Песок, модуль крупности 1–1,5')


def test_concrete_grade_does_not_turn_bulk_concrete_into_finished_kerbs():
    result=check_offer(name='Камни бортовые бетонные марки БР, БВ, бетон В22,5 (М300)', unit='м3',
                       title='Бетон М300 В22,5', snippet='Бетон М300 В22,5 4720 руб/м3', price=4720,
                       url='https://supplier.example/concrete/m300',page_checked=True,source_unit='м3')
    assert result.status == 'candidate' and 'бортового камня' in result.reason


def test_explicit_concrete_durability_is_part_of_the_requested_variant():
    assert technical_conflict('Бетон В20 F(1)150 W6 на гравии','Бетон В20 F150 W4 на гравии')
    assert technical_conflict('Бетон В20 F(1)100 W4 на гравии','Бетон В20 на гравии')
    assert not technical_conflict('Бетон В20 F(1)150 W6 на гравии','Бетон В20 F(1)150 W6 на гравии')
    assert technical_conflict('Кабель АВБШв 4х16ок(N)-660','Кабель АВБШв 4х120ос(N)-660').startswith('Не совпадает')
    assert technical_conflict('Щебень М1200 20-40 мм','Щебень 20-40 мм')
    assert technical_conflict('Песок I класс мелкий','Песок карьерный')
    assert not technical_conflict('Песок I класс мелкий','Песок мелкозернистый 1 класса')


def test_stored_wrong_variant_is_rechecked_when_report_is_opened():
    from datetime import datetime, timezone
    offer = {'title': 'Кабель ВВГнг-LS 3х1,5', 'price': 120, 'unit': 'м', 'matched_unit': 'м',
             'url': 'https://supplier.example/product/123', 'verification': 'verified',
             'page_checked': True, 'observed_at': datetime.now(timezone.utc).isoformat(),
             'evidence': 'Кабель ВВГнг-LS 3х1,5, 120 ₽/м'}
    row = {'Название работы/услуги': 'Кабель ВВГнг-LS 3х2,5', 'Ед. изм.': 'м',
           'Цена-сайт-телефон (json)': json.dumps([offer], ensure_ascii=False)}
    assert offers_for_row(row)[0]['verification'] == 'candidate'
