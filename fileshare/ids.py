import re

_REF = re.compile(r"(?:file)?([1-9][0-9]{0,11})", re.IGNORECASE | re.ASCII)


def format_id(n: int) -> str:
    return f"FILE{n}"


def parse_ref(ref: str) -> int | None:
    m = _REF.fullmatch(ref) if isinstance(ref, str) else None
    return int(m.group(1)) if m else None


# Tickets (spec D5): TIX-42, tix-42, TIX42, tix42 and 42 all name ticket 42.
_TICKET_REF = re.compile(r"(?:tix-?)?([1-9][0-9]{0,11})", re.IGNORECASE | re.ASCII)


def format_ticket_id(n: int) -> str:
    return f"TIX-{n}"


def parse_ticket_ref(ref: str) -> int | None:
    m = _TICKET_REF.fullmatch(ref) if isinstance(ref, str) else None
    return int(m.group(1)) if m else None
