"""docs/bridge-protocol.md §12: the vectors must catch every mutation of the reference below (the first
review's 16, plus one per rule added since). A mutation that no vector catches means a MUST is unpinned:
add a negative vector, do not delete the mutation."""
import json
import types

import pytest

from tests.support import bridge_protocol_ref as ref_mod

SRC = open(ref_mod.__file__, encoding="utf-8").read()
VEC = json.loads(ref_mod.VECTORS.read_text(encoding="utf-8"))


def load(src, name):
    m = types.ModuleType(name)
    m.__file__ = ref_mod.__file__
    exec(compile(src, name, "exec"), m.__dict__)
    return m


def run(ref):
    fails = []
    K = bytes.fromhex(VEC["hkdf"][0]["okm"])
    host = bytes.fromhex(VEC["keys"]["host"]["pub"])
    for c in VEC["host_cases"]:
        s = json.loads(json.dumps(c["state"])); s["k_ws"] = K
        for st in c.get("steps", [c]):
            env = bytes(st["envelope_zeros"]) if "envelope_zeros" in st else bytes.fromhex(st["envelope"])
            try:
                r = ref.host_check(env, s, st["now_ms"], st["mailbox_id"])
            except Exception:
                fails.append(c["name"]); break
            rec = s["rids"].get(st["mailbox_id"])
            if {k: v for k, v in r.items() if k in st["expect"]} != st["expect"] or r["result"] != st["expect"]["result"] \
                    or (rec["until"] if rec else None) != st["record_until"]:
                fails.append(c["name"]); break
    for c in VEC["device_cases"]:
        ctx = {"workspace": VEC["keys"]["workspace"], "k_ws": K, "key_version": 1,
               "device": VEC["ids"]["device_a"]["device_id"], "host_pub": host,
               "pending": json.loads(json.dumps(c["pending"])), "offset_ms": c["offset_ms"]}
        r = ref.device_check(bytes.fromhex(c["envelope"]), ctx, c["mailbox"], c["now_ms"])
        if {k: r.get(k) for k in c["expect"]} != c["expect"]:
            fails.append(c["name"])
    for c in VEC["sign"]:
        if ref.verify(bytes.fromhex(c["pub"]), bytes.fromhex(c["sig"]), bytes.fromhex(c["msg"])) is not c["valid"]:
            fails.append(c["name"])
    for c in VEC["sig_scalars"]:
        if ref.scalars_in_range(bytes.fromhex(c["sig"])) is not c["in_range"]:
            fails.append(c["name"])
    for c in VEC["assertion"]["cases"]:
        cred = dict(c["credential"])
        if ref.verify_assertion(cred, json.loads(json.dumps(c["pending"])), c["sender"], c["assertion"],
                                c["now_ms"]) != c["expect"] or cred["sign_count"] != c["sign_count_after"]:
            fails.append(c["name"])
    for c in VEC["shown"]:
        try:
            got = ref.clean_shown("".join(map(chr, c["input"])))
        except ValueError:
            got = None
        if got != c["expect"]:
            fails.append(c["name"])
    return fails


SEQ = ('    if not seq_accept(dev, h.seq):\n'
       '        return _recorded_refusal(state, h, env, now_ms, "stale_sequence", high=dev["high"])\n')
TIME = ('    if abs(now_ms - h.ts_ms) > WINDOW_MS:\n'
        '        return _recorded_refusal(state, h, env, now_ms, "stale_timestamp", host_ms=now_ms)\n')

SEQ_NOTE = '    # sequence BEFORE time: a stale_timestamp refusal consumes its seq, so the same bytes can never run\n'
MAL = ('    try:\n        meta, data = unframe(pt)\n    except ValueError:\n'
       '        return _recorded_refusal(state, h, env, now_ms, "malformed")\n')

