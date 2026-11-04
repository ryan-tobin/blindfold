"""Day 8: plain English to QuerySpec, with a fake NEAR AI Cloud.

The fake serves the three endpoints the planner uses (attestation report,
chat completions, signature) and signs like a NEAR AI model key would, so
every check in planner.py runs without the network.

    cd engine && python -m pytest -q tests/test_planner.py
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest
from fastapi.testclient import TestClient

from blindfold import ethsig, load, plan_bench, receipts
from blindfold.app import Blindfold, create_app
from blindfold.planner import (EXAMPLES, PLAN_EPSILON, NearAI, PlanError, Planner, check_spec, describe,
                               with_plan_epsilon)
from blindfold.planner_stub import StubPlanner
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
MODEL = "test/private-model"
KEY = 0x2222222222222222222222222222222222222222222222222222222222222222
READMIT = {"dataset": "riverside", "measure": "rate", "table": "admissions",
           "numerator_filter": [["readmit_30d", "=", True]],
           "filters": [["encounter_class", "=", "inpatient"], ["patients.age_band", "in", ["65-79", "80+"]],
                       ["conditions.condition_group", "=", "diabetes"]],
           "group_by": ["patients.sex"]}
READMIT_PATIENTS = {"dataset": "riverside", "measure": "rate", "table": "patients",
                    "numerator_filter": [["admissions.encounter_class", "=", "inpatient"],
                                         ["admissions.readmit_30d", "=", True]],
                    "filters": [["admissions.encounter_class", "=", "inpatient"], ["age_band", "in", ["65-79", "80+"]],
                                ["conditions.condition_group", "=", "diabetes"]],
                    "group_by": ["sex"]}
BY_SEX = {"dataset": "riverside", "measure": "count", "table": "patients", "group_by": ["sex"], "epsilon": 1.0}


def sign(text: str, key: int = KEY) -> str:
    """personal_sign, for the fake: deterministic k is fine in a test."""
    z = int.from_bytes(ethsig.personal_sign_hash(text), "big")
    k = int.from_bytes(hashlib.sha256(text.encode() + key.to_bytes(32, "big")).digest(), "big") % ethsig.N
    R = ethsig._mul(k, ethsig.G)
    r = R[0] % ethsig.N
    s = pow(k, -1, ethsig.N) * (z + r * key) % ethsig.N
    v = R[1] % 2
    if s > ethsig.N // 2:
        s, v = ethsig.N - s, v ^ 1
    return (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + v])).hex()


def address(key: int = KEY) -> str:
    x, y = ethsig._mul(key, ethsig.G)
    return "0x" + ethsig.keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:].hex()


class FakeNearAI:
    """Scripted replies. Knobs let a test break one check at a time."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.chats: dict[str, tuple[bytes, bytes]] = {}
        self.calls: list[str] = []
        self.echo_nonce = True
        self.sign_key = KEY
        self.tamper = False
        self.missing_signatures = 0

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "Bearer sk-test"
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append(f"{method} {u.path}")
        if u.path.endswith("/attestation/report"):
            nonce = q["nonce"] if self.echo_nonce else "00" * 32
            rd = bytes.fromhex(address()[2:]) + bytes(12) + bytes.fromhex(nonce)
            return 200, json.dumps({
                "gateway_attestation": {"signing_address": "ab" * 32, "signing_algo": "ed25519"},
                "model_attestations": [{"signing_address": address(), "signing_algo": "ecdsa",
                                        "request_nonce": nonce, "report_data": rd.hex(), "intel_quote": "00"}],
            }).encode()
        if u.path.endswith("/chat/completions"):
            chat_id = f"chatcmpl-{len(self.chats)}"
            content = self.replies.pop(0)
            resp = json.dumps({"id": chat_id, "choices": [{"message": {"role": "assistant", "content": content}}]})
            self.chats[chat_id] = (body, resp.encode())
            return 200, resp.encode()
        if "/signature/" in u.path:
            if self.missing_signatures:
                self.missing_signatures -= 1
                return 404, b'{"detail": "not found"}'
            req, resp = self.chats[u.path.rsplit("/", 1)[1]]
            if self.tamper:
                resp += b" "
            text = f"{MODEL}:{hashlib.sha256(req).hexdigest()}:{hashlib.sha256(resp).hexdigest()}"
            return 200, json.dumps({"text": text, "signature": "0x" + sign(text, self.sign_key),
                                    "signing_address": address(self.sign_key), "signing_algo": "ecdsa",
                                    "signature_kind": "provider_tee"}).encode()
        return 404, b"{}"


