#!/usr/bin/env bash
# sharing — device installer for {{PUBLIC_URL}}
#
#   curl --proto '=https' --tlsv1.2 -fsSL {{PUBLIC_URL}}/onboarding.txt | bash -s -- 'shr1.…' [options]
#
# Installs the Claude Code skill "sharing" into THIS git repo and onboards it as one device, which
# you then approve in the web UI by comparing a fingerprint.
# Options: --device NAME  --project NAME  --force  --yes   (see --help)
# Safe to read first:  curl -fsSL {{PUBLIC_URL}}/onboarding.txt -o onboarding.sh && less onboarding.sh
# Nothing executes until the final line, so a truncated download does nothing.
# Exit codes (as onboarding.ps1): the handshake's own (3 = rejected, still pending or code refused),
# 5 = integrity (skill download fails its manifest sha256, or cryptography won't load), 6 = local guard.

SHARING_TMP=""
SHARING_DEST=""
SHARING_CREATED=0

say() { printf 'sharing: %s\n' "$*" >&2; }
# die MESSAGE [EXIT]: 1 by default; 5 = integrity, 6 = local guard (the same codes as onboarding.ps1).
die() { say "error: $1"; exit "${2:-1}"; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# The sha256 the server's /skill/manifest.json lists for file $1, or nothing. The manifest is
# compact JSON ({"version":…,"files":{"SKILL.md":"<hex>",…}}); the key must match exactly.
manifest_sha() {
  local key="${1//./\\.}"
  grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[0-9a-f]{64}\"" "$SHARING_TMP/manifest.json" \
    | grep -oE '[0-9a-f]{64}' | head -n 1
}

usage() {
  cat >&2 <<'EOF'
usage: … | bash -s -- 'shr1.<code>' [--device NAME] [--project NAME] [--force] [--yes]
  --device NAME   device name (default: short hostname, lower-cased)
  --project NAME  project name (default: git top-level directory name)
  --force         replace an identity already installed in this repo (revoked once the new one is approved)
  --yes           install uv without asking if it is missing
EOF
}

cleanup() {
  if [ -n "$SHARING_TMP" ] && [ -d "$SHARING_TMP" ]; then rm -rf "$SHARING_TMP"; fi
  # A skill folder this run created but that never received a config.json is a partial install: remove it.
  if [ "$SHARING_CREATED" -eq 1 ] && [ -n "$SHARING_DEST" ] && [ ! -f "$SHARING_DEST/config.json" ]; then
    rm -rf "$SHARING_DEST"
  fi
}

slug() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._-' '-' | sed -e 's/^[-.]*//' -e 's/-*$//' | cut -c1-64
}

ask_yes() {
  # stdin is this script (curl | bash), so ask on the terminal
  local answer=""
  if ! (exec </dev/tty) 2>/dev/null; then return 1; fi
  printf 'sharing: %s [y/N] ' "$1" >/dev/tty
  read -r answer </dev/tty || return 1
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

platform() {
  case "$(uname -s 2>/dev/null)" in Darwin) echo darwin ;; *) echo linux ;; esac
}

