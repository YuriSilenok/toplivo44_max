#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
mkdir -p "$base/releases" /etc/max-bots /var/lib/max-bots
release="$base/releases/$(date -u +%Y%m%dT%H%M%S)"
mkdir "$release"
tar -xzf "$base/source.tar.gz" -C "$release"
id maxbots >/dev/null 2>&1 || useradd --system --home /var/lib/max-bots --shell /usr/sbin/nologin maxbots
chown root:maxbots /var/lib/max-bots
chmod 0750 /var/lib/max-bots
if [[ ! -x "$base/python/bin/python3.12" ]]; then
  cp -a /opt/gibdd-test-20261117/python/cpython-3.12.16-linux-x86_64-gnu "$base/python"
  chown -R root:maxbots "$base/python"
  chmod -R g+rX "$base/python"
fi
if [[ ! -x "$base/venv/bin/python" ]]; then
  "$base/python/bin/python3.12" -m venv "$base/venv"
fi
ln -sfn "$base/python/bin/python3.12" "$base/venv/bin/python3.12"
sed -i 's|/opt/gibdd-test-20261117/python/cpython-3.12-linux-x86_64-gnu/bin|/opt/max-bots/python/bin|g; s|/opt/gibdd-test-20261117/python/cpython-3.12.16-linux-x86_64-gnu/bin|/opt/max-bots/python/bin|g' "$base/venv/pyvenv.cfg"
"$base/venv/bin/pip" install --disable-pip-version-check -r "$release/requirements.txt"
"$base/venv/bin/python" - <<'PY'
from pathlib import Path
base=Path('/opt/max-bots/bootstrap')
parts=[Path('/etc/ssl/certs/ca-certificates.crt'), base/'russian-root.pem', base/'portal-chain.pem']
(base/'ca.pem').write_text('\n'.join(p.read_text().replace('\r','').strip() for p in parts)+'\n')
PY
chown -R root:maxbots "$base"
chmod -R g+rX "$base"
chmod 0755 /opt/max-bots /opt/max-bots/releases "$release"
ln -sfn "$release" "$base/current"
"$base/venv/bin/python" - <<'PY'
import json,secrets,os
from pathlib import Path
configfile=Path('/etc/max-bots/config.json')
config=json.loads(configfile.read_text()) if configfile.exists() else {}
tokenspath=Path('/etc/max-bots/tokens.json')
tokens=json.loads(tokenspath.read_text()) if tokenspath.exists() else {k:v['token'] for k,v in config.items()}
names={'fuel':'fuelmap_digitalkostroma_bot','wifi':'wifipoints_digitalkostroma_bot','labor':'trud_digitalkostroma_bot'}
for i,(kind,username) in enumerate(names.items()):
    cfg=config.setdefault(kind,{'secret':secrets.token_urlsafe(36)})
    cfg.update(token=tokens[kind], username=username, port=8460+i)
    folder=Path('/var/lib/max-bots')/kind
    folder.mkdir(exist_ok=True)
    import pwd
    account=pwd.getpwnam('maxbots')
    os.chown(folder, account.pw_uid, account.pw_gid)
    os.chmod(folder,0o700)
    # Administrators are added by single-use invitations, generated separately.
    values={'BOT_KIND':kind,'BOT_USERNAME':username,'MAX_BOT_TOKEN':cfg['token'],'WEBHOOK_SECRET':cfg['secret'],
            'DATA_DIR':str(folder),'PORT':str(cfg['port']),'TZ':'Europe/Moscow',
            'CA_BUNDLE':'/opt/max-bots/bootstrap/ca.pem','ADMIN_IDS':''}
    Path('/etc/max-bots/'+kind+'.env').write_text(''.join(k+'='+v+'\n' for k,v in values.items()))
    os.chmod('/etc/max-bots/'+kind+'.env',0o600)
configfile.write_text(json.dumps(config))
os.chmod(configfile,0o600)
tokenspath.unlink(missing_ok=True)
PY
for kind in fuel wifi labor; do
cat >"/etc/systemd/system/max-bot-$kind.service" <<EOF
[Unit]
Description=MAX bot $kind
After=network-online.target
Wants=network-online.target
[Service]
User=maxbots
Group=maxbots
WorkingDirectory=$base/current
EnvironmentFile=/etc/max-bots/$kind.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$base/venv/bin/python -B $base/current/app.py
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/max-bots/$kind
MemoryMax=130M
[Install]
WantedBy=multi-user.target
EOF
done
cat >/etc/nginx/snippets/max-bots.conf <<'EOF'
location = /max-bots/fuel/webhook {
    proxy_pass http://127.0.0.1:8460/webhook;
    client_max_body_size 1m;
    access_log off;
}
location = /max-bots/wifi/webhook {
    proxy_pass http://127.0.0.1:8461/webhook;
    client_max_body_size 1m;
    access_log off;
}
location = /max-bots/labor/webhook {
    proxy_pass http://127.0.0.1:8462/webhook;
    client_max_body_size 1m;
    access_log off;
}
EOF
if ! grep -q 'include /etc/nginx/snippets/max-bots.conf;' /etc/nginx/sites-enabled/pond; then
    cp -a /etc/nginx/sites-enabled/pond "$base/bootstrap/nginx-before-max-bots.conf"
    sed -i '/include \/etc\/nginx\/snippets\/gibdd-production.conf;/a\    include /etc/nginx/snippets/max-bots.conf;' /etc/nginx/sites-enabled/pond
fi
nginx -t
systemctl daemon-reload
systemctl enable --now max-bot-fuel max-bot-wifi max-bot-labor
systemctl restart max-bot-fuel max-bot-wifi max-bot-labor
systemctl reload nginx
"$base/venv/bin/python" "$base/current/register.py"
systemctl is-active max-bot-fuel max-bot-wifi max-bot-labor
