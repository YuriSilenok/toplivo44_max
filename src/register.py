"""Provision existing bot webhook subscriptions; print no credentials."""
import asyncio
import json
import os
import ssl
from pathlib import Path
import aiohttp


async def main():
    context = ssl.create_default_context(cafile='/opt/max-bots/bootstrap/ca.pem')
    config = json.loads(Path('/etc/max-bots/config.json').read_text())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as client:
        for kind, cfg in config.items():
            headers = {'Authorization': cfg['token']}
            base = cfg.get('api', 'https://platform-api2.max.ru')
            async with client.get(base+'/me', headers=headers) as r:
                me = await r.json()
                if r.status != 200:
                    raise RuntimeError('{} authentication HTTP {}'.format(kind, r.status))
                print(kind, 'bot identity:', me.get('username'), me.get('user_id'))
            async with client.get(base+'/subscriptions', headers=headers) as r:
                subscriptions = await r.json()
                print(kind, 'existing webhook count:', len(subscriptions.get('subscriptions', [])))
            url = 'https://xn--e1afhclgq.xn--p1ai/max-bots/'+kind+'/webhook'
            body = {'url': url, 'secret': cfg['secret'], 'update_types': ['bot_started', 'message_created', 'message_callback']}
            async with client.post(base+'/subscriptions', headers=headers, json=body) as r:
                result = await r.json()
                print(kind, 'webhook registration:', r.status, result.get('success'), result.get('code', ''))
                if r.status != 200 or not result.get('success'):
                    raise RuntimeError('Registration unsuccessful for '+kind)


asyncio.run(main())
