"""Shared helpers for the buyer's scripts: wallet.py, buyer.py and fund.py.

These run on the buyer's machine, never in the enclave, so they live
outside the engine package (a change here doesn't change the enclave image).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from nacl.signing import SigningKey

from blindfold.near import RPC_URLS, Near, b58decode, b58encode, public_key

ROOT = Path(__file__).resolve().parents[1]
USDC = {  # Circle's native USDC on NEAR (NEP-141, 6 decimals)
    "testnet": "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af",
    "mainnet": "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
}
EXPLORER = {"testnet": "https://testnet.nearblocks.io/txns/", "mainnet": "https://nearblocks.io/txns/"}
KEY_DIR = Path(os.environ.get("BLINDFOLD_AGENT_KEYS", Path.home() / ".blindfold" / "agent-keys"))
ONE_NEAR = 10**24


def near_client(network: str) -> Near:
    return Near(os.environ.get("NEAR_RPC_URL") or RPC_URLS[network])


def usdc_units(text: str) -> int:
    """'0.25' -> 250000. USDC has 6 decimals; anything finer is refused."""
    x = Decimal(text) * 10**6
    if x != x.to_integral_value() or x < 0:
        raise SystemExit(f"{text}: USDC amounts go down to 0.000001")
    return int(x)


def _trim(text: str) -> str:
    """'0.250000' -> '0.25', '20.000000' -> '20', but '20' stays '20'."""
    return text.rstrip("0").rstrip(".") if "." in text else text


def usdc_text(units: int | str) -> str:
    """250000 -> '0.25'."""
    return _trim(f"{Decimal(int(units)) / 10**6:f}")


def near_text(yocto: int | str) -> str:
    """Yocto NEAR as near-cli's text, e.g. 1250000000000000000000 -> '0.00125 NEAR'."""
    return _trim(f"{Decimal(int(yocto)) / ONE_NEAR:f}") + " NEAR"


def tx_link(network: str, tx: str | None) -> str:
    return EXPLORER[network] + tx if tx else "-"


# --- The agent's key: a function-call key that can only call request_query ---

def key_path(network: str, buyer: str) -> Path:
    return KEY_DIR / network / f"{buyer}.json"


def new_agent_key(network: str, buyer: str) -> SigningKey:
    """Make the agent's key once, readable only by you (chmod 600)."""
    path = key_path(network, buyer)
    if path.exists():
        return load_agent_key(network, buyer)
    key = SigningKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = "ed25519:" + b58encode(bytes(key) + bytes(key.verify_key))
    path.write_text(json.dumps({"account_id": buyer, "public_key": public_key(key), "private_key": secret}))
    path.chmod(0o600)
    return key


def load_agent_key(network: str, buyer: str) -> SigningKey:
    path = key_path(network, buyer)
    if not path.exists():
        raise SystemExit(f"no agent key at {path}. Make one with:\n"
                         f"    python agent/wallet.py agent-key --buyer {buyer}")
    data = json.loads(path.read_text())
    return SigningKey(b58decode(data["private_key"].removeprefix("ed25519:"))[:32])


def key_permission(near: Near, account: str, key: SigningKey) -> dict | str | None:
    """'FullAccess', {'FunctionCall': {...}}, or None if the key isn't on the account."""
    try:
        ak = near.rpc("query", {"request_type": "view_access_key", "finality": "final",
                                "account_id": account, "public_key": public_key(key)})
    except Exception:
        return None
    return ak["permission"]


def agent_key_problem(permission, contract: str) -> str | None:
    """Why this key isn't a safe agent key, or None if it is."""
    if permission is None:
        return "the key isn't on the account yet (python agent/wallet.py agent-key adds it)"
    if permission == "FullAccess":
        return "it's a full-access key: an agent should only hold a function-call key"
    fc = permission.get("FunctionCall") or {}
    if fc.get("receiver_id") != contract:
        return f"it can call {fc.get('receiver_id')}, not {contract}"
    if fc.get("method_names") != ["request_query"]:
        return f"it can call {fc.get('method_names') or 'any method'}, not only request_query"
    return None


# --- HTTP ---------------------------------------------------------------------

def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None,
              timeout: float = 180):
    """JSON in and out. Raises SystemExit with the server's reply on an HTTP error."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "blindfold-agent",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url.split('?')[0]} said {e.code}: {e.read().decode()[:600]}") from e