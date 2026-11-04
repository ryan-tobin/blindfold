"""Pay for one question on the contract, then ask the engine for the answer.

    python agent/pay_and_ask.py --buyer alice.testnet --spec datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json
    python agent/pay_and_ask.py --buyer alice.testnet --question "How many patients have hypertension, by age band?"

0. With --question: POST /plan turns it into a QuerySpec (free) and prints it
   in plain words. A question the engine would refuse stops here, unless
   --pay-anyway (a refusal is refunded on settle).
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

from blindfold import load, receipts
from blindfold.chain import to_millionths
from blindfold.near import RPC_URLS, Near, b58decode
from blindfold.refuse import Refused
from blindfold.spec import validate


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--buyer", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--spec", help="a QuerySpec JSON file")
    src.add_argument("--question", help="a question in plain English, planned by the engine")
    p.add_argument("--pay-anyway", action="store_true", help="pay even if the plan says it will be refused")
    p.add_argument("--engine", default="http://127.0.0.1:8000")
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID"))
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    a = p.parse_args()
    if not a.contract:
        raise SystemExit("set --contract or CONTRACT_ID")

    if a.question:
        plan = post(a.engine, "/plan", {"question": a.question})
        print(f"plan ({plan['planner']}{', ' + plan['model'] if plan['model'] else ''}): "
              f"{plan['summary'] or json.dumps(plan['spec'])}")
        if plan["will_refuse"]:
            print(f"the engine will refuse this: {plan['will_refuse']['reason']}")
            if not plan["spec"] or not a.pay_anyway:
                raise SystemExit("not paying (use --pay-anyway to send it and get a signed refusal)")
        raw = plan["spec"]
    else:
        raw = json.loads(Path(a.spec).read_text())
    schema = load.load_schema(load.DEFAULT_SCHEMA)
    try:
        spec = validate(raw, schema)
        dataset, spec_hash, eps = spec.dataset, receipts.spec_hash(spec.canonical_json()), spec.epsilon
    except Refused:
        if not a.pay_anyway:
            raise
        # A spec that breaks the rules has no canonical form: the engine hashes it raw and refuses it
        dataset, spec_hash = raw["dataset"], receipts.sha256_hex(raw)
        eps = raw.get("epsilon") or schema["query_rules"]["default_epsilon"].get(raw.get("measure"), 0.1)
    epsilon = to_millionths(Fraction(str(eps)))

    creds = json.loads((Path.home() / ".near-credentials" / a.network / f"{a.buyer}.json").read_text())
    key = SigningKey(b58decode(creds["private_key"].removeprefix("ed25519:"))[:32])
    near = Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[a.network])
    tx, request_id = near.call(key, a.buyer, a.contract, "request_query",
                               {"dataset_id": dataset, "spec_hash": spec_hash, "epsilon": epsilon})
    print(f"paid: request {request_id}, tx {tx}")

    out = post(a.engine, "/query", {"request_id": int(request_id), "spec": raw})
    print(json.dumps(out, indent=2, ensure_ascii=False))


def post(engine: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(engine.rstrip("/") + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"engine said {e.code}: {e.read().decode()}") from e


if __name__ == "__main__":
    main()