"""The only source of "now". Tests monkeypatch `fileshare.clock.now`."""
from datetime import datetime, timezone

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(dt: datetime | None = None) -> str:
    dt = now() if dt is None else dt
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)
