"""The engine's HTTP service. Local mode today; inside the enclave from Day 6.

    make serve      # http://127.0.0.1:8000, interactive docs at /docs

    GET  /schema          the public schema.json, byte for byte
    GET  /budget          privacy budget: total, spent, remaining, closed
    POST /query           {"buyer": "alice.testnet", "spec": {...a QuerySpec...}}
    GET  /receipt/{id}    the receipt of an earlier request

Every /query gets a receipt, answered or refused. The checks run in order:
1. rate limit (rule 5): at most rate_limit_per_hour requests per buyer
2. the QuerySpec rules 1-4 (spec.py), including the per-query epsilon cap
3. replay cache: a question answered before comes back unchanged, for free
4. remaining budget (rule 4): at zero the dataset closes to new questions
5. compile, run, add noise, then charge the budget and cache the answer

Until the contract arrives (Day 5), the buyer is just a name in the request.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import duckdb
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import load, receipts
from .budget import State
from .compile import compile_spec, run
from .dp import answer, to_fraction
from .refuse import OVER_BUDGET, RATE_LIMITED, Refused
from .spec import validate

DEFAULT_STATE = load.REPO_ROOT / "data" / "state.sqlite"
HOUR = 3600


class Blindfold:
    """The query pipeline, without HTTP, so tests can call it directly."""

    def __init__(self, db_path: Path, schema_path: Path, state_path: Path | str,
                 epsilon_total: float, clock: Callable[[], float] = time.time):
        if not Path(db_path).exists():
            raise SystemExit(f"{db_path} not found: run `make data` first")
        self.schema_bytes = Path(schema_path).read_bytes()
        self.schema = json.loads(self.schema_bytes)
        self.schema_hash = hashlib.sha256(self.schema_bytes).hexdigest()
        self.dataset = self.schema["dataset"]
        self.rules = self.schema["query_rules"]
        self.total = to_fraction(epsilon_total)
        self.con = duckdb.connect(str(db_path), read_only=True)
        self.state = State(state_path)
        self.clock = clock
        self.lock = threading.Lock()  # one query at a time: check, charge and cache together

    @classmethod
    def from_env(cls) -> Blindfold:
        return cls(
            db_path=Path(os.environ.get("BLINDFOLD_DB", load.DEFAULT_DB)),
            schema_path=Path(os.environ.get("BLINDFOLD_SCHEMA", load.DEFAULT_SCHEMA)),
            state_path=Path(os.environ.get("BLINDFOLD_STATE", DEFAULT_STATE)),
            epsilon_total=float(os.environ.get("BLINDFOLD_EPSILON_TOTAL", "10")),
        )

    def close(self) -> None:
        self.con.close()
        self.state.close()

    def remaining(self) -> Fraction:
        return max(self.total - self.state.spent(self.dataset), Fraction(0))

    def budget(self) -> dict:
        left = self.remaining()
        return {
            "dataset": self.dataset,
            "epsilon_total": float(self.total),
            "epsilon_spent": float(self.total - left),
            "epsilon_remaining": float(left),
            "closed": left == 0,
        }

    def query(self, buyer: str, raw: Any) -> dict:
        with self.lock:
            now = self.clock()
            spec_hash = receipts.sha256_hex(raw)  # replaced by the canonical hash once the spec checks out
            try:
                limit = self.rules["rate_limit_per_hour"]
                if self.state.recent(buyer, now - HOUR) >= limit:
                    raise Refused(RATE_LIMITED, f"{buyer} has asked {limit} questions in the last hour, the limit")
                cq = compile_spec(validate(raw, self.schema))
                spec_hash = receipts.spec_hash(cq.spec.canonical_json())

                hit = self.state.cached(self.dataset, spec_hash)
                if hit:
                    return self._finish(now, buyer, spec_hash, answer=hit.answer, replay_of=hit.request_id)

                eps, left = to_fraction(cq.spec.epsilon), self.remaining()
                if left == 0:
                    raise Refused(OVER_BUDGET, "the privacy budget is used up, so this dataset is closed to new questions")
                if eps > left:
                    raise Refused(OVER_BUDGET, f"epsilon {cq.spec.epsilon:g} is more than the {float(left):g} left in the budget")

                result = answer(cq, run(self.con, cq), self.rules["small_cell_threshold"])
                return self._finish(now, buyer, spec_hash, answer=result, charge=eps)
            except Refused as r:
                return self._finish(now, buyer, spec_hash, refusal=r.to_dict())

    def _finish(self, now: float, buyer: str, spec_hash: str, answer: dict | None = None,
                refusal: dict | None = None, charge: Fraction | None = None, replay_of: int | None = None) -> dict:
        status = "refused" if refusal else "answered"
        charged = charge or Fraction(0)
        left_after = self.remaining() - charged

        def make_receipt(request_id: int) -> dict:
            return receipts.build(request_id, self.dataset, spec_hash, status, answer,
                                  charged, left_after, refusal, replay_of, now)

        receipt = self.state.log(self.dataset, buyer, now, status, refusal["rule"] if refusal else None,
                                 make_receipt, (spec_hash, charge, answer) if charge else None)
        return {
            "request_id": receipt["request_id"],
            "status": status,
            "replay_of": replay_of,
            "answer": answer,
            "refusal": refusal,
            "receipt": receipt,
            "receipt_hash": receipts.receipt_hash(receipt),
        }


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buyer: str = Field(pattern=r"^[a-z0-9_.-]{2,64}$", description="Buyer's NEAR account, e.g. alice.testnet")
    spec: Any = Field(description="A QuerySpec; see /schema for tables, columns and query_rules")


def create_app(engine: Blindfold | None = None) -> FastAPI:
    engine = engine or Blindfold.from_env()
    app = FastAPI(
        title="Blindfold engine",
        version="0.1.0",
        description="Answers, not data: differentially private answers with a receipt for every question.",
    )
    app.state.engine = engine

    @app.get("/schema")
    def get_schema() -> Response:
        return Response(content=engine.schema_bytes, media_type="application/json")

    @app.get("/budget")
    def get_budget() -> dict:
        return engine.budget()

    @app.post("/query")
    def post_query(req: QueryRequest) -> dict:
        return engine.query(req.buyer, req.spec)

    @app.get("/receipt/{request_id}")
    def get_receipt(request_id: int) -> dict:
        receipt = engine.state.receipt(request_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail=f"no request {request_id}")
        return {"receipt": receipt, "receipt_hash": receipts.receipt_hash(receipt)}

    return app