from argparse import Namespace
import json

import pytest

from autobot import main, tender_search_profiles as profiles, tender_search_state as state


def settings(**changes):
    return dict(profiles.default_filters(), **changes)


def args(filters=None, **changes):
    values = dict(max_pages=2, max_tenders=15, days_back=30, search_filters=filters, catalog_only=False,
                  resume_downloads=False, from_tender_id='', from_tender_url='', from_downloaded_tender_id='', emit_new_ids_to='')
    if filters:
        values.update({key: filters[key] for key in ('max_pages', 'max_tenders', 'days_back')})
    return Namespace(**dict(values, **changes))


def tender(**changes):
    return main.Tender(**dict(dict(tender_id='12345678', title='Ремонт школы', url='https://zakupki.gov.ru/notice/12345678',
        region='Москва', stage='Подача заявок', price_rub=1_000_000.01, publish_date='15.09.2026'), **changes))


def test_save_reopen_repeat_and_stale_writer(tmp_path):
    original = profiles.load_profiles(tmp_path)
    assert not (tmp_path/'search_profiles.json').exists()
    row = {'id':'a'*32, 'name':'Ремонт школ', 'filters':settings(regions=['Москва'], keywords=['ремонт школы'])}
    request = {'revision':0, 'profile':row}
    saved = profiles.save_profile(tmp_path, request)
    assert saved['revision'] == 1 and profiles.load_profiles(tmp_path) == saved
    assert profiles.save_profile(tmp_path, request) == saved
    with pytest.raises(profiles.ProfileConflict):
        profiles.save_profile(tmp_path, dict(request, profile=dict(row, name='Другая правка')))
    snapshot = profiles.filters_for_profile(tmp_path, row['id'])
    profiles.save_profile(tmp_path, {'revision':1, 'profile':dict(row, filters=settings(keywords=['кровля']))})
    assert snapshot['keywords'] == ['ремонт школы']
    assert original['profiles'][0]['filters'] == profiles.default_filters()


def test_competing_writers_keep_the_first_saved_change(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier=Barrier(2)
    def save(identifier):
        barrier.wait(timeout=5)
        try:
            profiles.save_profile(tmp_path, {'revision':0,'profile':{'id':identifier*32,'name':identifier,'filters':settings()}})
            return 'saved'
        except profiles.ProfileConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(save,['a','b']))
    assert sorted(results)==['conflict','saved']
    stored=profiles.load_profiles(tmp_path)
    assert stored['revision']==1 and len(stored['profiles'])==2


def test_failed_or_oversized_save_preserves_previous_bytes(tmp_path,monkeypatch):
    request={'revision':0,'profile':{'id':'legacy','name':'Первый','filters':settings()}}
    profiles.save_profile(tmp_path,request)
    path=tmp_path/'search_profiles.json';before=path.read_bytes()
    monkeypatch.setattr(profiles,'MAX_BYTES',len(before)+5)
    with pytest.raises(ValueError,match='слишком большой'):
        profiles.save_profile(tmp_path,{'revision':1,'profile':dict(request['profile'],name='Следующий профиль с длинным названием')})
    assert path.read_bytes()==before
    monkeypatch.setattr(profiles,'MAX_BYTES',128*1024)
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(profiles,'atomic_json',fail)
    with pytest.raises(OSError):
        profiles.save_profile(tmp_path,{'revision':1,'profile':dict(request['profile'],name='Следующий')})
    assert path.read_bytes()==before


@pytest.mark.parametrize('changes', [
    {'regions':[]}, {'regions':'Москва'}, {'keywords':['']}, {'keywords':['bad\x00text']},
    {'price_min_kopecks':True}, {'price_min_kopecks':-1}, {'price_max_kopecks':1.5},
    {'price_min_kopecks':100, 'price_max_kopecks':99}, {'days_back':0}, {'max_pages':21},
    {'max_tenders':101}, {'needed_stage':'Завершено'}, {'regions':['р'+str(i) for i in range(10)], 'keywords':['т'+str(i) for i in range(7)]},
])
def test_invalid_filters_rejected(changes):
    with pytest.raises(ValueError): profiles.validate_filters(settings(**changes))


