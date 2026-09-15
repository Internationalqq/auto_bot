import hashlib
import io
import json
import uuid

import pytest

from autobot import web_ui as web, upload_admission as admission, uploaded_estimates as store
from test_uploaded_estimate_store import bind

KEY = 'a' * 32


def setup(tmp_path, monkeypatch):
    root = bind(tmp_path, monkeypatch)
    starts = []
    def start(key, **_):
        starts.append(key)
        web.estimate_upload_workers.add(key)
        return True
    monkeypatch.setattr(web, '_start_estimate_upload_worker', start)
    return root, web.app.test_client(), starts


def post(client, *, key=KEY, data=b'original bytes', title='Школа', filename='estimate.xlsx'):
    form = {'file': (io.BytesIO(data), filename), 'title': title}
    if key is not None: form['operation_id'] = key
    return client.post('/api/estimates/upload', data=form)


def test_repeated_request_returns_same_job_and_unchanged_original(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    first = post(client); source = root/KEY/'estimate.xlsx'
    assert first.status_code == 200 and first.json['accepted'] and not first.json['duplicate']
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    second = post(client, key=str(uuid.UUID(KEY)))
    assert second.status_code == 200 and second.json['duplicate']
    assert first.json['job_id'] == second.json['job_id'] == KEY and starts == [KEY]
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert len(list(root.glob('*/estimate.xlsx'))) == 1


@pytest.mark.parametrize('change', [{'data': b'different'}, {'title': 'Другой объект'}, {'filename': 'renamed.xlsx'}])
def test_changed_request_is_rejected_without_overwrite(tmp_path, monkeypatch, change):
    root, client, starts = setup(tmp_path, monkeypatch)
    assert post(client).status_code == 200
    before = (root/KEY/'estimate.xlsx').read_bytes()
    assert post(client, **change).status_code == 409
    assert (root/KEY/'estimate.xlsx').read_bytes() == before and starts == [KEY]


def test_legacy_requests_remain_separate_and_invalid_key_does_not_write(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    assert post(client, key='../a').status_code == 400
    assert not root.exists()
    first, second = post(client, key=None), post(client, key=None)
    assert first.status_code == second.status_code == 200
    assert first.json['job_id'] != second.json['job_id'] and len(starts) == 2


def test_missing_first_response_recovers_job_without_reupload(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    write = store.write_json
    def fail_job(path, value):
        if path.parent == root/'.upload_jobs': raise OSError('job write failed')
        write(path, value)
    monkeypatch.setattr(store, 'write_json', fail_job)
    response = post(client)
    assert response.status_code == 503 and not starts
    assert (root/KEY/'estimate.xlsx').read_bytes() == b'original bytes'
    assert not (root/'.upload_jobs'/(KEY+'.json')).exists()
    monkeypatch.setattr(store, 'write_json', write)
    response = client.get('/api/estimates/upload-status/'+KEY)
    assert response.status_code == 200 and response.json['running'] and starts == [KEY]
    assert response.json['original_url'] == '/estimates/uploads/'+KEY+'/original'
    assert post(client).json['duplicate'] and starts == [KEY]


def test_reservation_without_source_requires_same_file_retry(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    replace = admission.os.replace
    def fail_source(source, target):
        if str(source).split('/')[-1].split('\\')[-1].startswith('.receiving-'):
            raise OSError('source write failed')
        replace(source, target)
    monkeypatch.setattr(admission.os, 'replace', fail_source)
    assert post(client).status_code == 503 and not starts
    status = client.get('/api/estimates/upload-status/'+KEY)
    assert status.status_code == 409 and status.json['retry_upload']
    assert post(client, data=b'other').status_code == 409
    monkeypatch.setattr(admission.os, 'replace', replace)
    assert post(client).status_code == 200 and starts == [KEY]


def test_terminal_job_never_restarts_and_failed_original_is_downloadable(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    post(client)
    job = web.estimate_upload_jobs[KEY]
    job.update(running=False, ok=False, stage='Ошибка', error='Не удалось разобрать таблицу')
    store.write_json(root/'.upload_jobs'/(KEY+'.json'), job)
    web.estimate_upload_jobs.clear(); web.estimate_upload_workers.clear()
    repeated = post(client)
    assert repeated.status_code == 200 and repeated.json['duplicate'] and starts == [KEY]
    status = client.get('/api/estimates/upload-status/'+KEY).json
    assert not status['running'] and status['error'] and status['original_filename'] == 'estimate.xlsx'
    response = client.get(status['original_url'])
    assert response.status_code == 200 and response.data == b'original bytes'
    assert response.headers['Cache-Control'] == 'private, no-store'
    response.close()
    (root/KEY/'estimate.xlsx').unlink()
    assert post(client).status_code == 410 and not (root/KEY/'estimate.xlsx').exists()


def test_successful_response_loss_does_not_run_parser_again(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    post(client)
    job = web.estimate_upload_jobs[KEY]
    job.update(running=False, ok=True, stage='Готово', progress=100, estimate_id=KEY)
    store.write_json(root/'.upload_jobs'/(KEY+'.json'), job)
    web.estimate_upload_jobs.clear(); web.estimate_upload_workers.clear()
    assert post(client).json['duplicate']
    status = client.get('/api/estimates/upload-status/'+KEY).json
    assert status['result_ok'] and status['estimate_id'] == KEY and starts == [KEY]


def test_failed_job_original_cannot_escape_own_folder(tmp_path, monkeypatch):
    root, client, _ = setup(tmp_path, monkeypatch)
    post(client)
    foreign = tmp_path/'secret.pdf'; foreign.write_bytes(b'private')
    job = dict(web.estimate_upload_jobs[KEY], source_path=str(foreign))
    store.write_json(root/'.upload_jobs'/(KEY+'.json'), job)
    assert client.get('/estimates/uploads/'+KEY+'/original').status_code == 404
    assert client.get('/estimates/uploads/invalid/original').status_code == 404


def test_corrupt_receipt_and_job_are_not_silently_replaced(tmp_path, monkeypatch):
    root, client, starts = setup(tmp_path, monkeypatch)
    post(client)
    path = root/'.upload_jobs'/(KEY+'.json')
    path.write_text('{broken', encoding='utf-8')
    assert post(client).status_code == 503
    assert client.get('/api/estimates/upload-status/'+KEY).status_code == 503
    assert path.read_text() == '{broken' and starts == [KEY]
    receipt = root/'.upload_jobs/admissions'/(KEY+'.json')
    receipt.write_text('{broken', encoding='utf-8')
    assert post(client).status_code == 503 and receipt.read_text() == '{broken'


def test_oversized_stream_is_rejected_before_receipt(tmp_path):
    with pytest.raises(admission.AdmissionError) as error:
        admission.receive(io.BytesIO(b'abcd'), key=KEY, original_name='a.xlsx',title='',
                          source_root=tmp_path/'estimates',jobs_dir=tmp_path/'jobs',repo_root=tmp_path,max_bytes=3)
    assert error.value.status == 413 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('field,value', [('started_at', None), ('original_name', '../source.xlsx')])
def test_receipt_schema_failure_is_explicit(tmp_path, monkeypatch, field, value):
    root,client,starts=setup(tmp_path,monkeypatch)
    post(client)
    path=root/'.upload_jobs/admissions'/(KEY+'.json')
    record=json.loads(path.read_text(encoding='utf-8'));record[field]=value
    store.write_json(path,record)
    assert client.get('/api/estimates/upload-status/'+KEY).status_code==503
    assert post(client).status_code==503 and starts==[KEY]


def test_valid_json_cannot_change_the_job_source_binding(tmp_path, monkeypatch):
    root,client,starts=setup(tmp_path,monkeypatch)
    post(client)
    path=root/'.upload_jobs'/(KEY+'.json')
    job=json.loads(path.read_text(encoding='utf-8'));job['source_sha256']='0'*64
    store.write_json(path,job)
    assert client.get('/api/estimates/upload-status/'+KEY).status_code==503
    assert post(client).status_code==503 and starts==[KEY]


def test_parser_refuses_changed_accepted_source(tmp_path, monkeypatch):
    root,client,_=setup(tmp_path,monkeypatch)
    post(client)
    source=root/KEY/'estimate.xlsx'
    source.write_bytes(b'changed after receipt')
    from autobot import estimate_parse_worker
    monkeypatch.setattr(estimate_parse_worker,'run_uploaded_parser',lambda *a,**k:pytest.fail('Changed source parsed'))
    web._run_estimate_upload_worker(KEY,estimate_id=KEY,title_raw='Школа',original_name=source.name,src_path=source)
    status=client.get('/api/estimates/upload-status/'+KEY).json
    assert not status['running'] and not status['result_ok']
    assert 'исходник изменился' in status['error']
    assert not store.catalogue(root)
