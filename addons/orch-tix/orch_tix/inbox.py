"""Down-sync (spec §4.2–§4.4): decisions from TIX become direct applies (paired phones, through core's
ctx.remote_decision) or Apply / Ignore items. A phone never moves, closes or reopens a ticket."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from orch.addons.api import Intent, PendingDecision, Snapshot

from .cli import SharingError

MAX_AGE = timedelta(days=14)
APPLYING_TIMEOUT = timedelta(minutes=10)
ACK_BATCH = 5
WAIT_S = 25
FINAL = ("applied", "ignored", "stale", "superseded", "answered-locally", "unlinked")
DIRECT = ("answer", "approve", "request_changes", "verdict")
KINDS = DIRECT + ("comment", "ticket_request")
GATES = ("requirements", "plan")
# Why core held a decision (RemoteResult pending), as the fixed code the phone shows (server WAITING), looked up on
# RemoteResult.code (orch-core API 2.4). Any other pending code, and an unknown one, reads "waiting-check".
_CODES = {"not-paired": "waiting-unpaired", "bad-signature": "waiting-signature",
          "kind-switched-off": "waiting-switched-off", "implausible-time": "waiting-time"}
# Older orch-core sends no code: matched on its own messages.
_WAITING = (("not paired", "waiting-unpaired"), ("signature", "waiting-signature"), ("switched off", "waiting-switched-off"),
            ("not plausible", "waiting-time"))


def waiting_code(result) -> str:
    code = str(getattr(result, "code", "") or "")
    if code:
        return _CODES.get(code, "waiting-check")
    m = str(getattr(result, "message", "") or "").lower()
    return next((c for needle, c in _WAITING if needle in m), "waiting-check")


_REMOTE_MAP = {"applied": "applied", "stale": "stale", "superseded": "superseded", "duplicate": "superseded",
               "answered-locally": "answered-locally", "unlinked": "unlinked"}
_QID = re.compile(r"Q[1-9][0-9]*")
_AUTH = ("unauthenticated", "revoked", "pending", "not_configured", "no_space", "not_owner", "refused")
VERDICTS = {"done": "done", "follow-up": "follow-up", "send_back": "follow-up"}   # send_back: legacy PWA alias
UNDECRYPTABLE = "could not be decrypted on this desktop"
TOO_OLD = "older than 14 days; answer again on the desktop"


def _at(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def _too_old(rec: dict, now: datetime) -> bool:
    d = rec.get("decision") or {}
    at = _at(d.get("at")) or _at(rec.get("created_at"))
    return at is not None and at < now - MAX_AGE


def _doc(ctx, key):
    if not key:
        return None
    try:
        return ctx.document(str(key))
    except Exception:
        return None


def _str(value, cap: int = 2000) -> str:
    return value[:cap] if isinstance(value, str) else ""


def _question(doc: dict, qid) -> dict | None:
    want = str(qid or "").strip().upper()
    return next((q for q in doc.get("questions") or [] if str(q.get("id", "")).upper() == want), None)


def _verdict_round(doc: dict):
    return next((n.get("round") for n in doc.get("needs") or [] if (n or {}).get("kind") == "verdict"), None)


def overtaken(addon, rec: dict, doc: dict | None, now: datetime) -> tuple[str | None, str]:
    """What the desktop did meanwhile, as an ack outcome (None: the item still stands)."""
    kind, d = rec.get("kind"), rec.get("decision") or {}
    if _too_old(rec, now):
        return "stale", TOO_OLD
    if kind == "ticket_request":
        return None, ""
    if doc is None:
        return "unlinked", "the ticket is gone"
    link = addon.state.links().get(str(doc.get("id")))
    if not link or link.get("retired"):
        return "unlinked", "the ticket is not synced to TIX"
    target = d.get("target") if isinstance(d.get("target"), dict) else {}
    if kind == "answer":
        q = _question(doc, target.get("qid"))
        if q is None:
            return "stale", "the question is gone"
        if q.get("answer") not in (None, ""):
            return "answered-locally", "answered on the desktop"
        if q.get("hash") != target.get("hash"):
            return "stale", "the question changed since"
    elif kind in ("approve", "request_changes"):
        gate = target.get("gate")
        g = (doc.get("gates") or {}).get(gate) if gate in GATES else None
        if not isinstance(g, dict):
            return "stale", "no such gate"
        if g.get("state") == "approved":
            return "superseded", "already approved on the desktop"
        if g.get("hash") != target.get("hash"):
            return "stale", f"the {gate} changed since"
        if kind == "approve" and target.get("plan_hash"):      # requirements and plan together (schema 1.4)
            plan = (doc.get("gates") or {}).get("plan")
            if not isinstance(plan, dict) or plan.get("hash") != target.get("plan_hash"):
                return "stale", "the plan changed since"
    elif kind == "verdict":
        if d.get("value") not in VERDICTS:
            return "stale", "a verdict is done or follow-up"
        if doc.get("status") != "testing":
            return "stale", f"{doc.get('id')} is {doc.get('status')} now"
        if target.get("round") != _verdict_round(doc):
            return "stale", "this verdict was given for an earlier testing round"
        # schema 1.3: the verdict hash binds the criteria and evidence the phone showed; a phone app that sends none
        # (or the hash of other text) decides nothing
        vh = (doc.get("verdict") or {}).get("hash") if isinstance(doc.get("verdict"), dict) else None
        if not target.get("hash"):
            return "stale", "the phone sent no verdict hash; update the phone app"
        if target.get("hash") != vh:
            return "stale", "the criteria or evidence changed since"
    return None, ""


_LOG_RESULT = {None: "waiting for Apply", "applying": "applying", "applied": "applied", "ignored": "ignored"}


def log_decision(addon, rec: dict, outcome, message) -> None:
    """A phone decision in the "Sent to TIX" log: its kind, ticket and outcome (applied or refused, and why)."""
    addon.state.log({"kind": f"decision {rec.get('kind') or '?'}", "key": rec.get("ticket") or "", "tix": rec.get("tix"),
                     "result": _LOG_RESULT.get(outcome, "refused"), "reason": str(message or "")[:200]})


def receive(addon, pctx, items, *, now) -> None:
    st = addon.state
    known = st.decisions()
    for item in items or []:
        did = str((item or {}).get("id") or "")
        if not did.startswith("dec_") or did in known:
            continue
        d = item.get("decision") if isinstance(item.get("decision"), dict) else {}
        kind = str(item.get("kind") or "")
        rec = {"kind": kind, "tix": item.get("ticket"), "ticket": d.get("ticket"), "gen": item.get("gen"),
               "decision": d, "created_at": item.get("created_at"), "received_at": now.isoformat(),
               "outcome": None, "message": "", "acked": False}
        doc = None if kind == "ticket_request" else _doc(pctx, d.get("ticket"))
        if doc is not None:
            rec["ticket"] = doc.get("id")
        if item.get("error") or not d:
            rec.update(outcome="stale", message=UNDECRYPTABLE)
        elif _too_old(rec, now):
            rec.update(outcome="stale", message=TOO_OLD)
        elif kind not in KINDS:
            rec.update(outcome="stale", message=f"a phone cannot {kind.replace('_', ' ')} a ticket")
        elif kind == "ticket_request":
            # Signed by a paired phone, core creates the backlog ticket directly (feedback round A); the desktop
            # "Create in backlog" card stays only as the fallback when core answers pending (unpaired, revoked,
            # switched off, malformed).
            result = addon.remote(pctx, d)
            if result.status in _REMOTE_MAP:
                rec.update(outcome=_REMOTE_MAP[result.status], message=str(result.message or ""))
                if result.status == "applied" and getattr(result, "ticket", None):
                    rec["ticket"] = str(result.ticket)
            else:
                rec["message"] = str(result.message or "")
                rec["waiting"] = waiting_code(result)
        else:
            outcome, msg = overtaken(addon, rec, doc, now) if doc is None else _link_check(addon, rec, doc)
            if outcome:
                rec.update(outcome=outcome, message=msg)
            elif kind == "comment":
                pctx.addon.ops().log(doc["id"], f"phone: {' '.join(_str(d.get('value')).split())}")
                rec.update(outcome="applied")
            elif kind == "verdict" and d.get("value") != VERDICTS[d.get("value")]:
                rec["message"] = "an older phone sent this verdict; apply it on the desktop"   # send_back
            else:
                result = addon.remote(pctx, d)
                if result.status in _REMOTE_MAP:
                    rec.update(outcome=_REMOTE_MAP[result.status], message=str(result.message or ""))
                else:
                    rec["message"] = str(result.message or "")          # pending: wait for a desktop Apply
                    rec["waiting"] = waiting_code(result)
        st.put_decision(did, rec)
        known[did] = rec
        log_decision(addon, rec, rec["outcome"], rec["message"])


def _link_check(addon, rec: dict, doc: dict) -> tuple[str | None, str]:
    link = addon.state.links().get(str(doc.get("id")))
    if not link or link.get("retired"):
        return "unlinked", "the ticket is not synced to TIX"
    if rec.get("kind") == "verdict" and (rec.get("decision") or {}).get("value") not in VERDICTS:
        return "stale", "a verdict is done or follow-up"
    if isinstance(rec.get("gen"), int) and rec["gen"] != int(link.get("gen") or 1):
        return "unlinked", "the decision is for an earlier link of this ticket"
    return None, ""


def reconcile(addon, pctx, now) -> list[str]:
    """Pending items that the desktop overtook: answered here, gate changed, ticket gone, too old."""
    changed = []
    for did, rec in addon.state.decisions().items():
        if rec.get("outcome") == "applying":
            at = _at(rec.get("applying_at"))
            if at is None or now - at > APPLYING_TIMEOUT:      # a crash between resolve and the result
                addon.state.update_decision(did, outcome=None, applying_at=None)
                changed.append(did)
            continue
        if rec.get("outcome") is not None:
            continue
        doc = None if rec.get("kind") == "ticket_request" else _doc(pctx, rec.get("ticket"))
        outcome, msg = overtaken(addon, rec, doc, now)
        if outcome:
            addon.state.update_decision(did, outcome=outcome, message=msg)
            log_decision(addon, rec, outcome, msg)
            changed.append(did)
    return changed


def send_acks(addon, pctx) -> int:
    """Ack final outcomes, a few per round. Returns how many went out; a failing ack is marked with its error and
    retried on a later round, and never stops the long poll."""
    sent = 0
    for did, rec in list(addon.state.decisions().items()):
        if sent >= ACK_BATCH:
            break
        if rec.get("outcome") is None and rec.get("waiting") and not rec.get("waiting_acked"):
            # non-final: the phone says why it was not applied (an older CLI or server refuses it: tried once)
            try:
                addon.sharing(pctx).run_json("inbox", "ack", did, rec["waiting"], timeout=6)
            except SharingError:
                pass
            addon.state.update_decision(did, waiting_acked=True)
            sent += 1
            continue
        if rec.get("outcome") in FINAL and not rec.get("acked"):
            try:
                addon.sharing(pctx).run_json("inbox", "ack", did, rec["outcome"], timeout=6)
            except SharingError as e:
                if e.code not in ("conflict", "not_found"):          # already acked (also after unlink): done
                    addon.state.update_decision(did, ack_error=f"{e.code}: {e.detail}"[:300])
                    continue
            addon.state.update_decision(did, acked=True, ack_error=None)
            sent += 1
    return sent


# -- what the desktop shows -------------------------------------------------------------------------------------

def _labels(q: dict | None, value) -> str:
    text = value if isinstance(value, str) else ""
    options = {str(o.get("key")): str(o.get("label")) for o in (q or {}).get("options") or [] if isinstance(o, dict)}
    if options and text:
        parts = [p.strip() for p in text.split(",")]
        if all(p in options for p in parts):
            return ", ".join(options[p] for p in parts)
    return text or "(empty)"


def _card(rec: dict, doc: dict | None) -> tuple[str, str, str | None]:
    kind, d = rec.get("kind"), rec.get("decision") or {}
    target = d.get("target") if isinstance(d.get("target"), dict) else {}
    note = _str(d.get("note"))
    if kind == "answer":
        q = _question(doc or {}, target.get("qid"))
        qid = str(target.get("qid") or "").upper()
        body = _str((q or {}).get("text")) + (f"\nNote: {note}" if note else "")
        return f"Answer from phone: {_labels(q, d.get('value'))}", body, qid if _QID.fullmatch(qid) else None
    gate = target.get("gate") if target.get("gate") in GATES else None
    if kind == "approve":
        return f"Approval from phone · {gate}", note, f"gate:{gate}" if gate else None
    if kind == "request_changes":
        return f"Changes requested from phone · {gate}", _str(d.get("value")) or note, f"gate:{gate}" if gate else None
    if kind == "verdict":
        word = "done" if d.get("value") == "done" else "send back" if d.get("value") in VERDICTS else "unknown"
        return f"Verdict from phone: {word}", note, "verdict"
    value = d.get("value") if isinstance(d.get("value"), dict) else {}
    return "New ticket from phone", _str(value.get("title")), None


def pending_decisions(addon, view) -> list:
    from orch.clock import now as clock_now
    now = clock_now()
    out = []
    for did, rec in addon.state.decisions().items():
        if rec.get("outcome") is not None:
            continue
        request = rec.get("kind") == "ticket_request"
        doc = None if request else _doc(addon.ctx, rec.get("ticket"))
        title, body, anchor = _card(rec, doc)
        stale = overtaken(addon, rec, doc, now)[0] is not None
        together = rec.get("kind") == "approve" and bool(((rec.get("decision") or {}).get("target") or {}).get("plan_hash"))
        if request:
            choices = (("apply", "Create in backlog"), ("ignore", "Ignore"))
        elif together:
            # an Apply here would approve the requirements alone: the pair is approved on the ticket page
            choices = (("ignore", "Ignore"),)
            body = f"{body}\nRequirements and plan together: approve them on the ticket page."
        else:
            choices = (("apply", "Apply"), ("ignore", "Ignore"))
        out.append(PendingDecision(did, title[:200], body[:2000], None if request else rec.get("ticket"), stale,
                                   choices, "info", anchor))
    return out


def _intent(rec: dict) -> Intent:
    kind, d, key = rec.get("kind"), rec.get("decision") or {}, rec.get("ticket")
    target = d.get("target") if isinstance(d.get("target"), dict) else {}
    note = _str(d.get("note"))
    if kind == "answer":
        return Intent("answer", ref=key, qid=str(target.get("qid") or "").upper(), value=_str(d.get("value")),
                      reason=note, expected_hash=target.get("hash"))
    if kind == "approve":
        if target.get("plan_hash"):
            # an Apply here would approve the requirements alone; the pair is approved on the ticket page
            raise ValueError("requirements and plan were approved together on the phone; approve both on the ticket page")
        return Intent("approve", ref=key, gate=target.get("gate"), expected_hash=target.get("hash"))
    if kind == "request_changes":
        return Intent("request_changes", ref=key, gate=target.get("gate"), reason=_str(d.get("value")) or note,
                      expected_hash=target.get("hash"))
    if kind == "verdict":
        if d.get("value") not in VERDICTS:
            raise ValueError("a verdict is done or follow-up")
        return Intent("verdict", ref=key, value=VERDICTS[d["value"]], reason=note, expected_hash=target.get("hash"))
    if kind == "ticket_request":
        value = d.get("value") if isinstance(d.get("value"), dict) else {}
        return Intent("new", value=" ".join(_str(value.get("title"), 200).split()), reason=_str(value.get("body")))
    raise ValueError(f"a {kind} from the phone cannot be applied")


def resolve(addon, did: str, choice: str):
    rec = addon.state.decisions().get(did)
    if rec is None:
        return "This phone decision is no longer here"
    if rec.get("outcome") is not None:
        return f"Already {rec['outcome']}"
    if choice == "ignore":
        addon.state.update_decision(did, outcome="ignored", message="ignored on the desktop")
        log_decision(addon, rec, "ignored", "ignored on the desktop")
        return "Ignored"
    if choice != "apply":
        return None
    try:
        intent = _intent(rec)
    except ValueError as e:
        addon.state.update_decision(did, outcome="stale", message=str(e))
        log_decision(addon, rec, "stale", str(e))
        return str(e)
    addon.state.update_decision(did, outcome="applying", applying_at=datetime.now(timezone.utc).isoformat())
    return intent


def on_intent_result(addon, did: str, outcome: str, message: str) -> None:
    rec = addon.state.decisions().get(did)
    if rec is None:
        return
    if outcome == "applied":
        addon.state.update_decision(did, outcome="applied", message=str(message or "")[:300], applying_at=None)
    else:
        addon.state.update_decision(did, outcome="stale", message=str(message or "")[:300], applying_at=None)
    log_decision(addon, rec, "applied" if outcome == "applied" else "stale", message)


class InboxProvider:
    id, kind, interval_s, always_on, mode = "inbox", "status", 5, True, "long_poll"

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        return ["space"] if self.addon.state.space() and ctx.settings.get("link_mode") != "off" else []

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        try:
            if send_acks(self.addon, ctx):          # acks first; the next long-poll round follows at once
                return self._snapshot(scope, now)
            cursor = self.addon.state.cursor()
            r = self.addon.sharing(ctx).run_json("inbox", "wait", "--after", str(cursor), "--timeout", str(WAIT_S),
                                                 timeout=WAIT_S + 7)
            receive(self.addon, ctx, r.get("decisions") or [], now=now)
            try:
                self.addon.state.set_cursor(max(cursor, int(r.get("cursor") or cursor)))
            except (TypeError, ValueError):
                pass
            reconcile(self.addon, ctx, now)
        except SharingError as e:
            health = "auth_required" if e.code in _AUTH else "offline"
            return Snapshot(self.id, scope, now, health=health, message=e.detail[:200])
        return self._snapshot(scope, now)

    def _snapshot(self, scope, now):
        items = tuple({"id": did, "label": str(rec.get("kind") or ""), "role": "info",
                       "text": str(rec.get("outcome") or "waiting")}
                      for did, rec in self.addon.state.decisions().items() if not rec.get("acked"))
        return Snapshot(self.id, scope, now, items=items[:200])
