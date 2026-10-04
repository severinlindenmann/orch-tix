# Security policy

TIX is an end-to-end encrypted file share, so security reports matter more here than most bugs.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**https://github.com/severinlindenmann/orch-tix/security/advisories/new**

Do not open a public issue, pull request or discussion for a security problem, and do not post
details, proofs of concept or step-by-step reproductions anywhere public before a fix is released.

A useful report says which component is affected, what an attacker can achieve, what they need
first (network position, a stolen device, a malicious server, and so on), and how you found it.
You will get an answer in the advisory thread. Once a fix ships, the advisory is published with
credit unless you prefer otherwise.

## Scope

In scope:

- **Server** (`fileshare/`): authentication, sessions, device approval, rate limits, headers,
  storage and anything that leaks more than [docs/threat-model.md](docs/threat-model.md) says the
  server may see.
- **Web app and PWA** (`fileshare/static/`): XSS, CSP or sandbox escapes, the service worker, the
  offline outbox, and anything that exposes keys or plaintext.
- **Cryptography**: the key hierarchy, file and metadata encryption, public and upload links, the
  device handshake, mirrored tickets and decisions, and the shared test vectors.
- **The `sharing` skill and CLI** (`skill/sharing/`): local key storage, file handling, the
  installers and the update path.
- **The orch-tix addon** (`addons/orch-tix/`): what it mirrors, how it applies phone decisions, and
  the files it writes.

Out of scope: the limits [docs/threat-model.md](docs/threat-model.md) already states (for example,
a server that is compromised and serves modified JavaScript, or a compromised onboarded device),
denial of service by volume, and issues in third-party services or dependencies that do not
affect TIX. Never test against someone else's deployment; run your own (see the README).
