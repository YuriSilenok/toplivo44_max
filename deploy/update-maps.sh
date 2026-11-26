#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
stamp=$(date -u +%Y%m%dT%H%M%S)
snapshot="$base/backups/maps-$stamp"
release="$base/releases/$stamp-maps"
mkdir -p "$snapshot" "$release"
readlink -f "$base/current" > "$snapshot/previous-release"
cp -a /etc/nginx/snippets/max-bots.conf "$snapshot/nginx.conf"
for kind in fuel wifi; do cp -a "/etc/max-bots/$kind.env" "$snapshot/$kind.env"; done
"$base/venv/bin/python" - "$snapshot" <<'PY'
import sqlite3,sys
for kind in ['fuel','wifi']:
    with sqlite3.connect('/var/lib/max-bots/'+kind+'/bot.sqlite3') as source, sqlite3.connect(sys.argv[1]+'/'+kind+'.sqlite3') as target:
        source.backup(target)
PY
cat > "$snapshot/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
snapshot=$(cd -- "$(dirname -- "$0")" && pwd)
previous=$(cat "$snapshot/previous-release")
[[ "$previous" == /opt/max-bots/releases/* && -f "$previous/app.py" ]]
cp -a "$snapshot/nginx.conf" /etc/nginx/snippets/max-bots.conf
for kind in fuel wifi; do cp -a "$snapshot/$kind.env" "/etc/max-bots/$kind.env"; done
ln -sfn "$previous" /opt/max-bots/current
nginx -t
systemctl restart max-bot-fuel max-bot-wifi
systemctl reload nginx
systemctl is-active max-bot-fuel max-bot-wifi
echo 'Rolled back map release; current databases retained.'
ROLLBACK
chmod 0700 "$snapshot/rollback.sh"
ln -sfn "$snapshot/rollback.sh" "$base/rollback-maps.sh"
trap 'bash "$snapshot/rollback.sh"' ERR
tar -xzf "$base/maps-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
for kind in fuel wifi; do
    sed -i '/^MAP_ENABLED=/d' "/etc/max-bots/$kind.env"
    printf '\nMAP_ENABLED=1\n' >> "/etc/max-bots/$kind.env"
done
# Existing webhook routes are preserved byte for byte in the saved snapshot.
cat >> /etc/nginx/snippets/max-bots.conf <<'NGINX'

location /max-bots/fuel/map {
    proxy_pass http://127.0.0.1:8460/map;
    access_log off;
}
location /max-bots/wifi/map {
    proxy_pass http://127.0.0.1:8461/map;
    access_log off;
}
NGINX
ln -sfn "$release" "$base/current"
nginx -t
systemctl restart max-bot-fuel max-bot-wifi
systemctl reload nginx
systemctl is-active max-bot-fuel max-bot-wifi
trap - ERR
printf 'Release: %s\nRollback: bash %s/rollback-maps.sh\nSnapshot: %s\n' "$release" "$base" "$snapshot"
