"""Three independent MAX applications, persistent inbox/outbox, HTTPS webhooks."""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import ssl
import time
import sys
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web
from peewee import (Model, SqliteDatabase, CharField, TextField, IntegerField,
                    FloatField, BooleanField, CompositeKey)

KIND = os.environ['BOT_KIND']
USERNAME = os.environ['BOT_USERNAME']
TOKEN = os.environ['MAX_BOT_TOKEN']
SECRET = os.environ['WEBHOOK_SECRET']
DATA = Path(os.environ.get('DATA_DIR', '.'))
DATA.mkdir(parents=True, exist_ok=True)
db = SqliteDatabase(str(DATA / 'bot.sqlite3'), pragmas={'journal_mode': 'wal', 'foreign_keys': 1}, timeout=10)


class Base(Model):
    class Meta:
        database = db


class Role(Base):
    uid = CharField()
    role = CharField()
    class Meta:
        primary_key = CompositeKey('uid', 'role')


class Session(Base):
    uid = CharField(primary_key=True)
    state = CharField(default='')
    data = TextField(default='{}')
    expires = FloatField(default=0)


class Invite(Base):
    token = CharField(primary_key=True)
    role = CharField()
    station = IntegerField(null=True)
    expires = FloatField()
    used = BooleanField(default=False)


class Point(Base):
    name = TextField()
    address = TextField()
    lat = FloatField()
    lon = FloatField()
    extra = TextField(default='{}')
    source = CharField(default='manual')


class Assignment(Base):
    uid = CharField()
    station = IntegerField()
    class Meta:
        primary_key = CompositeKey('uid', 'station')


class Report(Base):
    station = IntegerField()
    fuel = CharField()
    available = BooleanField()
    queue = IntegerField(default=0)
    updated = FloatField()
    source = CharField(default='portal')
    class Meta:
        indexes = ((('station', 'fuel', 'source'), True),)


class Witness(Base):
    uid = CharField(primary_key=True)
    case_id = IntegerField()
    last = FloatField()


class Case(Base):
    contacts = TextField(default='')


class CaseMessage(Base):
    case_id = IntegerField()
    body = TextField()
    media = TextField(default='[]')
    delivered = BooleanField(default=False)
    sent_at = FloatField(default=0)
    source_mid = TextField(default='')
    original_text = TextField(default='')


class Inbox(Base):
    key = CharField(primary_key=True)
    body = TextField()
    done = BooleanField(default=False)
    attempts = IntegerField(default=0)
    due = FloatField(default=0)


class Outbox(Base):
    uid = CharField()
    body = TextField()
    due = FloatField(default=0)
    attempts = IntegerField(default=0)


class Setting(Base):
    key = CharField(primary_key=True)
    value = TextField()


MODELS = [Role, Session, Invite, Point, Assignment, Report, Witness, Case, CaseMessage, Inbox, Outbox, Setting]
db.connect()
db.create_tables(MODELS)
if KIND == 'labor':
    columns = {column.name for column in db.get_columns('casemessage')}
    if 'sent_at' not in columns:
        db.execute_sql('ALTER TABLE casemessage ADD COLUMN sent_at REAL NOT NULL DEFAULT 0')
    if 'source_mid' not in columns:
        db.execute_sql("ALTER TABLE casemessage ADD COLUMN source_mid TEXT NOT NULL DEFAULT ''")
    if 'original_text' not in columns:
        db.execute_sql("ALTER TABLE casemessage ADD COLUMN original_text TEXT NOT NULL DEFAULT ''")
# Bootstrap is an explicit operator configuration, never 'first user wins'.
for uid in os.environ.get('ADMIN_IDS', '').split(','):
    if uid.strip():
        Role.get_or_create(uid=uid.strip(), role='admin')

FUELS = ['АИ-92', 'АИ-95', 'ДТ']
NO_FUEL = 'В последних полученных сведениях нет АЗС с подтверждённым наличием этого топлива. Попробуйте уточнить позже.'


def roles(uid):
    return {r.role for r in Role.select().where(Role.uid == str(uid))}


def state(uid, name=None, data=None):
    if name is not None:
        Session.replace(uid=str(uid), state=name, data=json.dumps(data or {}, ensure_ascii=False), expires=time.time()+600).execute()
        return
    s = Session.get_or_none(Session.uid == str(uid))
    return (s.state, json.loads(s.data)) if s and s.expires > time.time() else ('', {})


def button(text, payload=None, intent='default'):
    return {'type': 'callback', 'text': ('✓ ' if intent == 'positive' else '')+text, 'payload': payload or text}


def geo():
    return {'type': 'request_geo_location', 'text': 'Отправить мои координаты', 'quick': True}