MUTS = {
    # the reviewer's 16
    "seq0_allowed": ("if seq < 1 or i >= SEQ_WINDOW", "if i >= SEQ_WINDOW"),
    "no_r_upper_bound": ("0 < r < P256_N and 0 < s < P256_N", "0 < r and 0 < s"),
    "no_crossOrigin_check": (' or client.get("crossOrigin")', ""),
    "no_issued_device_check": ('issued["device"] != sender_device or ', ""),
    "no_meta_len_bound": ("n > MAX_META or ", ""),
    "no_UV_only_UP": ("ad[32] & (AD_UP | AD_UV) != AD_UP | AD_UV", "ad[32] & AD_UP != AD_UP"),
    "no_rpid_check": ('or ad[:32] != hashlib.sha256(cred["rp_id"].encode()).digest()', ""),
    "no_type_check": ('client.get("type") != "webauthn.get" or ', ""),
    "pair_ts_unchecked": ('    if abs(now_ms - h.ts_ms) > WINDOW_MS:\n        return _unverified(state, now_ms, "stale_timestamp", host_ms=now_ms, **ob)\n    if resend:',
                          '    if resend:'),
    "no_pair_devid_bind": ("device_id(h.workspace, pub) != h.device or ", ""),
    "window_strict_lt": ('if abs(now_ms - h.ts_ms) > WINDOW_MS:\n        return _recorded_refusal',
                         'if abs(now_ms - h.ts_ms) >= WINDOW_MS:\n        return _recorded_refusal'),
    "replay_ignores_device": ('known["device"] == did and ', ""),
    "device_skips_ts": ('    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n', ""),
    "device_no_rid_pending": ('    if pend is None:\n        return _drop("unknown_request")\n',
                              '    if pend is None:\n        pend = {"next": 0, "stream": False}\n'),
    "count_lt_instead_of_le": ('count <= cred["sign_count"]', 'count < cred["sign_count"]'),
    "seq_bitmap_off_by_one": ("((bm << shift) | 1)", "((bm << (shift-1)) | 1)"),
    # the review's new rules
    "stale_ts_not_recorded": ('return _recorded_refusal(state, h, env, now_ms, "stale_timestamp"', 'return _refuse("stale_timestamp"'),
    "stale_seq_not_recorded": ('return _recorded_refusal(state, h, env, now_ms, "stale_sequence"', 'return _refuse("stale_sequence"'),
    "no_stream_ownership": ('if h.stream != ZERO_ID and streams.get(h.stream.hex()) != did:', "if False:"),
    "no_mailbox_id_check": ("if mailbox_id is not None and mailbox_id != h.rid.hex():", "if False:"),
    "no_budget": ("if len(recent) >= limit:", "if False:"),
    "budget_also_on_signed": ('    # 4. a known request id', '    if len([t for t in state.get("unverified", []) if now_ms - t < BUDGET_WINDOW_MS]) >= BUDGET:\n        return _drop("budget")\n    # 4. a known request id'),
    "skewed_refusal_dropped": ('if h.flags & F_REFUSAL and meta.get("refusal") == "stale_timestamp"', "if False and h.flags"),
    "no_offset_used": ('now_ms + ctx.get("offset_ms", 0)', "now_ms"),
    "no_credential_id_check": ('a.get("credential_id") != cred["credential_id"]', "False"),
    "no_be_check": ('bool(ad[32] & AD_BE) != cred["be"]', "False"),
    "be_counter_enforced": ('        if not cred["be"]:\n            return _refuse', '        if True:\n            return _refuse'),
    "no_phone_proof": ('if hmac.compare_digest(phone_link_proof(bytes.fromhex(phone_key), h.device), proof):', "if True:"),
    "shown_keeps_default_ignorables": (" or any(a <= o <= b for a, b in _IGNORABLE)", ""),
    "shown_keeps_format_cf": ('_REMOVED_CATEGORIES = {"Cc", "Cf", ', '_REMOVED_CATEGORIES = {"Cc", '),
    "shown_keeps_separators": ('"Zl", "Zp", ', ""),
    "shown_keeps_private_use": ('"Co", "Cn"}', '"Cn"}'),
    "shown_keeps_unassigned": ('"Co", "Cn"}', '"Co"}'),
    "shown_allows_surrogates": ("if any(0xD800 <= ord(c) <= 0xDFFF for c in s):", "if False:"),
    "device_id_without_workspace": ("L_DEVICE + workspace + pub", "L_DEVICE + pub"),
    "crash_replay_runs": ('return _refuse("already_done", status="unknown")', 'return {"result": "accept"}'),
    # the second review's 12
    "retention_follows_senders_ts": ('"until": now_ms + RID_RETENTION_MS}',
                                     '"until": max(now_ms + RID_RETENTION_MS, h.ts_ms + 360_000)}'),
    "budget_per_device_not_global": ('holder = state if bucket is None else bucket',
                                     'holder = state.setdefault("per_code", {}).setdefault(code, {}) if bucket is None else bucket'),
    "budget_stops_counting_pairing_closed": ('if now_ms - t < BUDGET_WINDOW_MS]',
                                             'if now_ms - t < BUDGET_WINDOW_MS and code != "pairing_closed"]'),
    "offset_sign_flipped": ('off = meta["host_ms"] - now_ms', 'off = now_ms - meta["host_ms"]'),
    "stale_ts_exemption_any_refusal": ('meta.get("refusal") == "stale_timestamp" and type', 'type'),
    "c1_not_removed": ("unicodedata.category(c) in _REMOVED_CATEGORIES",
                       "(unicodedata.category(c) in _REMOVED_CATEGORIES and not 0x80 <= o <= 0x9F)"),
    "lf_removed_too": ('    if c == "\\n":\n        return False\n', ""),
    "phone_proof_without_device_bind": ("phone_link_proof(bytes.fromhex(phone_key), h.device)",
                                        "phone_link_proof(bytes.fromhex(phone_key), h.device[:8] + bytes(8))"),
    "be_advisory_updates_count": ('return {"result": "verified", "counter_warning": True}',
                                  'cred["sign_count"] = count; return {"result": "verified", "counter_warning": True}'),
    "stream_open_not_owned": ("        streams[rid] = did\n", "        streams[rid] = 'x'\n"),
    "rid_conflict_ignores_digest": ('known["device"] == did and known["digest"] == digest(env).hex()', 'known["device"] == did'),
    "unknown_outcome_becomes_replay": ('return _refuse("already_done", status="unknown")',
                                       'return {"result": "replay", "outcome": None}'),
    # this round's rules
    "time_before_sequence": (SEQ + TIME, TIME + SEQ),
    "replay_host_ms_not_restamped": ('                    extra["host_ms"] = now_ms', '                    pass'),
    "replay_high_not_restamped": ('                    extra["high"] = dev["high"]', '                    pass'),
    "offset_adopted_every_time": ('if not pend.get("offset_adopted"):', "if True:"),
    "offset_unbounded": ("if abs(off) <= MAX_OFFSET_MS:", "if True:"),
    "offer_budget_ignored": ('ob = {"bucket": offer, "limit": OFFER_BUDGET}', "ob = {}"),
    # the third review's 4 boundaries, and the pairing resend
    "offset_bound_strict_lt": ("if abs(off) <= MAX_OFFSET_MS:", "if abs(off) < MAX_OFFSET_MS:"),
    "keep_cr": ('    if c == "\\n":', '    if c in "\\n\\r":'),
    "budget_window_le": ("if now_ms - t < BUDGET_WINDOW_MS]", "if now_ms - t <= BUDGET_WINDOW_MS]"),
    "malformed_after_seq": (MAL + SEQ_NOTE + SEQ, SEQ_NOTE + SEQ + MAL),
    "no_pair_resend": ('held.get("pairing_id") == pid and held.get("pub") == pub.hex()', "False"),
    "pair_resend_for_any_key": ('held.get("pairing_id") == pid and held.get("pub") == pub.hex()', "True"),
    "pair_resend_ignores_key": ('held.get("pairing_id") == pid and held.get("pub") == pub.hex()',
                                'held.get("pairing_id") == pid or bool(state.get("pending_pairs"))'),
}


def test_unmutated_reference_passes_every_vector():
    assert run(load(SRC, "base")) == []


@pytest.mark.parametrize("name", list(MUTS))
def test_mutation_is_caught(name):
    old, new = MUTS[name]
    assert SRC.count(old) >= 1, f"mutation {name} no longer applies: update its anchor text"
    assert run(load(SRC.replace(old, new, 1), name)), f"no vector catches mutation {name}"
