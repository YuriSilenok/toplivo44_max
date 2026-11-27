#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
stamp=$(date -u +%Y%m%dT%H%M%S)
snapshot="$base/backups/operator-reports-$stamp"
release="$base/releases/$stamp-operator-reports"
mkdir -p "$snapshot" "$release"
readlink -f "$base/current" > "$snapshot/previous-release"
cat > "$snapshot/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
snapshot=$(cd -- "$(dirname -- "$0")" && pwd)
previous=$(cat "$snapshot/previous-release")
[[ "$previous" == /opt/max-bots/releases/* && -f "$previous/app.py" ]]
ln -sfn "$previous" /opt/max-bots/current
systemctl restart max-bot-fuel
systemctl is-active max-bot-fuel
ROLLBACK
chmod 0700 "$snapshot/rollback.sh"
ln -sfn "$snapshot/rollback.sh" "$base/rollback-operator-reports.sh"
"$base/venv/bin/python" - "$snapshot" <<'PY'
import sqlite3,sys
with sqlite3.connect('/var/lib/max-bots/fuel/bot.sqlite3') as source, sqlite3.connect(sys.argv[1]+'/fuel.sqlite3') as target:
    source.backup(target)
PY
trap 'bash "$snapshot/rollback.sh"' ERR
tar -xzf "$base/operator-reports-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
ln -sfn "$release" "$base/current"
systemctl restart max-bot-fuel
systemctl is-active max-bot-fuel
trap - ERR
echo "Release: $release"
echo 'Rollback: bash /opt/max-bots/rollback-operator-reports.sh'
