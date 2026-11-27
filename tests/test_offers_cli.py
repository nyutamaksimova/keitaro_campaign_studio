import json

import httpx

import offers
from app.config import Settings
from app.keitaro import KeitaroClient


def configure_tracker(monkeypatch, catalog=None, status=200):
    settings = Settings(_env_file=None, keitaro_api_key='secret')

    def respond(request):
        assert request.method == 'GET'
        assert request.url.path == '/admin_api/v1/offers'
        assert request.headers['Api-Key'] == 'secret'
        return httpx.Response(status, json=catalog)

    tracker = KeitaroClient(settings, httpx.MockTransport(respond))
    monkeypatch.setattr(offers, 'Settings', lambda: settings)
    monkeypatch.setattr(offers, 'KeitaroClient', lambda settings: tracker)


def test_export_saves_all_offers_and_fields_as_utf8(monkeypatch, tmp_path, capsys):
    catalog = [{'id': i, 'name': f'Оффер {i}', 'url': 'https://example.com'}
               for i in range(1, 171)]
    configure_tracker(monkeypatch, catalog)
    destination = tmp_path / 'exports' / 'offers.json'
    assert offers.main(['export', '--output', str(destination)]) == 0
    assert json.loads(destination.read_text(encoding='utf-8')) == catalog
    assert 'Оффер' in destination.read_text(encoding='utf-8')
    assert '170' in capsys.readouterr().out


def test_list_live_offers_prints_id_name_and_total(monkeypatch, capsys):
    configure_tracker(monkeypatch, [{'id': 18001, 'name': 'Travel MX'}])
    assert offers.main(['list']) == 0
    output = capsys.readouterr().out
    assert '18001' in output
    assert 'Travel MX' in output
    assert 'Всего офферов: 1' in output
    assert 'secret' not in output


def test_list_saved_json_needs_no_key_or_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(offers, 'Settings', lambda: (_ for _ in ()).throw(AssertionError()))
    source = tmp_path / 'offers.json'
    source.write_text('[{"id": 123, "name": "Saved offer"}]', encoding='utf-8')
    assert offers.main(['list', '--file', str(source)]) == 0
    assert 'Saved offer' in capsys.readouterr().out


def test_failed_export_does_not_overwrite_existing_file(monkeypatch, tmp_path, capsys):
    configure_tracker(monkeypatch, {'detail': 'secret'}, status=401)
    destination = tmp_path / 'offers.json'
    destination.write_text('original', encoding='utf-8')
    assert offers.main(['export', '--output', str(destination)]) == 1
    assert destination.read_text(encoding='utf-8') == 'original'
    assert 'secret' not in capsys.readouterr().err


def test_missing_key_is_reported_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(offers, 'Settings', lambda: Settings(_env_file=None))
    assert offers.main(['list']) == 1
    assert 'KEITARO_API_KEY' in capsys.readouterr().err


def test_invalid_saved_file_is_reported(tmp_path, capsys):
    source = tmp_path / 'offers.json'
    source.write_text('{"unexpected": []}', encoding='utf-8')
    assert offers.main(['list', '--file', str(source)]) == 1
    assert 'формат' in capsys.readouterr().err
