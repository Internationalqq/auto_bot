import pytest
from autobot.pdf_estimate_adapter import PdfEstimateAdapter, normalize_source_unit
from test_pdf_position_integrity import words_for_position


def token(text,left,top=100,page=1):
    return {'text':text,'left':left,'top':top,'width':20,'height':15,'page':page}


def parse(words,secondary=None):
    return PdfEstimateAdapter._position_records_from_words(words,1000,section_words=secondary)[0]


def test_multiline_title_keeps_line_order_and_physical_money():
    words=[word for word in words_for_position() if word['left']!=220]
    words += [token('Щебень',220,100),token('из горных',280,98),token('пород,',345,101),
              token('фракция',220,125),token('20–40 мм',300,124)]
    row=parse(words)
    assert row['name']=='Щебень из горных пород, фракция 20–40 мм'
    assert row['position_id']=='pdf:ocr:1:100:100' and row['total']==2000 and row['qty']==.2


def test_normative_explanation_does_not_join_the_position_title():
    words=words_for_position()
    words += [token('321/пр_2025_прил.5',100,142),token('в стеснённых условиях',280,130)]
    row=parse(words)
    assert row['name']=='Демонтаж облицовки откосов' and row['total']==2000
    assert any(word['text']=='в стеснённых условиях' for word in words)


def test_title_continuation_before_reference_stays_in_its_own_row():
    words=words_for_position()
    words += [token('с сохранением плит',220,125),token('321/пр_2025_прил.5',100,164),
              token('Условия выполнения',220,155)]
    assert parse(words)['name']=='Демонтаж облицовки откосов с сохранением плит'


def test_third_title_line_keeps_scope_but_stops_before_quantity_formula():
    words=words_for_position()
    words += [token('площадью отдельных мест',220,130),token('до 5 м2 толщиной до 20 мм',220,160),
              token('Объем=(0,5*3,8*13) / 100',220,190),token('Соседняя строка',220,220)]
    row=parse(words)
    assert row['name']=='Демонтаж облицовки откосов площадью отдельных мест до 5 м2 толщиной до 20 мм'
    assert row['qty']==.2 and row['total']==2000


def test_secondary_observation_recovers_only_the_missing_unit():
    words=[word for word in words_for_position() if word['left'] not in (395,425)]
    row=parse(words,[token('кг',425,102),token('9900',570,100)])
    assert row['unit']=='кг' and row['qty']==.2 and row['total']==2000


@pytest.mark.parametrize('secondary',[
    [token('кг',425,100,page=2)], [token('кг',570)], [token('кг',425,200)],
    [token('кг',425),token('м3',430,105)], [token('ма',425)],
])
def test_other_cells_or_ambiguous_ocr_cannot_fill_a_missing_unit(secondary):
    words=[word for word in words_for_position() if word['left'] not in (395,425)]
    assert parse(words,secondary)['unit']==''


@pytest.mark.parametrize('prefix_present',[True,False])
def test_secondary_unit_preserves_printed_scale_once(prefix_present):
    words=[word for word in words_for_position() if word['left']!=425 and (prefix_present or word['left']!=395)]
    row=parse(words,[token('100',395),token('м2',425)])
    assert normalize_source_unit(row['unit'])=='100 м2' and row['qty']==.2


def test_dimension_in_description_does_not_replace_unit_from_its_column():
    words=[word for word in words_for_position() if word['left'] not in (220,395)]
    words += [token('Труба длиной',220),token('3',320),token('м',345)]
    row=parse(words)
    assert row['name']=='Труба длиной 3 м' and row['unit']=='м2'


def test_existing_unit_is_not_overwritten_by_a_second_pass():
    assert parse(words_for_position(),[token('т',425)])['unit']=='100 м2'


def test_conflicting_primary_units_are_not_resolved_by_guessing():
    words=words_for_position()+[token('кг',430,110)]
    assert parse(words,[token('м2',425)])['unit']==''


@pytest.mark.parametrize('secondary',[True,False])
def test_thousand_scale_is_retained_in_either_ocr_pass(secondary):
    words=[word for word in words_for_position() if word['left'] not in (395,425)]
    units=[token('1000',395),token('м3',425)]
    row=parse(words,units) if secondary else parse(words+units)
    assert normalize_source_unit(row['unit'])=='1000 м3' and row['qty']==.2
