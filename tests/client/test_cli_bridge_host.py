"""R15b: `sharing bridge-host` relays mailbox operations over a JSON-lines pipe and keeps the device token."""
import io
import json
import os
import queue
import sys
import threading
import time

import pytest

SECRET = "shd_" + "Q" * 40
OWNER_LABEL = "Acme"


@pytest.fixture
def desk(make_device, cli):
    d = make_device("desk", "acme")
    assert cli(d.root, "space", "create", "--label", OWNER_LABEL, "--json").code == 0
    return d


def _ws(d, cli) -> str:
    return cli(d.root, "space", "show", "--json").json()["space_id"]


class _Out:
    """A thread-safe stand-in for stdout that keeps whole lines."""
    def __init__(self):
        self.q, self.raw, self._buf = queue.Queue(), [], ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.raw.append(line)
            self.q.put(json.loads(line))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class Host:
    def __init__(self, sharing, monkeypatch, root, *args):
        self.out, self.err, self.code = _Out(), io.StringIO(), None
        r, self._w = os.pipe()
        self._wf = os.fdopen(self._w, "wb", buffering=0)
        monkeypatch.chdir(root)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(os.fdopen(r, "rb")))
        monkeypatch.setattr(sys, "stdout", self.out)
        monkeypatch.setattr(sys, "stderr", self.err)
        self.t = threading.Thread(target=lambda: setattr(self, "code", sharing.main(["bridge-host", *map(str, args)])),
                                  daemon=True)
        self.t.start()
        self.pending = {}

    def send(self, **cmd):
        self.sendraw((json.dumps(cmd) + "\n").encode())

    def sendraw(self, b: bytes):
        self._wf.write(b)

    def get(self, timeout=20):
        return self.out.q.get(timeout=timeout)

    def answer(self, cid, timeout=20):
        """The answer for cid; other answers that arrive first are kept for later."""
        if cid in self.pending:
            return self.pending.pop(cid)
        end = time.monotonic() + timeout
        while True:
            m = self.get(max(0.1, end - time.monotonic()))
            if m.get("id") == cid:
                return m
            self.pending[m.get("id")] = m

    def call(self, cid="1", **cmd):
        self.send(id=cid, **cmd)
        return self.answer(cid)

    def close(self):
        self._wf.close()
        self.t.join(60)
        assert not self.t.is_alive()
        return self.code


@pytest.fixture
def start(sharing, monkeypatch, desk, cli):
    hosts = []
    ws = _ws(desk, cli)

    def go(*extra, root=None, ready=True):
        h = Host(sharing, monkeypatch, root or desk.root, "--workspace", ws, *extra)
        hosts.append(h)
        if ready:
            assert h.get()["event"] == "ready"
        return h
    go.ws = ws
    yield go
    for h in hosts:
        try:
            h._wf.close()
        except OSError:
            pass
        h.t.join(40)


@pytest.fixture
def client(make_device, sharing, desk):
    """A second approved device acting as a mailbox client (it may post requests and read answers)."""
    other = make_device("phone", "acme")
    return sharing.Api(json.loads(other.config_path.read_text())["server_url"], other.token)


def _post(client, ws, rid, body="QUJD"):
    return client.post_json(f"/api/bridge/{ws}/requests", {"id": rid, "body": body})


def _noise(out: _Out, err, caplog, desk, sharing):
    return "\n".join(out.raw) + err.getvalue() + caplog.text


def _no_secrets(text, desk, sharing):
    mk = json.loads(desk.config_path.read_text())["mk"]
    assert desk.token not in text and SECRET not in text
    assert mk not in text and sharing.unb64u(mk).hex() not in text


# --- the ops against the real server ---------------------------------------------------------------

