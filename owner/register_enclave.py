"""Register the running engine's enclave on the Blindfold contract (admin only).

    python owner/register_enclave.py --contract blindfold.ryan-tobin.testnet \
        --admin ryan-tobin.testnet --engine http://127.0.0.1:8000

1. Reads the engine's /attestation: its NEAR account (the implicit account
   of the signer key), public keys, compose hash and quote hash.
2. Sends that account 1 NEAR from the admin, so it exists and can pay gas
   for settle. Skip with --no-fund once it has NEAR.
3. Calls register_enclave, so only this enclave can settle requests.

Both steps use near-cli-rs with the admin's key from the legacy keychain.
--dry-run prints the commands without sending them.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import urllib.request
from pathlib import Path

from blindfold import attest


def run(cmd: list[str], dry_run: bool) -> None:
    print(shlex.join(cmd))
    if not dry_run and subprocess.run(cmd).returncode != 0:
        raise SystemExit(f"failed: {cmd[1]} {cmd[2]}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID"), help="contract account (or CONTRACT_ID)")
    p.add_argument("--admin", required=True, help="the contract's admin account, which signs")
    p.add_argument("--engine", default="http://127.0.0.1:8000")
    p.add_argument("--fund", default="1 NEAR", help="NEAR sent to the enclave account for gas")
    p.add_argument("--no-fund", action="store_true")
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    p.add_argument("--compose", type=Path, default=Path(__file__).resolve().parents[1] / "docker-compose.tee.yml", help="the compose file the enclave should run (docker-compose.mainnet.yml for mainnet)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if not a.contract:
        raise SystemExit("set --contract or CONTRACT_ID")

    with urllib.request.urlopen(a.engine.rstrip("/") + "/attestation", timeout=30) as r:
        att = json.loads(r.read())
    print(f"enclave mode {att['mode']}, account {att['account_id']}")
    results = attest.check(att, a.compose.read_text())
    for ok, text in results:
        print({True: "✓", None: "!", False: "✗"}[ok], text)
    if any(ok is False for ok, _ in results):
        raise SystemExit("the attestation failed: not registering this enclave")
    if att["mode"] != "tdx":
        print("warning: not a real TEE. Fine for testnet; the receipts will say so.")

    sign = ["network-config", a.network, "sign-with-legacy-keychain", "send"]
    if not a.no_fund:
        run(["near", "tokens", a.admin, "send-near", att["account_id"], a.fund] + sign, a.dry_run)
    args = {
        "account_id": att["account_id"],
        "signer_pubkey": att["signer_pubkey"],
        "encryption_pubkey": att["encryption_pubkey"],
        "compose_hash": att["compose_hash"] or "",
        "quote_hash": att["quote_hash"] or "",
    }
    run(["near", "contract", "call-function", "as-transaction", a.contract, "register_enclave",
         "json-args", json.dumps(args), "prepaid-gas", "30 Tgas", "attached-deposit", "0 NEAR",
         "sign-as", a.admin] + sign, a.dry_run)


if __name__ == "__main__":
    main()