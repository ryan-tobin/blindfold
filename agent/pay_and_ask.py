"""Pay for one question on the contract, then ask the engine for the answer.

    python agent/pay_and_ask.py --buyer alice.testnet --spec datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json

1. Checks the QuerySpec and computes its canonical hash and epsilon.
2. Calls request_query on the contract, signed with the buyer's key from
   ~/.near-credentials/<network>/<buyer>.json (near-cli's legacy keychain).
   The contract escrows the price and returns a request id.
3. POSTs {"request_id", "spec"} to the engine, which answers and settles.

The full buyer agent, on a function-call key, arrives on Day 9.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from fractions import Fraction
from pathlib import Path

from nacl.signing import SigningKey

from engine.blindfold import load, receipts
from engine.blindfold.chain import to_millionths
from engine.blindfold.near import RPC_URLS, Near, b58decode
from engine.blindfold.spec import validate


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--buyer", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--engine", default="http://127.0.0.1:8000")
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID"))
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    a = p.parse_args()
    if not a.contract:
        raise SystemExit("set --contract or CONTRACT_ID")

    raw = json.loads(Path(a.spec).read_text())
    spec = validate(raw, load.load_schema(load.DEFAULT_SCHEMA))
    spec_hash = receipts.spec_hash(spec.canonical_json())
    epsilon = to_millionths(Fraction(str(spec.epsilon)))

    creds = json.loads((Path.home() / ".near-credentials" / a.network / f"{a.buyer}.json").read_text())
    key = SigningKey(b58decode(creds["private_key"].removeprefix("ed25519:"))[:32])
    near = Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[a.network])
    tx, request_id = near.call(key, a.buyer, a.contract, "request_query",
                               {"dataset_id": spec.dataset, "spec_hash": spec_hash, "epsilon": epsilon})
    print(f"paid: request {request_id}, tx {tx}")

    body = json.dumps({"request_id": int(request_id), "spec": raw}).encode()
    req = urllib.request.Request(a.engine.rstrip("/") + "/query", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            print(json.dumps(json.loads(r.read()), indent=2, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"engine said {e.code}: {e.read().decode()}") from e


if __name__ == "__main__":
    main()