def test_ready_then_poll_heartbeat_goodbye_release_and_eof(start, desk, sharing, caplog):
    with caplog.at_level("DEBUG"):
        h = start("--holder", "h-one")
        assert h.out.raw[0] == json.dumps({"event": "ready", "holder": "h-one", "lease_s": 40})
        assert h.call("a", op="poll", wait=0) == {"id": "a", "ok": True, "requests": [], "lease_s": 40}
        beat = {"sessions": 2, "in_progress": 1, "needs_you": 0, "factory": "running"}
        assert h.call("b", op="heartbeat", **beat) == {"id": "b", "ok": True}
        assert h.call("c", op="heartbeat", **beat, children_done=1, children_total=3, budget_pct=5)["ok"]
        assert h.call("d", op="goodbye") == {"id": "d", "ok": True}
        assert h.call("e", op="release") == {"id": "e", "ok": True}
        assert h.close() == 0
    _no_secrets(_noise(h.out, h.err, caplog, desk, sharing), desk, sharing)


def test_poll_with_requests_then_respond(start, client, desk):
    h = start()
    rid = "a1" * 16
    assert _post(client, start.ws, rid)["id"] == rid
    r = h.call("p", op="poll", wait=2)
    assert r["requests"] == [{"rid": rid, "body": "QUJD"}] and r["lease_s"] == 40
    assert h.call("r", op="respond", rid=rid, idx=0, last=True, body="REVG") == {"id": "r", "ok": True}
    got = client.get_json(f"/api/bridge/{start.ws}/responses?wait=2")["chunks"]
    assert [(c["id"], c["idx"], c["last"], c["body"]) for c in got] == [(rid, 0, True, "REVG")]
    assert h.close() == 0


def test_a_respond_is_answered_while_a_poll_is_pending(start, client):
    h = start()
    rid = "b2" * 16
    _post(client, start.ws, rid)
    assert h.call("p0", op="poll", wait=2)["requests"]
    h.send(id="slow", op="poll", wait=20)
    time.sleep(0.5)
    t0 = time.monotonic()
    assert h.call("r", op="respond", rid=rid, idx=0, last=True, body="REVG")["ok"]
    assert time.monotonic() - t0 < 5 and "slow" not in h.pending
    _post(client, start.ws, "c3" * 16)           # now the pending poll completes
    assert h.answer("slow")["requests"][0]["rid"] == "c3" * 16
    assert h.close() == 0


def test_pipelined_commands_are_answered_out_of_order(start):
    h = start()
    h.send(id="long", op="poll", wait=4)
    time.sleep(0.3)
    h.send(id="hb", op="heartbeat", sessions=0, in_progress=0, needs_you=0, factory="none")
    h.send(id="bye", op="goodbye")
    first = {h.get()["id"], h.get()["id"]}
    assert first == {"hb", "bye"}
    assert h.answer("long")["requests"] == []
    assert h.close() == 0


def test_take_over_is_sent_on_the_first_poll_only(start, sharing, monkeypatch):
    seen = []
    real = sharing.Api.get_json

    def spy(self, path, headers=None):
        if "/requests?" in path:
            seen.append(path)
        return real(self, path, headers)
    monkeypatch.setattr(sharing.Api, "get_json", spy)
    h = start("--holder", "old")
    assert h.call("1", op="poll", wait=0)["ok"]
    h2 = start("--holder", "new", "--take-over")
    assert h2.call("1", op="poll", wait=0)["ok"] and h2.call("2", op="poll", wait=0)["ok"]
    new = [p for p in seen if "holder=new" in p]
    assert "take_over=1" in new[0] and "take_over" not in new[1]
    assert "take_over" not in seen[0]
    # the replaced host has lost the lease
    assert h.call("3", op="respond", rid="f" * 32, idx=0, last=True, body="AA")["code"] == "lease_lost"
    assert h2.close() == 0 and h.close() == 0


def test_a_second_live_host_without_take_over_gets_host_taken(start):
    h1 = start("--holder", "one")
    assert h1.call("1", op="poll", wait=0)["ok"]
    h2 = start("--holder", "two")
    r = h2.call("1", op="poll", wait=0)
    assert r["ok"] is False and r["code"] == "host_taken" and r["id"] == "1"
    assert h2.call("2", op="heartbeat", sessions=0, in_progress=0, needs_you=0, factory="none")["ok"]  # still alive
    assert h1.close() == 0 and h2.close() == 0


