import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILL_DIR = REPO_ROOT / "skill" / "sharing"
_FALSE = {"0", "false", "no", "off"}
LEGACY_TICKETS = ("writable", "readonly")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str
    max_upload: int = 209_715_200
    skill_dir: Path = DEFAULT_SKILL_DIR
    cookie_secure: bool = True
    legacy_tickets: str = "writable"    # "readonly": every legacy ticket write is 410 legacy_readonly

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "skill_dir", Path(self.skill_dir))
        object.__setattr__(self, "public_url", self.public_url.rstrip("/"))
        if self.legacy_tickets not in LEGACY_TICKETS:
            # a typo must never leave legacy tickets writable without anyone noticing (final review M1)
            raise ValueError(f"FS_LEGACY_TICKETS must be writable or readonly, not {self.legacy_tickets!r}")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fileshare.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("FS_DATA_DIR", "/var/lib/fileshare")),
            public_url=env.get("FS_PUBLIC_URL", "https://tix.severin.io"),
            max_upload=int(env.get("FS_MAX_UPLOAD", "209715200")),
            skill_dir=Path(env.get("FS_SKILL_DIR", str(DEFAULT_SKILL_DIR))),
            cookie_secure=env.get("FS_COOKIE_SECURE", "1").strip().lower() not in _FALSE,
            legacy_tickets=env.get("FS_LEGACY_TICKETS", "writable").strip().lower(),
        )
