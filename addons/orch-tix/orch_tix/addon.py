"""The orch-tix addon object (spec §3.5): mirror tickets up, bring phone decisions back."""
from __future__ import annotations

from orch.addons.api import PairingTarget
from orch.errors import ValidationError

from . import files, inbox, sync, widgets
from .cli import Sharing, SharingError
from .providers import DevicesProvider, FilesProvider, HealthProvider, MessagesProvider
from .state import State

MAX_ERRORS = 20
LATER = "not available yet"
TICKET_ACTIONS = ("link", "unlink", "push_now", "redaction", "send_artifact")
FILE_ACTIONS = ("download", "save_to_ticket", "upload", "public_link", "upload_link", "ack_message")


class _Pending:
    status = "pending"

    def __init__(self, message: str):
        self.message = message


class TixAddon:
    def __init__(self, ctx):
        self.ctx = ctx
        self.state = State(ctx.state_dir)
        self.errors: list[str] = []
        self.providers = [inbox.InboxProvider(self), HealthProvider(self), DevicesProvider(self),
                          MessagesProvider(self), FilesProvider(self)]

    # -- plumbing --------------------------------------------------------------------------------------------
    def sharing(self, pctx) -> Sharing:
        return Sharing(pctx)

    def note_error(self, text: str) -> None:
        self.errors.append(str(text)[:300])
        del self.errors[:-MAX_ERRORS]

    # -- events ----------------------------------------------------------------------------------------------
    def on_event(self, event, outbox) -> None:
        sync.on_event(event, outbox)

    def drain(self, ctx, items) -> list[str]:
        return sync.drain(self, ctx, items)

    # -- widgets ---------------------------------------------------------------------------------------------
    def widgets(self, slot, view) -> list:
        if slot == "workspace.settings":
            return widgets.workspace_section(self, view)
        if slot == "ticket.sync":
            return widgets.ticket_panel(self, view)
        if slot == "today.summary":
            return widgets.summary_tile(self, view)
        if slot == f"page.{self.ctx.name}":
            return widgets.page(self, view)
        return []

    # -- decisions from the phone ----------------------------------------------------------------------------
    def remote(self, pctx, decision: dict):
        """The one seam to core's verify-and-apply. Tests set ctx.remote_hook to an orch.testing.FakeRemote;
        nothing in production sets it, and it never bypasses core."""
        hook = getattr(self.ctx, "remote_hook", None)
        try:
            return hook(decision) if hook is not None else pctx.remote_decision(decision)
        except Exception as e:                  # never lose the item: it waits for a desktop Apply
            return _Pending(f"could not hand the decision to orch: {type(e).__name__}")

    def inbox_receive(self, pctx, items, now=None) -> None:
        now = now or pctx.now()
        inbox.receive(self, pctx, items, now=now)
        inbox.reconcile(self, pctx, now)

    def decisions(self, view) -> list:
        return inbox.pending_decisions(self, view)

    def resolve(self, decision_id, choice, ctx):
        return inbox.resolve(self, decision_id, choice)

    def on_intent_result(self, decision_id, outcome, message) -> None:
        inbox.on_intent_result(self, decision_id, outcome, message)

    def pairing_target(self, view):
        space = self.state.space() or {}
        server, space_id = str(space.get("server") or "").rstrip("/"), str(space.get("space_id") or "")
        if not server.startswith("https://") or not space_id:
            return None
        try:
            return PairingTarget(f"{server}/pair#{space_id}", "TIX")
        except ValueError:
            return None

    # -- actions ---------------------------------------------------------------------------------------------
    def _ticket(self, ctx, ref: str) -> str:
        return str(ctx.document(ref)["id"])

    def _push(self, ctx, key: str) -> str:
        status = sync.push(self, ctx, key, ctx.document(key))
        link = self.state.links().get(key) or {}
        if status == "gone":
            return f"{key} is no longer on the phone; use Sync to TIX to link it again"
        return f"Synced to TIX as {link.get('n')}" if link.get("n") else f"Synced {key} to TIX"

    def act(self, action_id, target, ctx, upload=None):
        if action_id not in TICKET_ACTIONS and action_id not in FILE_ACTIONS:
            return LATER
        if not self.sharing(ctx).configured:
            return "Set the sharing CLI path in Workspace & addons → TIX first."
        try:
            if action_id in FILE_ACTIONS:
                return self._file_act(action_id, str(target or ""), ctx, upload)
            return self._act(action_id, str(target or ""), ctx)
        except SharingError as e:
            # the CLI's detail never carries a link URL or a key (it redacts them); the target is a FILE id,
            # a message id or a ticket key
            self.note_error(f"{action_id} {str(target)[:80]}: {e.detail}")
            raise ValidationError(f"TIX: {e.detail}") from None

    def _file_act(self, action_id: str, target: str, ctx, upload):
        if action_id == "download":
            return files.download(self, ctx, target)
        if action_id == "save_to_ticket":
            return files.save_to_ticket(self, ctx, target)
        if action_id == "upload":
            return files.upload(self, ctx, upload)
        if action_id == "public_link":
            return files.public_link(self, ctx, target)
        if action_id == "upload_link":
            return files.upload_link(self, ctx)
        return files.ack_message(self, ctx, target)

    def _act(self, action_id: str, target: str, ctx):
        if action_id == "push_now" and target == "":
            keys = list(self.state.active_links())
            for key in keys:
                self._push(ctx, key)
            return f"Synced {len(keys)} ticket(s) to TIX"
        if action_id == "send_artifact":
            ref, sep, name = target.partition("|")
            if not sep or not name:
                raise ValidationError("send_artifact needs KEY|name")
            key = self._ticket(ctx, ref)
            if sync.level_of(self._require_link(key), ctx.settings) == "key-only":
                raise ValidationError(f"{key} shows only its key on the phone (Key only), so no files are sent")
            file_id = sync.share_artifact(self, ctx, key, name)
            self._push(ctx, key)
            return f"Sent {name} to the phone as {file_id}"
        key = self._ticket(ctx, target)
        if action_id == "link":
            self.state.link(key, by="you", auto=False)
            return self._push(ctx, key)
        link = self._require_link(key)
        if action_id == "unlink":
            self.sharing(ctx).run_json("mirror", "unlink", "--key", key, "--gen", str(int(link.get("gen") or 1)))
            self.state.unlink(key, by_hand=True)
            return f"Stopped syncing {key}"
        if action_id == "redaction":
            self.state.set_redaction(key, None if link.get("redaction") == "full" else "full")
            return self._push(ctx, key)
        return self._push(ctx, key)            # push_now for one ticket

    def _require_link(self, key: str) -> dict:
        link = self.state.links().get(key)
        if not link or link.get("retired"):
            raise ValidationError(f"{key} is not synced to TIX")
        return link
