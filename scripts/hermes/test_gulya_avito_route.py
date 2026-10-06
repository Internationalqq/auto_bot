import copy
import install_gulya_avito_route as route


def test_route_preserves_job_settings_and_existing_rules():
    job = {'prompt':'Existing no-send rule.', 'enabled_toolsets':['file','browser'],
           'schedule':{'expr':'0 6-21/3 * * *'}, 'model':'keep-model', 'origin':{'chat_id':'keep'}}
    before = copy.deepcopy(job)
    updates = route.job_updates(job)
    assert job == before
    assert set(updates) == {'prompt','enabled_toolsets'}
    assert updates['prompt'].startswith(job['prompt'])
    assert updates['enabled_toolsets'] == ['file','browser','computer_use']
    assert route.job_updates({**job, **updates}) == updates
