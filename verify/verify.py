"""Check a Blindfold receipt: its signature, the enclave that signed it, and the chain.

    python verify/verify.py --engine http://127.0.0.1:8000 --request 0
    python verify/verify.py receipt.json --engine URL --contract blindfold.ryan-tobin.testnet

Checks, in order:
1. the receipt hash, recomputed from the receipt
2. the ed25519 signature, with the signer key named in the receipt
3. that key is the engine's enclave key (its /attestation)
4. the attestation (engine/blindfold/attest.py): report_data binds the keys
   and the schema; for a real TDX enclave, the quote is signed by Intel, the
   event log replays to RTMR3, and the compose hash in it is our
   docker-compose.tee.yml
5. the planner: if NEAR AI wrote the spec, its signature verifies with the
   model's TEE key; with NEAR_AI_API_KEY set, that key is in a fresh
   attestation from NEAR AI and its Intel quote checks out
6. with a contract: the signer, compose hash and quote are the registered

A simulated or dev enclave passes in yellow, never green: its quote proves
nothing. Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from blindfold import attest, receipts
from blindfold.planner import NearAI, PlanError, signature_ok
from blindfold.near import RPC_URLS, Near

GREEN, YELLOW, RED, END = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
TEE_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.tee.yml"
failed = False


def check(ok: bool | None, text: str) -> None:
    """ok True = pass, None = pass with a warning, False = fail."""
    global failed
    mark = {True: f"{GREEN}✓", None: f"{YELLOW}!", False: f"{RED}✗"}[ok]
    print(f"{mark} {text}{END}")
    failed |= ok is False


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "blindfold-verify"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("receipt", nargs="?", help="a receipt JSON file (the receipt, or a /query or /receipt reply)")
    p.add_argument("--engine", help="engine URL, e.g. http://127.0.0.1:8000")
    p.add_argument("--request", type=int, help="fetch this request's receipt from the engine")
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID") or None)
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    p.add_argument("--compose", type=Path, default=TEE_COMPOSE, help="the compose file the enclave should run")
    p.add_argument("--pccs", default=attest.PHALA_PCCS, help="collateral server, or https://api.trustedservices.intel.com")
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

    ms = r.get("model_signature")
    if not ms:
        check(None, "no model signature: the spec came from the offline stub or from the buyer")
    else:
        check(signature_ok(ms["text"], ms["signature"], ms["signing_address"], ms["signing_algo"]),
              f"the spec was planned by {ms['model']}, signed by its key {ms['signing_address']}")
        key = os.environ.get("NEAR_AI_API_KEY")
        if key and ms["kind"] == "provider_tee":
            try:
                fresh = NearAI(key, ms["model"]).attest()
                check(fresh["signing_address"] == ms["signing_address"], "NEAR AI attests that key today (fresh nonce)")
                quote = fresh["report"]["model_attestations"][0]["intel_quote"]
                ok, text = attest.intel_check(bytes.fromhex(quote.removeprefix("0x")), a.pccs)
                check(ok, f"the model's TEE: {text}")
            except PlanError as e:
                check(False, f"couldn't check the model's attestation: {e}")
        else:
            check(None, "set NEAR_AI_API_KEY to check the model's key against a fresh NEAR AI attestation")


    att = info = None
    if a.engine:
        att = get_json(f"{a.engine.rstrip('/')}/attestation")
        info = get_json(f"{a.engine.rstrip('/')}/info")
        check(att["signer_pubkey"] == r["signer_pubkey"], "signer is the engine's enclave key")
        compose = a.compose.read_text() if a.compose.exists() else None
        for ok, text in attest.check(att, compose, a.pccs):
            check(ok, text)
        if att["mode"] == "tdx":
            check(r.get("compose_hash") == att["compose_hash"], "the receipt names this compose hash")

    if a.contract:
        chain = Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[a.network])
        enclave = chain.view(a.contract, "get_enclave", {})
        check(bool(enclave) and enclave["signer_pubkey"] == r["signer_pubkey"],
              f"signer is the enclave registered on {a.contract}")
        if enclave and r.get("compose_hash"):
            check(enclave["compose_hash"] == r["compose_hash"], "compose hash matches the registered one")
        if enclave and att and att.get("quote_hash"):
            same = enclave["quote_hash"] == att["quote_hash"]
            check(True if same else None, "quote hash matches the registered one" if same
                  else "the quote changed since registration (normal after a restart; keys and code are checked above)")
        if info and info["data"]["source"] == "bundle":
            ds = chain.view(a.contract, "get_dataset", {"id": info["dataset"]})
            check(bool(ds) and ds["bundle_hash"] == info["data"]["bundle_hash"],
                  f"the engine loaded the bundle listed on-chain ({info['data']['bundle_hash'][:16]}…)")
        req = chain.view(a.contract, "get_request", {"id": str(r["request_id"])})
        check(bool(req) and req["receipt_hash"] == h and req["status"] == r["status"],
              f"request {r['request_id']} settled on-chain with this receipt hash")

    print(f"{RED}FAILED{END}" if failed else f"{GREEN}all checks passed{END}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()