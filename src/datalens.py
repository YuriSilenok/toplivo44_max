"""Read the public map response used by toplivo.kostroma.gov.ru itself."""
import hashlib
import html
import json
import logging
import math
import re
import aiohttp
from datetime import datetime
from zoneinfo import ZoneInfo

MAP_URL = 'https://datalens.yandex/charts/api/run'
CHART_ID = '1mfpck09rnyck'
MOSCOW = ZoneInfo('Europe/Moscow')


def markup_text(node):
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'br':
        return '\n'
    if isinstance(node.get('children'), list):
        return ''.join(markup_text(child) for child in node['children'])
    return markup_text(node.get('content'))


def parse_map(payload):
    layers = payload.get('data')
    if not isinstance(layers, list) or not layers:
        raise ValueError('DataLens map layers missing')
    records = {}
    seen = skipped = 0
    for layer in layers:
        children = layer.get('collection', {}).get('children')
        if not isinstance(children, list):
            raise ValueError('DataLens map collection missing')
        for child in children:
            seen += 1
            try:
                feature = child['feature']
                if feature['geometry']['type'] != 'Point':
                    raise ValueError('Not a point')
                lat, lon = map(float, feature['geometry']['coordinates'])
                if not math.isfinite(lat+lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError('Invalid coordinates')
                blocks = feature['properties']['data']
                text = '\n'.join(markup_text(block.get('value')) for block in blocks)
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                address_line = next(line for line in lines if line.startswith('📍'))
                address = address_line.lstrip('📍').strip()
                name = lines[0]
                match = re.search(r'Обновлено:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)', text)
                if not match or not address or not name:
                    raise ValueError('Station identity or source timestamp missing')
                stamp = datetime.fromisoformat(match.group(1)+'T'+match.group(2)).replace(tzinfo=MOSCOW)
                queue = next((line.split('Очередь:', 1)[1].strip() for line in lines if '🚗 Очередь:' in line), '')
                restrictions = next((line.split('Ограничения:', 1)[1].strip() for line in lines if '⚠️ Ограничения:' in line), '')
                fuels = {}
                for fuel, raw in re.findall(r'^\s*(АИ-92|АИ-95|ДТ):\s*(.*?)\s*₽\s*$', text, re.MULTILINE):
                    value = raw.strip().lower()
                    price = None
                    if value != 'нет':
                        price = float(value.replace('\u00a0', '').replace(' ', '').replace(',', '.'))
                        if not math.isfinite(price) or price < 0:
                            raise ValueError('Invalid source price')
                    fuels[fuel] = {'available': price is not None and price > 0,
                                   'price': price, 'queue_minutes': -1}
                if not fuels:
                    raise ValueError('Fuel fields missing')
                identity = '{}\0{}\0{:.6f}\0{:.6f}'.format(name, address, lat, lon)
                sid = 'datalens-'+hashlib.sha256(identity.encode()).hexdigest()[:24]
                row = {'id': sid, 'name': name, 'address': address, 'latitude': lat,
                       'longitude': lon, 'updated_at': stamp.isoformat(), 'fuels': fuels,
                       'queue_text': queue, 'restrictions': restrictions,
                       'availability_basis': 'published_price', 'source_url': 'https://toplivo.kostroma.gov.ru/'}
                old = records.get(sid)
                if old is None or old['updated_at'] <= row['updated_at']:
                    records[sid] = row
            except (KeyError, ValueError, TypeError, StopIteration, IndexError):
                skipped += 1
    if not records:
        raise ValueError('No timestamped stations in DataLens response')
    logging.info('DataLens snapshot: %s points, %s usable stations, %s skipped', seen, len(records), skipped)
    return list(records.values())


def safe_error_text(value, limit=400):
    text = str(value)
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', text, flags=re.I | re.S)
    text = html.unescape(re.sub(r'<[^>]*>', ' ', text))
    text = re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+', '[скрыто]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,}\b', '[IP скрыт]', text)
    text = re.sub(r'(?i)\b(token|password|secret|authorization|cookie|key)\b\s*[=:]\s*\S+', r'\1=[скрыто]', text)
    text = re.sub(r'\b[A-Za-z0-9_+/=-]{32,}\b', '[идентификатор скрыт]', text)
    return ' '.join(text.split())[:limit]


async def check_map_response(response):
    if response.status < 400:
        return
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
    retry = response.headers.get('Retry-After', '')
    # Bounded read of this failed response only; never log full bodies or headers.
    try:
        body = (await response.content.read(8192)).decode('utf-8', errors='replace')
    except (aiohttp.ClientError, TimeoutError):
        body = ''
    description = ''
    if 'json' in content_type.lower():
        try:
            payload = json.loads(body)
            parts = []
            def collect(obj, depth=0):
                if depth > 6 or len(parts) >= 12:
                    return
                if isinstance(obj, list):
                    for item in obj[:5]:
                        collect(item, depth+1)
                elif isinstance(obj, dict):
                    for key, value in obj.items():
                        if key in ('code', 'message', 'title', 'description', 'detail', 'error', 'db_message', 'status', 'statusCode') and isinstance(value, (str, int)):
                            part = safe_error_text(value, 250)
                            if part and part not in parts:
                                parts.append(part)
                        elif key in ('details', 'errors', 'error', 'cause', 'innerError', 'inner_error', 'sources', 'data', 'response') and isinstance(value, (dict, list)):
                            collect(value, depth+1)
            collect(payload)
            description = safe_error_text('; '.join(parts))
        except (ValueError, TypeError):
            pass
    elif 'html' in content_type.lower():
        title = re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S)
        description = safe_error_text(title.group(1)) if title else ''
        if re.search(r'captcha|showcaptcha|smartcaptcha', body, re.I):
            description += ' (в ответе обнаружена страница проверки CAPTCHA)'
    elif content_type.lower() == 'text/plain':
        description = safe_error_text(body)
    details = ['Content-Type: '+(safe_error_text(content_type, 80) or 'не указан')]
    if retry:
        details.append('Retry-After: '+safe_error_text(retry, 100))
    details.append('Описание источника: '+(description or 'безопасное описание в ответе отсутствует'))
    try:
        response.raise_for_status()
    except aiohttp.ClientResponseError as error:
        error.portal_diagnostic = '\n'.join(details)
        raise


async def fetch_map(client):
    async with client.post(MAP_URL, json={'id': CHART_ID, 'params': {}}) as response:
        await check_map_response(response)
        return parse_map(await response.json())
