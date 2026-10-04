# Contributing

Thanks for helping. A few things keep this project moving:

- **Run the suites locally before you push.** CI runs only on pull requests to `main` and on
  `main` itself, not on pushes to other branches, so your machine is the first check:

  ```bash
  uv sync
  uv run pytest -m "not e2e and not browser"
  node --test tests/js/
  uv run pytest -m e2e
  uv run pytest -m browser          # Playwright; needs: uv run playwright install chromium
  ```

  If you touch `addons/orch-tix/`, also run its tests against a checkout of
  [orch-core](https://github.com/severinlindenmann/orch-core) (see [addons/orch-tix/README.md](addons/orch-tix/README.md)).
- **Open a pull request against `main`.** Keep it focused, say what changed and why, and say how
  you tested it.
- **Crypto and wire formats change together.** A change to an envelope, a key derivation or an
  AAD prefix needs the Python CLI, the browser code and the vectors in `tests/vectors/` updated in
  the same pull request.
- **Security problems go to a private advisory**, never a public issue. See [SECURITY.md](SECURITY.md).
