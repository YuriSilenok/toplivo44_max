"""Administrator-only case browsing and ZIP export; no public download endpoints."""
import html
import ipaddress
import json
import os
import ssl
import tempfile
import zipfile
import calendar
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from labor_media import PublicResolver, https_url


def stamp(value):
    return datetime.fromtimestamp(value, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК') if value else 'время неизвестно'


def message_text(message):
    if not message.original_text:
        return message.body
    contacts = message.body.partition('\nКонтакты предоставлены')[2]
    return 'Заявление №{}\nСодержание: {}'.format(message.case_id, message.original_text) + ('\nКонтакты предоставлены'+contacts if contacts else '')


def show_case(uid, number, offset=0):
    from app import CaseMessage, roles, send, button, Outbox
    if 'admin' not in roles(uid):
        return
    query = CaseMessage.select().where(CaseMessage.case_id == number).order_by(CaseMessage.id)
    count = query.count()
    if not count:
        send(uid, 'Заявление с таким номером не найдено.')
        return
    offset = max(0, min(offset, max(0, count-1)))
    for message in query.offset(offset).limit(10):
        text = 'Время отправки: '+stamp(message.sent_at)+'\n'+message_text(message)
        media = json.loads(message.media)
        for start in range(0, len(text), 3800):
            Outbox.create(uid=str(uid), body=json.dumps({'text': text[start:start+3800], '_admin_material': True}, ensure_ascii=False))
        for start in range(0, len(media), 12):
            Outbox.create(uid=str(uid), body=json.dumps({'text': 'Заявление №{}\nМатериалы, отправлены: {}'.format(number, stamp(message.sent_at)),
                'attachments': media[start:start+12], '_admin_material': True}, ensure_ascii=False))
    rows = [[button('Далее', 'casepage:{}:{}'.format(number, offset+10))]] if offset+10 < count else None
    send(uid, 'Заявление №{}: сообщения {}–{} из {}.'.format(number, offset+1, min(offset+10, count), count), rows)


async def download_media(client, url, target, kind='image'):
    for redirect in range(4):
        if not https_url(url):
            raise ValueError('Invalid photo URL')
        try:
            address = ipaddress.ip_address(urlsplit(url).hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError('Non-public photo address')
        async with client.get(url, allow_redirects=False) as response:
            if response.status in [301, 302, 303, 307, 308]:
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            response.raise_for_status()
            media_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            formats = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp',
                       'image/heic': '.heic', 'image/tiff': '.tiff', 'image/bmp': '.bmp'} if kind == 'image' else {
                       'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
                       'video/x-matroska': '.mkv', 'application/octet-stream': '.mp4'}
            suffix = formats.get(media_type)
            if suffix is None:
                raise ValueError('Unsupported photo content type')
            size = 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > (50 if kind == 'image' else 250)*1024*1024:
                    raise ValueError('Photo too large')
                target.write(chunk)
            if not size:
                raise ValueError('Empty photo')
            return suffix
    raise ValueError('Too many photo redirects')


def period_start(end, months):
    date = datetime.fromtimestamp(end, ZoneInfo('Europe/Moscow'))
    total = date.year*12+date.month-1-months
    year, month = total//12, total % 12+1
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])).timestamp()


async def video_url(api_client, api_url, bot_token, item):
    token = item.get('payload', {}).get('token')
    if token:
        async with api_client.get(api_url+'/videos/'+quote(token, safe=''), headers={'Authorization': bot_token}) as response:
            if response.status == 200:
                urls = (await response.json()).get('urls') or {}
                for quality in ['mp4_480', 'mp4_360', 'mp4_720', 'mp4_1080', 'mp4_240', 'mp4_144']:
                    if https_url(urls.get(quality)):
                        return urls[quality]
    return item.get('_download_url') or item.get('payload', {}).get('url')


