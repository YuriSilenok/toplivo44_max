"""Read-only public map. Only explicitly allowed station fields leave the DB."""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web


def register_routes(application, kind):
    assets = Path(__file__).parent / 'map'

    async def page(request):
        return web.FileResponse(assets / 'index.html', headers={
            'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'strict-origin-when-cross-origin'})

    async def data(request):
        from app import Point, Report
        now = time.time()
        reports = {}
        if kind == 'fuel':
            for report in Report.select().where((Report.source == 'portal') & (Report.updated <= now)):
                if report.fuel in ['АИ-92', 'АИ-95', 'ДТ']:
                    reports.setdefault(report.station, {})[report.fuel] = report
        points = []
        for point in Point.select():
            row = dict(id=point.id, name=point.name, address=point.address,
                       latitude=point.lat, longitude=point.lon)
            if kind == 'fuel':
                extra = json.loads(point.extra)
                row['queue'] = extra.get('queue_text', '')
                row['restrictions'] = extra.get('restrictions', '')
                row['fuels'] = {}
                for brand, report in reports.get(point.id, {}).items():
                    status = extra.get('portal_status', {}).get(brand, {})
                    row['fuels'][brand] = dict(available=report.available,
                        price=status.get('price'), updated=report.updated,
                        updated_text=datetime.fromtimestamp(report.updated, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M'),
                        stale=now-report.updated > 3600)
            points.append(row)
        return web.json_response(dict(kind=kind, points=points), headers={
            'Cache-Control': 'public, max-age=30', 'X-Content-Type-Options': 'nosniff'})

    application.router.add_get('/map', page)
    application.router.add_get('/map/data', data)
    application.router.add_static('/map/assets', assets, show_index=False)
