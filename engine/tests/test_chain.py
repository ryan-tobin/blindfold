"""Day 6: chain mode. A buyer pays on the contract, the engine answers and settles.

FakeChain stands in for the deployed contract (same methods as chain.Chain),
so these run offline. The NEAR transaction bytes are checked against a
vector made with py-near 1.2.22, so the hand-written borsh is right.

    cd engine && python -m pytest -q tests/test_chain.py
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from blindfold import load, near, receipts
from blindfold.app import Blindfold, create_app
from blindfold.chain import MILLION, to_millionths
from blindfold.refuse import Refused
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
SCHEMA_HASH = hashlib.sha256(load.DEFAULT_SCHEMA.read_bytes()).hexdigest()
BY_SEX = {"dataset": "riverside", "measure": "count", "table": "patients", "group_by": ["sex"], "epsilon": 1.0}


def spec_hash(spec):
    """What a buyer pays for: the canonical hash, or the raw hash for a spec that breaks the rules."""
    try:
        return receipts.spec_hash(validate(spec, SCHEMA).canonical_json())
    except Refused:
        return receipts.sha256_hex(spec)


class FakeChain:
    contract_id = "blindfold.test.near"

    def __init__(self, schema_hash=SCHEMA_HASH, epsilon_total=10):
        self.ds = {"id": "riverside", "schema_hash": schema_hash, "epsilon_total": epsilon_total * MILLION,
                   "epsilon_spent": 0, "epsilon_pending": 0, "active": True}
        self.requests = []
        self.settled = []
        self.fail_next_settle = False
        self.expire_before_settle = False

    def pay(self, spec, buyer="alice.testnet", epsilon=None):
        eps = to_millionths(Fraction(str(spec.get("epsilon", 0.1) if epsilon is None else epsilon)))
        self.requests.append({"id": str(len(self.requests)), "buyer": buyer, "dataset_id": "riverside",
                              "spec_hash": spec_hash(spec), "epsilon": eps, "status": "pending"})
        self.ds["epsilon_pending"] += eps
        return len(self.requests) - 1

    def expire(self, request_id):
        r = self.requests[request_id]
        assert r["status"] == "pending"
        r["status"] = "expired"
        self.ds["epsilon_pending"] -= r["epsilon"]

    def dataset(self, dataset_id):
        return dict(self.ds)

    def request(self, request_id):
        return dict(self.requests[request_id]) if request_id < len(self.requests) else None

    def settle(self, request_id, status, receipt_hash, epsilon_charged, refusal):
        if self.fail_next_settle:
            self.fail_next_settle = False
            raise near.NearError("RPC timed out")
        r = self.requests[request_id]
        if self.expire_before_settle:  # the buyer's expire() lands first
            self.expire_before_settle = False
            self.expire(request_id)
        if r["status"] != "pending":
            raise near.NearError("Smart contract panicked: request is not pending")
        assert r["status"] == "pending" and to_millionths(epsilon_charged) <= r["epsilon"]
        r["status"] = status
        self.ds["epsilon_pending"] -= r["epsilon"]
        self.ds["epsilon_spent"] += to_millionths(epsilon_charged)
        self.settled.append((request_id, status, receipt_hash, epsilon_charged, refusal))
        return f"tx{len(self.settled)}"


@pytest.fixture(scope="module")
def tiny_db(tmp_path_factory):
    import duckdb
    path = tmp_path_factory.mktemp("chain") / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE patients AS
        SELECT i::INTEGER AS patient_id, CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS sex,
               '65-79' AS age_band, '606' AS zip3, 'Cook County' AS county
        FROM range(1, 1001) t(i)""")
    con.close()
    return path


def make(tiny_db, tmp_path, chain=None):
    chain = chain or FakeChain()
    engine = Blindfold(tiny_db, load.DEFAULT_SCHEMA, tmp_path / "state.sqlite", 99, chain=chain)
    return chain, engine, TestClient(create_app(engine))


def ask(client, request_id, spec=BY_SEX, status=200):
    r = client.post("/query", json={"request_id": request_id, "spec": spec})
    assert r.status_code == status, r.text
    return r.json()


def test_paid_question_is_answered_signed_and_settled(tiny_db, tmp_path):
    chain, engine, client = make(tiny_db, tmp_path)
    out = ask(client, chain.pay(BY_SEX))
    r = out["receipt"]
    assert out["status"] == "answered" and out["settle_tx"] == "tx1"
    assert r["request_id"] == 0 and r["spec_hash"] == spec_hash(BY_SEX)
    assert receipts.signature_ok(r) and r["signer_pubkey"] == engine.enclave.signer_pubkey
    assert chain.settled == [(0, "answered", out["receipt_hash"], Fraction(1), None)]
    assert client.get("/budget").json()["epsilon_total"] == 10.0  # the total comes from the contract
    assert client.get("/budget").json()["epsilon_remaining"] == 9.0
    assert client.get("/receipt/0").json()["receipt"] == r


