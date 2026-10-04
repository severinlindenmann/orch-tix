"""Regenerate tests/vectors/shr1.json:  uv run python tests/vectors/make_vectors.py"""
import base64
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = HERE / "shr1.json"
OUT_ULINK = HERE / "ulink1.json"
OUT_MIRROR = HERE / "mirror1.json"


def load_sharing():
    if "sharing" in sys.modules:
        return sys.modules["sharing"]
    spec = importlib.util.spec_from_file_location("sharing", REPO / "skill" / "sharing" / "sharing.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sharing"] = mod
    spec.loader.exec_module(mod)
    return mod


def pt_of(n):
    return bytes(i % 251 for i in range(n))


def seq(start, n):
    return bytes((start + i) % 256 for i in range(n))


def build():
    s = load_sharing()
    v = {"version": 1, "envelope": [], "kdf": [], "blob": [], "file_meta": [], "approve": [], "recovery": [],
         "settings": [], "link": [], "tickets": []}

    for name, key, nonce, aad, pt in [
        ("empty", seq(0, 32), seq(100, 12), s.AAD_MK, b""),
        ("mk-wrap", seq(0, 32), seq(100, 12), s.AAD_MK, seq(200, 32)),
        ("utf8", seq(32, 32), seq(7, 12), b"sharing/meta/v1|" + seq(9, 16), "Grüße ✓".encode("utf-8")),
    ]:
        v["envelope"].append({"name": name, "key": key.hex(), "nonce": nonce.hex(), "aad": aad.hex(),
                              "pt": pt.hex(), "env": s.seal(key, pt, aad, nonce=nonce).hex()})

    for name, pw in [
        ("ascii", "correct horse battery staple"),
        ("nfc", "p\u00e4ssw\u00f6rt \u00fc"),
        ("nfd-input", "pa\u0308sswo\u0308rt u\u0308"),  # NFD via escapes
    ]:
        auth, kek = s.derive(pw, seq(0, 16), 1000)
        v["kdf"].append({"name": name, "passphrase": pw, "salt": seq(0, 16).hex(), "iterations": 1000,
                         "auth_key": auth.hex(), "kek": kek.hex()})

    dek, uuid, prefix = seq(50, 32), seq(150, 16), seq(250, 8)
    for cs, n in [(16, 0), (16, 1), (16, 16), (16, 17), (16, 32), (16, 40),
                  (s.CHUNK, 0), (s.CHUNK, 1), (s.CHUNK, s.CHUNK), (s.CHUNK, s.CHUNK + 1)]:
        out = io.BytesIO()
        s.encrypt_stream(dek, uuid, 1, io.BytesIO(pt_of(n)), out, chunk_size=cs, nonce_prefix=prefix)
        blob = out.getvalue()
        case = {"name": f"cs{cs}-n{n}", "dek": dek.hex(), "uuid": uuid.hex(), "key_version": 1,
                "chunk_size": cs, "nonce_prefix": prefix.hex(), "pt_len": n,
                "blob_len": len(blob), "blob_sha256": hashlib.sha256(blob).hexdigest()}
        if len(blob) <= 256:
            case["blob"] = blob.hex()
        v["blob"].append(case)

    stream = bytes((i * 7 + 3) % 256 for i in range(72))
    pos = [0]

    def rng(n):
        out = stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    mk = seq(10, 32)
    meta = {"name": "deploy-notes.md", "mime": "text/markdown", "note": "für FILE7"}
    fc = s.new_file_crypto(mk, meta["name"], meta["mime"], meta["note"], _rng=rng)
    v["file_meta"].append({"name": "basic", "mk": mk.hex(), "rng": stream.hex(), "meta": meta,
                           "uuid": fc["uuid_hex"], "dek": fc["dek"].hex(),
                           "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"]})

    # public link (§17): the same file's DEK wrapped under a fixed link key and nonce; the holder
    # opens the file's existing enc_meta with it
    lk, nonce = seq(180, 32), seq(60, 12)
    v["link"].append({"name": "basic", "lk": lk.hex(), "lk_b64u": s.b64u(lk), "nonce": nonce.hex(),
                      "uuid": fc["uuid_hex"], "dek": fc["dek"].hex(), "aad": s.aad_link(fc["uuid"]).hex(),
                      "wrapped_dek_link": s.wrap_dek_for_link(fc["dek"], fc["uuid"], lk, _nonce=nonce),
                      "enc_meta": fc["enc_meta"], "meta": meta})

    # device approval (§4.6): both private scalars are fixed so every derived byte is pinned
    from cryptography.hazmat.primitives.asymmetric import ec
    dev_int = int.from_bytes(seq(100, 32), "big")      # 0x6465… < curve order n
    eph_int = int.from_bytes(seq(140, 32), "big")      # 0x8c8d… < n
    device_id, nonce = "dev_0123456789ab", seq(33, 12)
    dev_der, dev_pub = s.new_device_keypair(_priv_int=dev_int)
    eph_der, eph_pub = s.new_device_keypair(_priv_int=eph_int)
    z = ec.derive_private_key(eph_int, ec.SECP256R1()).exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), dev_pub))
    wk = s.approval_key(z, eph_pub, dev_pub, device_id)
    bundle = s.seal_to_device(mk, dev_pub, device_id, _eph_priv_int=eph_int, nonce=nonce)
    assert bundle[:65] == eph_pub
    v["approve"].append({"name": "basic", "device_id": device_id, "mk": mk.hex(), "nonce": nonce.hex(),
                         "device_priv_int": format(dev_int, "064x"), "device_priv_pkcs8": s.b64u(dev_der),
                         "eph_priv_int": format(eph_int, "064x"), "eph_priv_pkcs8": s.b64u(eph_der),
                         "device_pub": dev_pub.hex(), "eph_pub": eph_pub.hex(),
                         "fingerprint": s.fingerprint(dev_pub), "z": z.hex(), "wk": wk.hex(),
                         "bundle": bundle.hex(), "code_lookup": seq(1, 16).hex(),
                         "code": f"shr1.{s.b64u(seq(1, 16))}"})

    # encrypted settings (§15). The input is a raw JSON string with its keys deliberately out of order
    # (the vector file itself is written with sort_keys, so an object here would arrive pre-sorted).
    # U+1F600 before U+FB01 checks code-point order: UTF-16 order would sort them the other way.
    settings_in = ('{"zeta":"Grüße ✓","\U0001F600":1,"\uFB01":2,"deepgram_api_key":"dg-vector-not-a-real-key",'
                   '"alpha":{"b":2,"a":[1,"x",{"z":0,"y":1}]}}')
    settings = json.loads(settings_in)
    assert list(settings) != sorted(settings)
    nonce = seq(77, 12)
    v["settings"].append({"name": "basic", "mk": mk.hex(), "nonce": nonce.hex(), "input_json": settings_in,
                          "json": s.canonical_json(settings).decode("utf-8"),
                          "enc_settings": s.seal_settings(mk, settings, _nonce=nonce)})

    # meta re-seal (§14 G, §20): the browser adds a transcript to the whole decrypted meta, keeping
    # keys it doesn't know, and seals it again under the same DEK. Raw JSON input, keys out of order.
    reseal_in = ('{"note":"für FILE7","x_future":{"b":[2,{"z":0,"a":1}],"a":"✓"},"name":"memo.webm",'
                 '"transcript":{"text":"Grüezi\\nmiteinand","model":"nova-3","language":"de-CH",'
                 '"created_at":"2026-09-25T12:00:00Z","by":"Chrome on Mac"},"mime":"audio/webm"}')
    reseal = json.loads(reseal_in)
    assert list(reseal) != sorted(reseal)
    nonce = seq(90, 12)
    v["meta_reseal"] = [{"name": "transcript", "dek": fc["dek"].hex(), "uuid": fc["uuid_hex"], "nonce": nonce.hex(),
                         "input_json": reseal_in, "json": s.canonical_json(reseal).decode("utf-8"),
                         "enc_meta": s.seal_file_meta(fc["dek"], fc["uuid"], reseal, _nonce=nonce)}]

    # tickets (spec T3, T5): a fixed ticket uuid and event uuid, with new_ticket_crypto's rng stream
    # pinned so it yields that ticket uuid, a fixed DEK and two fixed envelope nonces, in that order.
    ticket_uuid = bytes.fromhex("00112233445566778899aabbccddeeff")
    event_uuid = bytes.fromhex("ffeeddccbbaa99887766554433221100")
    ticket_dek, wrap_nonce, content_nonce = seq(60, 32), seq(210, 12), seq(220, 12)
    ticket_stream = ticket_uuid + ticket_dek + wrap_nonce + content_nonce
    tpos = [0]

    def trng(n):
        out = ticket_stream[tpos[0]:tpos[0] + n]
        tpos[0] += n
        return out

    ticket_mk = seq(20, 32)
    content = {"title": "Tag suggestions", "body": "x",
               "fm": {"testing": {"mode": "auto", "run": ["pytest -q"], "then": "ask"}}}
    tc = s.new_ticket_crypto(ticket_mk, content, _rng=trng)
    event_nonce = seq(230, 12)
    body = {"text": "hi"}
    enc_body = s.seal_ticket_event(tc["_dek"], ticket_uuid, event_uuid, body, _nonce=event_nonce)
    v["tickets"].append({"name": "basic", "mk": ticket_mk.hex(), "rng": ticket_stream.hex(),
                         "content": content, "uuid": tc["uuid"], "key_version": tc["key_version"],
                         "dek": ticket_dek.hex(), "wrapped_dek": tc["wrapped_dek"],
                         "enc_content": tc["enc_content"], "event_uuid": event_uuid.hex(),
                         "event_nonce": event_nonce.hex(), "body": body, "enc_body": enc_body})

    for mkr in [mk, bytes(32), b"\xff" * 32]:
        raw = mkr + hashlib.sha256(mkr).digest()[:2]
        b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
        assert len(b32) == 55
        v["recovery"].append({"mk": mkr.hex(),
                              "recovery": "shrk-" + "-".join(b32[i:i + 5] for i in range(0, 55, 5))})
    return v


