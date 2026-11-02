"""Privacy budget ledger, replay cache and rate limit (refusal rules 4 and 5).

State lives in one small SQLite file (Python's standard library), so a
restart can't reset the budget or empty the replay cache. From Day 5 the
contract's epsilon_spent is the source of truth and this file is a local copy.

- Ledger: every new answer charges its epsilon, and costs add up (basic
  composition). Amounts are stored as exact fractions ("1/10"), so 100
  questions at 0.1 spend exactly 10, with no floating-point leftover.
- Replay cache: the same canonical QuerySpec asked again, by anyone, gets the
  stored noisy answer back at no cost. Asking 1,000 times and averaging gets
  nothing, because every copy carries the same noise.
- Rate limit: count a buyer's requests in the last hour. Every request
  counts (answered, replayed or refused) except ones the rate limit refused.
"""

from __future__ import annotations

import json 
import sqlite3 
from dataclasses import dataclass 
from fractions import Fraction 
from pathlib import Path 
from typing import Callable 

TABLES = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    buyer TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    rule TEXT,
    receipt TEXT
);
CREATE INDEX IF NOT EXISTS requests_by_buyer ON requests (buyer, ts);
CREATE TABLE IF NOT EXISTS answers (
    dataset TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    request_id INTEGER NOT NULL,
    epsilon TEXT NOT NULL,
    answer TEXT NOT NULL,
    PRIMARY KEY (dataset, spec_hash)
);
"""

@dataclass(frozen=True)
class Cached:
    request_id: int 
    answer: dict 

class State:
    """The ledger, the replay cache and the request log, in on SQLite file."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.executescript(TABLES)

    def close(self) -> None:
        self.con.close()

    def spent(self, dataset: str) -> Fraction:
        rows = self.con.execute("SELECT epsilon FROM answers WHERE dataset = ?", (dataset,))
        return sum((Fraction(e) for (e,) in rows), Fraction(0))
    
    def cached(self, dataset: str, spec_hash: str) -> Cached | None:
        row = self.con.execute(
            "SELECT request_id, answer FROM answers WHERE dataset = ? AND spec_hash = ?",
            (dataset, spec_hash)
        ).fetchone()
        return Cached(row[0], json.loads(row[1])) if row else None 
    
    def recent(self, buyer: str, since: float) -> int:
        (n,) = self.con.execute(
            "SELECT count(*) FROM requests WHERE buyer = ? AND ts > ? AND coalesce(rule, '') != 'rate_limited'",
            (buyer, since),
        ).fetchone()
        return n 
    
    def receipt(self, request_id: int) -> dict | None:
        row = self.con.execute("SELECT receipt FROM requests WHERE id = ?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def log(self, dataset: str, buyer: str, ts: float, status: str, rule: str | None,
            make_receipt: Callable[[int], dict], charge: tuple[str, Fraction, dict] | None = None) -> dict:
        with self.con:
            cur =  self.con.execute(
                "INSERT INTO requests (dataset, buyer, ts, status, rule) VALUES (?, ?, ?, ?, ?)",
                (dataset, buyer, ts, status, rule),
            )
            request_id = cur.lastrowid
            receipt = make_receipt(request_id)
            self.con.execute("UPDATE requests SET receipt = ? WHERE id = ?", (json.dumps(receipt), request_id))
            if charge:
                spec_hash, epsilon, answer = charge
                self.con.execute(
                    "INSERT INTO answers (dataset, spec_hash, request_id, epsilon, answer) VALUES (?, ?, ?, ?, ?)",
                    (dataset, spec_hash, request_id, str(epsilon), json.dumps(answer, ensure_ascii=False)),
                )
        return receipt
    