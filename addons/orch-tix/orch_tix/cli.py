"""The sharing CLI through ctx.run: argv only, JSON out, errors as SharingError. Never a shell."""
from __future__ import annotations

import json

from orch.addons.runner import AddonRunError

NOT_CONFIGURED = "set the sharing CLI path in Workspace & addons → TIX"


class SharingError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def configured_path(value) -> bool:
    value = str(value or "")
    return value.startswith("/") or (len(value) > 2 and value[1] == ":")


class Sharing:
    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def path(self) -> str:
        return str(self.ctx.settings.get("sharing_path") or "")

    @property
    def configured(self) -> bool:
        return configured_path(self.path)

    def run_json(self, *args: str, timeout: float = 20.0) -> dict:
        data = self.run_any(*args, timeout=timeout)
        if not isinstance(data, dict):
            raise SharingError("bad_output", "the sharing CLI did not print a JSON object")
        return data

    def run_list(self, *args: str, timeout: float = 20.0) -> list:
        """For commands whose --json output is an array (`list`, `tags`)."""
        data = self.run_any(*args, timeout=timeout)
        if not isinstance(data, list):
            raise SharingError("bad_output", "the sharing CLI did not print a JSON array")
        return data

    def run_any(self, *args: str, timeout: float = 20.0):
        if not self.configured:
            raise SharingError("not_configured", NOT_CONFIGURED)
        try:
            r = self.ctx.run([self.path, *args, "--json"], timeout=timeout)
        except AddonRunError as e:
            raise SharingError("run_failed", str(e)[:300]) from None
        try:
            data = json.loads(r.stdout or "{}")
        except ValueError:
            raise SharingError("bad_output", "the sharing CLI did not print JSON") from None
        if r.returncode != 0:
            err = data if isinstance(data, dict) else {}
            raise SharingError(str(err.get("error") or "failed"), str(err.get("detail") or f"exit {r.returncode}"))
        return data
