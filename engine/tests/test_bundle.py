"""Day 7: the encrypted bundle, from the owner's DuckDB file to the enclave's memory.

    cd engine && python -m pytest -q tests/test_bundle.py
"""

from __future__ import annotations

import hashlib
import io
import tarfile

import duckdb
import pytest
from fastapi.testclient import TestClient
from nacl.public import PrivateKey

from blindfold import bundle, enclave, load
from blindfold.app import Blindfold, create_app

SCHEMA_HASH = hashlib.sha256(load.DEFAULT_SCHEMA.read_bytes()).hexdigest()
BY_SEX = {"dataset": "riverside", "measure": "count", "table": "patients", "group_by": ["sex"], "epsilon": 1.0}


@pytest.fixture(scope="module")
def tiny_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("bundle") / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE patients AS
        SELECT i::INTEGER AS patient_id, CASE WHEN i % 2 = 0 THEN 'female' ELSE 'male' END AS sex,
               '65-79' AS age_band, '606' AS zip3, 'Cook County' AS county FROM range(1, 1001) t(i)""")
    for t in bundle.TABLES[1:]:
        con.execute(f"CREATE TABLE {t} AS SELECT 1 AS patient_id, DATE '2020-01-01' AS d, true AS flag")
    con.close()
    return path


@pytest.fixture(scope="module")
def sealed(tiny_db):
    key = PrivateKey.generate()
    ct = bundle.seal(bundle.pack(tiny_db, "riverside", SCHEMA_HASH), bytes(key.public_key).hex())
    return key, ct, bundle.sha256(ct)


def test_round_trip_keeps_every_row_and_type(tiny_db, sealed):
    key, ct, h = sealed
    con = bundle.open_bundle(ct, h, key, "riverside", SCHEMA_HASH)
    orig = duckdb.connect(str(tiny_db), read_only=True)
    for t in bundle.TABLES:
        q = f"SELECT * FROM {t} ORDER BY ALL"
        assert con.execute(q).fetchall() == orig.execute(q).fetchall()
        assert con.execute(f"DESCRIBE {t}").fetchall() == orig.execute(f"DESCRIBE {t}").fetchall()


def test_no_file_access_after_loading(tiny_db, sealed):
    key, ct, h = sealed
    con = bundle.open_bundle(ct, h, key, "riverside", SCHEMA_HASH)
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT * FROM read_parquet('{tiny_db}')")
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")


def test_ciphertext_reveals_nothing_and_is_fresh(tiny_db, sealed):
    key, ct, _ = sealed
    assert b"female" not in ct and b"PAR1" not in ct
    again = bundle.seal(bundle.pack(tiny_db, "riverside", SCHEMA_HASH), bytes(key.public_key).hex())
    assert again != ct  # a new ephemeral key each time


@pytest.mark.parametrize("change, message", [
    ("hash", "hashes to"),
    ("key", "different enclave key"),
    ("schema", "not riverside with"),
])
def test_refuses_the_wrong_bundle(sealed, change, message):
    key, ct, h = sealed
    args = [ct, h, key, "riverside", SCHEMA_HASH]
    if change == "hash":
        args[1] = "00" * 32
    elif change == "key":
        args[2] = PrivateKey.generate()
    else:
        args[4] = "11" * 32
    with pytest.raises(bundle.BundleError, match=message):
        bundle.open_bundle(*args)


def test_refuses_unexpected_files_in_the_archive():
    key = PrivateKey.generate()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        bundle.add(tar, "../../etc/evil", b"x")
    ct = bundle.seal(out.getvalue(), bytes(key.public_key).hex())
    with pytest.raises(bundle.BundleError, match="unexpected entry"):
        bundle.open_bundle(ct, bundle.sha256(ct), key, "riverside", SCHEMA_HASH)


def test_engine_starts_without_data_then_loads_the_bundle(tiny_db, tmp_path):
    e = enclave.dev(SCHEMA_HASH)
    path = tmp_path / "riverside.bundle"
    engine = Blindfold(None, load.DEFAULT_SCHEMA, tmp_path / "s.sqlite", 10, enclave=e,
                       bundle_url=str(path), bundle_hash=None)
    client = TestClient(create_app(engine))
    assert client.get("/attestation").status_code == 200  # the owner needs the key before any data exists
    assert client.post("/query", json={"buyer": "alice.testnet", "spec": BY_SEX}).status_code == 503

    path.write_bytes(bundle.seal(bundle.pack(tiny_db, "riverside", SCHEMA_HASH), e.encryption_pubkey))
    assert client.post("/bundle").status_code == 503  # no hash listed yet
    engine.bundle_hash = bundle.sha256(path.read_bytes())
    r = client.post("/bundle")
    assert r.status_code == 200 and r.json()["source"] == "bundle"
    assert client.get("/info").json()["data"]["bundle_hash"] == engine.bundle_hash
    r = client.post("/query", json={"buyer": "alice.testnet", "spec": BY_SEX})
    assert r.status_code == 200 and r.json()["status"] == "answered"

    engine.bundle_hash = "00" * 32
    assert client.post("/bundle").status_code == 409  # no swapping data under a running engine


@pytest.mark.skipif(not load.DEFAULT_DB.exists(), reason="run `make data` to bundle the real tables")
def test_real_tables_survive_the_bundle():
    key = PrivateKey.generate()
    ct = bundle.seal(bundle.pack(load.DEFAULT_DB, "riverside", SCHEMA_HASH), bytes(key.public_key).hex())
    con = bundle.open_bundle(ct, bundle.sha256(ct), key, "riverside", SCHEMA_HASH)
    orig = duckdb.connect(str(load.DEFAULT_DB), read_only=True)
    for t in bundle.TABLES:
        assert con.execute(f"DESCRIBE {t}").fetchall() == orig.execute(f"DESCRIBE {t}").fetchall()
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone() == orig.execute(f"SELECT count(*) FROM {t}").fetchone()
    assert len(ct) < 200_000_000


class ListedChain:
    """Just enough of chain.Chain: the dataset's on-chain listing."""
    contract_id = "blindfold.test.near"

    def __init__(self, bundle_hash):
        self.bundle_hash = bundle_hash

    def dataset(self, dataset_id):
        return {"schema_hash": SCHEMA_HASH, "epsilon_total": 10_000_000, "epsilon_spent": 0,
                "bundle_hash": self.bundle_hash}


def test_chain_mode_loads_only_the_listed_bundle(tiny_db, tmp_path):
    e = enclave.dev(SCHEMA_HASH)
    path = tmp_path / "riverside.bundle"
    path.write_bytes(bundle.seal(bundle.pack(tiny_db, "riverside", SCHEMA_HASH), e.encryption_pubkey))
    chain = ListedChain("00" * 32)  # the owner listed some other bundle
    engine = Blindfold(None, load.DEFAULT_SCHEMA, tmp_path / "s.sqlite", 10, enclave=e, chain=chain,
                       bundle_url=str(path), bundle_hash=bundle.sha256(path.read_bytes()))
    assert engine.con is None and "hashes to" in engine.data["error"]  # BUNDLE_HASH is ignored with a chain
    chain.bundle_hash = bundle.sha256(path.read_bytes())
    assert engine.load_bundle()["source"] == "bundle"