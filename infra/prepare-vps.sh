#!/usr/bin/env bash
# One-time VPS preparation for fileshare. Idempotent -- safe to re-run.
# Run ON the VPS as root, from the copy `bash infra/sync.sh --stage-only` pushed:
#
#     sudo bash ~/fileshare-staging/infra/prepare-vps.sh tix.severin.io
#
# Creates the service user and directories, installs uv, installs the code, the
# the systemd unit and fail2ban, writes an env-file template, and
# installs the Caddy site. It never edits the shared /etc/caddy/Caddyfile beyond
# one `import` line (already present on this box), and it REFUSES to install the
# Caddy site while the old orchestrator site still serves the same domain --
# remove that site file, then re-run.
set -euo pipefail

DOMAIN="${1:-tix.severin.io}"
# FS_PUBLIC_URL is compared literally with the browser's Origin header, which is
# always lowercase with no default port -- normalise before it is written.
DOMAIN=$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')
DOMAIN="${DOMAIN%:443}"
# It goes into the env file, a sed replacement and the Caddy site address.
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || { echo "invalid domain '$DOMAIN' (want e.g. tix.severin.io)" >&2; exit 1; }
STAGE="${FS_STAGE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PREFIX=/opt/fileshare
DATA=/var/lib/fileshare
CONF=/etc/fileshare
ENVF=$CONF/fileshare.env
CADDY_SITES=/etc/caddy/sites
UV=$PREFIX/.local/bin/uv
# Trust boundary: everything under $PREFIX is owned (writable) by the service
# user. Root never executes anything there (uv runs via `sudo -u fileshare`),
# and every root-owned file (units, fail2ban, Caddy) is
# installed from $STAGE/infra -- the operator's own staging copy -- never from
# $PREFIX/infra.

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -f "$STAGE/pyproject.toml" ] && [ -f "$STAGE/fileshare/app.py" ] \
  || { echo "no fileshare checkout at $STAGE -- run 'bash infra/sync.sh --stage-only' from the laptop first"; exit 1; }
[ "$STAGE" != "$PREFIX" ] || { echo "run from the staging copy, not from $PREFIX"; exit 1; }

say "packages"
need=()
for p in sqlite3 rsync fail2ban curl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -qq && apt-get install -y -qq "${need[@]}"
else
  echo "   all present"
fi

say "service user and directories"
id -u fileshare >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin fileshare
install -d -o fileshare -g fileshare -m 0750 "$PREFIX" "$PREFIX/.cache" "$PREFIX/.local" "$DATA"
install -d -o root -g fileshare -m 0750 "$CONF"

say "uv"
[ -x "$UV" ] || sudo -u fileshare env HOME="$PREFIX" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u fileshare env HOME="$PREFIX" "$UV" --version

say "code"
rsync -a --delete \
  --exclude '.venv/' --exclude '.local/' --exclude '.cache/' --exclude '.config/' --exclude '.ssh/' \
  "$STAGE/" "$PREFIX/"
chown -R fileshare:fileshare "$PREFIX"
sudo -u fileshare env HOME="$PREFIX" "$UV" sync --frozen --no-dev --project "$PREFIX"

say "environment file"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
FS_DATA_DIR=$DATA
FS_PUBLIC_URL=https://$DOMAIN
FS_MAX_UPLOAD=209715200
FS_SKILL_DIR=$PREFIX/skill/sharing
FS_COOKIE_SECURE=1
# Build stamp for ?v= cache busting; infra/sync.sh rewrites it on every deploy.
FS_BUILD=dev
EOF
  echo "   wrote $ENVF"
else
  echo "   keeping existing $ENVF"
fi
chown root:fileshare "$ENVF"
chmod 0640 "$ENVF"

say "systemd"
# All from $STAGE, never $PREFIX: the fileshare user owns /opt/fileshare and
# must not be able to edit a unit, or a script that root executes.
install -o root -g root -m 0644 "$STAGE/infra/fileshare.service"        /etc/systemd/system/fileshare.service
# Backups were dropped (2026-09-25): remove the units an older install left behind.
systemctl disable --now fileshare-backup.timer 2>/dev/null || true
rm -f /etc/systemd/system/fileshare-backup.service /etc/systemd/system/fileshare-backup.timer \
      /usr/local/sbin/fileshare-backup
