#!/usr/bin/env bash
# Apply a complete runtime archive to the existing VDS; database backups are retained.
set -euo pipefail
umask 077
kind=${1:?Usage: bash update.sh fuel|wifi|labor|all}
case "$kind" in fuel|wifi|labor) kinds=("$kind");; all) kinds=(fuel wifi labor);; *) exit 2;; esac
base=/opt/max-bots
stamp=$(date -u +%Y%m%dT%H%M%S)
release="$base/releases/$stamp-update"
backup="$base/backups/update-$stamp"
mkdir -p "$release" "$backup"
tar -xzf "$base/update.tar.gz" -C "$release"
for kind in "${kinds[@]}"; do
    "$base/venv/bin/python" - "$kind" "$backup" <<'PY'
import sqlite3,sys
kind,backup=sys.argv[1:]
with sqlite3.connect('/var/lib/max-bots/'+kind+'/bot.sqlite3') as source, sqlite3.connect(backup+'/'+kind+'.sqlite3') as target:
    source.backup(target)
PY
done
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
ln -sfn "$release" "$base/current"
for kind in "${kinds[@]}"; do systemctl restart "max-bot-$kind"; done
for kind in "${kinds[@]}"; do systemctl is-active "max-bot-$kind"; done
echo "Release: $release"
