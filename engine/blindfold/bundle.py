"""The encrypted data bundle: the only way data reaches the enclave.

Owner side (owner/encrypt.py):
    pack()  the five tables as Parquet, plus a manifest, in one tar archive
    seal()  encrypt it to the enclave's X25519 key (a libsodium sealed box)

Enclave side (app.py):
    fetch() the ciphertext from BUNDLE_URL (https or a local path)
    open_bundle() checks its SHA-256 against the hash the owner put on-chain,
    decrypts it in memory, and loads the tables into an in-memory DuckDB.

Only the enclave holds the private key, so only this exact code can read the
data. The Parquet files sit briefly in tmpfs (RAM, never disk) while DuckDB
reads them, and are deleted straight after. Then DuckDB's file access is
switched off, so no query can read or write a file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import duckdb
from nacl.public import PrivateKey, PublicKey, SealedBox

TABLES = ["patients", "admissions", "conditions", "medications", "labs"]
VERSION = 1


class BundleError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(db_path: Path, dataset: str, schema_hash: str) -> bytes:
    """The tables of db_path as one tar archive (the plaintext)."""
    con = duckdb.connect(str(db_path), read_only=True)
    out = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=out, mode="w") as tar:
        hashes = {}
        for t in TABLES:
            path = Path(tmp) / f"{t}.parquet"
            con.execute(f"COPY (SELECT * FROM {t}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
            data = path.read_bytes()
            hashes[t] = sha256(data)
            add(tar, f"{t}.parquet", data)
        manifest = {"v": VERSION, "dataset": dataset, "schema_hash": schema_hash, "tables": hashes}
        add(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
    con.close()
    return out.getvalue()


def add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def seal(plaintext: bytes, encryption_pubkey: str) -> bytes:
    return SealedBox(PublicKey(bytes.fromhex(encryption_pubkey))).encrypt(plaintext)


def fetch(url: str, timeout: float = 120) -> bytes:
    if url.startswith(("https://", "http://")):
        req = urllib.request.Request(url, headers={"User-Agent": "blindfold-engine"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    return Path(url.removeprefix("file://")).read_bytes()


def tmpfs_dir() -> str:
    """A RAM-backed folder for the Parquet files while DuckDB reads them."""
    for d in [os.environ.get("BLINDFOLD_TMP"), "/dev/shm"]:
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return tempfile.gettempdir()  # dev machines without /dev/shm (macOS)


def open_bundle(ciphertext: bytes, expected_hash: str, box_key: PrivateKey, dataset: str,
                schema_hash: str) -> duckdb.DuckDBPyConnection:
    """Check, decrypt and load a bundle. Returns an in-memory DuckDB with the five tables."""
    got = sha256(ciphertext)
    if got != expected_hash:
        raise BundleError(f"bundle hashes to {got}, but the listing says {expected_hash}")
    try:
        plaintext = SealedBox(box_key).decrypt(ciphertext)
    except Exception as e:
        raise BundleError("can't decrypt the bundle: it was sealed to a different enclave key") from e

    files = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as tar:
        for m in tar.getmembers():  # only plain files with the names we expect, nothing else
            if not m.isfile() or m.name not in {"manifest.json", *(f"{t}.parquet" for t in TABLES)}:
                raise BundleError(f"unexpected entry in bundle: {m.name!r}")
            files[m.name] = tar.extractfile(m).read()
    del plaintext

    manifest = json.loads(files.pop("manifest.json", b"{}"))
    if manifest.get("dataset") != dataset or manifest.get("schema_hash") != schema_hash:
        raise BundleError(f"bundle is for {manifest.get('dataset')} with schema {manifest.get('schema_hash')}, "
                          f"not {dataset} with {schema_hash}")
    for t in TABLES:
        data = files.get(f"{t}.parquet")
        if data is None or sha256(data) != manifest["tables"].get(t):
            raise BundleError(f"table {t} is missing or doesn't match the manifest")

    con = duckdb.connect(":memory:")
    tmp = tempfile.mkdtemp(prefix="blindfold-", dir=tmpfs_dir())
    try:
        for t in TABLES:
            path = Path(tmp) / f"{t}.parquet"
            path.write_bytes(files.pop(f"{t}.parquet"))
            con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{path}')")
            path.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    con.execute("SET enable_external_access = false")  # no query can touch files from here on
    con.execute("SET lock_configuration = true")
    return con