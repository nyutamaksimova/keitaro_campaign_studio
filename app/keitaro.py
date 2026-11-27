from hashlib import sha256

import httpx


class KeitaroError(Exception):
    def __init__(self, message, *, status_code=None, uncertain=False):
        super().__init__(message)
        self.status_code = status_code
        self.uncertain = uncertain


class KeitaroClient:
    """Only the HTTP boundary knows the API key; never expose raw response bodies."""

    def __init__(self, settings, transport=None, cache=None):
        self.settings = settings
        self.transport = transport
        self.cache = cache

    def _cache_key(self, path, params=None):
        return (
            self.settings.keitaro_base_url,
            sha256(self.settings.keitaro_api_key.get_secret_value().encode()).digest(),
            path, tuple(sorted((params or {}).items())),
        )

    def request(self, method, path, *, json=None, params=None):
        with httpx.Client(
            base_url=self.settings.keitaro_base_url + '/',
            headers={'Api-Key': self.settings.keitaro_api_key.get_secret_value()},
            timeout=self.settings.keitaro_timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            try:
                response = client.request(method, path.lstrip('/'), json=json, params=params)
            except httpx.RequestError as exc:
                raise KeitaroError(
                    'Нет ответа от Keitaro. Проверьте соединение и состояние операции.',
                    uncertain=method != 'GET',
                ) from exc
        if not response.is_success:
            message = ('Keitaro отклонил API-ключ.' if response.status_code in (401, 403)
                       else f'Keitaro вернул HTTP {response.status_code}.')
            raise KeitaroError(message, status_code=response.status_code,
                               uncertain=method != 'GET' and response.status_code >= 500)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KeitaroError('Некорректный JSON в ответе Keitaro.',
                               uncertain=method != 'GET') from exc

    def catalog(self, path, params=None):
        def load():
            data = self.request('GET', path, params=params)
            if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                raise KeitaroError('Некорректный формат каталога Keitaro.')
            return data

        ttl = {'/offers': 60, '/domains': 300, '/groups': 300, '/traffic_sources': 300}
        if self.cache is not None and path in ttl:
            return self.cache.get_or_load(self._cache_key(path, params), ttl[path], load)
        return load()

    def offer(self, offer_id):
        # A search may have already loaded this offer as part of the full catalog.
        if self.cache is not None:
            catalog = self.cache.peek(self._cache_key('/offers'))
            if catalog is not None:
                match = next((item for item in catalog
                              if str(item.get('id')) == str(offer_id)
                              and isinstance(item.get('name'), str)), None)
                if match is not None:
                    return {'id': offer_id, 'name': match['name']}

        def load():
            data = self.request('GET', f'/offers/{offer_id}')
            if (not isinstance(data, dict) or str(data.get('id')) != str(offer_id)
                    or not isinstance(data.get('name'), str)):
                raise KeitaroError('Некорректный ответ Keitaro при проверке оффера.')
            return {'id': offer_id, 'name': data['name']}

        if self.cache is not None:
            return self.cache.get_or_load(self._cache_key(f'/offers/{offer_id}'), 60, load)
        return load()

    @staticmethod
    def resource_id(data):
        try:
            value = int(data['id'])
            if value <= 0:
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise KeitaroError('Keitaro не вернул ID созданного объекта.', uncertain=True) from exc
