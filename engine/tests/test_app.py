"""Day 4: the HTTP service end to end, from QuerySpec in to noisy answer and receipt out.

Runs on a small made-up patients table (1,000 patients, half women), so it
needs no `make data`. The last test uses the real tables when they exist.

    cd engine && python -m pytest -q tests/test_app.py
"""

from __future__ import annotations

import json

import duckdb
import pytest
from fastapi.testclient import TestClient

from blindfold import load, receipts
from blindfold.app import Blindfold, create_app
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
EXAMPLES = load.REPO_ROOT / "datasets" / "riverside" / "examples"
LIMIT = SCHEMA["query_rules"]["rate_limit_per_hour"]
needs_real_db = pytest.mark.skipif(not load.DEFAULT_DB.exists(), reason="run `make data` to check the real tables")

BY_SEX = {"dataset": "riverside", "measure": "count", "table": "patients", "group_by": ["sex"], "epsilon": 1.0}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def tiny_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("app") / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE patients AS
        SELECT i::INTEGER AS patient_id, CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS sex,
               '65-79' AS age_band, '606' AS zip3, 'Cook County' AS county
        FROM range(1, 1001) t(i)""")
    con.close()
    return path


def make(tiny_db, tmp_path, epsilon_total=10, clock=None):
    engine = Blindfold(tiny_db, load.DEFAULT_SCHEMA, tmp_path / "state.sqlite", epsilon_total, clock or Clock())
    return engine, TestClient(create_app(engine))


def ask(client, spec=BY_SEX, buyer="alice.testnet", **changes):
    r = client.post("/query", json={"buyer": buyer, "spec": {**spec, **changes}})
    assert r.status_code == 200, r.text
    return r.json()


def test_schema_is_served_byte_for_byte(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    r = client.get("/schema")
    assert r.content == load.DEFAULT_SCHEMA.read_bytes()

def test_the_browser_ui_may_call_the_engine(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    pre = client.options("/plan", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                           "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "*"
    r = client.get("/budget", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "*" and "access-control-allow-credentials" not in r.headers

def test_fresh_budget(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    assert client.get("/budget").json() == {"dataset": "riverside", "epsilon_total": 10.0, "epsilon_spent": 0.0,
                                            "epsilon_remaining": 10.0, "closed": False}


def test_answer_comes_with_a_receipt_and_charges_the_budget(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    out = ask(client)
    assert out["status"] == "answered" and out["refusal"] is None and out["replay_of"] is None
    cells = out["answer"]["cells"]
    assert [c["cell"] for c in cells] == [{"patients.sex": "female"}, {"patients.sex": "male"}]
    assert all(abs(c["value"] - 500) <= 20 for c in cells)  # scale 1 noise: almost always within ±5

    r = out["receipt"]
    canon = validate(BY_SEX, SCHEMA).canonical_json()
    assert r["request_id"] == out["request_id"] == 1
    assert r["spec_hash"] == receipts.spec_hash(canon)
    assert r["answer_hash"] == receipts.sha256_hex(out["answer"])
    assert (r["status"], r["epsilon_charged"], r["epsilon_remaining"]) == ("answered", 1.0, 9.0)
    assert receipts.signature_ok(r)  # signed from Day 6
    assert out["receipt_hash"] == receipts.receipt_hash(r)
    assert client.get("/budget").json()["epsilon_remaining"] == 9.0
    assert client.get("/receipt/1").json() == {"receipt": r, "receipt_hash": out["receipt_hash"]}


def test_same_question_again_is_free_and_identical(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    first = ask(client)
    # A new question, then the same one written differently ("=" with one
    # category is the same as "in" with a list of one), so it hashes the same.
    again = ask(client, buyer="mallory.testnet", filters=[["age_band", "in", ["65-79"]]])
    third = ask(client, filters=[["age_band", "=", "65-79"]])
    assert again["receipt"]["spec_hash"] != first["receipt"]["spec_hash"]  # a different question
    assert third["replay_of"] == again["request_id"]
    assert third["answer"] == again["answer"] and third["receipt"]["epsilon_charged"] == 0.0
    assert third["receipt"]["answer_hash"] == again["receipt"]["answer_hash"]
    assert client.get("/budget").json()["epsilon_spent"] == 2.0  # first and second only


def test_averaging_attack_gets_one_noise_draw(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    outs = [ask(client, buyer=f"sybil{i}.testnet") for i in range(25)]
    values = {tuple(c["value"] for c in o["answer"]["cells"]) for o in outs}
    assert len(values) == 1  # every copy carries the same noise, so averaging learns nothing
    assert all(o["replay_of"] == 1 for o in outs[1:])
    assert client.get("/budget").json()["epsilon_spent"] == 1.0


@pytest.mark.parametrize("spec, rule", [
    ({**BY_SEX, "select": ["patient_id"]}, "not_aggregate"),
    ({**BY_SEX, "table": "visits"}, "off_schema"),
    ([1, 2, 3], "off_schema"),
    ({**BY_SEX, "group_by": ["sex", "age_band", "zip3"]}, "too_fine"),
    ({**BY_SEX, "epsilon": 1.5}, "over_budget"),
])
def test_refusals_get_a_receipt_and_cost_nothing(tiny_db, tmp_path, spec, rule):
    _, client = make(tiny_db, tmp_path)
    r = client.post("/query", json={"buyer": "alice.testnet", "spec": spec})
    out = r.json()
    assert out["status"] == "refused" and out["answer"] is None and out["refusal"]["rule"] == rule
    assert out["receipt"]["refusal"] == out["refusal"] and out["receipt"]["answer_hash"] is None
    assert out["receipt"]["epsilon_charged"] == 0.0 and out["receipt"]["epsilon_remaining"] == 10.0
    assert client.get("/budget").json()["epsilon_spent"] == 0.0


def test_attack_example_is_refused(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    spec = json.loads((EXAMPLES / "attack_one_patient.json").read_text())
    assert ask(client, spec)["refusal"]["rule"] == "too_fine"


def test_budget_runs_out_and_the_dataset_closes(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path, epsilon_total=2.5)
    assert ask(client, epsilon=1.0)["status"] == "answered"
    assert ask(client, epsilon=0.9)["status"] == "answered"
    over = ask(client, epsilon=0.7)
    assert over["refusal"]["rule"] == "over_budget" and "0.6 left" in over["refusal"]["reason"]
    assert ask(client, epsilon=0.6)["receipt"]["epsilon_remaining"] == 0.0
    assert client.get("/budget").json()["closed"] is True
    closed = ask(client, epsilon=0.1)
    assert closed["refusal"]["rule"] == "over_budget" and "closed" in closed["refusal"]["reason"]
    assert ask(client, epsilon=1.0)["replay_of"] == 1  # answers already paid for stay available


def test_rate_limit_per_buyer_per_hour(tiny_db, tmp_path):
    clock = Clock()
    start = clock.now
    _, client = make(tiny_db, tmp_path, clock=clock)
    for _ in range(LIMIT):
        assert ask(client)["status"] == "answered"  # replays count too
        clock.now += 60
    limited = ask(client)
    assert limited["refusal"]["rule"] == "rate_limited" and limited["refusal"]["rule_number"] == 5
    assert ask(client, buyer="bob.testnet")["status"] == "answered"
    clock.now = start + 3600  # the first request is now an hour old and no longer counts
    assert ask(client)["status"] == "answered"
    assert ask(client)["refusal"]["rule"] == "rate_limited"


def test_budget_and_cache_survive_a_restart(tiny_db, tmp_path):
    engine, client = make(tiny_db, tmp_path)
    first = ask(client)
    engine.close()
    _, client = make(tiny_db, tmp_path)
    assert client.get("/budget").json()["epsilon_remaining"] == 9.0
    assert ask(client)["answer"] == first["answer"]


def test_bad_requests(tiny_db, tmp_path):
    _, client = make(tiny_db, tmp_path)
    assert client.post("/query", json={"buyer": "Not A Name!", "spec": BY_SEX}).status_code == 422
    assert client.post("/query", json={"spec": BY_SEX}).status_code == 422
    assert client.get("/receipt/99").status_code == 404


@needs_real_db
def test_demo_question_end_to_end(tmp_path):
    engine = Blindfold(load.DEFAULT_DB, load.DEFAULT_SCHEMA, tmp_path / "state.sqlite", 10)
    client = TestClient(create_app(engine))
    spec = json.loads((EXAMPLES / "readmit_patients_diabetes_65plus_by_sex.json").read_text())
    out = ask(client, spec)
    assert out["status"] == "answered"
    assert [abs(c["value"] - t) < 0.06 for c, t in zip(out["answer"]["cells"], (22 / 205, 23 / 191))] == [True, True]