"""Export the full Keitaro offer catalog or print its ID/name list.

Run from the project root: python offers.py --help
No running FastAPI server or database is required.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.keitaro import KeitaroClient, KeitaroError


def validate_catalog(data):
    if not isinstance(data, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get('id'), int)
        or isinstance(item.get('id'), bool)
        or item['id'] <= 0
        or not isinstance(item.get('name'), str)
        for item in data
    ):
        raise ValueError('Некорректный формат офферов: ожидается JSON-массив с id и name.')
    return data


def fetch_offers():
    settings = Settings()
    if not settings.keitaro_api_key.get_secret_value().strip():
        raise ValueError('Добавьте KEITARO_API_KEY в .env или переменные окружения.')
    # Stand documentation specifies GET /offers returns the complete catalog.
    # Do not use the application's search endpoint: it limits results to 50.
    return validate_catalog(KeitaroClient(settings).catalog('/offers'))


def export_json(catalog, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=destination.parent,
            prefix='.offers-', suffix='.tmp', delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(catalog, output, ensure_ascii=False, indent=2)
            output.write('\n')
        # A failed request/write cannot truncate a previous successful export.
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def print_offers(catalog):
    print(f'{"ID":<12}Название')
    print('-' * 60)
    for offer in catalog:
        name = ''.join(char if char.isprintable() else ' ' for char in offer['name'])
        print(f'{offer["id"]:<12}{name}')
    print(f'\nВсего офферов: {len(catalog)}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Выгрузка и просмотр офферов Keitaro.')
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='Выгрузить полный каталог в JSON (UTF-8).')
    export.add_argument('--output', type=Path, default=Path('exports/offers.json'),
                        help='Файл назначения (по умолчанию exports/offers.json).')
    listing = commands.add_parser('list', help='Вывести ID и названия офферов.')
    listing.add_argument('--file', type=Path,
                         help='Прочитать сохранённый JSON вместо запроса к Keitaro.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'list' and args.file:
            catalog = validate_catalog(json.loads(args.file.read_text(encoding='utf-8-sig')))
        else:
            catalog = fetch_offers()
        if args.command == 'export':
            export_json(catalog, args.output)
            print(f'Выгружено офферов: {len(catalog)}. Файл: {args.output.resolve()}')
        else:
            print_offers(catalog)
        return 0
    except ValidationError:
        print('Ошибка: проверьте настройки Keitaro в .env.', file=sys.stderr)
    except KeitaroError as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
    except (ValueError, UnicodeError):
        print('Ошибка: некорректный формат данных или не задан KEITARO_API_KEY.', file=sys.stderr)
    except OSError:
        print('Ошибка: не удалось прочитать или записать файл. Проверьте путь и права.',
              file=sys.stderr)
    return 1


if __name__ == '__main__':
    # Consistent Cyrillic output when redirected to a file in Windows.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