def menu(uid):
    r = roles(uid)
    if KIND == 'labor':
        return [[button('Создать приглашение', '/invite')],
                [button('Просмотр заявления')], [button('Выгрузить заявления')],
                [button('Снять администратора')], [button('Список ролей')]] if 'admin' in r else []
    if KIND == 'wifi':
        rows = []
        if r & {'operator', 'admin'}:
            rows += [[button(t)] for t in ['Добавить точку', 'Редактировать точку', 'Удалить точку', 'Список точек']]
    else:
        rows = [[button('Найти топливо')]]
        if r & {'operator', 'admin'}:
            rows += [[button('Обновить сведения')]]
        if 'admin' in r:
            rows += [[button('Добавить АЗС')], [button('Привязать оператора')]]
    if 'admin' in r:
        rows += [[button('Создать ссылку на оператора')], [button('Создать ссылку на администратора')],
                 [button('Снять оператора')], [button('Снять администратора')], [button('Список ролей')]]
    if KIND in ['fuel', 'wifi'] and os.environ.get('MAP_ENABLED') == '1':
        rows.insert(0, [{'type': 'link', 'text': 'Карта всех точек',
                         'url': 'https://xn--e1afhclgq.xn--p1ai/max-bots/'+KIND+'/map'}])
    return rows


def send(uid, text, rows=None, attachments=None):
    attachments = list(attachments or [])
    if rows:
        attachments.append({'type': 'inline_keyboard', 'payload': {'buttons': rows}})
    Outbox.create(uid=str(uid), body=json.dumps({'text': text, 'attachments': attachments}, ensure_ascii=False))


def edit_message(uid, message_id, text, rows=None):
    attachments = [{'type': 'inline_keyboard', 'payload': {'buttons': rows}}] if rows else []
    Outbox.create(uid=str(uid), body=json.dumps({'text': text, 'attachments': attachments, 'notify': False, '_edit_mid': message_id}, ensure_ascii=False))


def cancel(uid, text):
    send(uid, text, [[button('Отмена')]])


def distance(a, b, c, d):
    p, q = math.radians(a), math.radians(c)
    x = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(d-b)/2)**2
    return 6371000*2*math.asin(math.sqrt(max(0, min(1, x))))