main() {
  set -euo pipefail
  umask 077
  trap cleanup EXIT

  local server="${SHARING_SERVER:-{{PUBLIC_URL}}}"
  server="${server%/}"
  local code="" device="" project="" force=0 yes=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --device)  [ "$#" -ge 2 ] || die "--device needs a value"; device="$2"; shift 2 ;;
      --project) [ "$#" -ge 2 ] || die "--project needs a value"; project="$2"; shift 2 ;;
      --force)   force=1; shift ;;
      --yes)     yes=1; shift ;;
      -h|--help) usage; return 0 ;;
      shr1.*)    code="$1"; shift ;;
      *)         die "unexpected argument (see --help; the onboarding code starts with shr1.)" ;;
    esac
  done

  # 1. Free local checks. The code is never printed.
  [ -n "$code" ] || { usage; die "missing onboarding code"; }
  [[ "$code" =~ ^shr1\.[A-Za-z0-9_-]{22}$ ]] \
    || die "that is not a valid onboarding code — copy the whole command again from the web UI"

  local -a curl_opts=(-fsSL --retry 2)
  case "$server" in
    https://?*) curl_opts+=(--proto '=https' --tlsv1.2) ;;
    http://127.0.0.1|http://127.0.0.1:*|http://localhost|http://localhost:*|'http://[::1]'|'http://[::1]:'*)
      curl_opts+=(--proto '=http') ;;
    *) die "refusing server $server: only https (or http on localhost, for tests) is allowed" ;;
  esac

  # 2. Preflight: everything that can fail runs BEFORE the one-time code is spent.
  command -v curl >/dev/null 2>&1 || die "curl is required"
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
    || die "sha256sum or shasum is required to verify the downloaded skill; nothing was installed and the code was not used"
  local root="" in_git=0
  if command -v git >/dev/null 2>&1 && root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    in_git=1
  elif [ -n "$project" ]; then
    root="$PWD"
  else
    die "run this inside a git repository (or pass --project NAME to onboard this directory)" 6
  fi
  cd "$root"

  # The handshake re-checks this with git_tracked, which also refuses when a .git exists but git cannot run.
  if [ "$in_git" -eq 1 ]; then
    local tracked=""
    tracked="$(git -C "$root" ls-files -- .claude/skills/sharing)" \
      || die "could not ask git whether .claude/skills/sharing is tracked; fix git, then re-run" 6
    [ -z "$tracked" ] \
      || die "files under .claude/skills/sharing are tracked by git — the device key would be committed. Run: git rm -r --cached .claude/skills/sharing" 6
  fi

  [ -n "$device" ]  || device="$(hostname -s 2>/dev/null || hostname)"
  [ -n "$project" ] || project="$(basename "$root")"
  device="$(slug "$device")"
  project="$(slug "$project")"
  [ -n "$device" ]  || die "could not derive a device name; pass --device NAME"
  [ -n "$project" ] || die "could not derive a project name; pass --project NAME"

  local dest="$root/.claude/skills/sharing"
  if [ -f "$dest/config.json" ] && [ "$force" -ne 1 ]; then
    die "this repo is already onboarded ($dest/config.json). Re-run with --force to replace that identity." 6
  fi

  if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
      PATH="$HOME/.local/bin:$PATH"
    elif [ "$yes" -eq 1 ] || ask_yes "uv (https://docs.astral.sh/uv/) is required. Install it now?"; then
      say "installing uv…"
      curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh >&2
      PATH="$HOME/.local/bin:$PATH"
      command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH"
    else
      die "uv is required. Install it (https://docs.astral.sh/uv/) or re-run with --yes"
    fi
  fi
  say "checking that encryption works (the first run downloads 'cryptography')…"
  uv run --quiet --no-project --with 'cryptography>=43' python -c \
    'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.asymmetric import ec; AESGCM(bytes(32)); ec.generate_private_key(ec.SECP256R1())' >/dev/null \
    || die "could not load 'cryptography' through uv — nothing was installed and the code was not used" 5

  # 3. Fetch the skill into a private temp dir and verify every file against the server's manifest.
  SHARING_TMP="$(mktemp -d "${TMPDIR:-/tmp}/sharing.XXXXXX")"
  curl "${curl_opts[@]}" -o "$SHARING_TMP/manifest.json" "$server/skill/manifest.json" \
    || die "the server did not return a skill manifest ($server/skill/manifest.json)"
  local f want got
  for f in SKILL.md tickets-SKILL.md sharing.py sharing; do
    want="$(manifest_sha "$f" || true)"
    [ -n "$want" ] || die "the server's skill manifest does not list $f; nothing was installed and the code was not used" 5
    curl "${curl_opts[@]}" -o "$SHARING_TMP/$f" "$server/skill/$f" || die "could not download $f from $server"
    got="$(sha256_of "$SHARING_TMP/$f")"
    [ "$got" = "$want" ] \
      || die "download of $f is corrupt (sha256 mismatch); nothing was installed and the code was not used" 5
  done
  chmod 755 "$SHARING_TMP/sharing"
  chmod 644 "$SHARING_TMP/SKILL.md" "$SHARING_TMP/tickets-SKILL.md" "$SHARING_TMP/sharing.py"

  # 4. Install the files into the real skill folder. It ignores itself BEFORE anything secret lands in it.
  #    config.json (an existing identity under --force) is left alone; the handshake moves it aside.
  SHARING_DEST="$dest"
  [ -d "$dest" ] || SHARING_CREATED=1
  mkdir -p "$dest"
  printf '*\n' > "$dest/.gitignore"
  for f in SKILL.md tickets-SKILL.md sharing.py sharing; do mv -f "$SHARING_TMP/$f" "$dest/$f"; done

  # 5. Handshake, then wait for approval in the web UI. This spends the code; it travels over a pipe, never in argv.
  #    Its exit code is ours: 3 = rejected / still pending / code refused, 5 = integrity, 6 = local guard.
  local -a hs_flags=(--server "$server" --repo "$root" --skill-dir "$dest" --platform "$(platform)"
                     --device "$device" --project "$project" --hostname "$(hostname 2>/dev/null || echo unknown)")
  if [ "$force" -eq 1 ]; then hs_flags+=(--force); fi
  local hs_rc=0
  printf '%s' "$code" | "$dest/sharing" _handshake --code - "${hs_flags[@]}" || hs_rc=$?
  if [ "$hs_rc" -ne 0 ]; then
    say "error: onboarding did not complete (see the message above)"
    exit "$hs_rc"
  fi

  # 6. Prove it works; show what was written (nothing secret).
  "$dest/sharing" whoami
  say "installed skill: $dest"
  say "device config:   $dest/config.json (owner-only; .gitignore '*' keeps this folder out of every commit)"
  say "done. Start a NEW Claude Code session in this repo, then ask e.g. \"get me FILE1\" or \"share notes.md to the remote\"."
}

main "$@"
