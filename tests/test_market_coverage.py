from autobot.market_coverage import annotate_coverage


def test_every_row_has_one_explained_outcome_without_inventing_cost():
    rows = [
        {'type_slug': 'aggregate'},
        {'can_auto_price': False, 'requirements': {'issues': ['Единица неизвестна']}},
        {'can_auto_price': True, 'verified_count': 2, 'market_unit': 15.7},
        {'can_auto_price': True, 'candidate_count': 1},
        {'can_auto_price': True, 'market_processed': True, 'market_status': 'Сайт показал CAPTCHA'},
        {'can_auto_price': True, 'market_processed': True},
        {'can_auto_price': True},
    ]
    coverage = annotate_coverage(rows)
    assert coverage['total'] == 7
    assert coverage['priceable'] == 6
    assert all(coverage[key] == 1 for key in coverage if key not in {'total', 'priceable'})
    assert all(row['price_reason'] for row in rows)
    assert 'Единица неизвестна' == rows[1]['price_reason']
    assert rows[2]['market_unit'] == 15.7
    assert all('market_unit' not in row for row in rows[:2] + rows[3:])


def test_plan_keeps_denominator_and_all_quantities_without_counting_candidates():
    from autobot.market_coverage import coverage_plan
    make=lambda key,**kw:dict(name='Песок',unit='м3',position_key=key,can_auto_price=True,**kw)
    rows=[make('a',quantity=1,candidate_count=2),make('b',quantity=15),make('c',verified_count=1),
          dict(type_slug='aggregate'),dict(name='Неясно',can_auto_price=False)]
    plan=coverage_plan(rows)
    assert plan['priceable']==4 and plan['verified']==1
    assert plan['required_verified']==4 and plan['missing_to_target']==3 and not plan['target_reached']
    assert plan['unique_needs']==2
    assert [p['quantity'] for p in plan['needs'][0]['positions']]==[1,15]
    assert plan['needs'][1]['next_step']=='clarify_requirements'
    assert 'price_state' not in rows[0]
    import json
    json.dumps(plan,allow_nan=False)


def test_plan_separates_resource_compositions_and_does_not_claim_empty_success():
    from autobot.market_coverage import coverage_plan
    row={'name':'Укладка','unit':'м2','can_auto_price':True,'has_resources':True}
    rows=[dict(row,resource_scope={'kind':'auxiliary_only','components':[]}),
          dict(row,resource_scope={'kind':'resources','components':[{'name':'Песок','unit':'м3','kind':'resource','quantity':5}]}),
          dict(row,resource_scope={'kind':'resources','components':[{'name':'Щебень','unit':'м3','kind':'resource','quantity':3}]})]
    plan=coverage_plan(rows)
    assert plan['unique_needs']==3
    assert {r['next_step'] for r in plan['needs']}=={'service_with_consumables','complete_composition'}
    assert not coverage_plan([])['target_reached']


def test_coverage_route_is_read_only_and_preserves_uncovered_row_identity(monkeypatch,tmp_path):
    from autobot import web_ui
    monkeypatch.setattr(web_ui,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(web_ui,'load_tender_metadata',lambda:{'12345678':{'title':'Test'}})
    monkeypatch.setattr(web_ui,'build_tender_detail',lambda *args:{'region':'Ярославская область',
        'positions':[{'name':'Товар','position_key':'key','unit':'шт','quantity':2,
            'can_auto_price':True,'candidate_count':1}]})
    client=web_ui.app.test_client()
    response=client.get('/api/tenders/12345678/coverage-plan')
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert response.json['required_verified']==1 and response.json['verified']==0
    assert response.json['needs'][0]['positions'][0]['position_key']=='key'
    assert client.post('/api/tenders/12345678/coverage-plan').status_code==405
    assert client.get('/api/tenders/123456789/coverage-plan').status_code==404
    assert client.get('/api/tenders/invalid/coverage-plan').status_code==404


def test_coverage_route_does_not_read_a_partially_published_report(monkeypatch,tmp_path):
    from contextlib import contextmanager
    from autobot import web_ui,estimate_publication_recovery as recovery
    monkeypatch.setattr(web_ui,'REPORTS_DIR',tmp_path)
    @contextmanager
    def busy(*args):
        raise TimeoutError('publication is busy')
        yield
    monkeypatch.setattr(recovery,'consistent_report',busy)
    response=web_ui.app.test_client().get('/api/tenders/12345678/coverage-plan')
    assert response.status_code==503 and response.json['error']=='report_busy'
