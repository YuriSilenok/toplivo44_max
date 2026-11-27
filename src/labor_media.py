"""Normalize MAX media without forwarding sender metadata."""
import ipaddress
import tempfile
import os
import ssl
from urllib.parse import urlsplit, urljoin

import aiohttp
from aiohttp.resolver import DefaultResolver


def https_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.port in [None, 443]
    except ValueError:
        return False


def normalize(attachments):
    media, rejected = [], 0
    for item in attachments:
        typ = item.get('type')
        if typ not in ['image', 'video']:
            continue
        payload = item.get('payload') or {}
        token = payload.get('token')
        if isinstance(token, str) and token:
            normalized = {'type': typ, 'payload': {'token': token}}
            if typ == 'image' and https_url(payload.get('url')):
                normalized['_export_url'] = payload['url']
            media.append(normalized)
        elif https_url(payload.get('url')):
            media.append({'type': typ, 'payload': {'url': payload['url']}} if typ == 'image' else
                         {'type': typ, '_download_url': payload['url']})
        elif typ == 'image' and isinstance(payload.get('photos'), dict):
            photos = {key: {'token': value['token']} for key, value in payload['photos'].items()
                      if isinstance(value, dict) and isinstance(value.get('token'), str) and value['token']}
            if photos:
                media.append({'type': 'image', 'payload': {'photos': photos}})
            else:
                rejected += 1
        else:
            rejected += 1
    return media, rejected


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(address['host']).is_global for address in addresses):
            raise ValueError('Non-public media address')
        return addresses


async def prepare(payload, api_client, api_url, bot_token):
    """Resolve URL-only video once; caller persists the resulting reusable token."""
    changed = False
    for item in payload.get('attachments') or []:
        source = item.get('_download_url')
        if not source:
            continue
        if item.get('type') != 'video' or not https_url(source):
            raise ValueError('Unsupported media descriptor')
        # No bot credentials are sent to download/upload hosts. Validate DNS and redirects.
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False,
                                         ssl=ssl.create_default_context(cafile=os.environ.get('CA_BUNDLE') or None))
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=300)) as media_client:
            with tempfile.TemporaryFile() as video:
                for redirect in range(4):
                    if not https_url(source):
                        raise ValueError('Invalid media URL')
                    parsed = urlsplit(source)
                    try:
                        address = ipaddress.ip_address(parsed.hostname)
                    except ValueError:
                        address = None
                    if address is not None and not address.is_global:
                        raise ValueError('Non-public media address')
                    async with media_client.get(source, allow_redirects=False) as response:
                        if response.status in [301, 302, 303, 307, 308]:
                            source = urljoin(source, response.headers.get('Location', ''))
                            continue
                        response.raise_for_status()
                        size = 0
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > 250*1024*1024:
                                raise ValueError('Video exceeds MAX size limit')
                            video.write(chunk)
                        if not size:
                            raise ValueError('Empty video')
                        break
                else:
                    raise ValueError('Too many media redirects')
                async with api_client.post(api_url+'/uploads', params={'type': 'video'}, headers={'Authorization': bot_token}) as response:
                    response.raise_for_status()
                    upload = await response.json()
                upload_url, token = upload.get('url'), upload.get('token')
                if not https_url(upload_url) or not isinstance(token, str) or not token:
                    raise ValueError('Invalid MAX upload response')
                host = urlsplit(upload_url).hostname
                if not any(host == suffix or host.endswith('.'+suffix) for suffix in ['okcdn.ru', 'oneme.ru', 'max.ru']):
                    raise ValueError('Unexpected MAX upload host')
                video.seek(0)
                form = aiohttp.FormData()
                form.add_field('data', video, filename='video.mp4', content_type='video/mp4')
                async with media_client.post(upload_url, data=form, allow_redirects=False) as response:
                    if response.status >= 300:
                        raise ValueError('MAX video upload failed')
                    await response.read()
                item.clear()
                item.update(type='video', payload={'token': token})
                changed = True
    return changed
