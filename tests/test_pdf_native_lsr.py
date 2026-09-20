import pytest
from autobot.pdf_native_lsr import read_native_lsr,records_from_lines,UnsupportedLayout


def line(y,page=1,**cells):
    row=['']*12
    for column,text in cells.items(): row[int(column[1:])]=text
    return {'y':y,'page':page,'cells':row}


def test_native_description_final_quantity_and_own_total_survive_page_break():
    rows=records_from_lines([
        line(90,c0='1',c1='ГЭСН27-04-016-04',c2='Устройство прослойки',c3='1000 м2',c4='2',c5='1,2',c6='2,4'),
        line(100,c2='из нетканого материала'),line(110,c2='Объем=2400 / 1000'),
        line(90,page=2,c2='Всего по позиции',c9='50',c11='120'),
        line(110,page=2,c0='2',c1='ФСБЦ-24.3-0290',c2='Труба ПЭ100 SDR21',c3='м',c6='690',c11='194 642,10'),
        line(120,page=2,c2='Всего по позиции',c11='194 642,10'),
        line(130,page=2,c2='ВСЕГО по смете',c11='194 762,10'),
    ])
    assert len(rows)==2 and rows[0]['name']=='Устройство прослойки из нетканого материала'
    assert rows[0]['qty']==2.4 and rows[0]['unit']=='1000 м2' and rows[0]['total']==120
    assert rows[0]['unit_price']==50
    assert rows[1]['total']==194642.1


def test_fractional_resource_is_nested_without_counting_its_cost_twice():
    rows=records_from_lines([
        line(90,c0='10',c1='ГЭСН27-04-016-04',c2='Работа',c3='1000 м2',c6='2'),
        line(110,c1='ФСБЦ-01.7.12.05-0053',c2='Геополотно нетканое полиэфирное, иглопробивное,',c3='м2',c6='2200',c11='74 272'),
        line(120,c0='10.1',c2='поверхностная плотность 200 г/м2'),
        line(140,c2='Всего по позиции',c11='90 000'),
    ])
    assert len(rows)==1 and rows[0]['total']==90000
    child=rows[0]['resources'][0]
    assert child['name'].endswith('плотность 200 г/м2') and child['unit']=='м2'
    assert child['qty']==2200 and child['total']==74272
    assert child['parent_position_id']==rows[0]['position_id']


def test_own_cable_price_cannot_be_replaced_by_later_pipe_price():
    rows=records_from_lines([
        line(90,c0='70',c1='ФСБЦ-21.1.06.07-0016',c2='Кабель АВБШв 4х50ок(N)-660',c3='1000 м',c6='0,3519',c11='116 318,48'),
        line(100,c2='Всего по позиции',c11='116 318,48'),
        line(110,c0='71',c1='ТЦ_21.1.06.00_69_',c2='Кабель АВБбШв 4х150',c3='пм',c6='351,9',c11='353 487,07'),
        line(120,c1='6950018173_20.01.2026_01_26.3'),
        line(130,c2='Всего по позиции',c11='353 487,07'),
        line(140,c0='72',c1='ТЦ_24.3.03.00_76_',c2='Труба EKF ПНД',c3='м',c6='695',c11='131 361,95'),
        line(150,c2='Всего по позиции',c11='131 361,95'),
    ])
    assert [r['total'] for r in rows]==[116318.48,353487.07,131361.95]
    assert rows[1]['code']=='ТЦ_21.1.06.00_69_6950018173_20.01.2026_01_26.3'


def test_missing_final_quantity_does_not_use_base_quantity():
    row=records_from_lines([line(90,c0='1',c1='ГЭСН27-04-016-04',c2='Работа',c3='м2',c4='2',c11='100')])[0]
    assert row['qty'] is None and row['unit_price'] is None and row['total']==100


def test_signed_adjustment_survives_report_without_becoming_a_purchase(monkeypatch,tmp_path):
    from autobot import main,pdf_estimate_adapter
    from autobot.market_strategy import build_search_plan
    native=records_from_lines([
        line(90,c0='21',c1='ГЭСН27-07-005-06',c2='Добавлять (уменьшать) на каждые 10 мм: к норме 27-07-005-04',c3='м реза',c6='-830,4'),
        line(110,c2='Всего по позиции',c11='-65 182,27'),
    ])
    monkeypatch.setattr(pdf_estimate_adapter,'pdf_to_position_records',lambda _:native)
    tender=main.Tender('test','Test','','Ярославская область','',None,None)
    rows=main._rows_from_pdf_adapter(tmp_path/'estimate.pdf',tender)
    frame=main._build_tender_clean_df(rows)
    assert len(frame)==1 and frame.iloc[0]['Сумма, руб']==-65182.27
    assert frame.iloc[0]['Кол-во']==-830.4
    assert not build_search_plan(rows[0]['work_name'],rows[0]['unit'],rows[0]['basis_code']).can_auto_price


@pytest.mark.parametrize('lines',[
    [line(90,c0='1',c2='Неизвестная строка')],
    [line(90,c0='1.1',c1='ФСБЦ-01-0053',c2='Ресурс без родителя')],
    [line(90,c0='1',c1='ГЭСН27-04-016-04',c2='Работа'),line(110,c2='Всего по позиции',c11='100'),line(130,c2='Всего по позиции',c11='200')],
])
def test_ambiguous_native_layout_is_rejected(lines):
    with pytest.raises(UnsupportedLayout): records_from_lines(lines)


def native_pdf(*,mixed_scan=False):
    import fitz
    book=fitz.open();page=book.new_page(width=1200,height=600)
    bounds=[30,65,170,500,550,610,670,735,810,875,960,1040,1170]
    for i in range(12):
        page.draw_rect(fitz.Rect(bounds[i],40,bounds[i+1],60),width=.5)
        page.insert_text((bounds[i]+3,54),str(i+1),fontsize=8)
    values={0:'1',1:'FSBC-01-0053',2:'Geotextile needle-punched',3:'m2',6:'25',11:'1250'}
    for i,value in values.items(): page.insert_text((bounds[i]+3,83),value,fontsize=8)
    page.insert_text((bounds[2]+3,93),'density 200 g/m2',fontsize=8)
    if mixed_scan:
        image=page.get_pixmap().tobytes('png')
        scanned=book.new_page(width=1200,height=600)
        scanned.insert_image(scanned.rect,stream=image)
    raw=book.tobytes();book.close();return raw


def test_text_pdf_uses_printed_column_boundaries_without_ocr(monkeypatch):
    import sys
    from types import SimpleNamespace
    from autobot.pdf_estimate_adapter import PdfEstimateAdapter
    def forbid(*args,**kwargs): raise AssertionError('Text PDF must not go through OCR')
    monkeypatch.setitem(sys.modules,'pytesseract',SimpleNamespace(image_to_data=forbid))
    rows=PdfEstimateAdapter().to_position_records(native_pdf())
    assert len(rows)==1 and rows[0]['name']=='Geotextile needle-punched density 200 g/m2'
    assert rows[0]['unit']=='m2' and rows[0]['qty']==25 and rows[0]['total']==1250
    assert rows[0]['extract_source']=='PDF text LSR'


def test_mixed_scan_pdf_cannot_publish_only_text_pages():
    assert read_native_lsr(native_pdf(mixed_scan=True)) is None


def test_unreadable_text_layer_falls_back_to_the_existing_adapter():
    assert read_native_lsr(b'%PDF-test') is None
