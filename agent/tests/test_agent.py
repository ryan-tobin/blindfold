"""The buyer's scripts, with a fake engine, a fake NEAR RPC and a fake 1Click."""

import datetime as dt
import json

import pytest

import buyer
import common
import fund
import wallet


def test_amounts():
    assert common.usdc_units("0.25") == 250000
    assert common.usdc_units("2") == 2_000_000
    assert common.usdc_text(250000) == "0.25"
    assert common.usdc_text("2000000") == "2"
    assert common.usdc_text(0) == "0"
    assert common.near_text(1250000000000000000000) == "0.00125 NEAR"
    assert common.usdc_text(20_000_000) == "20"  # whole numbers keep their zeros
    assert common.usdc_text(10_500_000) == "10.5"
    assert common.near_text(10 * common.ONE_NEAR) == "10 NEAR"
    with pytest.raises(SystemExit):
        common.usdc_units("0.0000001")


def test_agent_key_problem():
    c = "blindfold.ryan-tobin.testnet"
    ok = {"FunctionCall": {"allowance": "1", "receiver_id": c, "method_names": ["request_query"]}}
    assert common.agent_key_problem(ok, c) is None
    assert "full-access" in common.agent_key_problem("FullAccess", c)
    assert "isn't on the account" in common.agent_key_problem(None, c)
    other = {"FunctionCall": {"receiver_id": "evil.testnet", "method_names": ["request_query"]}}
    assert "evil.testnet" in common.agent_key_problem(other, c)
    anym = {"FunctionCall": {"receiver_id": c, "method_names": []}}
    assert "any method" in common.agent_key_problem(anym, c)


def test_agent_key_file(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "KEY_DIR", tmp_path)
    k1 = common.new_agent_key("testnet", "alice.testnet")
    k2 = common.new_agent_key("testnet", "alice.testnet")  # made once, then reused
    assert bytes(k1) == bytes(common.load_agent_key("testnet", "alice.testnet")) == bytes(k2)
    assert (tmp_path / "testnet" / "alice.testnet.json").stat().st_mode & 0o777 == 0o600


DATASET = {"id": "riverside", "price": "250000", "epsilon_total": 10_000_000, "epsilon_spent": 1_000_000,
           "epsilon_pending": 0, "active": True}
BALANCE = {"amount": "1000000", "per_query_max": "250000", "daily_max": "500000", "spent_today": "0"}


def test_precheck():
    assert buyer.precheck(250000, 1_000_000, BALANCE, DATASET) is None
    assert "per-question" in buyer.precheck(300000, 1_000_000, BALANCE, DATASET)
    assert "daily" in buyer.precheck(250000, 1_000_000, {**BALANCE, "spent_today": "300000"}, DATASET)
    assert "balance" in buyer.precheck(250000, 1_000_000, {**BALANCE, "amount": "100"}, DATASET)
    assert "budget" in buyer.precheck(250000, 9_500_000, BALANCE, DATASET)
    assert "closed" in buyer.precheck(250000, 1, BALANCE, {**DATASET, "active": False})
    assert buyer.precheck(0, 1, {"amount": "0", "per_query_max": "0", "daily_max": "0", "spent_today": "0"},
                          DATASET) is None  # free datasets need no balance


SPEC = {"dataset": "riverside", "table": "patients", "measure": "count", "epsilon": 0.5}


class FakeNear:
    def __init__(self):
        self.calls, self.requests = [], {}

    def rpc(self, method, params):
        assert params["request_type"] == "view_access_key"
        return {"permission": {"FunctionCall": {"receiver_id": "c.testnet", "method_names": ["request_query"]}}}

    def view(self, contract, method, args=None):
        if method == "get_dataset":
            return DATASET
        if method == "get_balance":
            return BALANCE
        if method == "get_request":
            return self.requests[int(args["id"])]
        raise AssertionError(method)

    def call(self, key, signer, contract, method, args):
        self.calls.append((signer, contract, method, args))
        rid = len(self.calls) - 1
        self.requests[rid] = {"status": "answered", "receipt_hash": f"{rid:064x}"}
        return f"tx{rid}", str(rid)


