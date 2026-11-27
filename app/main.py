from contextlib import asynccontextmanager
from pathlib import Path

import pycountry
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.assets import asset_version
from app.cache import TTLCache
from app.config import Settings
from app.db import make_engine
from app.keitaro import KeitaroClient, KeitaroError
from app.models import Operation
from app.schemas import CampaignCreate, OperationOut
from app.service import create_campaign

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / 'templates', context_processors=[
    lambda request: {'asset_version': request.app.state.asset_version},
])


def get_session(request: Request):
    with request.app.state.sessions() as session:
        yield session


def get_tracker(request: Request):
    settings = request.app.state.settings
    if not settings.keitaro_api_key.get_secret_value():
        raise HTTPException(503, 'Добавьте KEITARO_API_KEY в .env и перезапустите приложение.')
    return KeitaroClient(settings, request.app.state.transport, request.app.state.keitaro_cache)


def create_app(settings=None, engine=None, transport=None):
    settings = settings or Settings()
    owns_engine = engine is None
    engine = engine or make_engine(settings.connection_url)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(title='Keitaro Campaign Creator', lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessionmaker(engine, expire_on_commit=False)
    app.state.transport = transport
    app.state.keitaro_cache = TTLCache()
    app.state.asset_version = asset_version(ROOT / 'static')
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

    @app.exception_handler(KeitaroError)
    async def tracker_error(request, exc):
        return JSONResponse(status_code=502, content={'detail': str(exc)})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={
            'detail': 'База данных недоступна. Проверьте PostgreSQL '
                      'и выполните alembic upgrade head. '
                      'Если запрос уже выполнялся, проверьте журнал и Keitaro перед повтором.'
        })

    @app.get('/', include_in_schema=False)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name='index.html',
                                          headers={'Cache-Control': 'no-store'}, context={
            'configured': bool(settings.keitaro_api_key.get_secret_value()),
            'domain_id': settings.keitaro_domain_id, 'group_id': settings.keitaro_group_id,
            'source_id': settings.keitaro_traffic_source_id,
            'countries': sorted(pycountry.countries, key=lambda country: country.name),
        })

    @app.get('/health')
    def health(session: Session = Depends(get_session)):
        session.execute(text('SELECT 1'))
        return {'status': 'ok', 'keitaro_configured': bool(
            settings.keitaro_api_key.get_secret_value())}

    @app.get('/api/offers')
    def offers(q: str = Query(default='', max_length=255),
               tracker: KeitaroClient = Depends(get_tracker)):
        query = q.strip().casefold()
        catalog = tracker.catalog('/offers')
        return [{'id': item['id'], 'name': str(item.get('name', ''))} for item in catalog
                if isinstance(item.get('id'), int) and item['id'] > 0
                and (query in str(item.get('name', '')).casefold()
                     or query in str(item['id']))][:50]

    @app.get('/api/offers/{offer_id}')
    def offer_by_id(offer_id: int = PathParam(gt=0, le=2147483647),
                    tracker: KeitaroClient = Depends(get_tracker)):
        try:
            offer = tracker.offer(offer_id)
        except KeitaroError as exc:
            if exc.status_code == 404:
                raise HTTPException(404, 'Оффер с таким ID не найден в Keitaro.') from exc
            raise
        return offer

    @app.get('/api/operations', response_model=list[OperationOut])
    def operations(session: Session = Depends(get_session)):
        query = select(Operation).order_by(Operation.created_at.desc()).limit(20)
        return session.scalars(query).all()

    @app.post('/api/campaigns', response_model=OperationOut, status_code=201)
    def campaigns(data: CampaignCreate, response: Response,
                  session: Session = Depends(get_session),
                  tracker: KeitaroClient = Depends(get_tracker)):
        operation, created = create_campaign(session, tracker, settings, data)
        response.status_code = 201 if created else 200
        return operation

    from app.editor_routes import build_editor_router

    app.include_router(build_editor_router(get_session, get_tracker, templates))
    return app