systemctl daemon-reload
systemctl enable fileshare.service
systemctl restart fileshare.service
body=""
for _ in $(seq 30); do
  body=$(curl -fsS http://127.0.0.1:8808/healthz 2>/dev/null) && break
  sleep 1
done
[ -n "$body" ] || { echo "   fileshare did not come up" >&2; journalctl -u fileshare -n 50 --no-pager >&2; exit 1; }
echo "   local healthz $body"

say "fail2ban"
install -o root -g root -m 0644 "$STAGE/infra/fail2ban/filter.d/fileshare.conf" /etc/fail2ban/filter.d/fileshare.conf
install -o root -g root -m 0644 "$STAGE/infra/fail2ban/jail.d/fileshare.conf"   /etc/fail2ban/jail.d/fileshare.conf
systemctl enable --now fail2ban >/dev/null
fail2ban-client reload
fail2ban-client status fileshare

say "caddy"
if [ -e "$CADDY_SITES/orchestrator.caddy" ]; then
  echo "   SKIPPING Caddy: $CADDY_SITES/orchestrator.caddy still serves $DOMAIN."
  echo "   fileshare is running on 127.0.0.1:8808. Remove that site file, then re-run this script."
  exit 0
fi
install -d -m 0755 "$CADDY_SITES"
MAIN_CADDY=/etc/caddy/Caddyfile
# Snapshot both files BEFORE touching either. Until validate + reload succeed,
# ANY exit (a failed command under set -e, an invalid config, Ctrl-C) restores
# them, so the shared Caddy is never left with an unvalidated fileshare site.
caddy_committed=0
prev_site=""
prev_main=$(mktemp)
cp -p "$MAIN_CADDY" "$prev_main"
if [ -f "$CADDY_SITES/fileshare.caddy" ]; then
  prev_site=$(mktemp); cp -p "$CADDY_SITES/fileshare.caddy" "$prev_site"
fi
caddy_rollback() {
  if [ "$caddy_committed" != 1 ]; then
    echo "   caddy: restoring the previous configuration" >&2
    if [ -n "$prev_site" ]; then cp -p "$prev_site" "$CADDY_SITES/fileshare.caddy"
    else rm -f "$CADDY_SITES/fileshare.caddy"; fi
    cat "$prev_main" > "$MAIN_CADDY"      # keeps the file's owner and mode
  fi
  rm -f "$prev_main" ${prev_site:+"$prev_site"}
}
trap 'caddy_rollback' EXIT
trap 'exit 130' INT TERM HUP

sed "s|FS_DOMAIN|$DOMAIN|g" "$STAGE/infra/Caddyfile.fileshare" > "$CADDY_SITES/fileshare.caddy"
if ! grep -qF "import $CADDY_SITES/*.caddy" "$MAIN_CADDY"; then
  printf '\n# fileshare: its site block lives in its own file.\nimport %s/*.caddy\n' \
    "$CADDY_SITES" >> "$MAIN_CADDY"
  echo "   added one import line to $MAIN_CADDY"
fi
# Validate with the same environment systemd gives Caddy: a site on this box
# addresses itself as {$CADDY_DOMAIN} from a drop-in, and a bare `caddy validate`
# fails on config that is actually fine. No drop-in at all is fine too: without
# `|| true` the failing cat would abort the script under pipefail.
caddy_env=$( { cat /etc/systemd/system/caddy.service.d/*.conf 2>/dev/null || true; } \
            | sed -n 's/^Environment=//p' | tr '\n' ' ')
# shellcheck disable=SC2086
if ! env $caddy_env caddy validate --config "$MAIN_CADDY" --adapter caddyfile >/dev/null 2>&1; then
  echo "   caddy config is INVALID -- reverting and stopping" >&2
  # shellcheck disable=SC2086
  env $caddy_env caddy validate --config "$MAIN_CADDY" --adapter caddyfile 2>&1 | tail -5 >&2 || true
  exit 1                                   # the EXIT trap restores both files
fi
systemctl reload caddy
caddy_committed=1

say "done"
echo "   local   $(curl -fsS http://127.0.0.1:8808/healthz)"
echo "   public  $(curl -fsS "https://$DOMAIN/healthz" || echo 'not yet reachable')"
echo
echo "   Next: create a setup code (RUNBOOK §2)."