def fake_engine(refuse_second=False):
    seen = []

    def http(method, url, payload=None, headers=None, timeout=0):
        path = url.split("8000")[1]
        seen.append((path, payload))
        if path == "/schema":
            return {"tables": {}}
        if path == "/plan":
            refused = refuse_second and "patient 7" in payload["question"]
            return {"planner": "stub", "model": None, "spec": SPEC, "spec_hash": "ab" * 32, "summary": "count",
                    "epsilon": 0.5, "will_refuse": {"reason": "too fine"} if refused else None}
        if path == "/query":
            rid = payload.get("request_id", 0)
            receipt = {"epsilon_charged": 0.5, "model_signature": None}
            return {"request_id": rid, "status": "answered", "refusal": None, "receipt": receipt,
                    "receipt_hash": f"{rid:064x}", "settle_tx": f"settle{rid}",
                    "answer": {"measure": "count", "epsilon": 0.5, "level": 0.95,
                               "cells": [{"cell": {"sex": "female"}, "display": "1,000 ± 6"}]}}
        raise AssertionError(path)
    return http, seen


def test_buyer_pays_with_agent_key_and_reports(tmp_path, monkeypatch, capsys):
    near = FakeNear()
    http, seen = fake_engine(refuse_second=True)
    monkeypatch.setattr(buyer, "ROOT", tmp_path)
    monkeypatch.setattr(buyer, "http_json", http)
    monkeypatch.setattr(buyer, "near_client", lambda network: near)
    monkeypatch.setattr(buyer, "load_agent_key", lambda network, b: object())
    monkeypatch.setattr(buyer, "key_permission", lambda n, b, k: near.rpc("query", {"request_type": "view_access_key"})["permission"])
    monkeypatch.setattr("sys.argv", ["buyer.py", "--buyer", "alice.testnet", "--contract", "c.testnet",
                                     "How many patients?", "Where does patient 7 live?"])
    buyer.main()
    # one payment: the question the engine would refuse is never paid for
    assert near.calls == [("alice.testnet", "c.testnet", "request_query",
                           {"dataset_id": "riverside", "spec_hash": "ab" * 32, "epsilon": 500000})]
    assert ("/query", {"request_id": 0, "spec": SPEC}) in seen
    report = next((tmp_path / "data" / "reports").glob("*.md")).read_text()
    assert "paid for 1: 0.25 USDC and ε 0.5 spent" in report
    assert "receipt hash matches on-chain" in report
    assert "Not paid: the engine would refuse it (too fine)" in report
    saved = json.loads((tmp_path / "data" / "receipts" / "c.testnet-0.json").read_text())
    assert saved["receipt_hash"] == "0" * 64


def test_buyer_refuses_full_access_key(monkeypatch):
    monkeypatch.setattr(buyer, "http_json", fake_engine()[0])
    monkeypatch.setattr(buyer, "near_client", lambda network: None)
    monkeypatch.setattr(buyer, "load_agent_key", lambda network, b: object())
    monkeypatch.setattr(buyer, "key_permission", lambda n, b, k: "FullAccess")
    monkeypatch.setattr("sys.argv", ["buyer.py", "--buyer", "a.testnet", "--contract", "c.testnet", "q?"])
    with pytest.raises(SystemExit, match="full-access"):
        buyer.main()


def test_buyer_local_mode_pays_nothing(tmp_path, monkeypatch):
    http, seen = fake_engine()
    monkeypatch.setattr(buyer, "ROOT", tmp_path)
    monkeypatch.setattr(buyer, "http_json", http)
    monkeypatch.delenv("CONTRACT_ID", raising=False)
    monkeypatch.setattr("sys.argv", ["buyer.py", "--buyer", "alice.testnet", "How many patients?"])
    buyer.main()
    assert ("/query", {"buyer": "alice.testnet", "spec": SPEC}) in seen
    assert "local, unpaid" in next((tmp_path / "data" / "reports").glob("*.md")).read_text()


