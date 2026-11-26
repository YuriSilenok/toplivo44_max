#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
release="$base/releases/$(date -u +%Y%m%dT%H%M%S)-labor-buttons"
mkdir -p "$base/backups" "$release"
tar -xzf "$base/labor-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
"$base/venv/bin/python" - <<'PY'
import sqlite3,time
for kind in ['fuel','wifi','labor']:
    with sqlite3.connect('/var/lib/max-bots/'+kind+'/bot.sqlite3') as source, sqlite3.connect('/opt/max-bots/backups/'+kind+'-'+str(int(time.time()))+'.sqlite3') as target:
        source.backup(target)
PY
readlink -f "$base/current" >"$base/backups/before-labor-buttons-release.txt"
ln -sfn "$release" "$base/current"
systemctl restart max-bot-labor
systemctl show max-bot-labor -p ActiveState -p SubState
