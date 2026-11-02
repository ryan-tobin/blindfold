"""Day 6: enclave keys, the quote's report_data, and signed receipts.

A fake dstack guest agent (GetKey, Info, GetQuote) runs on a unix socket
and on HTTP, the two ways the engine reaches dstack.

    cd engine && python -m pytest -q tests/test_enclave.py
"""

from __future__ import annotations

import hashlib
import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from nacl.signing import SigningKey

from blindfold import enclave, receipts
from blindfold.near import b58decode

SCHEMA_HASH = "ab" * 32
QUOTE_REPORT_DATA = 568  # offset of report_data in a TDX v4 quote: 48-byte header + 520


class FakeDstack(BaseHTTPRequestHandler):
    def do_POST(self):
        args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = self.path.strip("/")
        if method == "GetKey":
            reply = {"key": hashlib.sha256(args["path"].encode()).hexdigest(), "signature_chain": []}
        elif method == "Info":
            reply = {"app_id": "app1", "instance_id": "inst1", "compose_hash": "cd" * 32}
        elif method == "GetQuote":
            data = bytes.fromhex(args["report_data"]).ljust(64, b"\0")
            quote = b"\x04\x00\x02\x00" + bytes(QUOTE_REPORT_DATA - 4) + data + bytes(100)
            reply = {"quote": quote.hex(), "event_log": "[]", "vm_config": "{}"}
        else:
            self.send_error(404)
            return
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def address_string(self):
        return "unix"


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(params=["unix", "http"])
def endpoint(request):
    if request.param == "unix":
        path = tempfile.mkdtemp(prefix="bf", dir="/tmp") + "/d.sock"  # short: macOS caps socket paths at 104
        server = serve(UnixHTTPServer(path, FakeDstack))
        yield path
    else:
        server = serve(HTTPServer(("127.0.0.1", 0), FakeDstack))
        yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_keys_come_from_dstack_and_are_stable(endpoint):
    a = enclave.from_dstack(endpoint, "simulated", SCHEMA_HASH)
    b = enclave.from_dstack(endpoint, "simulated", SCHEMA_HASH)
    seed = hashlib.sha256(b"blindfold/signer/v1").digest()
    assert a.signer_pubkey == b.signer_pubkey
    assert bytes(a.signer.verify_key) == bytes(SigningKey(seed).verify_key)
    assert a.account_id == bytes(a.signer.verify_key).hex() and len(a.account_id) == 64
    assert a.encryption_pubkey != a.account_id  # two different keys
    assert (a.compose_hash, a.app_id) == ("cd" * 32, "app1")


def test_quote_binds_keys_and_schema(endpoint):
    e = enclave.from_dstack(endpoint, "simulated", SCHEMA_HASH)
    att = e.attestation()
    expected = hashlib.sha256(bytes(e.signer.verify_key) + bytes(e.box_key.public_key) + bytes.fromhex(SCHEMA_HASH))
    assert att["report_data"] == expected.hexdigest()
    quote = bytes.fromhex(att["quote"])
    assert quote[QUOTE_REPORT_DATA:QUOTE_REPORT_DATA + 64] == expected.digest() + bytes(32)
    assert att["quote_hash"] == hashlib.sha256(quote).hexdigest()
    assert att["simulated"] is True and att["mode"] == "simulated"


def test_dev_mode_keeps_its_key_in_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DSTACK_SIMULATOR_ENDPOINT", raising=False)
    monkeypatch.setattr(enclave, "SOCKETS", [])
    key_file = tmp_path / "dev_enclave.key"
    a = enclave.load(SCHEMA_HASH, key_file)
    b = enclave.load(SCHEMA_HASH, key_file)
    assert a.mode == "dev" and a.signer_pubkey == b.signer_pubkey
    assert a.attestation()["quote"] is None and a.attestation()["simulated"] is True
    assert enclave.dev(SCHEMA_HASH).signer_pubkey != a.signer_pubkey  # no file: fresh keys


def test_simulator_endpoint_picks_simulated_mode(endpoint, monkeypatch):
    monkeypatch.setenv("DSTACK_SIMULATOR_ENDPOINT", endpoint)
    assert enclave.load(SCHEMA_HASH, None).mode == "simulated"


def receipt(**changes):
    r = receipts.build(3, "riverside", "ef" * 32, "answered", {"cells": []}, 1, 9, None, None,
                       1_800_000_000, compose_hash="cd" * 32)
    return {**r, **changes}


def test_signed_receipt_verifies():
    key = SigningKey(bytes(32))
    r = receipts.sign(receipt(), key)
    assert r["signer_pubkey"].startswith("ed25519:") and len(b58decode(r["sig"][8:])) == 64
    assert receipts.signature_ok(r)
    assert receipts.receipt_hash(r) == receipts.receipt_hash({**r, "sig": None})  # the hash leaves out sig


@pytest.mark.parametrize("field,value", [("epsilon_charged", 0.5), ("answer_hash", "00" * 32), ("request_id", 4),
                                         ("compose_hash", None), ("refusal", {"rule": "too_fine"})])
def test_any_change_breaks_the_signature(field, value):
    r = receipts.sign(receipt(), SigningKey(bytes(32)))
    assert not receipts.signature_ok({**r, field: value})


def test_signature_by_another_key_fails():
    r = receipts.sign(receipt(), SigningKey(bytes(32)))
    other = receipts.sign(receipt(), SigningKey(bytes([1]) * 32))
    assert not receipts.signature_ok({**r, "signer_pubkey": other["signer_pubkey"]})
    assert not receipts.signature_ok({**r, "sig": None})
    assert not receipts.signature_ok({**r, "sig": "ed25519:0OIl"})  # not base58