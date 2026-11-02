"""The enclave's identity: its keys, its attestation quote, and its mode.

The mode comes from the environment:
- tdx: inside a real Intel TDX VM on Phala Cloud (/var/run/dstack.sock exists).
- simulated: DSTACK_SIMULATOR_ENDPOINT points at Phala's dstack simulator.
  Same code path, but the quote is a fixed sample with our report_data
  patched in, so it proves nothing. It is labelled "simulated" everywhere.
- dev: neither. A random key saved in data/dev_enclave.key, and no quote.

Keys come from dstack's GetKey, derived from the app's master key, so only
this app, in a VM that passed attestation, gets them:
- signer (blindfold/signer/v1): an ed25519 seed. It signs receipts and is
  the enclave's NEAR account key (implicit account = hex of the public key).
- data (blindfold/data/v1): an X25519 private key. Data owners encrypt the
  bundle to its public key (Day 7).

report_data = sha256(signer pubkey + encryption pubkey + schema hash) goes
into the quote, which binds both keys and the schema to this exact code.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from nacl.public import PrivateKey
from nacl.signing import SigningKey

from .near import implicit_account, public_key

SIGNER_PATH = "blindfold/signer/v1"
DATA_PATH = "blindfold/data/v1"
SOCKETS = ["/var/run/dstack.sock", "/run/dstack.sock", "/var/run/dstack/dstack.sock", "/run/dstack/dstack.sock"]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def dstack_call(endpoint: str, method: str, payload: dict, timeout: float = 10) -> dict:
    """POST /<method> to the dstack guest agent (a unix socket or an http URL)."""
    if endpoint.startswith(("http://", "https://")):
        host = endpoint.split("://", 1)[1].rstrip("/")
        cls = http.client.HTTPSConnection if endpoint.startswith("https") else http.client.HTTPConnection
        conn = cls(host, timeout=timeout)
    else:
        conn = UnixHTTPConnection(endpoint.removeprefix("unix://"), timeout)
    try:
        conn.request("POST", "/" + method, json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"dstack {method}: HTTP {resp.status} {body[:200]!r}")
    return json.loads(body)


@dataclass
class Enclave:
    mode: str  # tdx, simulated or dev
    signer: SigningKey
    box_key: PrivateKey
    schema_hash: str
    compose_hash: str | None = None
    app_id: str | None = None
    instance_id: str | None = None
    quote: dict | None = None  # the GetQuote reply: quote, event_log, vm_config
    tcb_info: dict | None = None  # from Info: app_compose, measurements, os_image_hash

    @property
    def signer_pubkey(self) -> str:
        return public_key(self.signer)

    @property
    def account_id(self) -> str:
        return implicit_account(self.signer)

    @property
    def encryption_pubkey(self) -> str:
        return bytes(self.box_key.public_key).hex()

    def report_data(self) -> bytes:
        return report_data(bytes(self.signer.verify_key), bytes(self.box_key.public_key), self.schema_hash)

    def attestation(self) -> dict:
        q = self.quote or {}
        quote = q.get("quote") or None
        return {
            "mode": self.mode,
            "simulated": self.mode != "tdx",
            "signer_pubkey": self.signer_pubkey,
            "account_id": self.account_id,
            "encryption_pubkey": self.encryption_pubkey,
            "schema_hash": self.schema_hash,
            "report_data": self.report_data().hex(),
            "compose_hash": self.compose_hash,
            "app_id": self.app_id,
            "quote": quote,
            "quote_hash": hashlib.sha256(bytes.fromhex(quote)).hexdigest() if quote else None,
            "event_log": q.get("event_log"),
            "vm_config": q.get("vm_config"),
            "app_compose": (self.tcb_info or {}).get("app_compose"),
            "os_image_hash": (self.tcb_info or {}).get("os_image_hash"),
        }


def report_data(signer_pub: bytes, box_pub: bytes, schema_hash: str) -> bytes:
    return hashlib.sha256(signer_pub + box_pub + bytes.fromhex(schema_hash)).digest()


def from_dstack(endpoint: str, mode: str, schema_hash: str) -> Enclave:
    def key(path: str) -> bytes:
        # Same request as dstack-sdk 0.5.4's get_key(). The 32 key bytes are the same
        # for every algorithm (dstack derives them from the path), so we use them
        # directly as the ed25519 seed and the X25519 private key.
        reply = dstack_call(endpoint, "GetKey", {"path": path, "purpose": "", "algorithm": "secp256k1"})
        return bytes.fromhex(reply["key"])[:32]

    e = Enclave(mode, SigningKey(key(SIGNER_PATH)), PrivateKey(key(DATA_PATH)), schema_hash)
    info = dstack_call(endpoint, "Info", {})
    e.compose_hash, e.app_id, e.instance_id = info.get("compose_hash"), info.get("app_id"), info.get("instance_id")
    tcb = info.get("tcb_info") or {}
    e.tcb_info = json.loads(tcb) if isinstance(tcb, str) else tcb  # dstack sends it as a JSON string
    e.quote = dstack_call(endpoint, "GetQuote", {"report_data": e.report_data().hex()})
    return e


def dev(schema_hash: str, key_file: Path | None = None) -> Enclave:
    """No TEE: keys from a local file (made on first run), or fresh ones if key_file is None."""
    if key_file is None:
        seed = os.urandom(64)
    elif key_file.exists():
        seed = bytes.fromhex(key_file.read_text().strip())
    else:
        seed = os.urandom(64)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(seed.hex())
        key_file.chmod(0o600)
    return Enclave("dev", SigningKey(seed[:32]), PrivateKey(seed[32:]), schema_hash)


def load(schema_hash: str, dev_key_file: Path | None) -> Enclave:
    if os.environ.get("DSTACK_SIMULATOR_ENDPOINT"):
        return from_dstack(os.environ["DSTACK_SIMULATOR_ENDPOINT"], "simulated", schema_hash)
    for path in SOCKETS:
        if os.path.exists(path):
            return from_dstack(path, "tdx", schema_hash)
    return dev(schema_hash, dev_key_file)