@pytest.mark.parametrize('raw', ['{bad', '[]', '{"schema_version":1,"revision":0,"profiles":[]}'])
def test_corrupt_store_is_not_replaced(tmp_path, raw):
    path=tmp_path/'search_profiles.json'; path.write_text(raw)
    with pytest.raises(ValueError):
        profiles.save_profile(tmp_path, {'revision':0,'profile':{'id':'legacy','name':'New','filters':settings()}})
    assert path.read_text() == raw


def test_exact_boundaries_unbounded_values_and_unknown_price(monkeypatch):
    monkeypatch.setattr(main.business_time, 'today_iso', lambda:'2026-09-15')
    filters=settings(price_min_kopecks=100_000_001, price_max_kopecks=100_000_001)
    assert main.tender_filter_reasons(tender(), 60, filters=filters) == []
    assert 'price' in main.tender_filter_reasons(tender(price_rub=1_000_000),60,filters=filters)
    unbounded=settings(price_min_kopecks=None,price_max_kopecks=None)
    assert main.tender_filter_reasons(tender(price_rub=500),60,filters=unbounded) == []
    assert main.tender_filter_reasons(tender(price_rub=None),60,filters=unbounded) == ['price_unknown']
    assert main.PRICE_MIN == 20_000_000


def test_eis_url_uses_snapshot_and_omits_absent_boundaries(monkeypatch):
    from unittest.mock import MagicMock
    from urllib.parse import urlparse, parse_qs
    playwright=MagicMock()
    page=MagicMock()
    cards=MagicMock();cards.count.return_value=0
    page.locator.side_effect=lambda selector: cards if selector.startswith('div.') else MagicMock(inner_text=lambda **kw:'По вашему запросу ничего не найдено')
    monkeypatch.setattr(main,'sync_playwright',lambda:playwright)
    monkeypatch.setattr(main,'_new_eis_page',lambda _:page)
    main.search_tenders('Москва','ремонт школы',1,filters=settings(price_min_kopecks=100_000_001,price_max_kopecks=None))
    query=parse_qs(urlparse(page.goto.call_args.args[0]).query)
    assert query['searchString']==['Москва ремонт школы']
    assert query['priceFromGeneral']==['1000000.01'] and 'priceToGeneral' not in query
    main.search_tenders('Москва','ремонт',1,filters=settings(price_min_kopecks=None,price_max_kopecks=0))
    query=parse_qs(urlparse(page.goto.call_args.args[0]).query)
    assert 'priceFromGeneral' not in query and query['priceToGeneral']==['0.00']


def test_catalogue_loop_filter_and_journal_use_same_conditions(tmp_path,monkeypatch):
    paths={'root':tmp_path,**{key:tmp_path/key for key in ('downloads','extracted','reports')}}
    for path in paths.values():path.mkdir(exist_ok=True)
    snapshot=settings(regions=['Москва','Тульская область'],keywords=['ремонт школы'],price_min_kopecks=100_000_001,price_max_kopecks=None,max_tenders=1)
    monkeypatch.setattr(main,'ensure_dirs',lambda:paths)
    monkeypatch.setattr(main,'parse_args',lambda:args(snapshot,catalog_only=True))
    monkeypatch.setattr(main,'telegram_config',lambda:None)
    monkeypatch.setattr(main,'configure_rar_backend',lambda:True)
    monkeypatch.setattr(main.business_time,'today_iso',lambda:'2026-09-15')
    calls=[]
    def search(region,keyword,**kwargs):
        calls.append((region,keyword,kwargs['filters']))
        return [tender(),tender(tender_id='23456789',price_rub=1_000_000)]
    monkeypatch.setattr(main,'search_tenders',search)
    main.main()
    saved=state.read_state(tmp_path/'last_search_run.json')
    assert len(calls)==2 and all(call[2]==snapshot for call in calls)
    assert saved['filters']==snapshot and saved['rejections']=={'price':1} and saved['counts']['selected']==1
    assert [row['tender_id'] for row in json.loads((tmp_path/'tenders.json').read_text(encoding='utf-8'))]==['12345678']


