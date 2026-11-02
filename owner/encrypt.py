"""Encrypt Riverside to the enclave's key, so only the enclave can read it (data owner).

    python owner/encrypt.py --engine https://<app-id>-8000.<gateway> \
        --contract blindfold.ryan-tobin.testnet --owner ryan-tobin.testnet

1. Reads the engine's /attestation and checks it first (engine/blindfold/attest.py):
   a genuine Intel TDX quote, running docker-compose.tee.yml, whose quote
   binds the encryption key. With --contract, the key must also be the
   registered enclave's. A simulated enclave is refused unless --allow-simulated.
2. Packs the five tables of data/riverside.duckdb (Parquet, in one tar) and
   seals them to the enclave's X25519 key: data/riverside.bundle.
3. With --owner, lists the bundle's SHA-256 on the contract (update_dataset),
   so the enclave will load this bundle and nothing else.

Then upload data/riverside.bundle to the BUNDLE_URL the enclave reads.
The ciphertext is safe to publish: only the enclave can open it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

from blindfold import attest, bundle, load
from blindfold.near import RPC_URLS, Near

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", required=True, help="the enclave's URL")
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID") or None)
    p.add_argument("--owner", help="the dataset owner's account: also run update_dataset with the new hash")
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    p.add_argument("--db", type=Path, default=load.DEFAULT_DB)
    p.add_argument("--out", type=Path, default=ROOT / "data" / "riverside.bundle")
    p.add_argument("--allow-simulated", action="store_true", help="for local tests with the simulator")
    a = p.parse_args()

    req = urllib.request.Request(a.engine.rstrip("/") + "/attestation", headers={"User-Agent": "blindfold-owner"})
    with urllib.request.urlopen(req, timeout=30) as r:
        att = json.loads(r.read())
    print(f"enclave mode {att['mode']}, encryption key {att['encryption_pubkey']}")
    results = attest.check(att, (ROOT / "docker-compose.tee.yml").read_text())
    for ok, text in results:
        print({True: "✓", None: "!", False: "✗"}[ok], text)
    if any(ok is False for ok, _ in results):
        sys.exit("the attestation failed: not encrypting")
    if att["mode"] != "tdx" and not a.allow_simulated:
        sys.exit("not a real TEE: refusing to encrypt (use --allow-simulated for a local test)")

    schema_hash = hashlib.sha256(load.DEFAULT_SCHEMA.read_bytes()).hexdigest()
    if schema_hash != att["schema_hash"]:
        sys.exit(f"the enclave serves schema {att['schema_hash']}, but yours is {schema_hash}")
    if a.contract:
        near = Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[a.network])
        enclave = near.view(a.contract, "get_enclave", {})
        if not enclave or enclave["encryption_pubkey"] != att["encryption_pubkey"]:
            sys.exit(f"this enclave isn't the one registered on {a.contract}: run `make register-enclave` first")
        print(f"✓ the key is the enclave registered on {a.contract}")

    plain = bundle.pack(a.db, "riverside", schema_hash)
    sealed = bundle.seal(plain, att["encryption_pubkey"])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(sealed)
    digest = bundle.sha256(sealed)
    print(f"wrote {a.out} ({len(sealed) / 1e6:.1f} MB), sha256 {digest}")

    if a.owner and a.contract:
        cmd = ["near", "contract", "call-function", "as-transaction", a.contract, "update_dataset",
               "json-args", json.dumps({"id": "riverside", "bundle_hash": digest}), "prepaid-gas", "30 Tgas",
               "attached-deposit", "0 NEAR", "sign-as", a.owner, "network-config", a.network,
               "sign-with-legacy-keychain", "send"]
        print(shlex.join(cmd))
        if subprocess.run(cmd).returncode != 0:
            sys.exit("update_dataset failed")
    print("next: upload the bundle to the enclave's BUNDLE_URL, then POST /bundle on the engine")


if __name__ == "__main__":
    main()