def test_real_server_errors_become_stable_codes(start, client):
    h = start()
    assert h.call("1", op="poll", wait=0)["ok"]
    r = h.call("2", op="respond", rid="9" * 32, idx=0, last=True, body="AA")   # nothing was fetched with that id
    assert (r["ok"], r["code"]) == (False, "bad_request")
    r = h.call("3", op="heartbeat", sessions=-1, in_progress=0, needs_you=0, factory="none")   # the server's schema
    assert r["code"] == "bad_request"
    r = h.call("4", op="heartbeat", sessions=0, in_progress=0, needs_you=0, factory="weird")
    assert r["code"] == "bad_request"
    assert h.close() == 0


def test_stdin_eof_releases_the_lease(start, client, sharing):
    h = start("--holder", "keeper")
    assert h.call("1", op="poll", wait=0)["ok"]
    assert h.close() == 0
    # the lease is gone: another holder polls without take_over and is not refused
    h2 = start("--holder", "next")
    assert h2.call("1", op="poll", wait=0)["ok"]
    assert h2.close() == 0


# --- arguments, malformed input, limits ---------------------------------------------------------------

def test_argument_errors_never_reach_the_network(start, sharing, monkeypatch):
    h = start()
    calls = []
    real = sharing.Api._req
    monkeypatch.setattr(sharing.Api, "_req", lambda self, *a, **k: (calls.append(a), real(self, *a, **k))[1])
    bad = [dict(op="poll", wait=26), dict(op="poll", wait=-1), dict(op="poll", wait="3"), dict(op="poll", wait=True),
           dict(op="respond", rid="xx", idx=0, last=True, body="AA"),
           dict(op="respond", rid="a" * 32, idx=-1, last=True, body="AA"),
           dict(op="respond", rid="a" * 32, idx=0, last="yes", body="AA"),
           dict(op="respond", rid="a" * 32, idx=0, last=True, body="not b64u!"),
           dict(op="respond", rid="a" * 32, idx=0, last=True, body=""),
           dict(op="respond", rid="a" * 32, idx=0, last=True),
           dict(op="heartbeat", sessions=1, evil=1)]
    for i, c in enumerate(bad):
        r = h.call(f"x{i}", **c)
        assert (r["ok"], r["code"]) == (False, "bad_request"), c
    big = "A" * ((256 * 1024 * 4 + 2) // 3 + 1)
    assert h.call("big", op="respond", rid="a" * 32, idx=0, last=True, body=big)["code"] == "too_large"
    assert [c for c in calls if "/api/bridge/" in c[1] or "/api/presence/" in c[1]] == []
    assert h.close() == 0


def test_malformed_and_overlong_lines_get_protocol_errors_and_the_loop_continues(start):
    h = start()
    for raw in (b"not json\n", b"[1,2]\n", b'{"op":"poll"}\n', b'{"id":7,"op":"poll"}\n', b"\xff\xfe\n"):
        h.sendraw(raw)
        m = h.get()
        assert m["ok"] is False and m["code"] == "protocol" and m["id"] is None, raw
    h.send(id="u", op="nope")
    assert h.answer("u")["code"] == "protocol"
    h.sendraw(b'{"id":"big","op":"goodbye","pad":"' + b"x" * 700_000 + b'"}\n')
    m = h.get()
    assert m["code"] == "protocol" and "long" in m["message"]
    assert h.call("ok", op="goodbye")["ok"]               # still alive, and in sync after the long line
    h.sendraw(b"\n\n")                                     # blank lines are ignored
    assert h.call("ok2", op="goodbye")["ok"]
    assert h.close() == 0


def test_a_command_without_the_final_newline_is_still_handled(start):
    h = start()
    h.sendraw(b'{"id":"z","op":"goodbye"}')
    h._wf.close()
    assert h.answer("z")["ok"]
    h.t.join(40)
    assert h.code == 0


def test_in_flight_commands_are_bounded_and_ids_are_unique(start, sharing, monkeypatch):
    gate = threading.Event()
    real = sharing.Api.get_json

    def slow(self, path, headers=None):
        if "/requests?" in path:
            gate.wait(30)
            return {"requests": [], "lease_s": 40}
        return real(self, path, headers)
    monkeypatch.setattr(sharing.Api, "get_json", slow)
    h = start()
    for i in range(sharing.HOST_MAX_INFLIGHT):
        h.send(id=f"p{i}", op="poll", wait=1)
    time.sleep(0.3)
    h.send(id="extra", op="poll", wait=1)
    assert h.answer("extra")["code"] == "rate_limited"
    h.send(id="p0", op="poll", wait=1)
    r = h.get()
    assert r["id"] == "p0" and r["code"] == "bad_request"
    gate.set()
    assert all(h.answer(f"p{i}")["ok"] for i in range(sharing.HOST_MAX_INFLIGHT))
    assert h.close() == 0


# --- every code, through an injected failing transport -----------------------------------------------

CASES = [
    (0, "network", "network"), (0, "bad_response", "network"),
    (500, "http_500", "server"), (502, "http_502", "server"), (302, "redirect", "server"),
    (401, "unauthenticated", "unauthorized"), (401, "revoked", "unauthorized"),
    (403, "pending", "pending"), (403, "not_owner", "not_owner"), (403, "forbidden", "not_owner"),
    (404, "no_space", "no_space"), (404, "unknown_request", "bad_request"),
    (409, "host_taken", "host_taken"), (409, "lease_lost", "lease_lost"), (409, "not_delivered", "bad_request"),
    (413, "too_large", "too_large"), (429, "rate_limited", "rate_limited"), (429, "mailbox_full", "rate_limited"),
    (400, "bad_request", "bad_request"), (418, "teapot", "bad_request"),
]


@pytest.mark.parametrize("status,code,expected", CASES)
def test_every_http_failure_maps_to_a_stable_code_and_never_crashes(start, sharing, monkeypatch, caplog, desk,
                                                                    status, code, expected):
    h = start()

    def boom(self, *a, **k):
        raise sharing.ApiError(status, code, f"detail with {desk.token} and {SECRET}")
    for name in ("get_json", "post_json", "delete"):
        monkeypatch.setattr(sharing.Api, name, boom)
    ops = [dict(op="poll", wait=0), dict(op="respond", rid="a" * 32, idx=0, last=True, body="AA"),
           dict(op="heartbeat", sessions=0, in_progress=0, needs_you=0, factory="none"),
           dict(op="goodbye"), dict(op="release")]
    with caplog.at_level("DEBUG"):
        for i, op in enumerate(ops):
            r = h.call(f"i{i}", **op)
            assert (r["ok"], r["code"]) == (False, expected) and r["message"]
        assert h.close() == 0                           # release at EOF failed too: still exit 0
    _no_secrets(_noise(h.out, h.err, caplog, desk, sharing), desk, sharing)
    assert sharing.host_error_code(sharing.ApiError(status, code)) == expected


def test_an_unexpected_exception_is_an_error_answer_not_a_crash(start, sharing, monkeypatch, desk, caplog):
    h = start()

    def boom(self, *a, **k):
        raise RuntimeError(f"oops {desk.token}")
    monkeypatch.setattr(sharing.Api, "get_json", boom)
    r = h.call("1", op="poll", wait=0)
    assert (r["ok"], r["code"]) == (False, "server") and desk.token not in json.dumps(r)
    assert h.call("2", op="goodbye")["ok"]
    assert h.close() == 0
    _no_secrets(_noise(h.out, h.err, caplog, desk, sharing), desk, sharing)


def test_a_bad_server_answer_shape_is_a_server_error(start, sharing, monkeypatch):
    h = start()
    monkeypatch.setattr(sharing.Api, "get_json", lambda self, p, headers=None: {"requests": "nope"})
    assert h.call("1", op="poll", wait=0)["code"] == "server"
    assert h.close() == 0


# --- refusals, as bridge-key ----------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["_stdin_is_tty", "_stdout_is_tty"])
def test_terminal_refusal_comes_before_config_master_key_and_network(desk, cli, sharing, monkeypatch, which):
    touched = []

    def boom(name):
        def f(*a, **k):
            touched.append(name)
            raise AssertionError(f"{name} reached before the terminal check")
        return f

    monkeypatch.setattr(sharing, which, lambda: True)
    monkeypatch.setattr(sharing, "load_config", boom("load_config"))
    monkeypatch.setattr(sharing, "Api", boom("Api"))
    r = cli(desk.root, "bridge-host", "--workspace", "0" * 32)
    assert r.code == 6 and "terminal" in r.err and r.out == "" and touched == []


def test_unknown_space_not_owner_pending_and_revoked(desk, make_device, cli, sharing):
    ws = _ws(desk, cli)
    r = cli(desk.root, "bridge-host", "--workspace", "0" * 32)
    assert r.code == 2 and r.out == ""
    other = make_device("other", "acme")
    r = cli(other.root, "bridge-host", "--workspace", ws)
    assert r.code == 6 and r.out == "" and "not_owner" not in r.out
    waiting = make_device("waiting", "acme", approve=False)
    assert cli(waiting.root, "bridge-host", "--workspace", ws).code == 3
    data = json.loads(desk.config_path.read_text())
    data["device_token"] = "shd_" + "A" * 40     # the 401 a revoked device gets
    desk.config_path.write_text(json.dumps(data))
    desk.config_path.chmod(0o600)
    r = cli(desk.root, "bridge-host", "--workspace", ws)
    assert r.code == 3 and r.out == ""
    assert sharing.unb64u(data["mk"]).hex() not in r.err


def test_bad_arguments_exit_1(desk, cli):
    assert cli(desk.root, "bridge-host").code == 1
    assert cli(desk.root, "bridge-host", "--workspace", "nope").code == 1
    ws = _ws(desk, cli)
    assert cli(desk.root, "bridge-host", "--workspace", ws, "--holder", "bad holder!").code == 1
    assert cli(desk.root, "bridge-host", "--workspace", ws, "--holder", "x" * 65).code == 1
    assert cli(desk.root, "bridge-host", "--workspace", ws, "--json").code == 1   # not accepted: stdout is the protocol


def test_a_random_holder_is_used_per_process(start):
    a, b = start(), start()
    assert a.out.raw[0] != b.out.raw[0]
    ha, hb = json.loads(a.out.raw[0])["holder"], json.loads(b.out.raw[0])["holder"]
    assert ha != hb and 1 <= len(ha) <= 64 and all(c.isalnum() or c in "_-" for c in ha)
    assert a.close() == 0 and b.close() == 0


# --- review round: unhashable values, redaction before ready, EOF wait, writer lock, closed stdout ------

WEIRD = [[], {}, [[1]], {"a": []}, 5, 1.5, True, False, None, "", ["x"]]


@pytest.mark.parametrize("weird", WEIRD, ids=lambda w: json.dumps(w))
def test_unhashable_and_odd_ops_and_ids_get_protocol_and_the_loop_continues(start, weird):
    h = start()
    h.send(id="a", op=weird)
    m = h.get()
    assert (m["id"], m["ok"], m["code"]) == ("a", False, "protocol")
    h.send(id=weird, op="goodbye")
    m = h.get()
    assert (m["id"], m["ok"], m["code"]) == (None, False, "protocol")
    h.send(op=weird)
    assert h.get()["code"] == "protocol"
    assert h.call("ok", op="goodbye")["ok"]               # a following valid command still works
    assert h.close() == 0 and "unexpected" not in h.err.getvalue()


@pytest.mark.parametrize("weird", [w for w in WEIRD if isinstance(w, (list, dict, float))], ids=lambda w: json.dumps(w))
def test_odd_argument_types_get_bad_request_and_the_loop_continues(start, weird):
    h = start()
    rid = "a" * 32
    cmds = [dict(op="poll", wait=weird), dict(op="respond", rid=weird, idx=0, last=True, body="AA"),
            dict(op="respond", rid=rid, idx=weird, last=True, body="AA"),
            dict(op="respond", rid=rid, idx=0, last=weird, body="AA"),
            dict(op="respond", rid=rid, idx=0, last=True, body=weird),
            dict(op="heartbeat", sessions=weird, in_progress=0, needs_you=0, factory="none"),
            dict(op="heartbeat", sessions=0, in_progress=0, needs_you=0, factory=weird)]
    for i, c in enumerate(cmds):
        r = h.call(f"w{i}", **c)
        assert r["ok"] is False and r["code"] in ("bad_request", "protocol"), (c, r)
    assert h.call("ok", op="goodbye")["ok"]
    assert h.close() == 0 and "unexpected" not in h.err.getvalue()


def test_the_device_token_is_redacted_when_the_failure_comes_before_ready(desk, cli, sharing, monkeypatch, caplog):
    odd = "tok-" + "z9" * 20          # not shd_-shaped: only the exact registration can redact it
    data = json.loads(desk.config_path.read_text())
    data["device_token"] = odd
    desk.config_path.write_text(json.dumps(data))
    desk.config_path.chmod(0o600)

    def boom(self, *a, **k):
        raise sharing.ApiError(500, "http_500", f"the server echoed {odd}")
    monkeypatch.setattr(sharing.Api, "get_json", boom)
    with caplog.at_level("DEBUG"):
        r = cli(desk.root, "bridge-host", "--workspace", "0" * 32)
    assert r.code == 1 and r.out == ""
    assert odd not in r.out + r.err + caplog.text and "ready" not in r.out


def test_stdin_eof_waits_for_the_pending_poll_then_releases_then_exits_0(start, sharing, monkeypatch):
    events = []
    real_get, real_del = sharing.Api.get_json, sharing.Api.delete

    def get(self, path, headers=None):
        out = real_get(self, path, headers)
        if "/requests?" in path:
            events.append("poll_done")
        return out

    def delete(self, path, headers=None):
        events.append("release")
        return real_del(self, path, headers)
    monkeypatch.setattr(sharing.Api, "get_json", get)
    monkeypatch.setattr(sharing.Api, "delete", delete)
    h = start()
    h.send(id="p", op="poll", wait=3)
    time.sleep(0.5)
    h._wf.close()
    time.sleep(1)
    assert h.t.is_alive() and events == []                 # still waiting for the pending call
    assert h.answer("p")["ok"]
    h.t.join(40)
    assert h.code == 0 and events == ["poll_done", "release"]


def test_the_stdout_writer_is_serialised(sharing):
    class Slow:
        """A write that is not atomic: two halves with a yield between them."""
        def __init__(self):
            self.parts = []

        def write(self, s):
            h = len(s) // 2
            self.parts.append(s[:h])
            time.sleep(0.0005)
            self.parts.append(s[h:])

        def flush(self):
            pass
    out = Slow()
    host = sharing.BridgeHost(None, "0" * 32, "h", False, None, out)
    ts = [threading.Thread(target=lambda n=n: [host.emit({"id": f"{n}-{i}", "ok": True, "pad": "x" * 40})
                                                for i in range(40)]) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = "".join(out.parts).split("\n")
    assert lines.pop() == "" and len(lines) == 320
    assert len({json.loads(line)["id"] for line in lines}) == 320   # every line is one whole object
    # ponytail: provoked by a deliberately split write; with the lock removed this fails on every run here.


def test_the_loop_stops_when_stdout_is_closed(start):
    h = start()

    def broken(s):
        raise BrokenPipeError()
    h.out.write = broken
    h.send(id="1", op="goodbye")        # its answer cannot be written: the writer notes that nobody listens
    time.sleep(1)
    h.send(id="2", op="goodbye")        # the next line the loop reads ends it, without stdin reaching EOF
    h.t.join(40)
    assert not h.t.is_alive() and h.code == 0
