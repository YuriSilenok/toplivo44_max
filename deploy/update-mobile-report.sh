#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
stamp=$(date -u +%Y%m%dT%H%M%S)
snapshot="$base/backups/mobile-report-$stamp"
release="$base/releases/$stamp-mobile-report"
mkdir -p "$snapshot" "$release"
readlink -f "$base/current" > "$snapshot/previous-release"
cat > "$snapshot/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
snapshot=$(cd -- "$(dirname -- "$0")" && pwd)
previous=$(cat "$snapshot/previous-release")
[[ "$previous" == /opt/max-bots/releases/* && -f "$previous/app.py" ]]
ln -sfn "$previous" /opt/max-bots/current
systemctl restart max-bot-fuel max-bot-wifi max-bot-labor
systemctl is-active max-bot-fuel max-bot-wifi max-bot-labor
ROLLBACK
chmod 0700 "$snapshot/rollback.sh"
ln -sfn "$snapshot/rollback.sh" "$base/rollback-mobile-report.sh"
"$base/venv/bin/python" - "$snapshot" <<'PY'
import sqlite3,sys
with sqlite3.connect('/var/lib/max-bots/labor/bot.sqlite3') as source, sqlite3.connect(sys.argv[1]+'/labor.sqlite3') as target:
    source.backup(target)
PY
trap 'bash "$snapshot/rollback.sh"' ERR
tar -xzf "$base/mobile-report-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
ln -sfn "$release" "$base/current"
systemctl restart max-bot-fuel max-bot-wifi max-bot-labor
systemctl is-active max-bot-fuel max-bot-wifi max-bot-labor
trap - ERR
echo "Release: $release"
echo 'Rollback: bash /opt/max-bots/rollback-mobile-report.sh'
