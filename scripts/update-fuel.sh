#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
release="$base/releases/$(date -u +%Y%m%dT%H%M%S)-datalens"
mkdir -p "$base/backups" "$release"
tar -xzf "$base/fuel-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
# Consistent backup of the live SQLite database; no application scenarios run.
"$base/venv/bin/python" - <<'PY'
import sqlite3,time
from pathlib import Path
path=Path('/var/lib/max-bots/fuel/bot.sqlite3')
with sqlite3.connect(str(path)) as source, sqlite3.connect('/opt/max-bots/backups/fuel-'+str(int(time.time()))+'.sqlite3') as target:
    source.backup(target)
PY
readlink -f "$base/current" >"$base/backups/fuel-before-datalens-release.txt"
ln -sfn "$release" "$base/current"
systemctl restart max-bot-fuel
systemctl show max-bot-fuel -p ActiveState -p SubState -p MainPID
