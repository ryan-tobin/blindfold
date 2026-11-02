"""Receipts: what the buyer keeps, and what the contract records.

Every request gets a receipt, answered or refused, signed by the enclave's
ed25519 key. model_signature stays null until the planner arrives (Day 8).

Hashes are SHA-256 of canonical JSON (sorted keys, no spaces, UTF-8), so
anyone can recompute them. receipt_hash covers every field except sig, the
enclave signs those same bytes, and settle stores receipt_hash on-chain.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from fractions import Fraction

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .near import b58decode, b58encode, public_key

VERSION = 1


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def spec_hash(canonical_json: str) -> str:
    """Hash of ValidSpec.canonical_json(): the same question always hashes the same."""
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build(request_id: int, dataset_id: str, spec_hash: str, status: str, answer: dict | None,
          epsilon_charged: Fraction, epsilon_remaining: Fraction, refusal: dict | None,
          replay_of: int | None, ts: float, compose_hash: str | None = None) -> dict:
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
        "compose_hash": compose_hash,
        "signer_pubkey": None,
        "ts": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sig": None,
    }


def receipt_hash(receipt: dict) -> str:
    return hashlib.sha256(unsigned_bytes(receipt)).hexdigest()


def unsigned_bytes(receipt: dict) -> bytes:
    return canonical({k: v for k, v in receipt.items() if k != "sig"})


def sign(receipt: dict, key: SigningKey) -> dict:
    """Fill in signer_pubkey and sig: ed25519 over the canonical receipt without sig."""
    receipt = {**receipt, "signer_pubkey": public_key(key)}
    receipt["sig"] = "ed25519:" + b58encode(key.sign(unsigned_bytes(receipt)).signature)
    return receipt


def signature_ok(receipt: dict) -> bool:
    """True if sig is a valid signature by signer_pubkey. Says nothing about who holds that key."""
    pub, sig = receipt.get("signer_pubkey") or "", receipt.get("sig") or ""
    if not (pub.startswith("ed25519:") and sig.startswith("ed25519:")):
        return False
    try:
        VerifyKey(b58decode(pub[8:])).verify(unsigned_bytes(receipt), b58decode(sig[8:]))
        return True
    except (BadSignatureError, ValueError):
        return False