def test_replay_is_paid_but_costs_no_epsilon(tiny_db, tmp_path):
    chain, _, client = make(tiny_db, tmp_path)
    first = ask(client, chain.pay(BY_SEX))
    again = ask(client, chain.pay(BY_SEX, buyer="bob.testnet"))
    assert again["replay_of"] == 0 and again["answer"] == first["answer"]
    assert chain.settled[1][1:] == ("answered", again["receipt_hash"], Fraction(0), None)


def test_a_spec_that_isnt_the_one_paid_for_changes_nothing(tiny_db, tmp_path):
    chain, engine, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    for wrong in ({**BY_SEX, "group_by": ["age_band"]}, {"x": 1}, {}, "x"):  # audit #2: a stranger goes first
        assert "nothing was recorded" in ask(client, paid, wrong, status=409)["detail"]
    assert chain.settled == [] and engine.state.receipt(paid) is None and engine.state.recent("alice.testnet", 0) == 0
    out = ask(client, paid)  # the buyer's own spec still works
    assert out["status"] == "answered" and out["settle_tx"] == "tx1"


def test_right_question_with_the_wrong_epsilon_is_refused_and_refunded(tiny_db, tmp_path):
    chain, _, client = make(tiny_db, tmp_path)
    cheap = chain.pay(BY_SEX, epsilon=0.1)  # right question, but only 0.1 reserved
    out = ask(client, cheap)
    assert out["refusal"]["rule"] == "spec_mismatch" and out["refusal"]["rule_number"] == 6
    assert chain.settled[-1][1:] == ("refused", out["receipt_hash"], Fraction(0), "spec_mismatch")
    assert chain.ds["epsilon_spent"] == 0


def test_a_stranger_cant_read_a_paid_answer(tiny_db, tmp_path):  # audit #1
    chain, _, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    ask(client, paid)
    for probe in ({}, {"garbage": 1}, "x", {**BY_SEX, "group_by": ["age_band"]}):
        r = client.post("/query", json={"request_id": paid, "spec": probe})
        assert r.status_code == 409 and "cells" not in r.text
    assert ask(client, paid)["answer"]["cells"]  # the buyer, with the spec, still can


def test_bad_spec_is_refused_on_chain_too(tiny_db, tmp_path):
    chain, _, client = make(tiny_db, tmp_path)
    bad = {**BY_SEX, "measure": "rows"}
    paid = chain.pay(bad)  # paid for by its raw hash, as agent/pay_and_ask.py --pay-anyway does
    out = ask(client, paid, bad)
    assert out["refusal"]["rule"] == "not_aggregate"
    assert chain.settled[-1][4] == "not_aggregate"


def test_a_request_is_answered_once(tiny_db, tmp_path):
    chain, _, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    first = ask(client, paid)
    again = ask(client, paid)  # the stored result, not a new answer, and no second settle
    assert again["receipt"] == first["receipt"] and again["settle_tx"] is None and len(chain.settled) == 1
    expired = chain.pay(BY_SEX)
    chain.requests[expired]["status"] = "expired"
    assert "already expired" in ask(client, expired, status=409)["detail"]
    assert ask(client, 99, status=404)["detail"].startswith("no request 99")
    assert "request_id" in ask(client, None, status=422)["detail"]


def test_failed_settle_can_be_retried_for_free(tiny_db, tmp_path):
    chain, engine, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    chain.fail_next_settle = True
    assert "retry" in ask(client, paid, status=502)["detail"]
    assert chain.settled == [] and engine.state.spent("riverside") == 1  # spent locally already
    out = ask(client, paid)  # same request_id again
    assert out["settle_tx"] == "tx1" and out["request_id"] == paid and out["answer"]["cells"]
    assert engine.state.spent("riverside") == 1  # not charged twice
    assert chain.settled[0][1:4] == ("answered", out["receipt_hash"], Fraction(1))


def test_budget_is_the_larger_of_chain_and_local(tiny_db, tmp_path):
    chain, engine, client = make(tiny_db, tmp_path)
    chain.ds["epsilon_spent"] = 9_500_000  # spent through an earlier deployment
    out = ask(client, chain.pay(BY_SEX))
    assert out["refusal"]["rule"] == "over_budget"
    assert client.get("/budget").json()["epsilon_remaining"] == 0.5


def test_engine_refuses_to_start_on_a_different_schema(tiny_db, tmp_path):
    with pytest.raises(SystemExit, match="hashes to"):
        make(tiny_db, tmp_path, FakeChain(schema_hash="00" * 32))


def test_info_and_attestation(tiny_db, tmp_path):
    _, engine, client = make(tiny_db, tmp_path)
    info = client.get("/info").json()
    assert info["contract_id"] == FakeChain.contract_id and info["schema_hash"] == SCHEMA_HASH
    att = client.get("/attestation").json()
    assert att["mode"] == "dev" and att["account_id"] == info["account_id"] == engine.enclave.account_id