async def export_archive(api_client, api_url, bot_token, months=12, end=None):
    from app import CaseMessage
    if not isinstance(months, int) or not 1 <= months <= 12:
        raise ValueError('Invalid report period')
    end = float(end or time.time())
    start = period_start(end, months)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False,
        ssl=ssl.create_default_context(cafile=os.environ.get('CA_BUNDLE') or None))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=300)) as media_client:
        with tempfile.TemporaryFile() as archive:
            sections, previous, navigation = [], None, []
            photos = videos = unavailable = 0
            # Materialize metadata to keep a stable snapshot while awaiting downloads.
            messages = list(CaseMessage.select().where((CaseMessage.sent_at >= start) & (CaseMessage.sent_at <= end)).order_by(CaseMessage.case_id, CaseMessage.sent_at, CaseMessage.id))
            unknown_dates = CaseMessage.select().where(CaseMessage.sent_at == 0).count()
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for message in messages:
                    if message.case_id != previous:
                        if previous is not None:
                            sections.append('</section>')
                        sections.append('<section class="case" id="case-{0}" data-case="{0}"><div class="case-heading"><span class="eyebrow">Материалы обращения</span><h2>Заявление №{0}</h2></div>'.format(message.case_id))
                        navigation.append('<a href="#case-{0}">№{0}</a>'.format(message.case_id))
                        previous = message.case_id
                    sections.append('<article class="message"><div class="message-date">{}</div><pre>{}</pre><div class="media-grid">'.format(html.escape(stamp(message.sent_at)), html.escape(message_text(message))))
                    media = json.loads(message.media)
                    fresh = {'image': [], 'video': []}
                    if media and message.source_mid:
                        async with api_client.get(api_url+'/messages/'+quote(message.source_mid, safe=''), headers={'Authorization': bot_token}) as response:
                            if response.status == 200:
                                original = await response.json()
                                for attached in (original.get('body') or {}).get('attachments') or []:
                                    if attached.get('type') in fresh:
                                        fresh[attached['type']].append(attached)
                    indices = {'image': 0, 'video': 0}
                    for index, item in enumerate(media):
                        kind = item.get('type')
                        if kind in ['image', 'video']:
                            refreshed = fresh[kind][indices[kind]] if indices[kind] < len(fresh[kind]) else item
                            indices[kind] += 1
                            url = (refreshed.get('payload', {}).get('url') or item.get('_export_url') or item.get('payload', {}).get('url')) if kind == 'image' else await video_url(api_client, api_url, bot_token, refreshed)
                            if not url and kind == 'video':
                                url = item.get('_download_url') or item.get('payload', {}).get('url')
                            try:
                                with tempfile.TemporaryFile() as photo:
                                    suffix = await download_media(media_client, url, photo, kind)
                                    name = 'statement-{}/{}-{}-{}{}'.format(message.case_id, 'photo' if kind == 'image' else 'video', message.id, index+1, suffix)
                                    photo.seek(0)
                                    with output.open(name, 'w') as target:
                                        while True:
                                            chunk = photo.read(65536)
                                            if not chunk:
                                                break
                                            target.write(chunk)
                                label = 'Фото' if kind == 'image' else 'Видео'
                                if kind == 'video':
                                    videos += 1
                                    player = '<video controls playsinline preload="none" src="{0}">Браузер не поддерживает видео.</video>'.format(name)
                                elif suffix in ['.jpg', '.png', '.gif', '.webp', '.bmp']:
                                    photos += 1
                                    player = '<a href="{0}" target="_blank" rel="noopener"><img loading="lazy" src="{0}" alt="Фото к заявлению"></a>'.format(name)
                                else:
                                    photos += 1
                                    player = '<div class="media-placeholder">Фото в исходном формате</div>'
                                sections.append('<figure><div class="media-frame">{}</div><figcaption><span>{}</span><a href="{}" download>Скачать</a></figcaption></figure>'.format(player, label, name))
                            except (aiohttp.ClientError, TimeoutError, ValueError):
                                unavailable += 1
                                sections.append('<figure><div class="media-frame media-placeholder">Вложение недоступно</div><figcaption>Исходная ссылка отсутствует, истекла или файл не поддерживается.</figcaption></figure>')
                    sections.append('</div></article>')
                if previous is not None:
                    sections.append('</section>')
                template = (Path(__file__).parent/'report.html').read_text(encoding='utf-8')
                replacements = {'PERIOD': html.escape(stamp(start)+' — '+stamp(end)), 'MONTHS': str(months),
                    'CASES': str(len(navigation)), 'MESSAGES': str(len(messages)), 'PHOTOS': str(photos), 'VIDEOS': str(videos),
                    'CONTENT': ''.join(sections) or '<div class="empty">За выбранный период сообщений нет.</div>',
                    'NAVIGATION': ''.join(navigation), 'UNKNOWN': str(unknown_dates), 'UNAVAILABLE': str(unavailable)}
                document = template
                # User content is substituted last so it cannot create template directives.
                for key in [key for key in replacements if key != 'CONTENT']+['CONTENT']:
                    document = document.replace('{{'+key+'}}', replacements[key])
                output.writestr('index.html', document.encode('utf-8'))
            archive.seek(0)
            async with api_client.post(api_url+'/uploads', params={'type': 'file'}, headers={'Authorization': bot_token}) as response:
                response.raise_for_status()
                upload = await response.json()
            url = upload.get('url')
            if not https_url(url) or not any(urlsplit(url).hostname == suffix or urlsplit(url).hostname.endswith('.'+suffix) for suffix in ['oneme.ru', 'okcdn.ru', 'max.ru']):
                raise ValueError('Unexpected MAX file upload host')
            form = aiohttp.FormData()
            form.add_field('data', archive, filename='statements.zip', content_type='application/zip')
            async with media_client.post(url, data=form, allow_redirects=False) as response:
                if response.status >= 300:
                    raise ValueError('MAX archive upload failed')
                result = await response.json(content_type=None)
            token = result.get('token')
            if not isinstance(token, str) or not token:
                raise ValueError('MAX file token missing')
            return token