def planner(fake: FakeNearAI) -> Planner:
    return Planner(NearAI("sk-test", MODEL, transport=fake, sleep=lambda s: None), SCHEMA)


def test_a_good_plan_is_checked_end_to_end():
    fake = FakeNearAI("```json\n" + json.dumps(READMIT) + "\n```")
    plan = planner(fake).plan("readmission rate, diabetics 65+, by sex")
    assert plan.planner == "near-ai" and plan.tries == 1 and plan.refusal is None
    assert plan.spec == validate({**READMIT, "epsilon": 1.0}, SCHEMA).canonical()
    sig = plan.model_signature
    assert sig["signing_address"] == address() and sig["kind"] == "provider_tee"
    assert ethsig.recover_personal(sig["text"], sig["signature"]) == address()
    req = plan.evidence["request"]
    assert hashlib.sha256(req.encode()).hexdigest() in sig["text"]
    assert json.loads(req)["temperature"] == 0 and json.loads(req)["stream"] is False
    assert fake.calls == ["GET /v1/attestation/report", "POST /v1/chat/completions", "GET /v1/signature/chatcmpl-0"]
    out = plan.to_dict()
    assert out["summary"].startswith("Rate of readmit_30d yes among admissions") and out["epsilon"] == 1.0


def test_bad_json_is_sent_back_once():
    fake = FakeNearAI("sure! here you go", json.dumps(READMIT))
    plan = planner(fake).plan("q")
    assert plan.tries == 2 and plan.model_signature["chat_id"] == "chatcmpl-1"
    retry = json.loads(fake.chats["chatcmpl-1"][0])["messages"]
    assert "not a valid QuerySpec" in retry[-1]["content"]


def test_two_off_schema_specs_ask_the_buyer_to_rephrase():
    bad = json.dumps({**READMIT, "table": "visits"})
    with pytest.raises(PlanError, match="rephrase"):
        planner(FakeNearAI(bad, bad)).plan("q")


def test_privacy_refusals_are_not_retried():
    narrow = {"dataset": "riverside", "measure": "count", "table": "patients",
              "filters": [["sex", "=", "female"], ["age_band", "=", "65-79"], ["zip3", "=", "606"],
                          ["county", "=", "Cook County"]]}
    fake = FakeNearAI(json.dumps(narrow))
    plan = planner(fake).plan("the 70 year old woman in 606, Cook County")
    assert plan.tries == 1 and plan.refusal["rule"] == "too_fine"
    assert plan.spec == {**narrow, "epsilon": 0.5}


def test_the_model_can_refuse():
    plan = planner(FakeNearAI('{"refuse": "not_aggregate", "reason": "asks for names"}')).plan("list names")
    assert plan.refusal["rule"] == "not_aggregate" and plan.spec is None


def test_attestation_must_echo_the_nonce():
    fake = FakeNearAI(json.dumps(READMIT))
    fake.echo_nonce = False
    with pytest.raises(PlanError, match="nonce"):
        planner(fake).plan("q")


def test_signature_from_another_key_fails():
    fake = FakeNearAI(json.dumps(READMIT))
    fake.sign_key = KEY + 1
    with pytest.raises(PlanError, match="not the attested"):
        planner(fake).plan("q")


def test_signature_over_other_bytes_fails():
    fake = FakeNearAI(json.dumps(READMIT))
    fake.tamper = True
    with pytest.raises(PlanError, match="doesn't match"):
        planner(fake).plan("q")


def test_a_late_signature_is_waited_for():
    fake = FakeNearAI(json.dumps(READMIT))
    fake.missing_signatures = 2
    assert planner(fake).plan("q").model_signature


def test_prompt_examples_are_valid_and_not_test_questions():
    tests = {c["question"] for c in plan_bench.cases(SCHEMA)}
    for question, spec in EXAMPLES:
        assert question not in tests
        check_spec(spec, SCHEMA)  # raises if an example breaks the schema


