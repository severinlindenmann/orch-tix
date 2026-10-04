"""Tags (spec §19): the one place that validates and normalises them, plus their storage helpers."""
import re
import sqlite3
from collections.abc import Iterable

TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}", re.ASCII)
MAX_TAGS = 10


class BadTag(ValueError):
    pass


def normalize_tag(raw) -> str:
    """Trim, lowercase, turn spaces and underscores into '-', then require ^[a-z0-9][a-z0-9-]{0,39}$."""
    if not isinstance(raw, str):
        raise BadTag("a tag must be a string")
    tag = re.sub(r"[ _]", "-", raw.strip().lower())
    if not TAG_RE.fullmatch(tag):
        raise BadTag(f"bad tag {raw!r}: use a-z, 0-9 and '-', starting with a letter or digit, "
                     "at most 40 characters")
    return tag


def normalize_tags(raw) -> list[str]:
    """A list of tags, normalised, deduplicated and sorted; at most MAX_TAGS once duplicates are gone."""
    if not isinstance(raw, list):
        raise BadTag("tags must be a list of strings")
    tags = sorted({normalize_tag(t) for t in raw})
    if len(tags) > MAX_TAGS:
        raise BadTag(f"at most {MAX_TAGS} tags per file")
    return tags


def tags_for(conn: sqlite3.Connection, ns: Iterable[int]) -> dict[int, list[str]]:
    """Every file's sorted tags in ONE query (no N+1). Files without tags are absent."""
    ns = list(ns)
    out: dict[int, list[str]] = {}
    if not ns:
        return out
    rows = conn.execute(f"SELECT file_n, tag FROM file_tags WHERE file_n IN ({','.join('?' * len(ns))})"
                        " ORDER BY file_n, tag", ns)
    for r in rows:
        out.setdefault(r["file_n"], []).append(r["tag"])
    return out


def set_tags(conn: sqlite3.Connection, n: int, tags: list[str]) -> None:
    """Replace a file's tags. The caller holds the transaction."""
    conn.execute("DELETE FROM file_tags WHERE file_n = ?", (n,))
    conn.executemany("INSERT INTO file_tags (file_n, tag) VALUES (?, ?)", [(n, t) for t in tags])
