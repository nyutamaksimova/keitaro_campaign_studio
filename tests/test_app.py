import json
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.main import create_app


class Tracker:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def __call__(self, request):
        path = request.url.path.removeprefix('/admin_api/v1')
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.headers['Api-Key'] == 'test-secret'
        if path == '/offers':
            return httpx.Response(200, json=[
                {'id': 18001, 'name': 'Travel MX'}, {'id': 18002, 'name': 'Shopping'}
            ])
        if path == '/offers/18001':
            return httpx.Response(200, json={'id': 18001, 'name': 'Travel MX'})
        if path == '/offers/999':
            return httpx.Response(404, json={'error': 'Missing'})
        if path == '/domains':
            return httpx.Response(200, json=[{'id': 1813, 'name': 'https://in.example.com'}])
        if path == '/groups':
            assert request.url.params['type'] == 'campaigns'
            return httpx.Response(200, json=[{'id': 10332, 'name': 'FORTESTS'}])
        if path == '/traffic_sources':
            return httpx.Response(200, json=[{'id': 836, 'name': 'FORTESTS'}])
        if path == '/campaigns' and request.method == 'POST':
            if self.failure == 'timeout':
                raise httpx.ReadTimeout('SECRET RAW CONTENT', request=request)
            return httpx.Response(201, json={'id': 77, 'alias': body['alias']})
        if path == '/streams':
            if self.failure in ('stream', 'cleanup') and body['type'] == 'default':
                return httpx.Response(500, text='SECRET RAW CONTENT')
            return httpx.Response(201, json={'id': 101 if body['type'] == 'regular' else 102})
        if path == '/campaigns/77/enable':
            return httpx.Response(200, json={'id': 77, 'state': 'active'})
        if path == '/campaigns/77' and request.method == 'DELETE':
            return httpx.Response(500 if self.failure == 'cleanup' else 204)
        raise AssertionError(f'Unexpected request: {request.method} {path}')


@pytest.fixture
def setup_app():
    engines = []

    def build(failure=None, key='test-secret'):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                               poolclass=StaticPool)
        engines.append(engine)
        Base.metadata.create_all(engine)
        tracker = Tracker(failure)
        settings = Settings(_env_file=None, database_url='sqlite://', keitaro_api_key=key)
        return TestClient(create_app(settings, engine, httpx.MockTransport(tracker))), tracker

    yield build
    for engine in engines:
        engine.dispose()


def payload(**changes):
    return {'name': 'Summer campaign', 'geo': 'mx', 'offer_id': 18001,
            'request_id': str(uuid4()), **changes}


def test_creates_country_redirect_then_default_offer_and_records_result(setup_app):
    client, tracker = setup_app()
    result = client.post('/api/campaigns', json=payload())
    assert result.status_code == 201, result.text
    data = result.json()
    assert data['status'] == 'completed'
    assert data['campaign_id'] == 77
    assert data['geo'] == 'MX'
    assert data['stream_ids'] == [101, 102]
    assert data['campaign_url'].startswith('https://in.example.com/')
    mutations = [c for c in tracker.calls if c[0] != 'GET']
    campaign = mutations[0][2]
    assert campaign['domain_id'] == 1813
    assert campaign['group_id'] == 10332
    assert campaign['traffic_source_id'] == 836
    assert campaign['state'] == 'disabled'
    assert campaign['type'] == 'position'
    assert mutations[1][2] == {
        'campaign_id': 77, 'name': 'Geo MX → Google', 'position': 1,
        'type': 'regular', 'schema': 'redirect', 'state': 'active',
        'action_type': 'http', 'action_payload': 'https://google.com',
        'filters': [{'name': 'country', 'mode': 'accept', 'payload': ['MX']}],
    }
    assert mutations[2][2] == {
        'campaign_id': 77, 'name': 'Оффер', 'position': 2, 'type': 'default',
        'schema': 'landings', 'state': 'active',
        'offers': [{'offer_id': 18001, 'share': 100, 'state': 'active'}],
    }
    assert mutations[3][:2] == ('POST', '/campaigns/77/enable')
    assert client.get('/api/operations').json()[0]['campaign_id'] == 77


@pytest.mark.parametrize('changes', [
    {'name': '   '}, {'geo': 'XX'}, {'geo': 'MX,AU'}, {'offer_id': 0},
    {'offer_id': 'oops'}, {'request_id': 'invalid'},
])
def test_invalid_input_never_calls_tracker(setup_app, changes):
    client, tracker = setup_app()
    assert client.post('/api/campaigns', json=payload(**changes)).status_code == 422
    assert tracker.calls == []


