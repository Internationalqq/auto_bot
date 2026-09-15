from pathlib import Path
import json
from datetime import datetime, timezone

import pytest

from autobot import main, pdf_estimate_adapter as adapter
from autobot.market_contract import BUNDLE_COLUMN, merge_market_frames, position_identity


def words_for_position(*, top=100, page=1, position='1', quantity='0.2', base_quantity=None):
    cells = [(20, position), (100, 'ГЭСН15-01-050-04'), (220, 'Демонтаж облицовки откосов'),
             (395, '100'), (425, 'м2'), (930, '2000')]
    if quantity is not None: cells.append((570, quantity))
    if base_quantity is not None: cells.append((480, base_quantity))
    return [{'text':text, 'left':x, 'top':top, 'width':20, 'height':15, 'page':page} for x,text in cells]


def source_records():
    return adapter.PdfEstimateAdapter._position_records_from_words([
        *words_for_position(), *words_for_position(top=220, position='2'),
        *words_for_position(top=1600, page=2, position='1')], 1000)


def tender():
    return main.Tender('12345678', 'Смета', '', '', '', 100000, None)


def test_three_physical_pdf_positions_keep_original_block_and_prices(monkeypatch):
    records = source_records()
    monkeypatch.setattr(adapter, 'pdf_to_position_records', lambda path: records)
    rows = main._rows_from_pdf_adapter(Path('ЛСР.pdf'), tender())
    assert len(rows) == 3
    assert [row['unit'] for row in rows] == ['100 м2'] * 3
    assert [row['qty'] for row in rows] == [.2] * 3
    assert [row['unit_price_rub'] for row in rows] == [10000] * 3
    assert [row['price_from_estimate_rub'] for row in rows] == [2000] * 3
    assert len({row['position_id'] for row in rows}) == 3
    frame = main._build_tender_clean_df(rows + [dict(rows[0])])
    assert len(frame) == 3 and frame['Сумма, руб'].sum() == 6000
    assert len({position_identity(row) for row in frame.to_dict('records')}) == 3
    saved = frame.copy()
    saved[BUNDLE_COLUMN] = [json.dumps([{'price':price, 'verification':'verified',
        'url':f'https://supplier.example/item/{index}', 'matched_unit':'м2',
        'observed_at':datetime.now(timezone.utc).isoformat()}]) for index,price in enumerate((80,90,100))]
    joined = merge_market_frames(frame, saved.iloc[::-1])
    assert joined['Рынок цены за ед. (итог)'].tolist() == ['80', '90', '100']
    from autobot.tender_viability import compute_viability_stats
    assert compute_viability_stats(joined).comparable_market_total == 5400


@pytest.mark.parametrize('base_quantity', [None, '0.1'])
def test_blank_final_pdf_quantity_stays_unknown(base_quantity):
    records = adapter.PdfEstimateAdapter._position_records_from_words(
        words_for_position(quantity=None, base_quantity=base_quantity), 1000)
    assert len(records) == 1 and records[0]['total'] == 2000
    assert records[0]['qty'] is None
    assert records[0]['unit_price'] is None


def test_same_ocr_anchor_is_removed_but_different_positions_survive():
    words = words_for_position()
    records = adapter.PdfEstimateAdapter._position_records_from_words(words + [dict(words[1])], 1000)
    assert len(records) == 1
    assert len({row['position_id'] for row in source_records()}) == 3


@pytest.mark.parametrize('unit,expected', [('компл','компл'), ('м','м'), ('1000м3','1000 м3'), ('т','т')])
def test_pdf_units_do_not_guess_composition_or_dimension(unit, expected):
    assert main._normalize_pdf_unit_qty(unit, 2, 'Бетонная смесь', 'ФСБЦ-11-02-03') == (expected, 2)


def test_repeated_text_pdf_lines_remain_distinct(monkeypatch):
    monkeypatch.setattr(main, '_rows_from_pdf_adapter', lambda *args: [])
    monkeypatch.setattr(main, '_iter_pdf_lines', lambda path: ['Монтаж бордюров 45500', 'Монтаж бордюров 45500'])
    rows = main.extract_rows_from_pdf(Path('смета.pdf'), tender())
    assert len(rows) == 2 and rows[0]['position_id'] != rows[1]['position_id']
    assert len(main._build_tender_clean_df(rows)) == 2


def test_uploaded_pdf_retains_original_units_repetitions_and_unknown_quantity(monkeypatch):
    from autobot import estimate_excel_analysis as analysis
    records = source_records() + adapter.PdfEstimateAdapter._position_records_from_words(
        words_for_position(top=2000, page=2, position='4', quantity=None), 1000)
    monkeypatch.setattr(adapter, 'pdf_to_position_records', lambda path, **kwargs: records)
    rows = analysis._read_pdf_sparse_position_rows(Path('ЛСР.pdf'))
    assert len(rows) == 4
    assert [row.unit for row in rows] == ['100 м2'] * 4
    assert [row.qty for row in rows] == [.2, .2, .2, None]
    assert [row.unit_price for row in rows] == [10000,10000,10000,None]
    assert sum(row.total for row in rows) == 8000


