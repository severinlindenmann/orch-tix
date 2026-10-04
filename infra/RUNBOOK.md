# fileshare on the VPS — runbook

| | |
|---|---|
| Host | Debian 13 VPS, shared Caddy |
| Public | `https://tix.severin.io` |
| Service | `fileshare.service` — systemd + uv, no Docker |
| Runs as | `fileshare` (system user, nologin, HOME=`/opt/fileshare`) |
| Listens | `127.0.0.1:8808` only |
| Code | `/opt/fileshare` (rsync'd; the box has no git credentials) |
| Data | `/var/lib/fileshare/` — `fileshare.db` (SQLite, WAL) + `blobs/` + `tmp/` |
| Env | `/etc/fileshare/fileshare.env` — root:fileshare 0640 |
| Caddy | `/etc/caddy/sites/fileshare.caddy` (+ the shared `import /etc/caddy/sites/*.caddy` line) |
| fail2ban | jail `fileshare` |

**What the server holds.** Ciphertext blobs, a passphrase-wrapped master key, a
scrypt hash of the login key, hashed device and session tokens, and cleartext
size/device/project/timestamps. No key that opens file content, file names, or
notes. The env file holds **no secrets** (paths and URLs). There are no backups, by decision:
losing the box loses the shared files.

## 1. First install

The script installs the Caddy site last. It refuses while another site file,
`/etc/caddy/sites/orchestrator.caddy`, still serves the same domain; fileshare then keeps
running on 127.0.0.1:8808 until you remove that file and re-run the script.

```bash
# on the laptop, repo root
bash infra/sync.sh --stage-only
# on the box
ssh tix.severin.io
sudo bash ~/fileshare-staging/infra/prepare-vps.sh tix.severin.io
```

Expected tail while such an old site still exists:

```
==> caddy
   SKIPPING Caddy: /etc/caddy/sites/orchestrator.caddy still serves tix.severin.io.
   fileshare is running on 127.0.0.1:8808. Remove that site file, then re-run this script.
```

Once the old site file is gone, re-run the same command. It ends with
`public  {"ok":true}`.

## 2. First login (setup code)

The first browser to finish `/setup` becomes the owner. The setup code proves
you have shell access to the box. Codes are single-use and expire after 15 minutes.

```bash
sudo -u fileshare env HOME=/opt/fileshare \
  /opt/fileshare/.local/bin/uv run --frozen --no-dev --project /opt/fileshare \
  python -m fileshare.admin setup-code --data-dir /var/lib/fileshare
```

Expected: one line starting with `setup_`. Open `https://tix.severin.io/setup`,
paste the code, and choose a passphrase (at least 12 characters). **Store the recovery key the page
shows offline** (password manager plus paper). Passphrase and recovery key both lost = the
files are gone. That is by design, and nobody, including root on this box, can undo it.

## 3. Deploy a new version

```bash
bash infra/sync.sh
```

It refuses to deploy if vendored JS or skill files are missing locally. It
syncs with `uv sync --frozen --no-dev` and restarts. It then waits up to 30 s for
`/healthz`, printing the journal tail if it never answers, and checks
the vendored JS, the skill files and `/onboarding.txt` on the box. Finally it checks
the public `/healthz` body is exactly `{"ok":true}`.

`sync.sh` ships code only. Changes to the systemd unit, the fail2ban
files or the Caddy site take effect only after re-running
`sudo bash ~/fileshare-staging/infra/prepare-vps.sh tix.severin.io` on the box.

**The Caddy site file:** `sync.sh` does not copy `infra/Caddyfile.fileshare`. When it
changes (for example the per-path `X-Frame-Options` for `/sandbox/html`), re-install it to
`/etc/caddy/sites/fileshare.caddy` on deploy, by re-running `prepare-vps.sh` as above,
which substitutes the domain and validates before reloading Caddy.

Migrations (`fileshare/migrations/NNN_*.sql`) only go forward. Rolling code back
across one is unsupported.

Rollback without a migration: check out the older revision on the laptop and
re-run `bash infra/sync.sh`. `uv.lock` pins the dependencies.

## 4. Restore access with the recovery key (lost passphrase, or change it)

The master key never changes, so onboarded devices keep working and no file
is re-encrypted.

1. Create a setup code (§2).
2. Open `https://tix.severin.io/setup` → **Restore from recovery key**.
3. Enter the setup code, the recovery key (`shrk-…`) and a new passphrase.
   The browser rewraps the master key. The server replaces the KDF salt,
   auth hash and wrapped key, and signs out every session.

Changing the passphrase in v1 is this same flow.

## 5. Logs and bans

```bash
journalctl -u fileshare -f
journalctl -u fileshare --since -1h | grep 'auth: failed attempt'
sudo fail2ban-client status fileshare
sudo fail2ban-client set fileshare unbanip <ip>
journalctl -u caddy --since -1h            # Caddy errors only (TLS, upstream down)
```

Request logs come only from uvicorn, in `journalctl -u fileshare`. The Caddy site has no
`log` block (see `Caddyfile.fileshare`), so Caddy writes no access log; its journal holds
errors only.

Public-link tokens: uvicorn's log lines rewrite `/p/<token>` and `/api/public/<token>` to
`…/<redacted>`, so the fileshare journal never holds a token. **Caddy's own error log is not
redacted:** when uvicorn is down or restarting, a proxy error in `journalctl -u caddy` can
include the request path, and so a `/p/<token>`. That is limited exposure: the token alone
can't decrypt anything (the key is in the URL's `#fragment`, which browsers never send), but
it lets someone fetch the ciphertext until the link expires. If you share such a log excerpt,
strip those paths, or revoke the affected link.