def build_ulink():
    """tests/vectors/ulink1.json (Task 1, upload-link crypto §18): every input is fixed, so both the
    Python and JS conformance tests reproduce the same bytes, and the JS test opens what Python made."""
    s = load_sharing()

    mk = seq(300, 32)
    link_priv_int = int.from_bytes(seq(310, 32), "big")
    link_uuid_seed, label_nonce_seed = seq(320, 16), seq(340, 12)
    rng_stream = link_uuid_seed + label_nonce_seed
    pos = [0]

    def ulink_rng(n):
        out = rng_stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    wrapped_nonce = seq(360, 12)
    label = "logs from Jonas"
    file_uuid = seq(370, 16)
    dek = seq(390, 32)
    eph_priv_int = int.from_bytes(seq(410, 32), "big")
    sealed_nonce = seq(430, 12)
    wrapped_dek_nonce = seq(450, 12)

    ulc = s.new_upload_link_crypto(mk, label, _priv_int=link_priv_int, _rng=ulink_rng, _nonce=wrapped_nonce)
    assert ulc["uuid"] == link_uuid_seed

    priv, pub = s.open_upload_link_key(mk, ulc["uuid"], ulc["wrapped_lpriv"])
    assert pub == ulc["pub"]
    assert s.open_upload_label(mk, ulc["uuid"], ulc["enc_label"]) == label

    eph_der, eph_pub = s.new_device_keypair(_priv_int=eph_priv_int)
    sealed_dek = s.seal_dek_to_link(dek, ulc["pub"], ulc["uuid"], file_uuid,
                                    _eph_priv_int=eph_priv_int, _nonce=sealed_nonce)
    assert s.open_sealed_dek(priv, ulc["pub"], ulc["uuid"], file_uuid, sealed_dek) == dek

    wrapped_dek = s.wrap_dek_under_mk(mk, dek, file_uuid, _nonce=wrapped_dek_nonce)

    return {"version": 1, "basic": [{
        "name": "basic",
        "mk": mk.hex(),
        "link_priv_int": format(link_priv_int, "064x"),
        "link_priv_jwk_d": s.b64u(link_priv_int.to_bytes(32, "big")),
        "rng": rng_stream.hex(),
        "wrapped_nonce": wrapped_nonce.hex(),
        "label": label,
        "link_uuid": ulc["uuid_hex"],
        "pub": ulc["pub"].hex(),
        "wrapped_lpriv": ulc["wrapped_lpriv"],
        "enc_label": ulc["enc_label"],
        "file_uuid": file_uuid.hex(),
        "dek": dek.hex(),
        "eph_priv_int": format(eph_priv_int, "064x"),
        "eph_priv_pkcs8": s.b64u(eph_der),
        "eph_pub": eph_pub.hex(),
        "sealed_nonce": sealed_nonce.hex(),
        "sealed_dek": sealed_dek,
        "wrapped_dek_nonce": wrapped_dek_nonce.hex(),
        "wrapped_dek": wrapped_dek,
    }]}


