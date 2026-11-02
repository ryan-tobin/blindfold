"""Receipts: what the buyer keeps, and what the contract records (Day 5).

Every request gets a receipt, answered or refused. Day 4 receipts are
unsigned: sig, signer_pubkey and compose_hash stay null until the enclave
signs them on Day 6, and model_signature until the planner arrives on Day 8.

Hashes are SHA-256 of canonical JSON (sorted keys, no spaces, UTF-8), so
anyone can recompute them. receipt_hash covers every field except sig, and
on Day 6 the enclave signs those same bytes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from fractions import Fraction

VERSION = 1

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",",":"),
                      ensure_ascii=False).encode("utf-8")

def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()

def spec_hash(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

def build(
        request_id: int,
        dataset_id: str,
        spec_hash: str,
        status: str,
        answer: dict | None,
        epsilon_charged: Fraction,
        epsilon_remaining: Fraction,
        refusal: dict | None,
        replay_of: int | None,
        ts: float
) -> dict:
    return {
        "v": VERSION,
        "request_id": request_id,
        "dataset_id": dataset_id,
        "spec_hash": spec_hash,
        "status": status,
        "answer_hash": sha256_hex(answer) if answer is not None else None,
        "epsilon_charged": float(epsilon_charged),
        "epsilon_remaining": float(epsilon_remaining),
        "refusal": refusal,
        "replay_of": replay_of,
        "model_signature": None,
        "compose_hash": None,
        "signer_pubkey": None,
        "ts": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sig": None,
    }

def receipt_hash(receipt: dict) -> str:
    return sha256_hex({k: v for k, v in receipt.items() if k != "sig"})