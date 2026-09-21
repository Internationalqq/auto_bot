import json
from datetime import datetime, timezone

import pytest

from autobot.work_requirements import work_passport, work_match_reason
from autobot.market_strategy import market_query_name, check_offer, build_search_plan
from autobot.market_contract import offers_for_row, BUNDLE_COLUMN
from autobot.market_analytics import COL_NAME, COL_UNIT


@pytest.mark.parametrize('name,expected', [
    ('Покрытие кабеля в траншее лентой сигнальной', 'укладка сигнальной ленты'),
    ('Устройство прослойки из нетканого синтетического материала (НСМ)', 'укладка геотекстиля'),
    ('Устройство бетонной подготовки', 'устройство бетонной подготовки'),
    ('Погрузка в автотранспорт грунт растительного слоя', 'погрузка грунта'),
])
def test_queries_preserve_the_work_instead_of_searching_for_a_product(name, expected):
    assert expected in market_query_name(name, 'work')


@pytest.mark.parametrize('name,source', [
    ('Устройство бетонной подготовки', 'Бетон М100 3800 руб/м3'),
    ('Покрытие кабеля в траншее лентой сигнальной', 'Лента сигнальная ЛСЭ 150 руб/м'),
    ('Измерение сопротивления изоляции кабеля до 1 кВ', 'Измеритель сопротивления изоляции 2000 руб/шт'),
    ('Укладка брусчатки', 'Брусчатка серая 200х100х60 900 руб/м2'),
    ('Монтаж опор освещения', 'Монтаж светильников 600 руб/шт'),
])
def test_product_or_other_operation_is_not_the_requested_work(name, source):
    assert work_match_reason(name, source)


def test_service_alias_matches_without_losing_manual_method():
    name='Посев газонов обыкновенных вручную'
    assert work_match_reason(name, 'Посев газона вручную 100 руб/м2') == ''
    assert 'ручной' in work_match_reason(name, 'Посев газона 100 руб/м2')
    assert work_match_reason(name, 'Посев газона механизированный 100 руб/м2')


def test_voltage_bound_and_mounting_method_are_required():
    name='Испытание кабеля силового напряжением до 1 кВ'
    assert work_match_reason(name, 'Испытание силового кабеля до 1 кВ') == ''
    assert work_match_reason(name, 'Испытание силового кабеля до 10 кВ')
    assert work_match_reason('Установка опор наружного освещения металлических фланцевых',
                             'Монтаж опор освещения')


def test_generic_trench_service_cannot_use_corrugated_pipe_rate():
    assert work_match_reason('Прокладка кабеля в траншее', 'Прокладка кабеля в гофре')


def test_excavator_rate_keeps_bucket_and_soil_group():
    name='Разработка грунта экскаватором с ковшом вместимостью 0,25 м3, группа грунтов: 2'
    assert work_match_reason(name, 'Разработка грунта экскаватором')
    assert work_match_reason(name, 'Разработка грунта 1 группы экскаватором с ковшом 0,25 м3')
    assert not work_match_reason(name, 'Разработка грунта 2 группы экскаватором с ковшом 0,25 м3')


def test_diameter_label_inflection_does_not_change_explicit_dimension():
    assert not work_match_reason('Монтаж вертикального заземлителя диаметром 16 мм',
                                 'Монтаж вертикального заземлителя, диаметр: 16 мм')
    assert work_match_reason('Монтаж вертикального заземлителя диаметром 16 мм',
                             'Монтаж вертикального заземлителя диаметром 20 мм')


def test_supplier_height_limit_cannot_be_ignored_for_unspecified_height():
    assert work_match_reason('Монтаж видеокамеры', 'Монтаж видеокамеры на высоте до 3 м')


def test_search_passport_exposes_service_requirements():
    plan=build_search_plan('Посев газонов вручную','100 м2','ГЭСН47-01-001','', 'Ярославская область')
    assert plan.requirements['work']['operation']=='lawn'
    assert plan.requirements['work']['constraints'][0]['kind']=='manual'


def test_exact_alias_still_requires_direct_checked_price_and_unit():
    args=dict(name='Устройство прослойки из нетканого синтетического материала (НСМ)',
              unit='1000 м2', basis_code='ГЭСН27-04-016',
              title='Укладка геотекстиля',snippet='Укладка геотекстиля 80 руб/м2',
              url='https://supplier.example/price',price=80,page_checked=True,source_unit='м2')
    assert check_offer(**args).status=='verified'
    assert check_offer(**(args|{'page_checked':False})).status=='candidate'
    assert check_offer(**(args|{'source_unit':'м3','snippet':'Укладка геотекстиля 80 руб/м3'})).status=='candidate'


def test_saved_work_price_is_revalidated_against_operation():
    offer={'price':80,'title':'Геотекстиль','evidence':'Геотекстиль 80 руб/м2',
           'url':'https://supplier.example/product','verification':'verified','matched_unit':'м2',
           'observed_at':datetime.now(timezone.utc).isoformat(),'price_scope':'product'}
    row={COL_NAME:'Укладка геотекстиля',COL_UNIT:'м2',BUNDLE_COLUMN:json.dumps([offer])}
    result=offers_for_row(row)[0]
    assert result['verification']=='candidate'
    assert 'операцию' in result['verification_reason']


def test_unknown_operation_does_not_claim_a_match():
    assert work_match_reason('Специальная операция по проекту', 'Работы 100 руб') is None


def test_noun_first_norm_searches_installation_but_seller_noun_stays_product():
    name='Заземлитель вертикальный из круглой стали диаметром 16 мм'
    plan=build_search_plan(name,'10 шт','ГЭСНм08-02-471-04')
    assert 'монтаж вертикального заземлителя' in plan.queries[0]
    assert plan.requirements['work']['operation']=='vertical-earth'
    assert work_match_reason(name,name,declared_work=True)
    assert work_match_reason(name,'Монтаж вертикального заземлителя диаметром 16 мм',declared_work=True)==''
    assert work_passport(name)['operation']==''


def test_surge_protector_is_equipment_despite_word_device():
    plan=build_search_plan('УЗИП (Устройство защиты от импульсных перенапряжений) РИФ-Э-I+II 275/12,5','шт')
    assert plan.position.bucket=='materials'