def test_unknown_quantity_reaches_report_and_explicit_document_warning(tmp_path, monkeypatch):
    from autobot import estimate_parse_worker as worker, estimate_publication as publication
    from test_estimate_parse_pipeline import setup
    paths, source, value = setup(tmp_path)
    record = adapter.PdfEstimateAdapter._position_records_from_words(words_for_position(quantity=None), 1000)[0]
    monkeypatch.setattr(adapter, 'pdf_to_position_records', lambda path: [record])
    raw = main._rows_from_pdf_adapter(source, value)
    monkeypatch.setattr(main, 'extract_rows_from_excel', lambda *args, **kwargs: raw)
    parsed = worker.parse_files([source], [], value, worker.ParseLimits())
    assert parsed['documents'][0]['missing_quantity_rows'] == 1
    monkeypatch.setattr(publication, 'run_parser', lambda *args: parsed)
    frame = publication.parse_and_publish(value, [source], [], [source], paths)[2]
    assert len(frame) == 1 and frame['Кол-во'].isna().all()
    status = publication.display_status(paths['reports'], value.tender_id)
    assert status['state'] == 'complete' and 'не определены количество' in ' '.join(status['warnings'])


def test_last_position_uses_its_own_total_before_whole_estimate_summary():
    words = [word for word in words_for_position() if word['left'] != 930]
    def line(top, label, amount):
        return [{'text':text,'left':left,'top':top,'width':20,'height':15,'page':1}
                for left,text in [(220,label),(930,amount)]]
    words += line(200, 'Всего по позиции', '2000')
    words += line(300, 'Итого прямые затраты', '15000')
    words += line(420, 'ВСЕГО по смете', '25000')
    row = adapter.PdfEstimateAdapter._position_records_from_words(words, 1000)[0]
    assert row['total'] == 2000 and row['unit_price'] == 10000


def test_indented_resource_does_not_split_or_double_parent_position_total():
    words = words_for_position(position='11')
    child = words_for_position(top=200, position='11.1')
    next(word for word in child if word['left']==100)['left'] = 125
    words += child
    words += [{'text':text,'left':left,'top':300,'width':20,'height':15,'page':1}
              for left,text in [(220,'Всего по позиции'),(930,'4000')]]
    words += words_for_position(top=450, position='12')
    rows = adapter.PdfEstimateAdapter._position_records_from_words(words, 1000)
    assert len(rows) == 2
    assert rows[0]['total'] == 4000 and rows[0]['unit_price'] == 20000
    assert len(rows[0]['resources']) == 1 and rows[0]['resources'][0]['total'] == 2000
    assert rows[0]['resources'][0]['parent_position_id'] == rows[0]['position_id']
    assert sum(row['total'] for row in rows) == 6000


def test_pdf_row_without_amount_is_retained_for_review_with_explicit_warning(tmp_path, monkeypatch):
    from autobot import estimate_parse_worker as worker, estimate_publication as publication
    from test_estimate_parse_pipeline import setup
    paths, source, value = setup(tmp_path)
    words = words_for_position() + [word for word in words_for_position(top=250, position='2') if word['left']!=930]
    records = adapter.PdfEstimateAdapter._position_records_from_words(words, 1000)
    monkeypatch.setattr(adapter, 'pdf_to_position_records', lambda path: records)
    raw = main._rows_from_pdf_adapter(source, value)
    assert len(raw) == 2 and raw[1]['price_from_estimate_rub'] is None
    monkeypatch.setattr(main, 'extract_rows_from_excel', lambda *args, **kwargs: raw)
    result = worker.parse_files([source], [], value, worker.ParseLimits())
    assert result['documents'][0]['missing_amount_rows'] == 1
    monkeypatch.setattr(publication, 'run_parser', lambda *args: result)
    frame = publication.parse_and_publish(value, [source], [], [source], paths)[2]
    assert len(frame) == 2 and frame['Сумма, руб'].sum() == 2000 and frame['Сумма, руб'].isna().sum() == 1
    from autobot.tender_viability import compute_viability_stats
    stats = compute_viability_stats(frame)
    assert stats.rows_considered == 2 and stats.rows_without_amount == 1 and stats.coverage_cost_percent is None
    control = json.loads((paths['reports']/f'ESTIMATE_PARSE_{value.tender_id}.json').read_text(encoding='utf-8'))
    assert control['unresolved_amount_rows'][0]['position_id'] == raw[1]['position_id']
    assert 'сумма части позиций' in ' '.join(publication.display_status(paths['reports'],value.tender_id)['warnings'])


def test_quantity_column_ignores_coefficient_annotation_from_adjacent_text():
    words = words_for_position(quantity='2.56')
    words += [{'text':'ОЗП=1,1;', 'left':600, 'top':125, 'height':15, 'width':30, 'page':1}]
    row = adapter.PdfEstimateAdapter._position_records_from_words(words, 1000)[0]
    assert row['qty'] == 2.56


@pytest.mark.parametrize('label', ['Всего по позиции', 'Всего no позиции'])
def test_skewed_total_label_does_not_pick_next_position_amount(label):
    words = [word for word in words_for_position() if word['left']!=930]
    words += [{'text':label, 'left':220, 'top':200, 'height':18, 'width':150, 'page':1},
              {'text':'1000', 'left':930, 'top':176, 'height':18, 'width':20, 'page':1}]
    next_row = words_for_position(top=240, position='2')
    amount = next(word for word in next_row if word['left']==930)
    amount.update(top=207, height=17)
    words += next_row
    rows = adapter.PdfEstimateAdapter._position_records_from_words(words, 1000)
    assert rows[0]['total'] == 1000 and rows[1]['total'] == 2000
