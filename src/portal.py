"""Portal monitor and explicit timestamped feed adapter. Never invent freshness."""
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from app import Point, Report, Assignment, Role, Setting, Outbox, db, send, button, roles, edit_message


def ingest(records):
    count = 0
    with db.atomic():
        for row in records:
            # Source timestamps are mandatory. Polling time is NOT source freshness.
            stamp = row['updated_at']
            if isinstance(stamp, (float, int)):
                updated = float(stamp)
            else:
                parsed = datetime.fromisoformat(stamp.replace('Z','+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Moscow'))
                updated = parsed.timestamp()
            if not math.isfinite(updated) or updated > time.time()+300:
                logging.warning('Skipping station %s: invalid/future source timestamp %s; previous data retained',row.get('id','unknown'),stamp)
                continue
            lat, lon = float(row['latitude']), float(row['longitude'])
            if not math.isfinite(lat+lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('Invalid station coordinates')
            sid = str(row['id'])
            p = Point.get_or_none(Point.source == 'portal:'+sid)
            if p:
                latest = Report.select().where((Report.station == p.id) & (Report.source == 'portal')).order_by(Report.updated.desc()).first()
                if latest and latest.updated > updated:
                    continue
            extra = {'fuels': list(row['fuels']), 'portal_status': row['fuels']}
            for field in ['queue_text', 'restrictions', 'availability_basis', 'source_url']:
                if field in row:
                    extra[field] = row[field]
            values = dict(name=row['name'], address=row['address'], lat=lat, lon=lon,
                          extra=json.dumps(extra, ensure_ascii=False))
            if p:
                Point.update(**values).where(Point.id == p.id).execute()
            else:
                p = Point.create(source='portal:'+sid, **values)
            for fuel, status in row['fuels'].items():
                if fuel not in ['АИ-92','АИ-95','ДТ'] or type(status['available']) is not bool:
                    continue
                Report.insert(station=p.id, fuel=fuel, available=status['available'],
                              queue=int(status.get('queue_minutes', -1)), updated=updated, source='portal').on_conflict(
                    conflict_target=[Report.station,Report.fuel,Report.source],
                    update={Report.available:status['available'],Report.queue:int(status.get('queue_minutes',-1)),Report.updated:updated}).execute()
                count += 1
    return count


def notify_once(key, uid, text, interval=3600):
    last=Setting.get_or_none(Setting.key == key)
    if not last or float(last.value)+interval < time.time():
        send(uid,text)
        Setting.replace(key=key,value=str(time.time())).execute()


def stale_rows(uid, now=None):
    now = time.time() if now is None else now
    permissions = roles(uid)
    assigned = {a.station for a in Assignment.select().where(Assignment.uid == str(uid))} if permissions & {'operator','admin'} else set()
    rows = []
    for p in Point.select():
        # Match the importer's five-minute source-clock tolerance. Otherwise a
        # newly accepted record briefly disappears and is reported as missing.
        newest=Report.select().where((Report.station == p.id) & (Report.source.in_(['portal','operator'])) & (Report.updated <= now+300)).order_by(Report.updated.desc()).first()
        updated = newest.updated if newest else None
        age = now-updated if updated is not None else float('inf')
        if ('admin' in permissions and age > 7200) or (p.id in assigned and age > 3600):
            rows.append({'id': p.id, 'address': p.address, 'updated': updated})
    rows.sort(key=lambda row: (row['updated'] if row['updated'] is not None else float('-inf'), row['address'], row['id']))
    return rows


def stamp(updated):
    return datetime.fromtimestamp(updated, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M') if updated is not None else 'время не получено'


def summary_text(rows):
    lines = ['АЗС с устаревшими сведениями — {}.\nСамые давние обновления:'.format(len(rows))]
    for index, row in enumerate(rows[:3], 1):
        date = stamp(row['updated'])+(' МСК' if row['updated'] is not None else '')
        lines.append('{}. {}\nПоследнее обновление: {}.'.format(index, row['address'], date))
    lines.append('Порядок: от самых давних сведений к более свежим.')
    return '\n\n'.join(lines)


def full_pages(rows):
    # Group common timestamps to fit the full regional list in one MAX message.
    # Larger future registries retain the same message and use page navigation.
    pages = []
    header = 'АЗС с устаревшими сведениями — {}.\nОт давних обновлений к более свежим. Время — МСК.\n'.format(len(rows))
    page = header
    previous = object()
    for index, row in enumerate(rows, 1):
        updated = row['updated']
        group = '\nПоследнее обновление: {}\n'.format(stamp(updated)) if previous != updated else ''
        line = '{}. {}\n'.format(index, row['address'])
        if len(page)+len(group)+len(line) > 3850:
            pages.append(page.rstrip())
            page = header+'\nПоследнее обновление: {}\n'.format(stamp(updated))
            group = ''
        page += group+line
        previous = updated
    pages.append(page.rstrip())
    return pages


def expand_stale_message(uid, message_id, payload):
    if not roles(uid) & {'admin','operator'}:
        edit_message(uid, message_id, 'АЗС с устаревшими сведениями: доступ к списку больше недоступен.')
        return
    rows = stale_rows(uid)
    if not rows:
        edit_message(uid, message_id, 'АЗС с устаревшими сведениями: сейчас таких АЗС нет.')
        return
    try:
        number = 0 if payload == 'stale:all' else int(payload.removeprefix('stale:page:'))
    except ValueError:
        return
    pages = full_pages(rows)
    number = max(0, min(number, len(pages)-1))
    navigation = []
    if number:
        navigation.append(button('Назад', 'stale:page:'+str(number-1)))
    if number+1 < len(pages):
        navigation.append(button('Далее', 'stale:page:'+str(number+1)))
    text = pages[number]
    if len(pages)>1:
        text += '\n\nСтраница {} из {}.'.format(number+1,len(pages))
    edit_message(uid, message_id, text, [navigation] if navigation else None)


def stale_notifications():
    # Discard only unsent legacy per-station reminders, not unrelated messages.
    for pending in Outbox.select():
        text = json.loads(pending.body).get('text','')
        if text.startswith('Сведения по АЗС ') and ('не обновлялись более часа.' in text or 'не обновлялись более двух часов.' in text):
            pending.delete_instance()
    recipients = {r.uid for r in Role.select().where(Role.role.in_(['admin','operator']))}
    now = time.time()
    for uid in recipients:
        key = 'stale-summary:'+uid
        last = Setting.get_or_none(Setting.key == key)
        if last and float(last.value)+3600 >= now:
            continue
        rows = stale_rows(uid, now)
        if rows:
            send(uid, summary_text(rows), [[button('Показать все','stale:all')]])
            Setting.replace(key=key, value=str(now)).execute()


def failure_detail(error):
    # Do not expose response bodies, query strings, headers or feed credentials.
    if isinstance(error, aiohttp.ClientResponseError):
        detail = '{}: HTTP {}'.format(type(error).__name__, error.status)
        diagnostic = getattr(error, 'portal_diagnostic', '')
        return detail+'\n'+diagnostic if diagnostic else detail
    if isinstance(error, asyncio.TimeoutError):
        return '{}: превышено время ожидания ответа'.format(type(error).__name__)
    if isinstance(error, aiohttp.ClientConnectorError):
        return '{}: ошибка соединения, errno={}'.format(type(error).__name__, getattr(error.os_error, 'errno', None))
    return type(error).__name__


async def monitor(app):
    await asyncio.sleep(1)
    while True:
        try:
            client=app['client']
            feed=os.environ.get('FUEL_FEED_URL','')
            if feed:
                async with client.get(feed) as r:
                    r.raise_for_status()
                    data=await r.json()
                    count=ingest(data['stations'] if isinstance(data,dict) else data)
                    logging.info('Portal feed imported %s fuel records',count)
            else:
                from datalens import fetch_map
                records = await fetch_map(client)
                count = ingest(records)
                Setting.replace(key='portal_reachable', value=str(time.time())).execute()
                Setting.replace(key='portal_import', value=json.dumps({'stations':len(records), 'reports':count, 'loaded_at':time.time()})).execute()
                logging.info('DataLens import committed: received %s stations, updated %s fuel records',len(records),count)
            with db.atomic():
                Setting.replace(key='portal_last_success', value=str(time.time())).execute()
                Setting.replace(key='portal_failure_state', value='{}').execute()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, IndexError) as error:
            detail = failure_detail(error)
            logging.warning('Portal import unavailable (%s: %s); saved source timestamps retained',type(error).__name__,detail)
            with db.atomic():
                saved = Setting.get_or_none(Setting.key == 'portal_failure_state')
                state = json.loads(saved.value) if saved else {}
                now = time.time()
                state['count'] = state.get('count', 0)+1
                state.setdefault('first', now)
                state['last'] = now
                state['error'] = detail
                Setting.replace(key='portal_failure_state', value=json.dumps(state)).execute()
                if state['count'] >= 3:
                    last_success = Setting.get_or_none(Setting.key == 'portal_last_success') or Setting.get_or_none(Setting.key == 'portal_reachable')
                    source = 'настроенный источник FUEL_FEED_URL' if feed else 'DataLens, charts/api/run, чарт 1mfpck09rnyck'
                    message = ('Не удалось обновить данные топливного портала после трёх последовательных неудачных попыток с интервалом 5 минут. '
                               'Бот продолжает использовать последние сохранённые сведения с указанием даты источника.\n\n'
                               'Источник: {}.\nОшибка: {}.\nНеудачных попыток подряд: {}.\n'
                               'Первая ошибка: {} МСК.\nПоследняя попытка: {} МСК.\n'
                               'Последняя успешная загрузка: {}.\nПовторное получение — через 5 минут.').format(
                                   source, detail, state['count'], stamp(state['first']), stamp(now),
                                   stamp(float(last_success.value))+' МСК' if last_success else 'время не сохранено')
                    for a in Role.select().where(Role.role == 'admin'):
                        notify_once('portal-failure:'+a.uid, a.uid, message,3600)
        with db.atomic():
            stale_notifications()
        await asyncio.sleep(300)
