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
        env = bytes(c["envelope_zeros"]) if "envelope_zeros" in c else bytes.fromhex(c["envelope"])
        try:
            r = ref.device_check(env, ctx, c["mailbox"], c["now_ms"])
        except Exception:
            fails.append(c["name"]); continue
        if {k: r.get(k) for k in c["expect"]} != c["expect"]:
            fails.append(c["name"])
    by = {d["name"]: d for d in VEC["device_cases"]}
    for c in VEC["pin_runs"]:
        res = []
        for n in c["steps"]:
            d = by[n]
            ctx = {"workspace": VEC["keys"]["workspace"], "k_ws": K, "key_version": 1,
                   "device": VEC["ids"]["device_a"]["device_id"], "host_pub": host,
                   "pending": json.loads(json.dumps(d["pending"])), "offset_ms": d["offset_ms"]}
            res.append(ref.device_check(bytes.fromhex(d["envelope"]), ctx, d["mailbox"], d["now_ms"]))
        if ref.pin_run(res) != c["alarm"]:
            fails.append(c["name"])
    for c in VEC["pending_answers"]:
        ctx = {"workspace": VEC["keys"]["workspace"], "k_ws": K, "key_version": 1,
               "device": VEC["ids"]["device_a"]["device_id"], "host_pin": c["host_pin"],
               "pending": json.loads(json.dumps(c["pending"])), "offset_ms": 0}
        try:
            r = ref.open_pending_answer(bytes.fromhex(c["envelope"]), ctx, c["mailbox"], c["now_ms"])
        except Exception:
            fails.append(c["name"]); continue
        if {k: v for k, v in r.items() if k != "why"} != c["expect"]:
            fails.append(c["name"])
    for c in VEC["labels"]:
        s = "".join(map(chr, c["codepoints"]))
        if ref.device_label_ok(s) is not c["device_accepts"] or [ord(x) for x in ref.host_label(s)] != c["host_stores"]:
            fails.append(c["name"])
    for c in VEC["links"]:
        if ref.parse_pair_fragment(c["fragment"]) != c["parsed"]:
            fails.append(c["name"])
    for c in VEC["challenge_parts"]:
        try:
            n_, e_ = ref.parse_challenge_parts(c["meta"])
            got = {"nonce": n_.hex(), "expires_ms": e_}
        except ValueError:
            got = None
        if got != c["parsed"]:
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
MAL = ('    try:\n        meta, data = unframe(pt, strict=True)\n    except ValueError:\n'
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
    "offer_budget_ignored": ('ob = {"bucket": offer, "limit": OFFER_BUDGET, "host_pub": state["host_pub"]}',
                             'ob = {"host_pub": state["host_pub"]}'),
    # the amendment (#82)
    "pin_failure_for_any_bad_signature": ("            pin_failure = True\n        except InvalidTag:\n            pin_failure = False",
                                          "            pin_failure = True\n        except InvalidTag:\n            pin_failure = True"),
    "pin_failure_never": ("            open_body(ctx[\"k_ws\"], hb, body)\n            pin_failure = True",
                          "            open_body(ctx[\"k_ws\"], hb, body)\n            pin_failure = False"),
    "pin_run_not_reset": ('        elif r["result"] == "accept":\n            run = 0', '        elif False:\n            run = 0'),
    "pin_run_forged_resets": ('        elif r["result"] == "accept":', '        elif r["result"] in ("accept", "drop"):'),
    "pin_alarm_at_two": ("alarm = alarm or run >= PIN_FAILURES", "alarm = alarm or run >= PIN_FAILURES - 1"),
    "pending_pin_not_checked": ('    if not hmac.compare_digest(host_pin(host_pub), bytes.fromhex(ctx["host_pin"])):\n        return _drop("host_pin")\n', ""),
    "pending_signature_not_checked": ('    if not verify(host_pub, sig, signed_bytes(hb, body)):\n        return _drop("host_signature")\n    if h.seq != pend["next"]',
                                      '    if h.seq != pend["next"]'),
    "pending_not_required": ('else meta.get("state") != "pending")', 'else False)'),
    "status_without_scope": ('            if waiting["state"] == "approved":\n                answer["scope"] = waiting["scope"]\n', ""),
    "pending_answer_without_host_pub": ('{"state": "pending", "host_pub": state["host_pub"], ', '{"state": "pending", '),
    "label_in_utf16_units": ("return clean_shown(s)[:MAX_LABEL]",
                             'return clean_shown(s).encode("utf-16-le")[:2 * MAX_LABEL].decode("utf-16-le", "ignore")'),
    "device_label_in_utf16_units": ("and len(s) <= MAX_LABEL", 'and len(s.encode("utf-16-le")) // 2 <= MAX_LABEL'),
    "device_label_81": ("and len(s) <= MAX_LABEL", "and len(s) <= MAX_LABEL + 1"),
    "link_upper_case_hex": (r'([0-9a-f]{32})\.([0-9a-f]{32})', r'([0-9a-fA-F]{32})\.([0-9a-fA-F]{32})'),
    "link_other_version": (r'compile(r"#?v1\.', r'compile(r"#?v\\d+\.'),
    "link_secret_not_canonical": ("b64u(s) != m.group(3) or ", ""),
    "nonce_any_case": ('compile(r"[0-9a-f]{64}")', 'compile(r"[0-9a-fA-F]{64}")'),
    "expires_any_type": ("if type(exp) is not int or exp < 0:", "if exp is None:"),
    "device_direction_unchecked": ("h.version != VERSION or h.direction != TO_DEVICE or h.flags & ~(F_LAST | F_STREAM | F_REFUSAL)",
                                   "h.version != VERSION or h.flags & ~(F_LAST | F_STREAM | F_REFUSAL)"),
    "device_version_unchecked": ("h.magic != MAGIC or h.version != VERSION or h.direction != TO_DEVICE",
                                 "h.magic != MAGIC or h.direction != TO_DEVICE"),
    "device_flags_unchecked": ("h.direction != TO_DEVICE or h.flags & ~(F_LAST | F_STREAM | F_REFUSAL):",
                               "h.direction != TO_DEVICE:"),
    "device_size_unchecked": ("    if not OVERHEAD <= len(env) <= MAX_CHUNK:\n        return _drop(\"size\")\n    hb, body, sig = split(env)\n    h = Header.decode(hb)\n    if h.magic != MAGIC or h.version != VERSION or h.direction != TO_DEVICE or h.flags & ~",
                              "    if not OVERHEAD <= len(env):\n        return _drop(\"size\")\n    hb, body, sig = split(env)\n    h = Header.decode(hb)\n    if h.magic != MAGIC or h.version != VERSION or h.direction != TO_DEVICE or h.flags & ~"),
    "device_key_version_unchecked": ('    if h.key_version != ctx["key_version"] or h.workspace.hex() != ctx["workspace"] or h.device.hex() != ctx["device"]:\n        return _drop("not_for_this_device")\n    pend = ctx["pending"].get(h.rid.hex())\n    if pend is None:\n        return _drop("unknown_request")\n    if (mailbox["id"], mailbox["idx"], mailbox["last"], mailbox["stream"]) != \\',
                                     '    if h.workspace.hex() != ctx["workspace"] or h.device.hex() != ctx["device"]:\n        return _drop("not_for_this_device")\n    pend = ctx["pending"].get(h.rid.hex())\n    if pend is None:\n        return _drop("unknown_request")\n    if (mailbox["id"], mailbox["idx"], mailbox["last"], mailbox["stream"]) != \\'),
    "device_window_strict": ('    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:',
                             '    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) >= WINDOW_MS:'),
    "device_mailbox_stream_unchecked": ('bool(h.flags & F_STREAM)) or pend["stream"] != mailbox["stream"]:\n        return _drop("mailbox_mismatch")\n    if not verify(ctx',
                                        'mailbox["stream"]) or pend["stream"] != mailbox["stream"]:\n        return _drop("mailbox_mismatch")\n    if not verify(ctx'),
    # the amendment review: answers to a pair request
    "pending_window_skipped": ('    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n    pend["next"] += 1\n    return res',
                               '    pend["next"] += 1\n    return res'),
    "pending_order_unchecked": ('    if h.seq != pend["next"]:\n        return _drop("out_of_order")\n    res = {"result": "accept", "host_pub"',
                                '    res = {"result": "accept", "host_pub"'),
    "pending_mailbox_stream_ignored": ('(h.rid.hex(), h.seq, True, False)', '(h.rid.hex(), h.seq, True, mailbox["stream"])'),
    "pair_refusal_without_host_pub": ('"pairing_closed", host_pub=state["host_pub"])', '"pairing_closed")'),
    "pair_refusals_in_offer_without_host_pub": ('"limit": OFFER_BUDGET, "host_pub": state["host_pub"]}', '"limit": OFFER_BUDGET}'),
    "pair_refusal_unverified_accepted": ('    if not verify(host_pub, sig, signed_bytes(hb, body)):\n        return _drop("host_signature")\n    if h.seq != pend["next"]:\n        return _drop("out_of_order")\n    res = {',
                                         '    if not refusal and not verify(host_pub, sig, signed_bytes(hb, body)):\n        return _drop("host_signature")\n    if h.seq != pend["next"]:\n        return _drop("out_of_order")\n    res = {'),
    "pair_refusal_offset_not_adopted": ('                res["offset_ms"] = off\n            else:\n                res["clock_wrong"] = True\n    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n    pend["next"] += 1\n    return res',
                                        '                pass\n            else:\n                res["clock_wrong"] = True\n    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n    pend["next"] += 1\n    return res'),
    "pair_refusal_offset_unbounded": ('            if abs(off) <= MAX_OFFSET_MS:\n                res["offset_ms"] = off\n            else:\n                res["clock_wrong"] = True\n    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n    pend["next"] += 1\n    return res',
                                      '            if True:\n                res["offset_ms"] = off\n            else:\n                res["clock_wrong"] = True\n    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:\n        return _drop("stale_timestamp")\n    pend["next"] += 1\n    return res'),
    "pair_refusal_without_last_accepted": ("h.flags not in (F_LAST, F_LAST | F_REFUSAL)", "h.flags not in (F_LAST, F_REFUSAL, F_LAST | F_REFUSAL)"),
    "pair_refusal_needs_no_code": ('(meta.get("refusal") is None if refusal else', '(False if refusal else'),
    "link_host_pin_not_canonical": ("b64u(pin) != m.group(4)", "False"),
    "meta_duplicates_allowed": ("object_pairs_hook=_no_duplicates, ", ""),
    "meta_nan_allowed": (", parse_constant=_no_constant", ""),
    "meta_upper_case_hex": ('compile(r"(?:[0-9a-f]{2})*")', 'compile(r"(?:[0-9a-fA-F]{2})*")'),
    "meta_hex_any_length": ("len(v) != 2 * n_bytes or ", ""),
    "body_not_stored_replayed": ('            if out.get("body_stored") is False:', '            if False:'),
    "pair_status_named_pair": ('answer = {"state": waiting["state"]}', 'answer = {"pair": waiting["state"]}'),
    "device_host_ms_any_type": ('meta.get("refusal") == "stale_timestamp" and type(meta.get("host_ms")) is int:',
                                'meta.get("refusal") == "stale_timestamp" and meta.get("host_ms") is not None:'),
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
