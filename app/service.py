from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.keitaro import KeitaroError
from app.models import Operation


def replay(operation, data):
    if (operation.name, operation.geo, operation.offer_id) != (data.name, data.geo, data.offer_id):
        raise HTTPException(409, 'Этот request_id уже использован с другими параметрами.')
    if operation.status != 'completed':
        raise HTTPException(409, 'Запрос уже обработан или выполняется. Проверьте журнал операций.')
    return operation, False


def campaign_url(domain, alias):
    value = domain.get('name', '')
    if not isinstance(value, str):
        return None
    if '://' not in value:
        value = 'https://' + value
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username:
        return None
    return value.rstrip('/') + '/' + alias


def create_campaign(session, tracker, settings, data):
    request_id = str(data.request_id)
    existing = session.get(Operation, request_id)
    if existing:
        return replay(existing, data)

    operation = Operation(request_id=request_id, name=data.name, geo=data.geo,
                          offer_id=data.offer_id, alias='c' + data.request_id.hex[:20])
    session.add(operation)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return replay(session.get(Operation, request_id), data)

    remote_campaign_id = None
    remote_stream_ids = []
    try:
        # Offer validation always bypasses the UI cache before any mutating call.
        # Reference catalogs can be reused for up to five minutes.
        try:
            offer = tracker.request('GET', f'/offers/{data.offer_id}')
        except KeitaroError as exc:
            if exc.status_code == 404:
                raise HTTPException(422, 'Оффер с таким ID не найден в Keitaro.') from exc
            raise
        if not isinstance(offer, dict) or str(offer.get('id')) != str(data.offer_id):
            raise KeitaroError('Некорректный ответ Keitaro при проверке оффера.')
        domain = None
        for path, identifier, params, label in (
            ('/domains', settings.keitaro_domain_id, None, 'домен'),
            ('/groups', settings.keitaro_group_id, {'type': 'campaigns'}, 'группа'),
            ('/traffic_sources', settings.keitaro_traffic_source_id, None, 'источник'),
        ):
            match = next((item for item in tracker.catalog(path, params)
                          if str(item.get('id')) == str(identifier)), None)
            if match is None:
                raise KeitaroError(f'Настроенный {label} ID {identifier} не найден в Keitaro.')
            if path == '/domains':
                domain = match

        result = tracker.request('POST', '/campaigns', json={
            'name': data.name, 'alias': operation.alias, 'type': 'position',
            'state': 'disabled', 'domain_id': settings.keitaro_domain_id,
            'group_id': settings.keitaro_group_id,
            'traffic_source_id': settings.keitaro_traffic_source_id,
        })
        remote_campaign_id = tracker.resource_id(result)
        operation.campaign_id = remote_campaign_id
        operation.campaign_url = campaign_url(domain, operation.alias)
        session.commit()

        geo_stream = tracker.request('POST', '/streams', json={
            'campaign_id': operation.campaign_id, 'name': f'Geo {data.geo} → Google',
            'type': 'regular', 'position': 1, 'schema': 'redirect', 'state': 'active',
            'action_type': 'http', 'action_payload': 'https://google.com',
            'filters': [{'name': 'country', 'mode': 'accept', 'payload': [data.geo]}],
        })
        remote_stream_ids = [tracker.resource_id(geo_stream)]
        operation.stream_ids = remote_stream_ids
        session.commit()
        offer_stream = tracker.request('POST', '/streams', json={
            'campaign_id': operation.campaign_id, 'name': 'Оффер', 'type': 'default',
            'position': 2, 'schema': 'landings', 'state': 'active',
            'offers': [{'offer_id': data.offer_id, 'share': 100, 'state': 'active'}],
        })
        remote_stream_ids = [*remote_stream_ids, tracker.resource_id(offer_stream)]
        operation.stream_ids = remote_stream_ids
        session.commit()
        tracker.request('POST', f'/campaigns/{operation.campaign_id}/enable')
        operation.status = 'completed'
        session.commit()
        return operation, True
    except (KeitaroError, HTTPException) as exc:
        operation.error = str(exc) if isinstance(exc, KeitaroError) else str(exc.detail)
        operation.status = 'uncertain' if getattr(exc, 'uncertain', False) else 'failed'
        if operation.campaign_id:
            try:
                tracker.request('DELETE', f'/campaigns/{operation.campaign_id}')
                operation.status = 'failed'
                operation.error += ' Созданная кампания удалена вместе с потоками.'
            except KeitaroError:
                operation.status = 'cleanup_failed'
                operation.error += ' Не удалось подтвердить удаление; проверьте кампанию в Keitaro.'
        elif operation.status == 'uncertain':
            operation.error += (
                f' Проверьте кампанию по alias {operation.alias}; не повторяйте запрос.'
            )
        session.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, operation.error) from exc
    except SQLAlchemyError as exc:
        # Keep IDs outside ORM state: rollback expires mapped attributes.
        session.rollback()
        status = 'failed'
        message = 'Ошибка записи результата в базу данных.'
        if remote_campaign_id:
            try:
                tracker.request('DELETE', f'/campaigns/{remote_campaign_id}')
                message += f' Созданная кампания #{remote_campaign_id} удалена.'
            except KeitaroError:
                status = 'cleanup_failed'
                message += f' Проверьте удаление кампании #{remote_campaign_id} в Keitaro.'
        # If the database has recovered, retain recovery details in the journal.
        # If it has not, the 503 handler directs the user to inspect Keitaro.
        try:
            saved = session.get(Operation, request_id)
            if saved:
                saved.campaign_id = remote_campaign_id
                saved.stream_ids = remote_stream_ids
                saved.status = status
                saved.error = message
                session.commit()
        except SQLAlchemyError:
            session.rollback()
        raise HTTPException(503, message) from exc
