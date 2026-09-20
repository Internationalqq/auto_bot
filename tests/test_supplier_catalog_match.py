import time
import pytest
from autobot import supplier_catalog_store as store
from autobot.supplier_catalog_match import lookup,product_family


@pytest.fixture
def catalog(tmp_path):
    path=tmp_path/'catalog.sqlite3'; store.initialize(path);store.seed_sources(path)
    src=next(s for s in store.sources(path) if 'gamma-beton' in s['url'])
    body='<h1>Бетон в Ярославле</h1><p>ООО Бетон, телефон +7 (4852) 22-33-44.</p><p>Адрес: г. Ярославль, ул. Тестовая, 1.</p>'
    record={'name':'Бетон БСГ В22.5 М300 на гравии','price':4720,'unit':'м3','url':src['url'],
        'evidence':'Бетон БСГ В22.5 М300 на гравии. Цена 4720 руб/м3','details':{'price_scope':'product'}}
    store.save_page(src['id'],src['url'],body,time.time(),[record],path=path)
    return path,src,record,body


def test_finished_concrete_kerb_is_not_ready_mix(catalog):
    path,_,_,_=catalog
    assert product_family('Камни бортовые бетонные марки БР, БВ, бетон В22,5 (М300)')=='kerb'
    assert lookup(name='Камни бортовые бетонные марки БР, БВ, бетон В22,5 (М300)',unit='м3',region='Ярославская область',quantity=10,path=path)==[]
    assert lookup(name='Бетон В22.5 М300 на гравии',unit='м3',region='Ярославская область',quantity=10,path=path)


def test_wrong_grade_region_and_missing_spec_are_rejected(catalog):
    path,_,_,_=catalog
    assert not lookup(name='Бетон В15 М200 на гравии',unit='м3',region='Ярославская область',quantity=10,path=path)
    assert not lookup(name='Бетон В22.5 М300 на гравии',unit='м3',region='Челябинск',quantity=10,path=path)
    assert not lookup(name='Бетон В22.5 М300 на граните',unit='м3',region='Ярославская область',quantity=10,path=path)


def test_identical_refresh_does_not_invalidate_evidence(catalog):
    path,src,record,body=catalog
    item=store.items(path=path)['items'][0]
    store.save_page(src['id'],src['url'],body+'<span>changed footer</span>',time.time(),[record],path=path)
    assert not store.observation_reason(item['id'],item['observation_id'],path)


def test_national_delivery_filter_requires_saved_proof_and_can_be_revoked(catalog):
    path,src,record,body=catalog
    assert store.items(region='Челябинск',path=path)['total']==0
    store.save_coverage(src['id'],'Доставка по всей России',src['url'],time.time(),path=path)
    assert store.items(region='Челябинск',path=path)['total']==1
    store.save_coverage(src['id'],'',src['url'],time.time(),path=path)
    assert store.items(region='Челябинск',path=path)['total']==0


def test_tender_reuses_catalog_and_rechecks_saved_price_after_refresh(catalog,monkeypatch):
    import json
    import pandas as pd
    from autobot import market_price_index as index, real_market_scraper as scraper, market_contract
    path,src,record,body=catalog
    monkeypatch.setattr(index,'INDEX_DB',path)
    monkeypatch.setattr(scraper,'lookup_verified_offers',lambda **kwargs:[])
    row=pd.Series({scraper.COL_NAME:'Бетон В22.5 М300 на гравии','Ед. изм.':'м3',scraper.COL_QTY:10,scraper.COL_UNIT_PRICE:5000})
    offers=scraper._offers_from_local_index(row,max_results=3,region='Ярославская область')
    assert len(offers)==1 and offers[0].price==4720 and offers[0].catalog_item_id
    published=row.to_dict();published[market_contract.BUNDLE_COLUMN]=json.dumps(scraper._offer_bundle(offers))
    assert market_contract.offers_for_row(published)[0]['verification']=='verified'
    store.save_page(src['id'],src['url'],body+'new price',time.time(),[dict(record,price=4800)],path=path)
    assert market_contract.offers_for_row(published)[0]['verification']=='candidate'
    assert scraper._offers_from_local_index(row,max_results=3,region='Ярославская область')[0].price==4800


def test_work_price_must_match_surface_material(catalog):
    path,src,_,body=catalog
    rows=[{'name':'Монтаж кабель-канала: '+material,'price':price,'unit':'м','bucket':'works',
        'url':src['url'],'evidence':'Монтаж кабель-канала: '+material+' · '+str(price)+' руб/м, только работа',
        'details':{'price_scope':'work_only'}} for material,price in [('бетон',150),('кирпич',100),('ГКЛ',90)]]
    store.save_page(src['id'],src['url'],body,time.time(),rows,path=path)
    offers=lookup(name='Монтаж кабель-канала по бетону',unit='м',region='Ярославская область',quantity=100,path=path)
    assert [o['price'] for o in offers]==[150]
