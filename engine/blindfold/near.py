"""A small NEAR client: contract view calls and signed function calls.

Standard library plus PyNaCl. It speaks JSON-RPC over HTTPS, writes the
transaction in borsh (NEAR's binary format) by hand, and signs it with
ed25519. It only does what Blindfold needs: views, and transactions with
one FunctionCall action.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import urllib.request

from nacl.signing import SigningKey

RPC_URLS = {"testnet": "https://test.rpc.fastnear.com", "mainnet": "https://free.rpc.fastnear.com"}
TGAS = 10**12
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class NearError(Exception):
    pass


def b58encode(data: bytes) -> str:
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        n = n * 58 + B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def public_key(key: SigningKey) -> str:
    """NEAR's text form of an ed25519 public key: ed25519:<base58>."""
    return "ed25519:" + b58encode(bytes(key.verify_key))


def implicit_account(key: SigningKey) -> str:
    """The implicit account for a key is the hex of its public key."""
    return bytes(key.verify_key).hex()


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _string(s: str) -> bytes:
    b = s.encode("utf-8")
    return _u32(len(b)) + b


def function_call_tx(signer_id: str, pubkey: bytes, nonce: int, receiver_id: str, block_hash: bytes,
                     method: str, args: bytes, gas: int, deposit: int) -> bytes:
    """A borsh Transaction with one FunctionCall action (action index 2)."""
    return (_string(signer_id) + b"\x00" + pubkey + struct.pack("<Q", nonce)
            + _string(receiver_id) + block_hash
            + _u32(1) + b"\x02" + _string(method) + _u32(len(args)) + args
            + struct.pack("<Q", gas) + deposit.to_bytes(16, "little"))


class Near:
    def __init__(self, rpc_url: str, timeout: float = 60):
        self.url = rpc_url
        self.timeout = timeout

    def rpc(self, method: str, params) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": "blindfold", "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            reply = json.loads(resp.read())
        if "error" in reply:
            raise NearError(json.dumps(reply["error"])[:600])
        result = reply["result"]
        if isinstance(result, dict) and "error" in result:  # some query errors come back this way
            raise NearError(str(result["error"])[:600])
        return result

    def view(self, contract: str, method: str, args: dict | None = None):
        result = self.rpc("query", {
            "request_type": "call_function", "finality": "optimistic", "account_id": contract,
            "method_name": method, "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode(),
        })
        return json.loads(bytes(result["result"]))

    def call(self, key: SigningKey, signer_id: str, contract: str, method: str, args: dict,
             gas: int = 30 * TGAS, deposit: int = 0) -> tuple[str, object]:
        """Sign and send one function call. Returns (tx hash, decoded return value)."""
        try:
            ak = self.rpc("query", {"request_type": "view_access_key", "finality": "final",
                                    "account_id": signer_id, "public_key": public_key(key)})
        except NearError as e:
            raise NearError(f"no access key for {signer_id}: is the account funded? ({e})") from e
        tx = function_call_tx(signer_id, bytes(key.verify_key), ak["nonce"] + 1, contract,
                              b58decode(ak["block_hash"]), method, json.dumps(args).encode(), gas, deposit)
        signed = tx + b"\x00" + key.sign(hashlib.sha256(tx).digest()).signature
        out = self.rpc("send_tx", {"signed_tx_base64": base64.b64encode(signed).decode(),
                                   "wait_until": "EXECUTED_OPTIMISTIC"})
        tx_hash, status = out["transaction"]["hash"], out["status"]
        if "Failure" in status:
            raise NearError(f"{method} failed in tx {tx_hash}: {json.dumps(status['Failure'])[:600]}")
        value = base64.b64decode(status.get("SuccessValue") or "")
        return tx_hash, json.loads(value) if value else None