def test_repeat_request_returns_same_result_without_creating_twice(setup_app):
    client, tracker = setup_app()
    body = payload()
    assert client.post('/api/campaigns', json=body).status_code == 201
    count = len(tracker.calls)
    assert client.post('/api/campaigns', json=body).status_code == 200
    assert len(tracker.calls) == count
    body['name'] = 'Another campaign'
    assert client.post('/api/campaigns', json=body).status_code == 409


def test_partial_failure_deletes_new_campaign_and_hides_upstream_body(setup_app):
    client, tracker = setup_app('stream')
    body = payload()
    response = client.post('/api/campaigns', json=body)
    assert response.status_code == 502
    assert 'SECRET' not in response.text
    assert tracker.calls[-1][:2] == ('DELETE', '/campaigns/77')
    assert client.get('/api/operations').json()[0]['status'] == 'failed'
    count = len(tracker.calls)
    assert client.post('/api/campaigns', json=body).status_code == 409
    assert len(tracker.calls) == count


def test_cleanup_failure_keeps_campaign_id_for_manual_recovery(setup_app):
    client, _ = setup_app('cleanup')
    assert client.post('/api/campaigns', json=payload()).status_code == 502
    operation = client.get('/api/operations').json()[0]
    assert operation['status'] == 'cleanup_failed'
    assert operation['campaign_id'] == 77


def test_timeout_is_uncertain_and_never_retries_creation(setup_app):
    client, tracker = setup_app('timeout')
    assert client.post('/api/campaigns', json=payload()).status_code == 502
    operation = client.get('/api/operations').json()[0]
    assert operation['status'] == 'uncertain'
    assert operation['alias']
    assert len([c for c in tracker.calls if c[:2] == ('POST', '/campaigns')]) == 1


def test_missing_offer_does_not_create_campaign(setup_app):
    client, tracker = setup_app()
    assert client.post('/api/campaigns', json=payload(offer_id=999)).status_code == 422
    assert not [c for c in tracker.calls if c[0] == 'POST']


def test_search_by_name_and_id_and_page_does_not_expose_key(setup_app):
    client, _ = setup_app()
    assert client.get('/api/offers?q=travel').json() == [{'id': 18001, 'name': 'Travel MX'}]
    assert client.get('/api/offers?q=18002').json() == [{'id': 18002, 'name': 'Shopping'}]
    page = client.get('/')
    assert page.status_code == 200
    assert 'test-secret' not in page.text
    assert 'name="geo"' in page.text
    assert client.get('/static/app.js').status_code == 200


def test_missing_key_reports_setup_problem(setup_app):
    client, tracker = setup_app(key='')
    assert client.get('/').status_code == 200
    response = client.post('/api/campaigns', json=payload())
    assert response.status_code == 503
    assert 'KEITARO_API_KEY' in response.json()['detail']
    assert tracker.calls == []


def test_database_failure_after_remote_creation_compensates_and_saves_id(setup_app):
    client, tracker = setup_app()
    commit_count = 0

    def fail_second_commit(session):
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise OperationalError('write failed', None, Exception('database unavailable'))

    event.listen(client.app.state.sessions, 'before_commit', fail_second_commit)
    response = client.post('/api/campaigns', json=payload())
    assert response.status_code == 503
    assert tracker.calls[-1][:2] == ('DELETE', '/campaigns/77')
    operation = client.get('/api/operations').json()[0]
    assert operation['status'] == 'failed'
    assert operation['campaign_id'] == 77


def test_separate_database_fields_encode_special_characters(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    settings = Settings(_env_file=None, postgres_host='db', postgres_port=5432,
                        postgres_user='test', postgres_db='keitaro',
                        postgres_password='p@ss:/?#%')
    from sqlalchemy.engine import make_url

    url = make_url(settings.connection_url)
    assert url.password == 'p@ss:/?#%'
    assert url.host == 'db'
    assert url.port == 5432


@pytest.mark.parametrize('status,body', [(401, 'private error'), (200, 'not json')])
def test_invalid_upstream_response_is_safe(setup_app, status, body):
    client, _ = setup_app()
    client.app.state.transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text=body)
    )
    response = client.get('/api/offers?q=travel')
    assert response.status_code == 502
    assert body not in response.text


def test_malformed_catalog_is_rejected_without_remote_mutations(setup_app):
    client, _ = setup_app()
    client.app.state.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={'unexpected': []})
    )
    assert client.get('/api/offers').status_code == 502