def test_resume_recovers_its_snapshot_after_profile_changes(tmp_path, monkeypatch):
    paths={'root':tmp_path}
    initial=settings(regions=['Москва'],keywords=['ремонт'],price_min_kopecks=None,max_tenders=7)
    search_args=args(initial)
    main._save_search_checkpoint(paths,search_args,filtered=[tender()],completed_ids=set(),new_ids={'12345678'},search_total=1)
    profile={'id':'legacy','name':'Changed','filters':settings(keywords=['техника'])}
    profiles.save_profile(tmp_path, {'revision':0,'profile':profile})
    resumed=args(resume_downloads=True)
    loaded=main._load_search_checkpoint(paths,resumed)
    assert resumed.search_filters == initial and resumed.max_tenders == 7
    assert loaded['signature'] == main._checkpoint_signature(resumed)
    assert state.public_resume(tmp_path)['search_filters'] == initial
    # A caller cannot replace the saved snapshot while continuing.
    with pytest.raises(ValueError,match='Фильтры изменились'):
        main._load_search_checkpoint(paths,args(settings(),resume_downloads=True))
    corrupted=dict(loaded,search_filters=dict(initial,max_tenders=8))
    state.atomic_json(tmp_path/'search_resume_checkpoint.json',corrupted)
    assert not state.public_resume(tmp_path)['available']


def test_cli_snapshot_and_invalid_json(monkeypatch):
    snapshot=settings(regions=['Москва'],keywords=['ремонт'],price_max_kopecks=None,max_tenders=7)
    monkeypatch.setattr(main.sys,'argv',['autobot.main','--search-filters-json',json.dumps(snapshot),'--catalog-only'])
    result=main.parse_args()
    assert result.search_filters == snapshot and result.max_tenders == 7
    monkeypatch.setattr(main.sys,'argv',['autobot.main','--search-filters-json','[]'])
    with pytest.raises(SystemExit): main.parse_args()


def test_api_saves_profiles_and_starts_only_valid_snapshot(tmp_path, monkeypatch):
    from autobot import web_ui
    monkeypatch.setattr(web_ui,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web_ui,'_merge_site_busy',lambda:False)
    monkeypatch.setattr(web_ui,'parse_state',dict(web_ui.parse_state,running=False))
    calls=[]
    class Thread:
        def __init__(self,**kwargs): calls.append(kwargs['kwargs'])
        def start(self): pass
    monkeypatch.setattr(web_ui.threading,'Thread',Thread)
    client=web_ui.app.test_client()
    script=client.get('/tenders/search-profiles.js')
    assert script.status_code==200 and b'/api/tender-search-profiles' in script.data
    assert client.get('/api/tender-search-profiles').get_json()['profiles']==profiles.load_profiles(tmp_path)['profiles']
    assert client.post('/api/tender-search-profiles',json={},headers={'Origin':'https://other.example'}).status_code==403
    loaded=client.get('/api/search-profiles').get_json()
    row=dict(loaded['profiles'][0],filters=settings(regions=['Москва'],keywords=['ремонт'],max_tenders=7))
    assert client.post('/api/search-profiles',json={'revision':0,'profile':row}).status_code==200
    assert client.post('/api/start-parse',json={'search_filters':settings(max_tenders=101)}).status_code==400
    assert not calls
    assert client.post('/api/tender-search/start',json={'search_profile':'legacy'}).status_code==200
    command=calls[0]['cli_args']
    assert json.loads(command[command.index('--search-filters-json')+1])==row['filters']
    assert command[command.index('--max-tenders')+1]=='7' and '--catalog-only' in command
    assert client.post('/api/start-parse',json={'search_profile':'legacy'}).status_code==409


def test_api_resume_ignores_edited_profile_and_request_filters(tmp_path,monkeypatch):
    from autobot import web_ui
    snapshot=settings(regions=['Москва'],keywords=['ремонт'],max_tenders=7)
    main._save_search_checkpoint({'root':tmp_path},args(snapshot),filtered=[tender()],completed_ids=set(),new_ids=set(),search_total=1)
    monkeypatch.setattr(web_ui,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web_ui,'_merge_site_busy',lambda:False)
    monkeypatch.setattr(web_ui,'parse_state',dict(web_ui.parse_state,running=False))
    calls=[]
    class Thread:
        def __init__(self,**kwargs): calls.append(kwargs['kwargs'])
        def start(self): pass
    monkeypatch.setattr(web_ui.threading,'Thread',Thread)
    response=web_ui.app.test_client().post('/api/start-parse',json={'search_mode':'resume','search_filters':settings()})
    assert response.status_code==200
    command=calls[0]['cli_args']
    assert '--resume-downloads' in command and '--catalog-only' not in command
    assert json.loads(command[command.index('--search-filters-json')+1]) == snapshot
