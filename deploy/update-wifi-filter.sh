#!/usr/bin/env bash
set -euo pipefail
umask 077
base=/opt/max-bots
stamp=$(date -u +%Y%m%dT%H%M%S)
snapshot="$base/backups/wifi-filter-$stamp"
release="$base/releases/$stamp-wifi-filter"
mkdir -p "$snapshot" "$release"
readlink -f "$base/current" > "$snapshot/previous-release"
cat > "$snapshot/rollback.sh" <<'ROLLBACK'
#!/usr/bin/env bash
set -euo pipefail
snapshot=$(cd -- "$(dirname -- "$0")" && pwd)
previous=$(cat "$snapshot/previous-release")
[[ "$previous" == /opt/max-bots/releases/* && -f "$previous/app.py" ]]
ln -sfn "$previous" /opt/max-bots/current
systemctl restart max-bot-fuel max-bot-wifi
systemctl is-active max-bot-fuel max-bot-wifi
ROLLBACK
chmod 0700 "$snapshot/rollback.sh"
ln -sfn "$snapshot/rollback.sh" "$base/rollback-wifi-filter.sh"
trap 'bash "$snapshot/rollback.sh"' ERR
tar -xzf "$base/wifi-filter-update.tar.gz" -C "$release"
chown -R root:maxbots "$release"
chmod -R g+rX "$release"
chmod 0755 "$release"
ln -sfn "$release" "$base/current"
systemctl restart max-bot-fuel max-bot-wifi
systemctl is-active max-bot-fuel max-bot-wifi
trap - ERR
echo "Release: $release"
echo 'Rollback: bash /opt/max-bots/rollback-wifi-filter.sh'
