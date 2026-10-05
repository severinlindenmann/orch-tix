#!/usr/bin/env bash
# Deploy fileshare to the VPS by rsync, from a laptop. Run from the repo root:
#
#     bash infra/sync.sh                 # stage + install + restart + verify
#     bash infra/sync.sh --stage-only    # first install: only push to ~/fileshare-staging,
#                                        # then run prepare-vps.sh on the box (RUNBOOK §1)
#     FS_HOST=other-host bash infra/sync.sh
#     FS_SKIP_PUBLIC=1 bash infra/sync.sh   # before cutover: the domain still serves the old app
#
# The server never needs git credentials: code arrives by rsync only.
set -euo pipefail

HOST="${FS_HOST:-tix.severin.io}"
DOMAIN="${FS_DOMAIN:-tix.severin.io}"
PREFIX=/opt/fileshare
STAGE=fileshare-staging

cd "$(dirname "${BASH_SOURCE[0]}")/.."
say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
# The box gets no .git, so the page build stamp (?v=, Task 17) comes from FS_BUILD.
# The stamp also names the files for the browser's immutable cache (`?v=<build>` is cached for a year, fileshare/routes/
# pages.py): it must change whenever a shipped file changes, including an uncommitted edit on top of the same commit, so it
# carries a digest of fileshare/static next to the commit.
BUILD="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)-$(find fileshare/static -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -c1-10)"
fail() { echo "   FAILED: $1" >&2; exit 1; }

# Refuse to ship too little. A server without its vendored JS or its skill files
# passes /healthz while serving a broken UI and a broken installer -- the old
# deploy shipped exactly that once (missing design/static).
for f in pyproject.toml uv.lock fileshare/app.py fileshare/templates/onboarding.sh \
         fileshare/static/vendor/purify.min.js fileshare/static/vendor/markdown-it.min.js \
         fileshare/static/fonts/figtree-latin-wght.woff2 fileshare/static/fonts/manrope-latin-wght.woff2 \
         fileshare/static/fonts/OFL-Figtree.txt fileshare/static/fonts/OFL-Manrope.txt \
         fileshare/static/css/tokens.css fileshare/static/sw.js fileshare/static/precache.json \
         skill/sharing/sharing.py skill/sharing/SKILL.md skill/sharing/sharing; do
  [ -f "$f" ] || fail "missing $f locally -- refusing to deploy"
done

say "sync -> $HOST:~/$STAGE"
# Anything git ignores (per-directory .gitignore files included) stays local, as
# do env files and editor/backup leftovers even if .gitignore misses them.
rsync -az --delete \
  --filter=':- .gitignore' \
  --exclude '.env' --exclude '.env.*' --exclude '*.bak' \
  --exclude '.git/' --exclude '.venv/' --exclude '.cache/' --exclude '.local/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
  --exclude 'node_modules/' --exclude 'test-results/' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' --exclude '.DS_Store' \
  --exclude '.claude/' --exclude '.superpowers/' --exclude '.ruff_cache/' \
  ./ "$HOST:~/$STAGE/"

if [ "${1:-}" = "--stage-only" ]; then
  echo "   staged. Now, on the box: sudo bash ~/$STAGE/infra/prepare-vps.sh $DOMAIN"
  exit 0
fi

say "install -> $PREFIX"
# $PREFIX is also the service user's HOME, so uv, its caches and the venv live
# inside it. A plain --delete erased them once; exclude everything that belongs
# to the home rather than the repo.
# shellcheck disable=SC2029
ssh "$HOST" "sudo rsync -a --delete \
  --exclude '.venv/' --exclude '.local/' --exclude '.cache/' --exclude '.config/' --exclude '.ssh/' \
  ~/$STAGE/ $PREFIX/ \
  && sudo chown -R fileshare:fileshare $PREFIX \
  && sudo -u fileshare env HOME=$PREFIX $PREFIX/.local/bin/uv sync --frozen --no-dev --project $PREFIX \
  && sudo sed -i '/^FS_BUILD=/d' /etc/fileshare/fileshare.env \
  && echo FS_BUILD=$BUILD | sudo tee -a /etc/fileshare/fileshare.env >/dev/null \
  && sudo systemctl restart fileshare"

say "verify on the box (127.0.0.1:8808)"
# shellcheck disable=SC2016
ssh "$HOST" 'for i in $(seq 30); do
    if body=$(curl -fsS http://127.0.0.1:8808/healthz 2>/dev/null); then echo "   healthz $body"; break; fi
    sleep 1
  done
  if [ -z "${body:-}" ]; then
    echo "   FAILED: /healthz did not answer within 30 s" >&2
    sudo journalctl -u fileshare -n 50 --no-pager >&2
    exit 1
  fi
  for p in /static/vendor/purify.min.js /static/vendor/markdown-it.min.js /static/precache.json /sw.js /skill/sharing.py /onboarding.txt; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8808$p")
    [ "$code" = 200 ] || { echo "   FAILED: $p -> $code" >&2; exit 1; }
  done
  systemctl is-active --quiet fileshare || { echo "   FAILED: service not active" >&2; exit 1; }' \
  || fail "on-box verification"

if [ "${FS_SKIP_PUBLIC:-0}" = 1 ]; then
  echo "   skipping public check (FS_SKIP_PUBLIC=1)"
else
  say "verify public https://$DOMAIN"
  body=$(curl -fsS "https://$DOMAIN/healthz") || fail "public /healthz unreachable"
  [ "$body" = '{"ok":true}' ] || fail "public /healthz returned '$body' -- is the old app still serving $DOMAIN?"
  echo "   public healthz $body"
fi
echo "   ok"