def test_millionths_are_exact():
    assert to_millionths(Fraction("0.2")) == 200_000
    with pytest.raises(ValueError):
        to_millionths(Fraction(1, 3))


def test_transaction_bytes_match_py_near():
    key = SigningKey(bytes(range(32)))
    args = json.dumps({"request_id": "7", "status": "answered", "receipt_hash": "ab" * 32,
                       "epsilon_charged": 1000000, "refusal": None}).encode()
    tx = near.function_call_tx("abc" * 21 + "x", bytes(key.verify_key), 12345678901, "blindfold.ryan-tobin.testnet",
                               hashlib.sha256(b"block").digest(), "settle", args, 30 * near.TGAS, 1)
    signed = tx + b"\x00" + key.sign(hashlib.sha256(tx).digest()).signature
    assert hashlib.sha256(signed).hexdigest() == "0510906d383de4c405752cb986c54a5b57bfd3c80ece5fd7faba993c8df68df2"


@pytest.mark.parametrize("data,text", [(b"", ""), (b"\0\0\x01", "112"), (bytes(32), "1" * 32),
                                       (b"hello world", "StV1DL6CwTryKyV")])
def test_base58(data, text):
    assert near.b58encode(data) == text and near.b58decode(text) == data

def test_no_answer_after_the_request_expired(tiny_db, tmp_path):  # audit #3 and #5
    chain, engine, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    chain.expire_before_settle = True
    assert ask(client, paid, status=502)["detail"].startswith("answered, but settle failed")
    r = client.post("/query", json={"request_id": paid, "spec": BY_SEX})
    assert r.status_code == 410 and "cells" not in r.text
    assert engine.state.spent("riverside") == 0  # the unreleased answer gave its epsilon back
    again = ask(client, chain.pay(BY_SEX))  # the same question later: a fresh answer, charged on-chain
    assert again["replay_of"] is None and chain.settled[-1][3] == Fraction(1)
    assert engine.state.spent("riverside") == 1 == chain.ds["epsilon_spent"] / MILLION


def test_a_settle_that_landed_anyway_is_returned(tiny_db, tmp_path):
    chain, engine, client = make(tiny_db, tmp_path)
    paid = chain.pay(BY_SEX)
    chain.fail_next_settle = True
    ask(client, paid, status=502)
    chain.requests[paid]["status"] = "answered"  # the settle went through; only the reply was lost
    out = ask(client, paid)
    assert out["answer"]["cells"] and out["settle_tx"] is None


def test_replay_waits_until_the_first_answer_is_settled(tiny_db, tmp_path):  # audit #5
    chain, engine, client = make(tiny_db, tmp_path)
    first = chain.pay(BY_SEX)
    chain.fail_next_settle = True
    ask(client, first, status=502)  # answered and cached, but not settled yet
    second = chain.pay(BY_SEX, buyer="bob.testnet")
    assert "in a minute" in ask(client, second, status=409)["detail"]
    chain.expire(first)  # the first buyer never came back
    out = ask(client, second)
    assert out["replay_of"] is None and chain.settled[-1][1:4:2] == ("answered", Fraction(1))
    assert chain.ds["epsilon_spent"] == MILLION and engine.state.spent("riverside") == 1


def test_replay_of_request_0_returns_its_answer(tiny_db, tmp_path):  # audit #11
    chain, engine, client = make(tiny_db, tmp_path)
    first = ask(client, chain.pay(BY_SEX))
    again = chain.pay(BY_SEX, buyer="bob.testnet")
    chain.fail_next_settle = True
    ask(client, again, status=502)
    out = ask(client, again)
    assert out["replay_of"] == 0 and out["answer"] == first["answer"]


def test_a_request_over_the_epsilon_cap_is_closed_by_anyone(tiny_db, tmp_path):  # audit #6
    chain, engine, client = make(tiny_db, tmp_path)
    chain.requests.append({"id": "0", "buyer": "mallory.testnet", "dataset_id": "riverside",
                           "spec_hash": "ab" * 32, "epsilon": 10 * MILLION, "status": "pending"})
    chain.ds["epsilon_pending"] = 10 * MILLION
    out = ask(client, 0, {})  # no spec needed: no question may use that much
    assert out["refusal"]["rule"] == "over_budget" and chain.ds["epsilon_pending"] == 0
    assert chain.settled[0][1] == "refused" and ask(client, 0, {})["settle_tx"] is None


def test_a_lying_rpc_cant_raise_the_total_budget(tiny_db, tmp_path):  # audit #4
    chain = FakeChain(epsilon_total=1000)
    _, engine, client = make(tiny_db, tmp_path, chain)
    assert client.get("/budget").json()["epsilon_total"] == 99  # the engine's own cap