def test_offer_lookup_returns_exact_id_and_name(setup_app):
    client, tracker = setup_app()
    response = client.get('/api/offers/18001')
    assert response.status_code == 200
    assert response.json() == {'id': 18001, 'name': 'Travel MX'}
    assert tracker.calls == [('GET', '/offers/18001', None)]


def test_offer_lookup_reports_missing_id_without_creating_anything(setup_app):
    client, tracker = setup_app()
    response = client.get('/api/offers/999')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Оффер с таким ID не найден в Keitaro.'
    assert all(call[0] == 'GET' for call in tracker.calls)


@pytest.mark.parametrize('offer_id', ['0', '-1', '2147483648', 'abc'])
def test_offer_lookup_validates_id_before_tracker_request(setup_app, offer_id):
    client, tracker = setup_app()
    assert client.get(f'/api/offers/{offer_id}').status_code == 422
    assert tracker.calls == []


def test_offer_lookup_rejects_mismatched_upstream_id(setup_app):
    client, _ = setup_app()
    client.app.state.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={'id': 18002, 'name': 'Wrong offer'})
    )
    assert client.get('/api/offers/18001').status_code == 502


def test_search_queries_share_catalog_for_60_seconds(setup_app):
    client, tracker = setup_app()
    now = [0]
    client.app.state.keitaro_cache.clock = lambda: now[0]
    assert client.get('/api/offers?q=travel').status_code == 200
    assert client.get('/api/offers?q=18002').json() == [{'id': 18002, 'name': 'Shopping'}]
    now[0] = 59
    assert client.get('/api/offers?q=shopping').status_code == 200
    assert len(tracker.calls) == 1
    now[0] = 60
    assert client.get('/api/offers').status_code == 200
    assert len(tracker.calls) == 2


def test_id_validation_reuses_catalog_or_individual_lookup(setup_app):
    client, tracker = setup_app()
    assert client.get('/api/offers/18001').status_code == 200
    assert client.get('/api/offers/18001').status_code == 200
    assert len(tracker.calls) == 1
    assert client.get('/api/offers').status_code == 200
    assert client.get('/api/offers/18002').json() == {'id': 18002, 'name': 'Shopping'}
    assert len(tracker.calls) == 2


def test_missing_id_and_api_errors_are_not_cached(setup_app):
    client, tracker = setup_app()
    assert client.get('/api/offers/999').status_code == 404
    assert client.get('/api/offers/999').status_code == 404
    assert len(tracker.calls) == 2
    client.app.state.transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text='error')
    )
    assert client.get('/api/offers').status_code == 502
    client.app.state.transport = httpx.MockTransport(tracker)
    assert client.get('/api/offers').status_code == 200


def test_creation_uses_cached_references_but_rechecks_offer(setup_app):
    client, tracker = setup_app()
    now = [0]
    client.app.state.keitaro_cache.clock = lambda: now[0]
    assert client.get('/api/offers/18001').status_code == 200
    assert client.post('/api/campaigns', json=payload()).status_code == 201
    now[0] = 299
    assert client.post('/api/campaigns', json=payload()).status_code == 201
    assert len([c for c in tracker.calls if c[1] == '/domains']) == 1
    assert len([c for c in tracker.calls if c[1] == '/groups']) == 1
    assert len([c for c in tracker.calls if c[1] == '/traffic_sources']) == 1
    assert len([c for c in tracker.calls if c[1] == '/offers/18001']) == 3
    now[0] = 300
    assert client.post('/api/campaigns', json=payload()).status_code == 201
    assert len([c for c in tracker.calls if c[1] == '/domains']) == 2


def test_cached_offer_does_not_allow_creation_after_offer_removed(setup_app):
    client, tracker = setup_app()
    assert client.get('/api/offers/18001').status_code == 200
    client.app.state.transport = httpx.MockTransport(
        lambda request: httpx.Response(404, json={'error': 'deleted'})
    )
    assert client.post('/api/campaigns', json=payload()).status_code == 422
    assert not [c for c in tracker.calls if c[0] == 'POST']


@pytest.mark.parametrize('path', ['/', '/campaigns', '/campaigns/1'])
def test_pages_version_assets_and_do_not_cache_old_html(setup_app, path):
    client, _ = setup_app()
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assets = re.findall(r'(?:src|href)="([^\"]*/static/[^\"]+)"', response.text)
    assert len(assets) >= 3
    for asset in assets:
        version = parse_qs(urlsplit(asset).query).get('v')
        assert version and len(version[0]) >= 8
        assert client.get(asset).status_code == 200
