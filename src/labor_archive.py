"""Administrator-only case browsing and ZIP export; no public download endpoints."""
import html
import ipaddress
import json
import os
import ssl
import tempfile
import zipfile
from datetime import datetime
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


async def download_photo(client, url, target):
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
            suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp',
                      'image/heic': '.heic', 'image/tiff': '.tiff', 'image/bmp': '.bmp'}.get(media_type)
            if suffix is None:
                raise ValueError('Unsupported photo content type')
            size = 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 50*1024*1024:
                    raise ValueError('Photo too large')
                target.write(chunk)
            if not size:
                raise ValueError('Empty photo')
            return suffix
    raise ValueError('Too many photo redirects')


async def export_archive(api_client, api_url, bot_token):
    from app import CaseMessage
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False,
        ssl=ssl.create_default_context(cafile=os.environ.get('CA_BUNDLE') or None))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=300)) as media_client:
        with tempfile.TemporaryFile() as archive:
            sections, previous = [], None
            # Materialize metadata to keep a stable snapshot while awaiting downloads.
            messages = list(CaseMessage.select().order_by(CaseMessage.case_id, CaseMessage.id))
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for message in messages:
                    if message.case_id != previous:
                        sections.append('<h1>Заявление №{}</h1>'.format(message.case_id))
                        previous = message.case_id
                    sections.append('<article><h2>Отправлено: {}</h2><pre>{}</pre>'.format(html.escape(stamp(message.sent_at)), html.escape(message_text(message))))
                    media = json.loads(message.media)
                    fresh_photos = []
                    if any(item.get('type') == 'image' for item in media) and message.source_mid:
                        async with api_client.get(api_url+'/messages/'+quote(message.source_mid, safe=''), headers={'Authorization': bot_token}) as response:
                            if response.status == 200:
                                original = await response.json()
                                fresh_photos = [item for item in (original.get('body') or {}).get('attachments') or [] if item.get('type') == 'image']
                    photo_index = 0
                    for index, item in enumerate(media):
                        if item.get('type') == 'video':
                            sections.append('<p>Видео. Время отправки: {}. Видеофайл в архив не включён.</p>'.format(html.escape(stamp(message.sent_at))))
                        elif item.get('type') == 'image':
                            url = item.get('_export_url') or item.get('payload', {}).get('url')
                            if photo_index < len(fresh_photos):
                                url = fresh_photos[photo_index].get('payload', {}).get('url') or url
                            photo_index += 1
                            try:
                                with tempfile.TemporaryFile() as photo:
                                    suffix = await download_photo(media_client, url, photo)
                                    name = 'statement-{}/photo-{}-{}{}'.format(message.case_id, message.id, index+1, suffix)
                                    photo.seek(0)
                                    with output.open(name, 'w') as target:
                                        while True:
                                            chunk = photo.read(65536)
                                            if not chunk:
                                                break
                                            target.write(chunk)
                                if suffix in ['.jpg', '.png', '.gif', '.webp', '.bmp']:
                                    sections.append('<p><a href="{0}">Фото</a></p><img src="{0}" alt="Фото к заявлению">'.format(name))
                                else:
                                    sections.append('<p><a href="{}">Фото</a></p>'.format(name))
                            except (aiohttp.ClientError, TimeoutError, ValueError):
                                sections.append('<p>Фото недоступно: исходная ссылка отсутствует или истекла.</p>')
                    sections.append('</article>')
                document = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Архив заявлений</title><style>body{font:16px system-ui;max-width:1000px;margin:30px auto;padding:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%;max-height:700px}article{border-bottom:1px solid #bbb;padding:16px 0}</style><h1>Архив заявлений</h1><p>Время — МСК. Для старых сообщений дата может быть неизвестна. Видео представлены отметками времени.</p>'+''.join(sections)+'</html>'
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
