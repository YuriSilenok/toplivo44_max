"""Read-only public map. Only explicitly allowed station fields leave the DB."""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

CATEGORIES = {'rtk': 'РТК', 'szo': 'СЗО', 'additional': 'Дополнительные точки'}


def wifi_category(point, extra):
    category = extra.get('category')
    if category in CATEGORIES:
        return category
    # Stable import IDs identify the existing Rostelecom registry.
    return 'rtk' if point.source.startswith('import:rtk-') else 'additional'


def register_routes(application, kind):
    assets = Path(__file__).parent / 'map'

    async def page(request):
        title = 'АЗС Костромской области' if kind == 'fuel' else 'Бесплатный Wi-Fi'
        if kind == 'fuel':
            control = '<label>Топливо<select id="filter"><option value="">Все АЗС</option><option>АИ-92</option><option>АИ-95</option><option>ДТ</option></select></label>'
        else:
            control = '<label>Категория<select id="filter"><option value="">Все точки</option>' + ''.join('<option value="{}">{}</option>'.format(key, value) for key, value in CATEGORIES.items()) + '</select></label>'
        html = (assets / 'index.html').read_text(encoding='utf-8-sig').replace('__TITLE__', title).replace('__FILTER__', control)
        return web.Response(text=html, content_type='text/html', headers={
            'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'strict-origin-when-cross-origin'})

    async def data(request):
        from app import Point, latest_fuel_reports
        now = time.time()
        reports = {}
        if kind == 'fuel':
            for report in latest_fuel_reports().values():
                if report.fuel in ['АИ-92', 'АИ-95', 'ДТ']:
                    reports.setdefault(report.station, {})[report.fuel] = report
        points = []
        for point in Point.select():
            row = dict(id=point.id, name=point.name, address=point.address,
                       latitude=point.lat, longitude=point.lon)
            extra = json.loads(point.extra)
            if kind == 'fuel':
                row['queue'] = extra.get('queue_text', '')
                row['restrictions'] = extra.get('restrictions', '')
                row['fuels'] = {}
                for brand, report in reports.get(point.id, {}).items():
                    status = extra.get('portal_status', {}).get(brand, {})
                    row['fuels'][brand] = dict(available=report.available,
                        price=status.get('price') if report.source == 'portal' else None, updated=report.updated,
                        source='Оператор АЗС' if report.source == 'operator' else 'Портал',
                        queue=('более 30 минут' if report.queue == 31 else '{} минут'.format(report.queue)) if report.source == 'operator' else '',
                        updated_text=datetime.fromtimestamp(report.updated, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M'),
                        stale=now-report.updated > 3600)
            else:
                row['category'] = wifi_category(point, extra)
                row['category_label'] = CATEGORIES[row['category']]
            points.append(row)
        return web.json_response(dict(kind=kind, points=points), headers={
            'Cache-Control': 'public, max-age=30', 'X-Content-Type-Options': 'nosniff'})

    application.router.add_get('/map', page)
    application.router.add_get('/map/data', data)
    application.router.add_static('/map/assets', assets, show_index=False)
