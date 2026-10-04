import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VEC_PATH = REPO / "tests" / "vectors" / "shr1.json"
VEC = json.loads(VEC_PATH.read_text(encoding="utf-8"))
ULINK_VEC_PATH = REPO / "tests" / "vectors" / "ulink1.json"
ULINK_VEC = json.loads(ULINK_VEC_PATH.read_text(encoding="utf-8"))


def pt_of(n):
    return bytes(i % 251 for i in range(n))


def load_make_vectors():
    spec = importlib.util.spec_from_file_location("make_vectors", REPO / "tests" / "vectors" / "make_vectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vector_file_is_up_to_date(sharing):
    mv = load_make_vectors()
    assert mv.render(mv.build()) == VEC_PATH.read_text(encoding="utf-8"), \
        "run: uv run python tests/vectors/make_vectors.py"


def test_ulink_vector_file_is_up_to_date(sharing):
    mv = load_make_vectors()
    assert mv.render(mv.build_ulink()) == ULINK_VEC_PATH.read_text(encoding="utf-8"), \
        "run: uv run python tests/vectors/make_vectors.py"


@pytest.mark.parametrize("c", VEC["envelope"], ids=lambda c: c["name"])
def test_vectors_envelope(sharing, c):
    key, aad = bytes.fromhex(c["key"]), bytes.fromhex(c["aad"])
    env = sharing.seal(key, bytes.fromhex(c["pt"]), aad, nonce=bytes.fromhex(c["nonce"]))
    assert env.hex() == c["env"]
    assert sharing.open_(key, env, aad).hex() == c["pt"]


@pytest.mark.parametrize("c", VEC["kdf"], ids=lambda c: c["name"])
def test_vectors_kdf(sharing, c):
    auth, kek = sharing.derive(c["passphrase"], bytes.fromhex(c["salt"]), c["iterations"])
    assert (auth.hex(), kek.hex()) == (c["auth_key"], c["kek"])


def test_vectors_kdf_nfc_equals_nfd():
    by = {c["name"]: c for c in VEC["kdf"]}
    assert by["nfc"]["auth_key"] == by["nfd-input"]["auth_key"]


@pytest.mark.parametrize("c", VEC["blob"], ids=lambda c: c["name"])
def test_vectors_blob(sharing, c):
    dek, uuid = bytes.fromhex(c["dek"]), bytes.fromhex(c["uuid"])
    out = io.BytesIO()
    sharing.encrypt_stream(dek, uuid, c["key_version"], io.BytesIO(pt_of(c["pt_len"])), out,
                           chunk_size=c["chunk_size"], nonce_prefix=bytes.fromhex(c["nonce_prefix"]))
    blob = out.getvalue()
    assert len(blob) == c["blob_len"]
    assert hashlib.sha256(blob).hexdigest() == c["blob_sha256"]
    if "blob" in c:
        assert blob.hex() == c["blob"]
    back = io.BytesIO()
    sharing.decrypt_stream(dek, uuid, io.BytesIO(blob), back)
    assert back.getvalue() == pt_of(c["pt_len"])


@pytest.mark.parametrize("c", VEC["file_meta"], ids=lambda c: c["name"])
def test_vectors_file_meta(sharing, c):
    stream, pos = bytes.fromhex(c["rng"]), [0]

    def rng(n):
        out = stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    mk = bytes.fromhex(c["mk"])
    fc = sharing.new_file_crypto(mk, c["meta"]["name"], c["meta"]["mime"], c["meta"]["note"], _rng=rng)
    assert (fc["uuid_hex"], fc["wrapped_dek"], fc["enc_meta"]) == (c["uuid"], c["wrapped_dek"], c["enc_meta"])
    meta, dek = sharing.open_file_meta(mk, {"uuid": c["uuid"], "wrapped_dek": c["wrapped_dek"],
                                            "enc_meta": c["enc_meta"]})
    assert meta == c["meta"] and dek.hex() == c["dek"]


@pytest.mark.parametrize("c", VEC["approve"], ids=lambda c: c["name"])
def test_vectors_approve(sharing, c):
    dev_int, eph_int = int(c["device_priv_int"], 16), int(c["eph_priv_int"], 16)
    der, pub = sharing.new_device_keypair(_priv_int=dev_int)
    assert pub.hex() == c["device_pub"] and sharing.b64u(der) == c["device_priv_pkcs8"]
    assert sharing.fingerprint(pub) == c["fingerprint"]
    assert sharing.approval_key(bytes.fromhex(c["z"]), bytes.fromhex(c["eph_pub"]), pub,
                                c["device_id"]).hex() == c["wk"]
    bundle = sharing.seal_to_device(bytes.fromhex(c["mk"]), pub, c["device_id"],
                                    _eph_priv_int=eph_int, nonce=bytes.fromhex(c["nonce"]))
    assert bundle.hex() == c["bundle"]
    # the device side opens the pinned bundle — the same bytes the JS test must produce
    assert sharing.open_device_bundle(sharing.unb64u(c["device_priv_pkcs8"]), pub, c["device_id"],
                                      bytes.fromhex(c["bundle"])).hex() == c["mk"]
    assert sharing.parse_code(c["code"]).hex() == c["code_lookup"]


@pytest.mark.parametrize("c", VEC["settings"], ids=lambda c: c["name"])
def test_vectors_settings(sharing, c):
    mk, nonce = bytes.fromhex(c["mk"]), bytes.fromhex(c["nonce"])
    settings = json.loads(c["input_json"])                 # keys in the input's (unsorted) order
    assert list(settings) != sorted(settings), "the vector must exercise key sorting"
    assert sharing.canonical_json(settings).decode("utf-8") == c["json"]
    assert sharing.seal_settings(mk, settings, _nonce=nonce) == c["enc_settings"]
    assert sharing.open_settings(mk, c["enc_settings"]) == settings
    assert sharing.open_(mk, sharing.unb64u(c["enc_settings"]), sharing.AAD_SETTINGS).decode("utf-8") == c["json"]


@pytest.mark.parametrize("c", VEC["meta_reseal"], ids=lambda c: c["name"])
def test_vectors_meta_reseal(sharing, c):
    # The browser re-seals the whole meta with a transcript (§20); crypto.test.mjs checks the same bytes.
    dek, uuid, nonce = bytes.fromhex(c["dek"]), bytes.fromhex(c["uuid"]), bytes.fromhex(c["nonce"])
    meta = json.loads(c["input_json"])
    assert list(meta) != sorted(meta), "the vector must exercise key sorting"
    assert sharing.canonical_json(meta).decode("utf-8") == c["json"]
    assert sharing.seal_file_meta(dek, uuid, meta, _nonce=nonce) == c["enc_meta"]
    assert sharing._open_meta(dek, uuid, sharing.unb64u(c["enc_meta"])) == meta


@pytest.mark.parametrize("c", VEC["tickets"], ids=lambda c: c["name"])
def test_vectors_tickets(sharing, c):
    # T3: a fixed ticket uuid, DEK and two envelope nonces drive new_ticket_crypto's rng stream, so
    # wrapped_dek and enc_content are byte-exact with crypto.test.mjs's newTicketCrypto vector.
    stream, pos = bytes.fromhex(c["rng"]), [0]

    def rng(n):
        out = stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    mk = bytes.fromhex(c["mk"])
    tc = sharing.new_ticket_crypto(mk, c["content"], _rng=rng)
    assert tc["uuid"] == c["uuid"]
    assert tc["key_version"] == c["key_version"]
    assert tc["wrapped_dek"] == c["wrapped_dek"]
    assert tc["enc_content"] == c["enc_content"]
    assert tc["_dek"].hex() == c["dek"]

    content, dek = sharing.open_ticket(mk, {"uuid": c["uuid"], "wrapped_dek": c["wrapped_dek"],
                                            "enc_content": c["enc_content"]})
    assert content == c["content"] and dek.hex() == c["dek"]

    # T5: an event body sealed under the ticket's DEK, bound to both the ticket and the event uuid.
    ticket_uuid, event_uuid = bytes.fromhex(c["uuid"]), bytes.fromhex(c["event_uuid"])
    enc_body = sharing.seal_ticket_event(dek, ticket_uuid, event_uuid, c["body"],
                                         _nonce=bytes.fromhex(c["event_nonce"]))
    assert enc_body == c["enc_body"]
    assert sharing.open_ticket_event(dek, ticket_uuid, event_uuid, enc_body) == c["body"]


@pytest.mark.parametrize("c", VEC["link"], ids=lambda c: c["name"])
def test_vectors_link(sharing, c):
    lk, dek, uuid = bytes.fromhex(c["lk"]), bytes.fromhex(c["dek"]), bytes.fromhex(c["uuid"])
    assert sharing.b64u(lk) == c["lk_b64u"]
    assert sharing.aad_link(uuid).hex() == c["aad"]
    assert sharing.wrap_dek_for_link(dek, uuid, lk, _nonce=bytes.fromhex(c["nonce"])) == c["wrapped_dek_link"]
    assert sharing.open_link_dek(lk, uuid, c["wrapped_dek_link"]) == dek
    meta, dek2 = sharing.open_link_file(lk, {"uuid": c["uuid"], "wrapped_dek_link": c["wrapped_dek_link"],
                                             "enc_meta": c["enc_meta"]})
    assert meta == c["meta"] and dek2 == dek


@pytest.mark.parametrize("c", ULINK_VEC["basic"], ids=lambda c: c["name"])
def test_vectors_ulink(sharing, c):
    # Task 1 (spec §18): outputs reproduce exactly from the fixed inputs, both ways (Python vector
    # <-> Python vector); uploadlink-crypto.test.mjs checks the same bytes against JS.
    mk = bytes.fromhex(c["mk"])
    link_priv_int = int(c["link_priv_int"], 16)
    rng_stream, pos = bytes.fromhex(c["rng"]), [0]

    def rng(n):
        out = rng_stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    ulc = sharing.new_upload_link_crypto(mk, c["label"], _priv_int=link_priv_int, _rng=rng,
                                         _nonce=bytes.fromhex(c["wrapped_nonce"]))
    assert ulc["uuid_hex"] == c["link_uuid"]
    assert ulc["pub"].hex() == c["pub"]
    assert ulc["wrapped_lpriv"] == c["wrapped_lpriv"]
    assert ulc["enc_label"] == c["enc_label"]

    priv, pub = sharing.open_upload_link_key(mk, ulc["uuid"], c["wrapped_lpriv"])
    assert pub == ulc["pub"]
    assert sharing.open_upload_label(mk, ulc["uuid"], c["enc_label"]) == c["label"]

    dek, file_uuid = bytes.fromhex(c["dek"]), bytes.fromhex(c["file_uuid"])
    eph_priv_int = int(c["eph_priv_int"], 16)
    sealed_dek = sharing.seal_dek_to_link(dek, ulc["pub"], ulc["uuid"], file_uuid,
                                         _eph_priv_int=eph_priv_int, _nonce=bytes.fromhex(c["sealed_nonce"]))
    assert sealed_dek == c["sealed_dek"]
    assert sharing.open_sealed_dek(priv, ulc["pub"], ulc["uuid"], file_uuid, c["sealed_dek"]) == dek

    wrapped_dek = sharing.wrap_dek_under_mk(mk, dek, file_uuid, _nonce=bytes.fromhex(c["wrapped_dek_nonce"]))
    assert wrapped_dek == c["wrapped_dek"]


# ---- A4 (Task 5): spaces, mirrors, decisions, messages -----------------------------------------------------

MIRROR_VEC_PATH = REPO / "tests" / "vectors" / "mirror1.json"
MIRROR_VEC = json.loads(MIRROR_VEC_PATH.read_text(encoding="utf-8"))
MIRROR_CASES = {c["name"]: c for c in MIRROR_VEC["cases"]}


def test_mirror_vector_file_is_up_to_date(sharing):
    mv = load_make_vectors()
    assert mv.render(mv.build_mirror()) == MIRROR_VEC_PATH.read_text(encoding="utf-8"), \
        "run: uv run python tests/vectors/make_vectors.py"


def test_mirror_aad_prefixes_are_exact(sharing):
    assert sharing.AAD_SPACE_PREFIX == b"sharing/space/v1|"
    assert sharing.AAD_MIRROR_PREFIX == b"sharing/mirror/v1|"
    assert sharing.AAD_DECISION_PREFIX == b"sharing/decision/v1|"
    assert sharing.AAD_MSG_PREFIX == b"sharing/msg/v1|"
    u = bytes(range(16))
    assert sharing.aad_decision(b"S", u) == b"sharing/decision/v1|S|" + u
    with pytest.raises(ValueError):
        sharing.aad_mirror(b"short")


def test_vectors_mirror_uuids(sharing):
    assert sharing.mirror_uuid("a" * 32, "DEMO-0038", 1) == MIRROR_VEC["uuids"][0]["mirror_uuid"]
    for u in MIRROR_VEC["uuids"]:
        assert sharing.mirror_uuid(u["space_id"], u["key"], u["gen"]) == u["mirror_uuid"]
        assert sharing.mirror_event_uuid(u["space_id"], u["key"], u["gen"], u["rev"]) == u["mirror_event_uuid"]
        assert u["mirror_uuid"] == hashlib.sha256(f"{u['space_id']}|{u['key']}|{u['gen']}".encode()).digest()[:16].hex()
    assert MIRROR_VEC["uuids"][0]["mirror_uuid"] != MIRROR_VEC["uuids"][1]["mirror_uuid"]


def test_vectors_mirror_seals(sharing):
    s, mk, dek = sharing, bytes.fromhex(MIRROR_VEC["mk"]), bytes.fromhex(MIRROR_VEC["dek"])
    c = MIRROR_CASES["space-label"]
    assert s.seal_space_label(mk, c["space_id"], c["label"], _nonce=bytes.fromhex(c["nonce"])) == c["env"]
    assert s.open_space_label(mk, c["space_id"], c["env"]) == c["label"]
    c = MIRROR_CASES["mirror"]
    tu = bytes.fromhex(c["ticket_uuid"])
    assert s.seal_mirror(dek, tu, c["obj"], _nonce=bytes.fromhex(c["nonce"])) == c["env"]
    assert s.open_mirror(dek, tu, c["env"]) == c["obj"]
    for name, key in (("decision-ticket", dek), ("decision-space", mk)):
        c = MIRROR_CASES[name]
        scope, du = bytes.fromhex(c["scope"]), bytes.fromhex(c["decision_uuid"])
        assert s.seal_decision(key, scope, du, c["obj"], _nonce=bytes.fromhex(c["nonce"])) == c["env"]
    c = MIRROR_CASES["msg"]
    assert s.seal_msg(mk, bytes.fromhex(c["msg_uuid"]), c["obj"], _nonce=bytes.fromhex(c["nonce"])) == c["env"]
    assert s.open_msg(mk, bytes.fromhex(c["msg_uuid"]), c["env"]) == c["obj"]
    for c in MIRROR_VEC["cases"]:
        assert s.canonical_json(c["obj"]).hex() == c["pt"]


def test_vectors_inbox_items_open_both_scopes(sharing):
    s, mk = sharing, bytes.fromhex(MIRROR_VEC["mk"])
    t, sp = MIRROR_CASES["decision-ticket"], MIRROR_CASES["decision-space"]
    item = {"uuid": t["decision_uuid"], "ticket_uuid": MIRROR_CASES["mirror"]["ticket_uuid"],
            "ticket_wrapped_dek": MIRROR_VEC["wrapped_dek"]["env"], "enc_body": t["env"], "space": MIRROR_VEC["space_id"]}
    assert s.open_inbox_item(mk, item) == t["obj"]
    assert s.open_inbox_item(mk, {"uuid": sp["decision_uuid"], "ticket_uuid": None, "enc_body": sp["env"],
                                  "space": MIRROR_VEC["space_id"]}) == sp["obj"]
    with pytest.raises(s.IntegrityError):     # a ticket decision is bound to its ticket, not the space
        s.open_inbox_item(mk, {"uuid": t["decision_uuid"], "ticket_uuid": None, "enc_body": t["env"],
                               "space": MIRROR_VEC["space_id"]})


def test_decision_vectors_have_the_current_body_shapes(sharing):
    """Final review M8: the decision bodies are the ones the PWA seals today and the CLI checks (`_checked_decision`,
    `_bound_gen`): sealed space and time on both; a ticket answer with note; a ticket request's title and body in
    `value`."""
    answer, request = MIRROR_CASES["decision-ticket"]["obj"], MIRROR_CASES["decision-space"]["obj"]
    assert set(answer) == {"v", "decision_id", "space", "ticket", "kind", "target", "value", "note", "at"}
    assert answer["space"] == MIRROR_VEC["space_id"] and answer["target"]["hash"].startswith("sha256:")
    assert set(request) == {"v", "decision_id", "space", "kind", "value", "at"}
    assert request["kind"] == "ticket_request" and set(request["value"]) == {"title", "body"}
    sharing._checked_decision(answer, {"kind": "answer", "id": answer["decision_id"], "ticket": "TIX-1"})
    sharing._checked_decision(request, {"kind": "ticket_request", "id": request["decision_id"], "ticket": None})
