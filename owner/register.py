"""List a dataset on the Blindfold contract.

    python owner/register.py --contract blindfold.ryan-tobin.testnet --owner ryan-tobin.testnet

Hashes the public schema.json (the same SHA-256 the engine serves at /schema)
and calls register_dataset with near-cli-rs, signed by the owner account from
the near-cli keychain. The owner gets paid for every answered question.
--dry-run prints the command without sending it.

Units on-chain: price in USDC's smallest unit and epsilon in millionths.
Both are 10^6, so 0.25 USDC -> 250000 and epsilon 10 -> 10000000.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MILLION = 1_000_000


def millionths(text: str) -> int:
    """'0.25' -> 250000, exactly. Refuses anything finer than 0.000001."""
    x = Fraction(text) * MILLION
    if x.denominator != 1 or x < 0:
        raise SystemExit(f"{text}: must be a non-negative multiple of 0.000001")
    return int(x)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID"), help="contract account (or CONTRACT_ID)")
    p.add_argument("--owner", required=True, help="data owner account that signs and gets paid")
    p.add_argument("--schema", default=str(ROOT / "datasets" / "riverside" / "schema.json"))
    p.add_argument("--price", default="0.25", help="USDC per question (default 0.25)")
    p.add_argument("--epsilon-total", default="10", help="total privacy budget (default 10)")
    p.add_argument("--bundle-hash", default="", help="SHA-256 of the encrypted bundle (Day 7)")
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if not a.contract:
        raise SystemExit("set --contract or CONTRACT_ID")

    schema_bytes = Path(a.schema).read_bytes()
    args = {
        "id": json.loads(schema_bytes)["dataset"],
        "schema_hash": hashlib.sha256(schema_bytes).hexdigest(),
        "bundle_hash": a.bundle_hash,
        "price": str(millionths(a.price)),
        "epsilon_total": millionths(a.epsilon_total),
    }
    cmd = ["near", "contract", "call-function", "as-transaction", a.contract, "register_dataset",
           "json-args", json.dumps(args), "prepaid-gas", "30 Tgas", "attached-deposit", "0 NEAR",
           "sign-as", a.owner, "network-config", a.network, "sign-with-legacy-keychain", "send"]
    print(shlex.join(cmd))
    if not a.dry_run:
        raise SystemExit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()