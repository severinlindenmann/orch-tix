"""Who may push (per-ticket phone notifications). Everything here is cleartext routing state the owner chose; it never
reads a title or a text. Enforcement is server-side so a desktop that is off, old or out of date cannot make the phone
buzz: a push for a ticket goes out only while that mirror's `notify` is 1; an agent message names a ticket (follow that
ticket) or not (follow the space's `notify_messages`). A workspace join request ("<device> wants to sync") is not here:
it always notifies (it is a security prompt)."""
import sqlite3


def mirror_notifies(conn: sqlite3.Connection, space_id: str, ticket_id: str) -> bool:
    """TIX-n of a mirror of this space with notify on (an unlinked one keeps its switch, so its last clear still goes)."""
    try:
        n = int(str(ticket_id).removeprefix("TIX-"))
    except ValueError:
        return False
    return conn.execute("SELECT 1 FROM tickets WHERE n = ? AND space_id = ? AND mode = 'mirror' AND notify = 1",
                        (n, space_id)).fetchone() is not None


def message_push_allowed(conn: sqlite3.Connection, space_id: str | None, ticket_n: int | None) -> bool:
    """A message to the human: with a ticket it follows that ticket's switch; without one it follows the space's
    "messages without a ticket" setting. A message that belongs to no space has no switch to read: off."""
    if not space_id:
        return False
    if ticket_n is not None:
        return conn.execute("SELECT 1 FROM tickets WHERE n = ? AND space_id = ? AND mode = 'mirror' AND notify = 1"
                            " AND deleted_at IS NULL", (ticket_n, space_id)).fetchone() is not None
    return conn.execute("SELECT 1 FROM spaces WHERE id = ? AND notify_messages = 1 AND deleted_at IS NULL",
                        (space_id,)).fetchone() is not None


def message_clear_wanted(conn: sqlite3.Connection, space_id: str | None) -> bool:
    """Withdrawing a "message from agent" notification makes sense only where one could have been shown."""
    if not space_id:
        return False
    return (message_push_allowed(conn, space_id, None)
            or conn.execute("SELECT 1 FROM tickets WHERE space_id = ? AND mode = 'mirror' AND notify = 1"
                            " AND deleted_at IS NULL", (space_id,)).fetchone() is not None)
