"""Day 4: the budget ledger, replay cache, rate-limit counter and receipts.

    cd engine && python -m pytest -q tests/test_budget.py
"""

from __future__ import annotations

from fractions import Fraction

from blindfold import receipts
from blindfold.budget import State

ANSWER = {"measure": "count", "cells": [{"cell": {}, "value": 512, "display": "512 ± 30"}]}


def receipt_for(request_id):
    return {"request_id": request_id}


def charge(state, spec_hash, eps, buyer="alice.testnet", ts=0.0, dataset="riverside"):
    return state.log(dataset, buyer, ts, "answered", None, receipt_for, (spec_hash, Fraction(eps), ANSWER))


def test_fresh_state_has_spent_nothing():
    state = State(":memory:")
    assert state.spent("riverside") == 0
    assert state.cached("riverside", "abc") is None
    assert state.receipt(1) is None


def test_costs_add_up_exactly():
    state = State(":memory:")
    for i in range(100):
        charge(state, f"spec{i}", "1/10")
    assert state.spent("riverside") == 10  # exactly: Fractions, not floats
    total = 0.0
    for _ in range(100):
        total += 0.1
    assert total != 10  # a float running total ends at 9.99999999999998


def test_replay_cache_returns_the_stored_answer_and_who_paid():
    state = State(":memory:")
    first = charge(state, "h1", "1/5")
    hit = state.cached("riverside", "h1")
    assert hit.request_id == first["request_id"] and hit.answer == ANSWER
    assert state.cached("riverside", "h2") is None
    assert state.cached("other", "h1") is None


def test_refusals_cost_nothing():
    state = State(":memory:")
    state.log("riverside", "alice.testnet", 0.0, "refused", "too_fine", receipt_for)
    assert state.spent("riverside") == 0
    assert state.receipt(1) == {"request_id": 1}


def test_rate_counter_counts_one_buyer_in_the_window():
    state = State(":memory:")
    charge(state, "a", "1/10", ts=100.0)
    state.log("riverside", "alice.testnet", 200.0, "refused", "too_fine", receipt_for)
    state.log("riverside", "alice.testnet", 300.0, "refused", "rate_limited", receipt_for)
    charge(state, "b", "1/10", buyer="bob.testnet", ts=300.0)
    assert state.recent("alice.testnet", since=0.0) == 2  # answered + refused, not rate_limited
    assert state.recent("alice.testnet", since=150.0) == 1
    assert state.recent("bob.testnet", since=0.0) == 1


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    state = State(path)
    charge(state, "h1", "3/10")
    state.close()
    again = State(path)
    assert again.spent("riverside") == Fraction(3, 10)
    assert again.cached("riverside", "h1").answer == ANSWER
    assert again.receipt(1) == {"request_id": 1}


def test_a_failed_receipt_rolls_back_the_charge():
    state = State(":memory:")

    def broken(request_id):
        raise RuntimeError("boom")

    try:
        state.log("riverside", "alice.testnet", 0.0, "answered", None, broken, ("h1", Fraction(1), ANSWER))
    except RuntimeError:
        pass
    assert state.spent("riverside") == 0 and state.receipt(1) is None


def test_receipt_hash_ignores_sig_and_key_order():
    r = receipts.build(7, "riverside", "ab" * 32, "answered", ANSWER, Fraction(1, 5), Fraction(49, 5), None, None, 0.0)
    assert r["answer_hash"] == receipts.sha256_hex(ANSWER)
    assert r["epsilon_charged"] == 0.2 and r["epsilon_remaining"] == 9.8
    assert r["ts"] == "1970-01-01T00:00:00Z" and r["sig"] is None
    signed = {**r, "sig": "ed25519:something"}
    shuffled = dict(reversed(list(r.items())))
    assert receipts.receipt_hash(r) == receipts.receipt_hash(signed) == receipts.receipt_hash(shuffled)
    assert receipts.receipt_hash({**r, "epsilon_charged": 0.3}) != receipts.receipt_hash(r)