The app returns 429 after 10 failed login, setup or handshake attempts per IP in 5 minutes.
fail2ban bans at the firewall after 20 in 10 minutes, for 1 hour.

## 6. What there is to rotate: nothing on the server

- **No API secret, no session secret, no encryption key** lives on the box or in the env file.
  Sessions are random tokens stored hashed in the database. TLS certificates are Caddy's.
- **A lost or stolen device:** revoke it on the Devices page. Its token gets 401
  immediately. It still holds the master key, so treat files it could already
  reach as exposed. Key rotation is planned for v2.
- **A leaked onboarding code:** it expires after 15 minutes and works once, and the
  device it creates stays `pending` until you approve it after comparing fingerprints.
  If a pending device you don't recognise shows up, reject it on the Devices page; if
  one you don't recognise is already connected, revoke it.
- **Passphrase:** §4.

## 7. Operational notes

- **`FS_PUBLIC_URL` must be lowercase, with no explicit `:443`** (and no trailing
  path). The app compares it literally with the browser's `Origin` header, which is
  always lowercase and omits the default port, so `https://Tix.severin.io` or
  `https://tix.severin.io:443` makes every login and upload fail with `bad_origin`.
  `prepare-vps.sh` normalises the domain it writes; re-check after any `sudoedit`.
- **Leftover pending devices.** If a device crashes (or loses its network) between
  the onboarding handshake and writing its `config.json`, the server keeps a
  `pending` device that nothing will ever finish. It is harmless (a pending device
  can do nothing), and the owner rejects it on the Devices page. Onboard that repo
  again with a new code.
- **The onboarding code shows up in shell history and `ps`.** The installer takes it
  as a command-line argument, so it lands in `~/.bash_history` / PSReadLine history
  and is briefly visible in `ps` to other local users. That is by design: the code is
  single-use, expires after 15 minutes, carries no secret (it is a lookup handle), and
  the device it creates does nothing until you approve it by fingerprint.
- **`FS_BUILD`** is rewritten by `infra/sync.sh` on every deploy (git short sha of
  the laptop checkout). It only drives the `?v=` cache-busting stamp; `dev` means the
  box was never deployed through `sync.sh`.
- **Upload spooling.** The unit sets `TMPDIR=/var/lib/fileshare/spool` (created on
  every start) so multipart bodies spool to disk, never to a tmpfs `/tmp`.
- **The pushworker runs from the server's venv, not the app process.** `python -m
  fileshare.pushworker` is the only place that imports `cryptography` / `pywebpush`; the
  main `fileshare` service never does, so it must be invoked with the same
  `/opt/fileshare/.local/bin/uv run --project /opt/fileshare` (or an equivalent activation of that
  venv) as any other server-side command in this runbook, not a bare `python3`. `pywebpush` is an
  ordinary dependency in `pyproject.toml`, so a plain `uv sync --frozen --no-dev` on deploy (already
  part of `infra/sync.sh`, §3) installs it — no extra step.

## 8. Connect orch-core workspaces

Every step can be repeated safely.

1. **Deploy the server.** Run `bash infra/sync.sh` (§3). The database migration 008 (spaces, mirrors, decisions, messages)
   runs at startup.
2. **Install the orch-core plugin** on every machine that runs agents:
   `claude plugin marketplace add severinlindenmann/orch-core`, then `claude plugin install orch-core@orch-core`,
   then `orch instructions sync` in each workspace. The marketplace name is `orch-core`; check it with
   `claude plugin marketplace list`.
3. **Update the sharing skill** in every repo with `sharing update`. `sharing whoami` must show skill `2.0.0`
   or later.
4. **Create one space per workspace** with `sharing space create --label "<workspace>"`, run in the workspace repo.
5. **Install the TIX addon** (a human, at the terminal), from a clone of
   https://github.com/severinlindenmann/orch-tix: `orch addon install <clone>/addons/orch-tix`,
   `orch addon trust orch-tix`, `orch addon enable orch-tix`. Then save the absolute `sharing` path on
   Workspace & addons → TIX.
