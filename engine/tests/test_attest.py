"""Day 7: checking an enclave's attestation, on a made-up TDX quote and event log.

The Intel signature check (dcap-qvl) needs the network and a real quote, so
it is stubbed here; everything after it runs for real.

    cd engine && python -m pytest -q tests/test_attest.py
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from blindfold import attest, enclave

COMPOSE = "services:\n  engine:\n    image: ghcr.io/x/engine@sha256:" + "ab" * 32 + "\n"
APP_COMPOSE = json.dumps({"runner": "docker-compose", "docker_compose_file": COMPOSE})
COMPOSE_HASH = hashlib.sha256(APP_COMPOSE.encode()).hexdigest()


def event(name: str, payload: bytes, imr: int = 3) -> dict:
    e = {"imr": imr, "event_type": 0x08000001, "event": name, "event_payload": payload.hex()}
    e["digest"] = attest.event_digest(e)
    return e


def make_att() -> dict:
    e = enclave.dev("cd" * 32)
    events = [event("system-preparing", b""), event("app-id", bytes.fromhex("11" * 20)),
              event("compose-hash", bytes.fromhex(COMPOSE_HASH)), event("key-provider", b'{"name":"kms"}'),
              {"imr": 0, "event_type": 1, "event": "", "event_payload": "", "digest": "ff" * 48}]
    body = bytearray(584)
    for i in range(4):
        body[328 + 48 * i:376 + 48 * i] = attest.replay([x["digest"] for x in events if x["imr"] == i])
    body[520:584] = e.report_data().ljust(64, b"\0")
    quote = b"\x04\x00\x02\x00" + bytes(44) + bytes(body) + bytes(64)
    return {**e.attestation(), "mode": "tdx", "simulated": False, "quote": quote.hex(), "app_id": "11" * 20,
            "event_log": json.dumps(events), "compose_hash": COMPOSE_HASH, "app_compose": APP_COMPOSE}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(attest, "intel_check", lambda quote, pccs=None: (True, "Intel stub"))


def failures(att, compose=COMPOSE):
    return [text for ok, text in attest.check(att, compose) if ok is False]


def test_a_good_attestation_passes_every_check():
    results = attest.check(make_att(), COMPOSE)
    assert all(ok is True for ok, _ in results), results
    assert len(results) == 10


def test_the_digest_formula_matches_dstack():
    # sha384(u32 LE event type + ":" + name + ":" + payload), as in dstack's verifier
    e = event("compose-hash", b"\x01\x02")
    data = (0x08000001).to_bytes(4, "little") + b":compose-hash:" + b"\x01\x02"
    assert e["digest"] == hashlib.sha384(data).hexdigest()


def test_a_forged_event_log_fails():
    att = make_att()
    events = json.loads(att["event_log"])
    events[2]["event_payload"] = "00" * 32  # claim another compose hash
    att["event_log"] = json.dumps(events)
    assert "all 4 RTMR3 events match their digests" in failures(att)
    events[2]["digest"] = attest.event_digest(events[2])  # fix the digest: RTMR3 no longer replays
    att["event_log"] = json.dumps(events)
    assert "replaying the event log gives the quote's RTMR0-3" in failures(att)


def test_other_code_fails():
    att = make_att()
    assert failures(att, COMPOSE.replace("ab", "ef")) == ["the deployed compose file is docker-compose.tee.yml from this repo"]
    att2 = copy.deepcopy(att)
    att2["app_compose"] = APP_COMPOSE.replace("ab", "ef")
    assert any(t.startswith("compose hash") for t in failures(att2))

def test_keys_not_in_the_quote_fail():
    att = make_att()
    att["encryption_pubkey"] = "00" * 32  # someone else's key
    assert failures(att) == ["report_data = sha256(signer key + encryption key + schema hash)",
                             "the quote carries that report_data"]

def test_simulated_and_dev_are_warnings_not_passes():
    att = make_att()
    att["mode"] = "simulated"
    assert [ok for ok, _ in attest.check(att)][-1] is None
    dev = enclave.dev("cd" * 32).attestation()
    assert [ok for ok, _ in attest.check(dev)] == [True, None]

def test_empty_digests_are_computed_from_the_events():
    att = make_att()  # dstack 0.5.12 sends RTMR3 events without digests
    events = json.loads(att["event_log"])
    for x in events:
        if x["imr"] == 3:
            x["digest"] = ""
    att["event_log"] = json.dumps(events)
    assert failures(att) == []
    events[2]["event_payload"] = "00" * 32  # a forged compose hash no longer replays
    att["event_log"] = json.dumps(events)
    assert "replaying the event log gives the quote's RTMR0-3" in failures(att)

def test_operator_env_vars_are_flagged():
    att = make_att()
    att["app_compose"] = json.dumps({**json.loads(APP_COMPOSE), "allowed_envs": ["CONTRACT_ID"]})
    att["compose_hash"] = hashlib.sha256(att["app_compose"].encode()).hexdigest()
    flagged = [(ok, t) for ok, t in attest.check(att, COMPOSE) if "env vars" in t]
    assert flagged == [(None, "the operator can set env vars: CONTRACT_ID")]

def test_payload_as_hex_text_is_accepted():
    assert attest.as_text(b"ab12".hex()) == "ab12"
    assert attest.as_text("ab12") == "ab12"

def test_the_near_ai_key_is_the_one_allowed_secret():
    att = make_att()
    att["app_compose"] = json.dumps({**json.loads(APP_COMPOSE), "allowed_envs": ["NEAR_AI_API_KEY"]})
    att["compose_hash"] = hashlib.sha256(att["app_compose"].encode()).hexdigest()
    flagged = [(ok, t) for ok, t in attest.check(att, COMPOSE) if "operator can set" in t]
    assert flagged == [(True, "the operator can set only NEAR_AI_API_KEY (an encrypted secret; it changes no code)")]

def test_the_compose_label_names_the_file_checked():
    texts = [t for _, t in attest.check(make_att(), COMPOSE, compose_name="docker-compose.mainnet.yml")]
    assert "the deployed compose file is docker-compose.mainnet.yml from this repo" in texts