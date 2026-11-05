"""Day 11: the accuracy benchmark's 200 questions, and one quick scoring run.

    cd engine && python -m pytest -q tests/test_bench.py
"""

from __future__ import annotations

import random

import duckdb
import pytest

from blindfold import bench, load, receipts
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)


def test_there_are_200_different_valid_questions():
    items = bench.questions(SCHEMA)
    assert len(items) == 200
    hashes = {receipts.spec_hash(validate(q["spec"], SCHEMA).canonical_json()) for q in items}
    assert len(hashes) == 200
    assert len({q["question"] for q in items}) == 200


def test_every_measure_is_covered():
    measures = {q["spec"]["measure"] for q in bench.questions(SCHEMA)}
    assert measures == set(SCHEMA["query_rules"]["measures"])


@pytest.mark.skipif(not load.DEFAULT_DB.exists(), reason="needs make data")
def test_scoring_a_few_questions(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "BENCHMARK", tmp_path / "benchmark.jsonl")
    con = duckdb.connect(str(load.DEFAULT_DB), read_only=True)
    items = bench.build(con, SCHEMA)[:6]
    out = bench.score(items, con, SCHEMA, 1.0, 3, random.Random(0))
    con.close()
    assert 0 <= out["median_error"] < 0.5
    assert 0 <= out["inside_error_bar"] <= 1
    assert len(out["per_question"]) == 6