def test_quote_request():
    now = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc)
    body = fund.quote_request(2_000_000, "base", "ryan-tobin.near", "0xabc", dry=True, now=now)
    assert body == {
        "dry": True, "swapType": "EXACT_INPUT", "slippageTolerance": 100,
        "originAsset": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
        "depositType": "ORIGIN_CHAIN",
        "destinationAsset": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
        "amount": "2000000", "refundTo": "0xabc", "refundType": "ORIGIN_CHAIN",
        "recipient": "ryan-tobin.near", "recipientType": "DESTINATION_CHAIN",
        "deadline": "2026-11-04T13:00:00Z", "referral": "blindfold",
    }


def test_watch_until_success(monkeypatch, capsys):
    states = iter(["PENDING_DEPOSIT", "PENDING_DEPOSIT", "PROCESSING", "SUCCESS"])

    def http(method, url, payload=None, headers=None, timeout=0):
        assert url.endswith("/v0/status?depositAddress=0xdep")
        return {"status": next(states), "swapDetails": {"destinationChainTxHashes": [{"hash": "Nh"}]}}
    monkeypatch.setattr(fund, "http_json", http)
    monkeypatch.setattr(fund, "usdc_balance", lambda account: 1_990_000)
    assert fund.watch("0xdep", "ryan-tobin.near", sleep=lambda s: None) == "SUCCESS"
    out = capsys.readouterr().out
    assert out.count("PENDING_DEPOSIT") == 1 and "PROCESSING" in out
    assert "nearblocks.io/txns/Nh" in out and "1.99 USDC" in out


def test_describe_quote():
    q = {"quote": {"amountInFormatted": "2", "amountInUsd": "2.00", "amountOutFormatted": "1.99",
                   "amountOutUsd": "1.99", "minAmountOut": "1970000", "timeEstimate": 40,
                   "depositAddress": "0xdep", "deadline": "2026-11-04T13:00:00Z"}}
    text = fund.describe(q, "base")
    assert "at least 1.97" in text and "deposit to   0xdep" in text


class WalletNear:
    def view(self, contract, method, args=None):
        return {"storage_balance_bounds": {"min": "1250000000000000000000", "max": None},
                "storage_balance_of": None, "ft_balance_of": "5000000"}[method]

    def rpc(self, method, params):
        raise RuntimeError("no such key")


def make_wallet(monkeypatch, tmp_path, *argv):
    monkeypatch.setattr(wallet, "near_client", lambda n: WalletNear())
    monkeypatch.setattr(common, "KEY_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(wallet.Wallet, "send", lambda self, cmd, why: sent.append(cmd))
    monkeypatch.setattr("sys.argv", ["wallet.py", *argv, "--buyer", "alice.testnet", "--contract", "c.testnet"])
    monkeypatch.delenv("NEAR_NETWORK", raising=False)
    return sent


def test_wallet_register_both(monkeypatch, tmp_path):
    sent = make_wallet(monkeypatch, tmp_path, "register")
    wallet.main()
    assert [json.loads(c[6])["account_id"] for c in sent] == ["alice.testnet", "c.testnet"]
    assert all(c[3] == common.USDC["testnet"] and c[4] == "storage_deposit" and c[10] == "0.00125 NEAR" for c in sent)


def test_wallet_agent_key_is_request_query_only(monkeypatch, tmp_path):
    sent = make_wallet(monkeypatch, tmp_path, "agent-key")
    wallet.main()
    (cmd,) = sent
    assert cmd[:3] == ["account", "add-key", "alice.testnet"]
    assert cmd[cmd.index("--contract-account-id") + 1] == "c.testnet"
    assert cmd[cmd.index("--function-names") + 1] == "request_query"
    assert cmd[-1].startswith("ed25519:")


def test_wallet_deposit_needs_registered_contract(monkeypatch, tmp_path):
    make_wallet(monkeypatch, tmp_path, "deposit", "2")
    with pytest.raises(SystemExit, match="register first"):
        wallet.main()