def test_stub_knows_the_demo_questions_only():
    stub = StubPlanner(SCHEMA)
    assert len(stub.questions) == 5
    plan = stub.plan("what is the 30-DAY readmission rate for diabetic patients aged 65 and older by sex")
    assert plan.planner == "stub" and plan.valid.canonical() == validate({**READMIT_PATIENTS, "epsilon": 1.0}, SCHEMA).canonical()
    with pytest.raises(PlanError, match="NEAR_AI_API_KEY"):
        stub.plan("how many patients smoke?")


def test_stub_scores_the_5_demo_questions():
    rows = [plan_bench.score(StubPlanner(SCHEMA), c) for c in plan_bench.cases(SCHEMA)]
    assert len(rows) == 25
    assert sorted(r["id"] for r in rows if r["ok"]) == sorted(f"{g}/0" for g in
        ["readmission", "hypertension", "hba1c", "heart_failure_drugs", "attack_one_patient"])

def test_plans_get_the_planner_epsilon_unless_the_question_names_one():
    assert check_spec(BY_SEX, SCHEMA)[1].epsilon == 1.0  # asked for
    assert check_spec({k: v for k, v in BY_SEX.items() if k != "epsilon"}, SCHEMA)[1].epsilon == 0.5
    assert check_spec(READMIT_PATIENTS, SCHEMA)[1].epsilon == 1.0
    assert all(e <= SCHEMA["query_rules"]["max_epsilon"] for e in PLAN_EPSILON.values())
    refusal = {"refuse": "not_aggregate", "reason": "rows"}
    assert with_plan_epsilon(refusal, SCHEMA) == refusal


def test_readmission_rates_are_per_patient_in_the_prompt():
    prompt = Planner(NearAI("sk-test", MODEL), SCHEMA).prompt
    assert 'A readmission rate is per patient: table "patients"' in prompt
    assert "epsilon at most 1.0" in prompt
    assert any(spec.get("table") == "patients" and spec.get("measure") == "rate" for _, spec in EXAMPLES)

def test_describe_reads_like_the_brief():
    assert describe(validate(READMIT, SCHEMA)) == ("Rate of readmit_30d yes among admissions · encounter_class inpatient"
                                                  " · age_band 65-79, 80+ · condition_group diabetes · by sex · ε 0.2")


# --- the HTTP service ---

@pytest.fixture(scope="module")
def tiny_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("plan") / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE patients AS
        SELECT i::INTEGER AS patient_id, CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS sex,
               '65-79' AS age_band, '606' AS zip3, 'Cook County' AS county
        FROM range(1, 1001) t(i)""")
    con.close()
    return path


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


def client(tiny_db, tmp_path, planner=None, limit=120):
    engine = Blindfold(tiny_db, load.DEFAULT_SCHEMA, tmp_path / "state.sqlite", 10, Clock(),
                       planner=planner, plan_limit_per_hour=limit)
    return TestClient(create_app(engine))


def test_plan_endpoint_with_the_stub(tiny_db, tmp_path):
    c = client(tiny_db, tmp_path)
    r = c.post("/plan", json={"question": "How many patients have hypertension, by age band?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["planner"] == "stub" and body["epsilon"] == 0.5 and body["price"] is None
    assert body["summary"] == "Count of patients · condition_group hypertension · by age_band · ε 0.5"
    assert c.post("/plan", json={"question": "something else"}).status_code == 422
    assert c.get("/info").json()["planner"] == "stub"


def test_plans_are_rate_limited(tiny_db, tmp_path):
    c = client(tiny_db, tmp_path, limit=2)
    q = {"question": "How many patients have hypertension, by age band?"}
    assert [c.post("/plan", json=q).status_code for _ in range(3)] == [200, 200, 429]


def test_receipt_carries_the_model_signature(tiny_db, tmp_path):
    fake = FakeNearAI(json.dumps(BY_SEX))
    c = client(tiny_db, tmp_path, planner=planner(fake))
    plan = c.post("/plan", json={"question": "patients by sex, epsilon 1"}).json()
    assert plan["planner"] == "near-ai" and plan["model"] == MODEL
    out = c.post("/query", json={"buyer": "alice.testnet", "spec": plan["spec"]}).json()
    r = out["receipt"]
    assert out["status"] == "answered" and r["spec_hash"] == plan["spec_hash"]
    assert r["model_signature"] == plan["model_signature"] and receipts.signature_ok(r)
    other = c.post("/query", json={"buyer": "alice.testnet", "spec": {**BY_SEX, "epsilon": 0.5}}).json()
    assert other["receipt"]["model_signature"] is None