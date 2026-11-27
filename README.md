# Keitaro Campaign Studio

Приложение для создания кампаний и редактирования офферов в Keitaro.

## Стек

Python 3.12+, FastAPI, SQLAlchemy 2, PostgreSQL 17, Alembic.
Интерфейс — Jinja2, JavaScript и CSS.

## Запуск

Нужны Docker Desktop в режиме Linux containers и Docker Compose.
Все команды выполняются из корня проекта.

1. Скопируйте файл настроек:

   ```powershell
   Copy-Item .env.example .env
   ```

   Для Linux/macOS: `cp .env.example .env`.

2. Укажите в `.env` пароль `POSTGRES_PASSWORD` и ключ `KEITARO_API_KEY`.
   Адрес API, домен, группа и источник уже настроены для тестового стенда;
   при необходимости измените соответствующие переменные `KEITARO_*`.

3. Запустите приложение и базу:

   ```sh
   docker compose up -d --build --wait
   ```

   Миграции Alembic применяются автоматически перед запуском приложения.

4. Откройте [создание кампании](http://127.0.0.1:8000)
   или [редактор кампаний](http://127.0.0.1:8000/campaigns).

Для ручного применения миграций в работающем контейнере:

```sh
docker compose exec app python -m alembic upgrade head
```

После изменения `.env` пересоздайте контейнер приложения:

```sh
docker compose up -d --force-recreate app
```

Просмотр логов и остановка с сохранением данных PostgreSQL:

```sh
docker compose logs --tail=50 app
docker compose down
```

`compose.override.yaml` подключает локальные файлы и включает автоматическую
перезагрузку при разработке. Для запуска без этого режима:

```sh
docker compose -f compose.yaml up -d --build --wait
```