def coords(text):
    values = text.replace(';', ' ').replace(',', ' ').split()
    if len(values) != 2:
        raise ValueError('Укажите широту и долготу через пробел.')
    lat, lon = map(float, values)
    if not math.isfinite(lat+lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Координаты вне допустимого диапазона.')
    return lat, lon


def maps(p):
    return 'https://yandex.ru/maps/?pt={},{}&z=17&l=map'.format(p.lon, p.lat)


def invite(uid, role, station=None):
    lifetime = 900 if KIND == 'wifi' else (1800 if KIND == 'labor' else 86400)
    token = secrets.token_urlsafe(24)
    Invite.create(token=token, role=role, station=station, expires=time.time()+lifetime)
    send(uid, 'Одноразовая ссылка для роли «{}»: https://max.ru/{}?start=inv_{}\nДействует {} минут, используется один раз.'.format(role, USERNAME, token, lifetime//60))


def start(uid, payload=''):
    state(uid, '')
    if payload:
        token = payload.removeprefix('inv_') if hasattr(payload, 'removeprefix') else payload[4:] if payload.startswith('inv_') else payload
        i = Invite.get_or_none(Invite.token == token)
        if not i or i.used or i.expires <= time.time():
            send(uid, 'Ссылка недействительна или истекла.')
        else:
            changed = Invite.update(used=True).where((Invite.token == token) & (Invite.used == False) & (Invite.expires > time.time())).execute()
            if changed:
                Role.get_or_create(uid=str(uid), role=i.role)
                if i.station:
                    Assignment.get_or_create(uid=str(uid), station=i.station)
                send(uid, 'Вам назначена роль «{}».'.format(i.role), menu(uid))
                if KIND == 'labor' and i.role == 'admin':
                    for pending in CaseMessage.select().where(CaseMessage.delivered == False):
                        send(uid, pending.body, attachments=json.loads(pending.media))
                        pending.delivered = True
                        pending.save()
    if KIND == 'labor':
        send(uid, 'Это анонимный бот для сообщений о нарушениях трудовых отношений. Отправьте текст, фото или видео. Ответ по существу через бота не предусмотрен. При желании вы сможете добровольно передать контакты для личного ответа.', menu(uid))
    elif KIND == 'wifi':
        send(uid, 'Отправьте свою геолокацию, и я пришлю 3 ближайшие точки бесплатного Wi-Fi в Костроме.', menu(uid))
    else:
        send(uid, 'Здравствуйте! Здесь можно найти ближайшие АЗС с актуальными сведениями о топливе.', menu(uid))


def administrative(uid, text):
    actions = ['Создать ссылку на оператора', 'Создать ссылку на администратора', 'Снять оператора', 'Снять администратора', 'Список ролей', '/invite', 'Привязать оператора']
    if text not in actions:
        return False
    if 'admin' not in roles(uid):
        send(uid, 'Это действие доступно только администратору.')
        return True
    if text.startswith('Создать ссылку') or text == '/invite':
        role = 'operator' if text.endswith('оператора') else 'admin'
        if KIND == 'fuel' and role == 'operator':
            state(uid, 'invite_station')
            cancel(uid, 'Введите ID АЗС для оператора.\n'+point_list())
        else:
            invite(uid, role)
    elif text.startswith('Снять'):
        state(uid, 'remove_role', {'role': 'operator' if text.endswith('оператора') else 'admin'})
        cancel(uid, 'Введите ID пользователя.\n'+role_list())
    elif text == 'Привязать оператора':
        state(uid, 'assign')
        cancel(uid, 'Введите ID пользователя и ID АЗС через пробел.\n'+point_list())
    else:
        send(uid, role_list(), menu(uid))
    return True


def role_list():
    return 'Пользователи и роли:\n'+'\n'.join('{}: {}'.format(r.uid, r.role) for r in Role.select())[:3500]


def point_list():
    return '\n'.join('{}: {} — {}'.format(p.id, p.name, p.address) for p in Point.select())[:3400] or 'Точки пока не добавлены.'


def admin_state(uid, text, s, data):
    if s not in ['remove_role', 'remove_confirm', 'invite_station', 'assign']:
        return False
    if 'admin' not in roles(uid):
        state(uid, '')
        send(uid, 'Это действие доступно только администратору.')
        return True
    if s == 'remove_role':
        if not text.isdigit():
            raise ValueError('ID должен быть числом.')
        if data['role'] not in roles(text):
            send(uid, 'У пользователя нет указанной роли. Введите другой ID или нажмите «Отмена».')
            return True
        state(uid, 'remove_confirm', {'role': data['role'], 'target': text})
        send(uid, 'Снять роль {} у пользователя {}?'.format(data['role'], text), [[button('Да, снять')], [button('Отмена')]])
    elif s == 'remove_confirm':
        if text != 'Да, снять':
            raise ValueError('Нажмите «Да, снять» или «Отмена».')
        target, role = data['target'], data['role']
        if role == 'admin' and Role.select().where(Role.role == 'admin').count() <= 1 and 'admin' in roles(target):
            send(uid, 'Нельзя снять роль с последнего администратора.')
        else:
            Role.delete().where((Role.uid == target) & (Role.role == role)).execute()
            Session.delete().where(Session.uid == target).execute()
            send(uid, 'Роль снята.', menu(uid))
        state(uid, '')
    elif s == 'invite_station':
        p = Point.get_or_none(Point.id == int(text))
        if not p:
            raise ValueError('АЗС не найдена.')
        invite(uid, 'operator', p.id)
        state(uid, '')
    else:
        target, station = text.split()
        p = Point.get_or_none(Point.id == int(station))
        if not p or not roles(target) & {'operator', 'admin'}:
            raise ValueError('Нужны существующая АЗС и пользователь с ролью оператора.')
        Assignment.get_or_create(uid=target, station=p.id)
        state(uid, '')
        send(uid, 'Оператор привязан к АЗС.', menu(uid))
    return True


def wifi(uid, text, attachments):
    s, data = state(uid)
    operator = bool(roles(uid) & {'operator', 'admin'})
    location = next((a for a in attachments if a.get('type') == 'location'), None)
    if location and not s:
        lat, lon = float(location['latitude']), float(location['longitude'])
        coords('{} {}'.format(lat, lon))
        points = sorted(Point.select(), key=lambda p: distance(lat, lon, p.lat, p.lon))[:3]
        if not points:
            send(uid, 'Точки бесплатного Wi-Fi пока не добавлены.')
            return
        lines = ['Найдены ближайшие точки Wi-Fi:']
        for i, p in enumerate(points, 1):
            d = distance(lat, lon, p.lat, p.lon)
            lines.append('{}. {}\nАдрес: {}\nРасстояние: {}\nСсылка: {}'.format(i, p.name, p.address, '{:.0f} м'.format(d) if d < 1000 else '{:.2f} км'.format(d/1000), maps(p)))
        send(uid, '\n\n'.join(lines), menu(uid))
        return
    if text in ['Добавить точку', 'Редактировать точку', 'Удалить точку', 'Список точек']:
        if not operator:
            send(uid, 'Это действие доступно только оператору.')
            return
        if text == 'Список точек':
            send(uid, point_list(), menu(uid))
        else:
            s = {'Добавить точку': 'point_add', 'Редактировать точку': 'point_edit_id', 'Удалить точку': 'point_delete_id'}[text]
            state(uid, s)
            cancel(uid, 'Введите: Название; Адрес; Широта; Долгота; SSID; Пароль; Комментарий. Последние два поля можно оставить пустыми.' if s == 'point_add' else 'Введите ID точки.\n'+point_list())
        return
    if s.startswith('point_'):
        if not operator:
            state(uid, '')
            send(uid, 'Это действие доступно только оператору.')
            return
        if s in ['point_edit_id', 'point_delete_id']:
            p = Point.get_or_none(Point.id == int(text))
            if not p:
                raise ValueError('Точка не найдена.')
            state(uid, 'point_edit' if s == 'point_edit_id' else 'point_delete', {'id': p.id})
            if s == 'point_delete_id':
                send(uid, 'Удалить {}?'.format(p.name), [[button('Да'), button('Нет')]])
            else:
                e = json.loads(p.extra)
                cancel(uid, 'Текущие данные:\n{}; {}; {}; {}; {}; {}; {}\nОтправьте новые данные в том же формате.'.format(p.name, p.address, p.lat, p.lon, e.get('ssid',''), e.get('password',''), e.get('comment','')))
        elif s == 'point_delete':
            if text not in ['Да', 'Нет']:
                raise ValueError('Нажмите «Да» или «Нет».')
            if text == 'Да':
                Point.delete().where(Point.id == data['id']).execute()
            state(uid, '')
            send(uid, 'Точка Wi-Fi удалена.' if text == 'Да' else 'Удаление отменено.', menu(uid))
        else:
            parts = [p.strip() for p in text.split(';')]
            if len(parts) != 7 or not all(parts[i] for i in [0, 1, 4]):
                raise ValueError('Нужны 7 полей; название, адрес и SSID обязательны.')
            lat, lon = coords(parts[2]+' '+parts[3])
            values = dict(name=parts[0], address=parts[1], lat=lat, lon=lon, extra=json.dumps(dict(zip(['ssid','password','comment'], parts[4:])), ensure_ascii=False))
            if s == 'point_edit':
                existing = Point.get_by_id(data['id'])
                category = json.loads(existing.extra).get('category')
                if category in ['rtk', 'szo', 'additional']:
                    extra = json.loads(values['extra'])
                    extra['category'] = category
                    values['extra'] = json.dumps(extra, ensure_ascii=False)
            if s == 'point_add':
                p = Point.create(**values)
                result = 'Точка Wi-Fi добавлена. ID: {}.'.format(p.id)
            else:
                Point.update(**values).where(Point.id == data['id']).execute()
                result = 'Точка Wi-Fi обновлена.'
            state(uid, '')
            send(uid, result, menu(uid))
        return
    send(uid, 'Не удалось распознать геоточку. Отправьте свою геолокацию.', menu(uid))


def labor(uid, text, attachments, sent_at=0, source_mid=''):
    s, data = state(uid)
    if text in ['Просмотр заявления', 'Выгрузить заявления'] or text.startswith('casepage:') or s == 'case_lookup':
        if 'admin' not in roles(uid):
            state(uid, '')
            send(uid, 'Это действие доступно только администратору.')
            return
        from labor_archive import show_case
        if text.startswith('casepage:'):
            parts = text.split(':')
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                show_case(uid, int(parts[1]), int(parts[2]))
        elif text == 'Просмотр заявления':
            state(uid, 'case_lookup')
            cancel(uid, 'Введите номер заявления.')
        elif text == 'Выгрузить заявления':
            state(uid, '')
            Outbox.create(uid=str(uid), body=json.dumps({'text': 'Архив заявлений', '_labor_export': True}))
            send(uid, 'Подготовлю ZIP: тексты, фото и время отправки сообщений с видео, сгруппированные по номеру заявления.')
        elif text.isdigit():
            show_case(uid, int(text))
            state(uid, '')
        else:
            cancel(uid, 'Введите числовой номер заявления.')
        return
    if 'admin' in roles(uid):
        send(uid, 'Вы получаете заявления. Ответы по существу через бота не предусмотрены.', menu(uid))
        return
    from labor_media import normalize
    media, rejected_media = normalize(attachments)
    if rejected_media:
        logging.warning('Labor media unsupported: %s attachments', rejected_media)
        send(uid, 'Не удалось обработать часть фото или видео. Отправьте их заново отдельным сообщением.')
    if s == 'contact_entry' and not media:
        state(uid, 'contact_confirm', dict(data, contacts=text[:1500]))
        send(uid, 'Указанные вами данные будут переданы вместе с заявлением. Без них обращение останется анонимным и ответ вы не получите.\n\n'+text[:1500], [[button('Согласен передать контакты')], [button('Остаться анонимным')]])
        return
    if s == 'contact_confirm' and text == 'Согласен передать контакты':
        if not data.get('contacts') or not any(c.isdigit() for c in data['contacts']) and '@' not in data['contacts']:
            state(uid, 'contact_entry', data)
            cancel(uid, 'Укажите хотя бы один канал связи: телефон или email. ФИО — по желанию.')
            return
        Case.update(contacts=data['contacts']).where(Case.id == data['case']).execute()
        for a in Role.select().where(Role.role == 'admin'):
            send(a.uid, 'Заявление №{}\nКонтакты предоставлены очевидцем добровольно, с явного согласия:\n{}'.format(data['case'], data['contacts']))
        state(uid, '')
        send(uid, 'Контакты переданы вместе с заявлением.')
        return
    if text == 'Хочу получить ответ лично' and data.get('case'):
        state(uid, 'contact_entry', data)
        cancel(uid, 'Укажите только те данные, которые хотите передать: телефон и/или email, при желании ФИО. Перед передачей потребуется ваше подтверждение.')
        return
    if text == 'Остаться анонимным':
        if data.get('case'):
            Case.update(contacts='').where(Case.id == data['case']).execute()
        state(uid, '')
        send(uid, 'Обращение остаётся анонимным. Новые контакты передаваться не будут. Уже переданные сведения отозвать через бота нельзя.')
        return
    if text in ['Хочу получить ответ лично', 'Согласен передать контакты']:
        send(uid, 'Диалог завершён или истёк. Отправьте материалы обращения заново, чтобы выбрать передачу контактов.')
        return
    if not text and not media:
        send(uid, 'Отправьте текст, фото или видео.')
        return
    w = Witness.get_or_none(Witness.uid == str(uid))
    continuation = bool(w and time.time()-w.last <= 864000)
    case = Case.get_by_id(w.case_id) if continuation else Case.create()
    Witness.replace(uid=str(uid), case_id=case.id, last=time.time()).execute()
    admins = list(Role.select().where(Role.role == 'admin'))
    body = 'Заявление №{}{}\nСодержание: {}'.format(case.id, ' (продолжение)' if continuation else '', text[:3300])
    if case.contacts:
        body += '\nКонтакты предоставлены добровольно:\n'+case.contacts
    CaseMessage.create(case_id=case.id, body=body[:4000], media=json.dumps(media), delivered=bool(admins), sent_at=sent_at, source_mid=source_mid, original_text=text)
    for a in admins:
        # No forwarding: sender, source message id, geolocation and timestamps never leave this application.
        for offset in range(0, max(1, len(media)), 12):
            send(a.uid, body[:4000], attachments=media[offset:offset+12])
    send(uid, 'Заявление №{} принято. Материалы будут переданы администраторам.'.format(case.id), [[button('Хочу получить ответ лично')], [button('Остаться анонимным')]])
    state(uid, 'contact_offer', {'case': case.id})


def latest_fuel_reports():
    """Newest report per station/brand; absence is selected before availability filtering."""
    selected = {}
    for report in Report.select().where((Report.updated <= time.time()) & (Report.source.in_(['portal', 'operator']))):
        key = (report.station, report.fuel)
        old = selected.get(key)
        if old is None or (report.updated, report.source == 'operator') > (old.updated, old.source == 'operator'):
            selected[key] = report
    return selected


def can_report(uid, station):
    permissions = roles(uid)
    return 'admin' in permissions or ('operator' in permissions and Assignment.select().where((Assignment.uid == str(uid)) & (Assignment.station == station)).exists())


def report_prompt(uid, data):
    if data['index'] >= len(data['fuels']):
        state(uid, '')
        send(uid, 'Все марки обработаны. Сведения сохранены.', menu(uid))
        return
    station = Point.get_by_id(data['station'])
    state(uid, 'report_available', data)
    send(uid, '{}\n{} — наличие топлива?'.format(station.address, data['fuels'][data['index']]),
         [[button('Есть')], [button('Нет')], [button('Отмена')]])


def save_operator_report(uid, data, available, queue):
    brand = data['fuels'][data['index']]
    now = time.time()
    Report.insert(station=data['station'], fuel=brand, source='operator', available=available, queue=queue, updated=now).on_conflict(
        conflict_target=[Report.station, Report.fuel, Report.source],
        update={Report.available: available, Report.queue: queue, Report.updated: now}).execute()
    send(uid, 'Сохранено: {} — {}{}. Время: {} МСК.'.format(brand, 'есть' if available else 'нет',
         ', очередь '+('более 30 минут' if queue == 31 else str(queue)+' минут') if available else '',
         time.strftime('%d.%m.%Y %H:%M', time.localtime(now))))
    data['index'] += 1
    report_prompt(uid, data)


def fuel(uid, text, attachments):
    s, data = state(uid)
    r = roles(uid)
    if text == 'Обновить сведения':
        if not r & {'operator', 'admin'}:
            send(uid, 'Это действие доступно только оператору.')
            return
        assigned = {a.station for a in Assignment.select().where(Assignment.uid == str(uid))}
        stations = list(Point.select() if 'admin' in r else Point.select().where(Point.id.in_(assigned)))
        if not stations:
            send(uid, 'К вам пока не привязаны АЗС. Обратитесь к администратору.', menu(uid))
            return
        state(uid, 'report_station')
        cancel(uid, 'Введите ID АЗС для обновления:\n'+'\n'.join('{}: {}'.format(p.id, p.address) for p in stations)[:3400])
        return
    if s.startswith('report_'):
        if s == 'report_station':
            if not text.isdigit():
                send(uid, 'Введите числовой ID АЗС из списка или нажмите «Отмена».')
                return
            station = Point.get_or_none(Point.id == int(text))
            if not station or not can_report(uid, station.id):
                send(uid, 'АЗС не найдена или не привязана к вам. Выберите АЗС из списка.')
                return
            fuels = [f for f in FUELS if f in json.loads(station.extra).get('fuels', FUELS)]
            report_prompt(uid, {'station': station.id, 'fuels': fuels or list(FUELS), 'index': 0})
        elif not can_report(uid, data['station']):
            state(uid, '')
            send(uid, 'Нет прав на обновление этой АЗС.', menu(uid))
        elif s == 'report_available':
            if text == 'Нет':
                save_operator_report(uid, data, False, 0)
            elif text == 'Есть':
                state(uid, 'report_queue', data)
                send(uid, 'Выберите очередь в минутах.', [[button(v) for v in ['0', '5', '10']],
                     [button(v) for v in ['15', '20', 'Более 30']], [button('Отмена')]])
            else:
                send(uid, 'Выберите «Есть» или «Нет».')
        elif s == 'report_queue':
            if text not in ['0', '5', '10', '15', '20', 'Более 30']:
                send(uid, 'Выберите очередь кнопкой.')
            else:
                save_operator_report(uid, data, True, 31 if text == 'Более 30' else int(text))
        return
    if text == 'Найти топливо':
        state(uid, 'fuel_search')
        send(uid, 'Отправьте координаты, затем выберите марку топлива.', [[geo()], [button(f) for f in FUELS]])
        return
    location = next((a for a in attachments if a.get('type') == 'location'), None)
    if location:
        lat, lon = coords('{} {}'.format(location['latitude'], location['longitude']))
        state(uid, 'fuel_search', {'lat': lat, 'lon': lon})
        send(uid, 'Выберите марку топлива.', [[button(f) for f in FUELS]])
        return
    if s == 'fuel_search' and text in FUELS:
        if 'lat' not in data:
            send(uid, 'Сначала отправьте свои координаты.', [[geo()]])
            return
        now = time.time()
        latest = [report for (station, brand), report in latest_fuel_reports().items() if brand == text and report.available]
        ps = []
        for report in latest:
            p = Point.get_or_none(Point.id == report.station)
            if p:
                ps.append((distance(data['lat'], data['lon'], p.lat, p.lon), p, report))
        ps.sort(key=lambda x: x[0])
        if not ps:
            send(uid, NO_FUEL, menu(uid))
        for _, p, report in ps[:3]:
            stamp = time.strftime('%d.%m %H:%M', time.localtime(report.updated))
            extra = json.loads(p.extra)
            operator_report = report.source == 'operator'
            status = {} if operator_report else extra.get('portal_status', {}).get(text, {})
            queue = ('более 30 минут' if report.queue == 31 else '{} минут'.format(report.queue)) if operator_report else extra.get('queue_text') or ('нет данных' if report.queue < 0 else '{} минут'.format(report.queue))
            lines = [p.name, p.address, 'Марка: '+text]
            if status.get('price') is not None:
                lines.append('Цена на портале: {:g} ₽'.format(status['price']))
            if operator_report:
                lines.append('Источник: оператор АЗС. Топливо есть по сведениям оператора.')
            elif extra.get('availability_basis') == 'published_price':
                lines.append('Топливо есть по данным портала.')
            if queue.strip().casefold() == 'нет':
                lines.append('Очереди нет.')
            else:
                lines.append(('Очередь со слов оператора: ' if operator_report else 'Очередь по данным портала: ')+queue)
            if extra.get('restrictions'):
                restriction = extra['restrictions']
                selected = next((part.split(':', 1)[1].strip() for part in restriction.split(',') if part.split(':', 1)[0].strip() == text and ':' in part), None)
                if selected and selected.casefold() == 'да':
                    lines.append('Есть ограничение объёма отпуска в одни руки за одну заправку; размер уточняйте на АЗС.')
                elif selected and selected.casefold() == 'нет':
                    lines.append('Ограничения объёма отпуска по данным портала: нет.')
                else:
                    lines.append('Ограничения объёма отпуска в одни руки: '+restriction)
            lines.extend(['Обновлено источником: '+stamp+' МСК', maps(p)])
            if now-report.updated > 3600:
                lines.insert(-1, 'Сведения старше часа; наличие топлива могло измениться.')
            send(uid, '\n'.join(lines), [[button('Нашли ошибку?', 'error:{}:{}'.format(p.id, text))]])
        return
    if text.startswith('error:'):
        _, sid, brand = text.split(':')
        p = Point.get_by_id(int(sid))
        state(uid, 'fuel_error', {'station': p.id, 'fuel': brand})
        send(uid, 'Какая ошибка в сведениях по {}?'.format(p.address), [[button('Топлива нет'), button('Топливо есть')], [button('Отмена')]])
        return
    if s == 'fuel_error':
        if text not in ['Топлива нет', 'Топливо есть']:
            raise ValueError('Выберите вариант кнопкой.')
        key = 'complaint:{}:{}'.format(data['station'], data['fuel'])
        last = Setting.get_or_none(Setting.key == key)
        if not last or float(last.value)+300 <= time.time():
            recipients = {a.uid for a in Assignment.select().where(Assignment.station == data['station'])}
            if not recipients:
                recipients = {a.uid for a in Role.select().where(Role.role == 'admin')}
            p = Point.get_by_id(data['station'])
            for target in recipients:
                send(target, 'Водитель сообщил о возможной ошибке: {}, {}, {}. Проверьте и обновите сведения на портале.'.format(p.address, data['fuel'], text))
            Setting.replace(key=key, value=str(time.time())).execute()
        state(uid, '')
        send(uid, 'Спасибо. Информация принята для проверки оператором.', menu(uid))
        return
    if text == 'Добавить АЗС':
        if 'admin' not in r:
            send(uid, 'Это действие доступно только администратору.')
        else:
            state(uid, 'station_address')
            cancel(uid, 'Введите адрес АЗС.')
        return
    if s.startswith('station_'):
        if 'admin' not in r:
            state(uid, '')
            send(uid, 'Это действие доступно только администратору.')
        elif s == 'station_address':
            if not text:
                raise ValueError('Адрес обязателен.')
            state(uid, 'station_coords', {'address': text})
            cancel(uid, 'Введите широту и долготу через пробел.')
        elif s == 'station_coords':
            lat, lon = coords(text)
            state(uid, 'station_fuels', dict(data, lat=lat, lon=lon, fuels=[]))
            send(uid, 'Выберите марки топлива, затем нажмите «Готово».', [[button(f) for f in FUELS], [button('Готово'), button('Отмена')]])
        elif text in FUELS:
            fuels = data['fuels']
            fuels.remove(text) if text in fuels else fuels.append(text)
            state(uid, s, data)
            send(uid, 'Выбраны: '+', '.join(fuels), [[button(f, intent='positive' if f in fuels else 'default') for f in FUELS], [button('Готово'), button('Отмена')]])
        elif text == 'Готово':
            if not data['fuels']:
                raise ValueError('Выберите хотя бы одну марку топлива.')
            p = Point.create(name='АЗС', address=data['address'], lat=data['lat'], lon=data['lon'], extra=json.dumps({'fuels': data['fuels']}))
            state(uid, '')
            send(uid, 'АЗС создана. ID: {}. Для выдачи актуального наличия она должна быть сопоставлена с данными портала.'.format(p.id), menu(uid))
        return
    send(uid, 'Выберите действие в меню.', menu(uid))


def dispatch(update):
    typ = update.get('update_type')
    if typ == 'bot_started':
        start(str(update['user']['user_id']), update.get('payload') or '')
        return
    msg = update.get('message') or {}
    callback = update.get('callback') or {}
    user = callback.get('user') or msg.get('sender') or {}
    if user.get('is_bot') or not user.get('user_id'):
        return
    # Only private dialogs are accepted; group content is never treated as a private report.
    if msg.get('recipient', {}).get('chat_type') not in [None, 'dialog']:
        return
    uid = str(user['user_id'])
    body = msg.get('body') or {}
    text = (callback.get('payload') if typ == 'message_callback' else body.get('text')) or ''
    attachments = [] if typ == 'message_callback' else body.get('attachments') or []
    if text.strip() == '/id':
        send(uid, 'Ваш MAX ID: '+uid)
        return
    if typ == 'message_callback':
        Outbox.create(uid='', body=json.dumps({'callback_id': callback['callback_id']}))
        if KIND == 'fuel' and text.startswith('stale:'):
            from portal import expand_stale_message
            if body.get('mid') and (body.get('text') or '').startswith('АЗС с устаревшими сведениями'):
                expand_stale_message(uid, body['mid'], text)
            return
    if text.startswith('/start'):
        start(uid, text.partition(' ')[2])
        return
    if text == 'Отмена':
        state(uid, '')
        send(uid, 'Действие отменено.', menu(uid))
        return
    if administrative(uid, text):
        return
    s, data = state(uid)
    try:
        if admin_state(uid, text, s, data):
            return
        if KIND == 'labor':
            stamp = msg.get('timestamp')
            stamp = float(stamp)/1000 if isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > 0 else 0
            labor(uid, text, attachments, stamp, body.get('mid') or '')
        else:
            {'wifi': wifi, 'fuel': fuel}[KIND](uid, text, attachments)
    except (ValueError, KeyError, TypeError, Point.DoesNotExist):
        send(uid, 'Проверьте формат данных и повторите ввод или нажмите «Отмена».', [[button('Отмена')]])


async def receive(request):
    if not hmac.compare_digest(request.headers.get('X-Max-Bot-Api-Secret', ''), SECRET):
        raise web.HTTPForbidden()
    update = await request.json()
    kind = update.get('update_type')
    if kind not in ['bot_started', 'message_created', 'message_callback']:
        return web.json_response({'ok': True})
    mid = (update.get('message') or {}).get('body', {}).get('mid')
    callback = (update.get('callback') or {}).get('callback_id')
    raw = json.dumps(update, sort_keys=True, ensure_ascii=False)
    key = kind+':'+str(callback or mid or hashlib.sha256(raw.encode()).hexdigest())
    Inbox.get_or_create(key=key, defaults={'body': raw})
    return web.json_response({'ok': True})


async def worker(app):
    context = ssl.create_default_context(cafile=os.environ.get('CA_BUNDLE') or None)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context), timeout=aiohttp.ClientTimeout(total=25)) as client:
        app['client'] = client
        while True:
            item = Inbox.select().where((Inbox.done == False) & (Inbox.due <= time.time())).order_by(Inbox.due).first()
            if item:
                try:
                    with db.atomic():
                        dispatch(json.loads(item.body))
                        item.done = True
                        item.body = '{}'  # Erase source sender/media metadata after processing.
                        item.save()
                except Exception as e:
                    logging.error('Event processing failed: %s', type(e).__name__)
                    item.attempts += 1
                    item.due = time.time()+min(3600, 2**min(item.attempts, 10))
                    item.save()
            outgoing = Outbox.select().where(Outbox.due <= time.time()).order_by(Outbox.id).first()
            if outgoing:
                payload = json.loads(outgoing.body)
                case_material = payload.get('_labor_export') or payload.get('_admin_material') or (payload.get('text', '').startswith('Заявление №') and ('\nСодержание:' in payload.get('text', '') or '\nКонтакты предоставлены' in payload.get('text', '')))
                if KIND == 'labor' and case_material and 'admin' not in roles(outgoing.uid):
                    # Retain queued materials, but never deliver them without current access.
                    outgoing.due = time.time()+3600
                    outgoing.save()
                    continue
                edit_mid = payload.pop('_edit_mid', None)
                path = '/answers' if not outgoing.uid else '/messages'
                params = {'message_id': edit_mid} if edit_mid else ({'user_id': outgoing.uid} if outgoing.uid else {'callback_id': payload.pop('callback_id')})
                if not outgoing.uid:
                    payload = {'notification': 'Принято'}
                try:
                    if KIND == 'labor' and outgoing.uid:
                        if payload.get('_labor_export'):
                            from labor_archive import export_archive
                            token = await export_archive(client, os.environ.get('MAX_API_URL', 'https://platform-api2.max.ru'), TOKEN)
                            payload = {'text': 'Архив заявлений: тексты, фото и время отправки видео.', '_admin_material': True,
                                       'attachments': [{'type': 'file', 'payload': {'token': token}}]}
                            outgoing.body = json.dumps(payload, ensure_ascii=False)
                            outgoing.save()
                        from labor_media import prepare
                        if await prepare(payload, client, os.environ.get('MAX_API_URL', 'https://platform-api2.max.ru'), TOKEN):
                            outgoing.body = json.dumps(payload, ensure_ascii=False)
                            outgoing.save()
                    wire_payload = {key: value for key, value in payload.items() if not key.startswith('_')}
                    if 'attachments' in wire_payload:
                        wire_payload['attachments'] = [{key: value for key, value in attachment.items() if not key.startswith('_')} for attachment in wire_payload['attachments']]
                    if KIND == 'labor' and case_material and 'admin' not in roles(outgoing.uid):
                        outgoing.due = time.time()+3600
                        outgoing.save()
                        continue
                    async with client.request('PUT' if edit_mid else 'POST', os.environ.get('MAX_API_URL', 'https://platform-api2.max.ru')+path, params=params, headers={'Authorization': TOKEN}, json=wire_payload) as response:
                        if response.status >= 400:
                            logging.error('MAX delivery HTTP %s for queue %s', response.status, outgoing.id)
                            if not outgoing.uid or (response.status in [403, 404] and not (KIND == 'labor' and case_material)):
                                outgoing.delete_instance()
                            else:
                                outgoing.attempts += 1
                                outgoing.due = time.time()+min(3600, 2**min(outgoing.attempts, 10))
                                outgoing.save()
                        else:
                            outgoing.delete_instance()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as error:
                    logging.warning('MAX delivery deferred: %s for queue %s', type(error).__name__, outgoing.id)
                    outgoing.due = time.time()+30
                    outgoing.save()
                await asyncio.sleep(.55)
            elif not item:
                await asyncio.sleep(.3)


async def lifecycle(app):
    task = asyncio.create_task(worker(app))
    portal_task = None
    if KIND == 'fuel':
        from portal import monitor
        portal_task = asyncio.create_task(monitor(app))
    yield
    task.cancel()
    if portal_task:
        portal_task.cancel()
    await asyncio.gather(*[t for t in [task, portal_task] if t], return_exceptions=True)


app = web.Application(client_max_size=1024*1024)
app.router.add_post('/webhook', receive)
if KIND in ['fuel', 'wifi'] and os.environ.get('MAP_ENABLED') == '1':
    from public_map import register_routes
    register_routes(app, KIND)
app.cleanup_ctx.append(lifecycle)
if __name__ == '__main__':
    sys.modules['app'] = sys.modules[__name__]
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    web.run_app(app, host='127.0.0.1', port=int(os.environ['PORT']), access_log=None)