def build_mirror():
    """tests/vectors/mirror1.json (A4 Task 5): space labels, mirrors, decisions and messages with fixed keys,
    uuids and nonces. Each case stores its AAD and canonical plaintext, so the JS side (crypto.js seal +
    canonicalJson) reproduces the same envelope byte for byte, and Task 9's mirror-crypto.js opens it."""
    s = load_sharing()
    mk, dek = seq(500, 32), seq(540, 32)
    space_id = "a" * 32
    key, gen, rev = "DEMO-0038", 1, 7
    tu_hex = s.mirror_uuid(space_id, key, gen)
    tu = bytes.fromhex(tu_hex)
    decision_uuid, request_uuid, msg_uuid = seq(580, 16), seq(600, 16), seq(620, 16)
    label = "Acme Energy · Zürich"
    # as `sharing mirror push` seals it: the snapshot plus mirror_rev and gen (batch 3 review m4)
    doc = s.mirror_doc({"schema_version": "1.0.0", "id": key, "title": "Export the meter readings", "status": "waiting",
                        "questions": [{"id": "Q1", "text": "CSV or Parquet?", "options": ["CSV", "Parquet"]}]}, rev, gen)
    # the bodies the PWA seals today (final review M8): sealed space and time; a ticket request's text in value
    answer = {"v": 1, "decision_id": "dec_" + decision_uuid.hex(), "space": space_id, "ticket": key,
              "kind": "answer", "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A",
              "note": "Excel on the billing PC", "at": "2026-10-02T09:41:07Z"}
    request = {"v": 1, "decision_id": "dec_" + request_uuid.hex(), "space": space_id, "kind": "ticket_request",
               "value": {"title": "Rotate the API key", "body": "before Friday"}, "at": "2026-10-02T09:42:00Z"}
    msg = {"text": "nightly run finished ✓", "files": ["FILE12"], "ticket": None, "from": "desk"}
    cases = []

    def case(name, k, aad, pt_obj, sealed, nonce, **extra):
        cases.append({"name": name, "key": k.hex(), "aad": aad.hex(), "nonce": nonce.hex(),
                      "pt": s.canonical_json(pt_obj).hex(), "obj": pt_obj, "env": sealed, **extra})

    n = seq(640, 12)
    case("space-label", mk, s.aad_space(space_id), {"label": label},
         s.seal_space_label(mk, space_id, label, _nonce=n), n, space_id=space_id, label=label)
    n = seq(660, 12)
    case("mirror", dek, s.aad_mirror(tu), doc, s.seal_mirror(dek, tu, doc, _nonce=n), n, ticket_uuid=tu_hex)
    n = seq(680, 12)
    case("decision-ticket", dek, s.aad_decision(tu, decision_uuid), answer,
         s.seal_decision(dek, tu, decision_uuid, answer, _nonce=n), n,
         scope=tu.hex(), decision_uuid=decision_uuid.hex())
    n = seq(700, 12)
    case("decision-space", mk, s.aad_decision(space_id.encode("ascii"), request_uuid), request,
         s.seal_decision(mk, space_id.encode("ascii"), request_uuid, request, _nonce=n), n,
         scope=space_id.encode("ascii").hex(), decision_uuid=request_uuid.hex())
    n = seq(720, 12)
    case("msg", mk, s.aad_msg(msg_uuid), msg, s.seal_msg(mk, msg_uuid, msg, _nonce=n), n, msg_uuid=msg_uuid.hex())
    wrap_nonce = seq(740, 12)
    return {"version": 1, "mk": mk.hex(), "dek": dek.hex(), "space_id": space_id, "cases": cases,
            "aad_prefixes": {"space": s.AAD_SPACE_PREFIX.decode(), "mirror": s.AAD_MIRROR_PREFIX.decode(),
                             "decision": s.AAD_DECISION_PREFIX.decode(), "msg": s.AAD_MSG_PREFIX.decode()},
            "wrapped_dek": {"nonce": wrap_nonce.hex(), "aad": s.aad_tdek(tu).hex(),
                            "env": s.b64u(s.seal(mk, dek, s.aad_tdek(tu), nonce=wrap_nonce))},
            "uuids": [{"space_id": space_id, "key": key, "gen": g, "rev": rev,
                       "mirror_uuid": s.mirror_uuid(space_id, key, g),
                       "mirror_event_uuid": s.mirror_event_uuid(space_id, key, g, rev)} for g in (1, 2)]}


def render(v):
    return json.dumps(v, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(build()), encoding="utf-8")
    print(f"wrote {OUT}")
    OUT_ULINK.write_text(render(build_ulink()), encoding="utf-8")
    print(f"wrote {OUT_ULINK}")
    OUT_MIRROR.write_text(render(build_mirror()), encoding="utf-8")
    print(f"wrote {OUT_MIRROR}")
