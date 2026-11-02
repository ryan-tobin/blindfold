"""Check a Blindfold receipt: its signature, the enclave that signed it, and the chain.

    python verify/verify.py --engine http://127.0.0.1:8000 --request 0
    python verify/verify.py receipt.json --engine URL --contract blindfold.ryan-tobin.testnet

Checks, in order:
1. the receipt hash, recomputed from the receipt
2. the ed25519 signature, with the signer key named in the receipt
3. that key is the enclave's: the engine's /attestation and, with a contract,
   the enclave registered on-chain
4. the attestation binds the keys and the schema: report_data = sha256(signer
   + encryption key + schema hash), and the quote carries that report_data
5. with a contract: the request was settled with this exact receipt hash

A simulated or dev enclave passes in yellow, never green: its quote proves
nothing. Checking a real quote against Intel's certificates arrives on Day 7.
Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request

from blindfold import receipts
from blindfold.near import RPC_URLS, Near, b58decode

GREEN, YELLOW, RED, END = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
QUOTE_REPORT_DATA = 568  # TDX v4 quote: 48-byte header, report_data at byte 520 of the body
failed = False


def check(ok: bool | None, text: str) -> None:
    """ok True = pass, None = pass but simulated, False = fail."""
    global failed
    mark = {True: f"{GREEN}✓", None: f"{YELLOW}!", False: f"{RED}✗"}[ok]
    print(f"{mark} {text}{END}")
    failed |= ok is False


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("receipt", nargs="?", help="a receipt JSON file (the receipt, or a /query or /receipt reply)")
    p.add_argument("--engine", help="engine URL, e.g. http://127.0.0.1:8000")
    p.add_argument("--request", type=int, help="fetch this request's receipt from the engine")
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID") or None)
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    a = p.parse_args()

    if a.receipt:
        with open(a.receipt) as f:
            doc = json.load(f)
    elif a.engine and a.request is not None:
        doc = get_json(f"{a.engine.rstrip('/')}/receipt/{a.request}")
    else:
        p.error("give a receipt file, or --engine and --request")
    r = doc.get("receipt", doc)
    h = receipts.receipt_hash(r)
    print(f"receipt {r['request_id']} ({r['status']}), hash {h}")

    check(doc.get("receipt_hash", h) == h, "receipt hash matches its contents")
    check(receipts.signature_ok(r), f"signature valid for {r['signer_pubkey']}")

    if a.engine:
        att = get_json(f"{a.engine.rstrip('/')}/attestation")
        check(att["signer_pubkey"] == r["signer_pubkey"], "signer is the engine's enclave key")
        signer = b58decode(att["signer_pubkey"].removeprefix("ed25519:"))
        expected = hashlib.sha256(signer + bytes.fromhex(att["encryption_pubkey"]) + bytes.fromhex(att["schema_hash"]))
        check(att["report_data"] == expected.hexdigest(), "report_data = sha256(signer key + encryption key + schema hash)")
        if att["quote"]:
            quote = bytes.fromhex(att["quote"])
            in_quote = quote[QUOTE_REPORT_DATA:QUOTE_REPORT_DATA + 32]
            check(in_quote == expected.digest(), "the quote carries that report_data")
        real = att["mode"] == "tdx"
        check(True if real else None, f"enclave mode: {att['mode']}" + ("" if real else " (not a real TEE, the quote proves nothing)"))

    if a.contract:
        chain = Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[a.network])
        enclave = chain.view(a.contract, "get_enclave", {})
        check(bool(enclave) and enclave["signer_pubkey"] == r["signer_pubkey"],
              f"signer is the enclave registered on {a.contract}")
        if enclave and r.get("compose_hash"):
            check(enclave["compose_hash"] == r["compose_hash"], "compose hash matches the registered one")
        req = chain.view(a.contract, "get_request", {"id": str(r["request_id"])})
        check(bool(req) and req["receipt_hash"] == h and req["status"] == r["status"],
              f"request {r['request_id']} settled on-chain with this receipt hash")

    print(f"{RED}FAILED{END}" if failed else f"{GREEN}all checks passed{END}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()