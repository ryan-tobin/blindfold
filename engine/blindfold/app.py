"""The engine's HTTP service. Runs in the enclave, or locally for development.

    make serve      # http://127.0.0.1:8000, interactive docs at /docs

    GET  /schema          the public schema.json, byte for byte
    GET  /budget          privacy budget: total, spent, remaining, closed
    POST /plan            a plain-English question in, a QuerySpec in plain words, its epsilon and price out
    POST /query           a QuerySpec in, a noisy answer or a refusal out, with a signed receipt
    GET  /receipt/{id}    the receipt of an earlier request
    GET  /attestation     the enclave's public keys, report_data and quote
    GET  /info            mode, app id, compose hash, contract
    POST /bundle          load the encrypted bundle from BUNDLE_URL now (if it failed at startup)

Two ways to run:
- local (CONTRACT_ID blank): POST {"buyer": "alice.testnet", "spec": {...}}.
  The buyer is just a name and the budget lives in data/state.sqlite.
- chain (CONTRACT_ID set): the buyer first pays with request_query on the
  contract, then POSTs {"request_id": 7, "spec": {...}}. The engine reads the
  request on-chain, checks the spec is the one paid for, answers, and calls
  settle with the receipt hash. The budget is the larger of the contract's
  epsilon_spent and the local ledger, out of the smaller of the contract's
  epsilon_total and BLINDFOLD_EPSILON_TOTAL (so a lying RPC can't raise it).

In chain mode the spec is the proof of purchase: only the buyer knows the spec
behind the paid spec_hash (the chain shows just the hash). A /query whose spec
doesn't hash to it is turned away with 409 and changes nothing, so a stranger
can't read someone's answer or use up their request. An answer is only handed
out once its request is settled as answered on-chain.

Planning (POST /plan) is free and charges no budget. With NEAR_AI_API_KEY set
the question goes to a Private TEE model on NEAR AI Cloud (planner.py); without
it, the offline stub knows the 5 demo questions (planner_stub.py). When /query
sends the plan_id that /plan returned (and the same spec), the receipt carries
the model's signature.


Every /query gets a signed receipt, answered or refused. The checks run in order:
1. rate limit (rule 5): at most rate_limit_per_hour requests per buyer
2. the QuerySpec rules 1-4 (spec.py), including the per-query epsilon cap
3. chain mode: the spec hash and epsilon match the paid request (rule 6)
4. replay cache: a question answered before comes back unchanged, for free
5. remaining budget (rule 4): at zero the dataset closes to new questions
6. compile, run, add noise, then charge the budget and cache the answer
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import duckdb
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import enclave as enclave_mod
from . import bundle, load, receipts
from .budget import State
from .chain import Chain, from_millionths
from .compile import compile_spec, run
from .dp import answer, to_fraction
from .enclave import Enclave
from .planner import DEFAULT_MODEL, NearAI, PlanError, Planner
from .planner_stub import StubPlanner
from .refuse import OVER_BUDGET, RATE_LIMITED, SPEC_MISMATCH, Refused
from .spec import validate

DATA_DIR = load.REPO_ROOT / "data"
HOUR = 3600
KEEP_PLANS = 1000

class EngineError(Exception):
    """A request the engine can't take at all (no receipt). Becomes an HTTP error."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Blindfold:
    """The query pipeline, without HTTP, so tests can call it directly."""

    def __init__(self, db_path: Path | None, schema_path: Path, state_path: Path | str, epsilon_total: float,
                 clock: Callable[[], float] = time.time, enclave: Enclave | None = None,
                 chain: Chain | None = None, bundle_url: str | None = None, bundle_hash: str | None = None,
                 planner: Planner | StubPlanner | None = None, plan_limit_per_hour: int = 120):
        if not bundle_url and not Path(db_path).exists():
            raise SystemExit(f"{db_path} not found: run `make data` first")
        self.schema_bytes = Path(schema_path).read_bytes()
        self.schema = json.loads(self.schema_bytes)
        self.schema_hash = hashlib.sha256(self.schema_bytes).hexdigest()
        self.dataset = self.schema["dataset"]
        self.rules = self.schema["query_rules"]
        self.enclave = enclave or enclave_mod.dev(self.schema_hash)
        self.chain = chain
        self.total = to_fraction(epsilon_total)
        if chain:
            d = chain.dataset(self.dataset)
            if d["schema_hash"] != self.schema_hash:
                raise SystemExit(f"schema.json hashes to {self.schema_hash}, but {chain.contract_id} "
                                 f"lists {self.dataset} with {d['schema_hash']}")
            self.total = min(from_millionths(d["epsilon_total"]), self.total)
        self.state = State(state_path)
        self.planner = planner or StubPlanner(self.schema)
        self.plan_limit = plan_limit_per_hour
        self.plan_times: deque[float] = deque()
        self.plan_lock = threading.Lock()
        self.plans: OrderedDict[str, tuple[str, dict]] = OrderedDict()  # plan_id -> (spec_hash, signature)
        self.clock = clock
        self.lock = threading.Lock()  # one query at a time: check, charge, cache and settle together
        self.bundle_url, self.bundle_hash = bundle_url, bundle_hash
        self.con: duckdb.DuckDBPyConnection | None = None
        self.data = {"source": None, "bundle_url": bundle_url, "bundle_hash": None, "error": None}
        if bundle_url:
            try:
                self.load_bundle()
            except EngineError as e:  # start anyway: /attestation must work before the bundle exists
                print(f"no data yet: {e.detail}", flush=True)
        else:
            self.con = duckdb.connect(str(db_path), read_only=True)
            self.data["source"] = "file"

    def load_bundle(self) -> dict:
        """Fetch, check, decrypt and load the bundle from BUNDLE_URL. Safe for anyone to
        trigger: the URL is fixed, and the hash must match the owner's on-chain listing."""
        if not self.bundle_url:
            raise EngineError(409, "this engine reads a local file, not a bundle")
        expected = self.chain.dataset(self.dataset)["bundle_hash"] if self.chain else self.bundle_hash
        if self.con is not None:
            if expected == self.data["bundle_hash"]:
                return self.data
            raise EngineError(409, "a different bundle is already loaded: restart the engine to change data")
        try:
            if not expected:
                raise bundle.BundleError("no bundle hash listed yet (update_dataset, or BUNDLE_HASH locally)")
            self.con = bundle.open_bundle(bundle.fetch(self.bundle_url), expected, self.enclave.box_key,
                                          self.dataset, self.schema_hash)
        except (bundle.BundleError, OSError) as e:
            self.data["error"] = str(e)
            raise EngineError(503, f"bundle not loaded: {e}") from e
        self.data.update(source="bundle", bundle_hash=expected, error=None)
        return self.data

    @classmethod
    def from_env(cls) -> Blindfold:
        schema_path = Path(os.environ.get("BLINDFOLD_SCHEMA", load.DEFAULT_SCHEMA))
        schema_hash = hashlib.sha256(schema_path.read_bytes()).hexdigest()
        enclave = enclave_mod.load(schema_hash, DATA_DIR / "dev_enclave.key")
        chain = Chain.from_env(enclave)
        schema = json.loads(schema_path.read_bytes())
        key = os.environ.get("NEAR_AI_API_KEY")
        planner = Planner(NearAI(key, os.environ.get("NEAR_AI_MODEL") or DEFAULT_MODEL), schema) if key else None
        default_state = DATA_DIR / (f"state-{chain.contract_id}.sqlite" if chain else "state.sqlite")
        return cls(
            bundle_url=os.environ.get("BUNDLE_URL") or None,
            bundle_hash=os.environ.get("BUNDLE_HASH") or None,
            db_path=Path(os.environ.get("BLINDFOLD_DB", load.DEFAULT_DB)),
            schema_path=schema_path,
            state_path=Path(os.environ.get("BLINDFOLD_STATE", default_state)),
            epsilon_total=float(os.environ.get("BLINDFOLD_EPSILON_TOTAL", "10")),
            enclave=enclave,
            chain=chain,
            planner=planner,
            plan_limit_per_hour=int(os.environ.get("PLAN_LIMIT_PER_HOUR", "120")),
        )

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
        self.state.close()

    def spent(self) -> Fraction:
        local = self.state.spent(self.dataset)
        if not self.chain:
            return local
        return max(local, from_millionths(self.chain.dataset(self.dataset)["epsilon_spent"]))

    def remaining(self) -> Fraction:
        return max(self.total - self.spent(), Fraction(0))

    def budget(self) -> dict:
        left = self.remaining()
        return {
            "dataset": self.dataset,
            "epsilon_total": float(self.total),
            "epsilon_spent": float(self.total - left),
            "epsilon_remaining": float(left),
            "closed": left == 0,
        }

    def info(self) -> dict:
        e = self.enclave
        return {
            "mode": e.mode,
            "app_id": e.app_id,
            "instance_id": e.instance_id,
            "compose_hash": e.compose_hash,
            "account_id": e.account_id,
            "signer_pubkey": e.signer_pubkey,
            "encryption_pubkey": e.encryption_pubkey,
            "dataset": self.dataset,
            "schema_hash": self.schema_hash,
            "contract_id": self.chain.contract_id if self.chain else None,
            "planner": self.planner_name(),
            "data": self.data,
        }

    def planner_name(self) -> str:
        return f"near-ai:{self.planner.client.model}" if isinstance(self.planner, Planner) else "stub"

    def plan(self, question: str) -> dict:
        """Plain English to a QuerySpec, free. The question is never stored or logged."""
        with self.plan_lock:
            now = self.clock()
            while self.plan_times and self.plan_times[0] <= now - HOUR:
                self.plan_times.popleft()
            if len(self.plan_times) >= self.plan_limit:
                raise EngineError(429, f"the engine plans at most {self.plan_limit} questions an hour; try later")
            self.plan_times.append(now)
        try:
            plan = self.planner.plan(question)
        except PlanError as e:
            raise EngineError(422, str(e)) from e
        except OSError as e:
            raise EngineError(502, f"couldn't reach NEAR AI Cloud: {e}") from e
        out = plan.to_dict()
        out["plan_id"] = None
        if plan.model_signature and out["spec_hash"]:
            # only whoever holds this plan_id can put the signature in a receipt
            out["plan_id"] = secrets.token_hex(16)
            with self.plan_lock:
                self.plans[out["plan_id"]] = (out["spec_hash"], plan.model_signature)
                while len(self.plans) > KEEP_PLANS:
                    self.plans.popitem(last=False)
        price = self.chain.dataset(self.dataset)["price"] if self.chain else None
        out["price"] = {"amount": str(price), "usdc": int(price) / 1_000_000} if price is not None else None
        return out


    def query(self, buyer: str | None, raw: Any, request_id: int | None = None,
              plan_id: str | None = None) -> dict:
        if not self.chain:
            with self.lock:
                self._need_data()
                if buyer is None:
                    raise EngineError(422, "local mode: send a buyer name")
                return self._answer(self.clock(), buyer, raw, plan_id=plan_id)
        if request_id is None:
            raise EngineError(422, "chain mode: pay with request_query on the contract, then send its request_id")
        # Outside the lock: anyone can call /query, and these checks change nothing
        paid = self._paid(request_id)
        if from_millionths(paid["epsilon"]) > self._max_eps():
            return self._refuse_unanswerable(paid)
        if not self._proves(raw, paid):
            raise EngineError(409, f"this spec doesn't hash to the spec_hash request {request_id} paid for, so "
                                   f"nothing was recorded. Send the exact spec you paid for (a request paid "
                                   f"for the wrong hash is refunded by expire() once it times out)")
        with self.lock:
            self._need_data()
            paid = self._paid(request_id)  # fresh status, now that no one else can settle it
            receipt = self.state.receipt(request_id)
            if receipt is None:  # first time we see it
                if paid["status"] != "pending":
                    raise EngineError(409, f"request {request_id} is already {paid['status']}")
                out = self._answer(self.clock(), paid["buyer"], raw, paid, plan_id)
                out["settle_tx"] = self._settle(out)
                return out
            # seen before: never answer twice. Hand out what is stored only once it is settled.
            out = self._response(receipt, self.state.answer_for(receipt))
            if paid["status"] == "pending":
                out["settle_tx"] = self._settle(out)
            elif paid["status"] == receipt["status"] or receipt["status"] == "refused":
                out["settle_tx"] = None  # a refusal holds nothing secret
            else:
                raise self._unreleased(request_id, receipt, paid["status"])
            return out

    def _need_data(self) -> None:
        if self.con is None:
            raise EngineError(503, f"no data loaded yet: {self.data['error']}. POST /bundle to retry")

    def _max_eps(self) -> Fraction:
        return to_fraction(self.rules["max_epsilon"])

    def _paid(self, request_id: int) -> dict:
        paid = self.chain.request(request_id)
        if paid is None or paid["dataset_id"] != self.dataset:
            raise EngineError(404, f"no request {request_id} for {self.dataset} on {self.chain.contract_id}")
        return paid

    def _proves(self, raw: Any, paid: dict) -> bool:
        """True if raw is the spec behind the paid spec_hash: its canonical hash, or for a
        spec that breaks the rules (no canonical form), the hash of the raw JSON."""
        if receipts.sha256_hex(raw) == paid["spec_hash"]:
            return True
        try:
            return receipts.spec_hash(validate(raw, self.schema).canonical_json()) == paid["spec_hash"]
        except Refused:
            return False

    def _refuse_unanswerable(self, paid: dict) -> dict:
        """A request that reserved more epsilon than any question may use can never be
        answered, so anyone may close it: this frees the budget it holds at once."""
        request_id = int(paid["id"])
        with self.lock:
            paid = self._paid(request_id)
            receipt = self.state.receipt(request_id)
            if receipt is not None:  # always a refusal: no answer can carry that much epsilon
                out = self._response(receipt, None)
            elif paid["status"] != "pending":
                raise EngineError(409, f"request {request_id} is already {paid['status']}")
            else:
                r = Refused(OVER_BUDGET, f"request {request_id} reserved epsilon "
                                         f"{float(from_millionths(paid['epsilon'])):g}, above the per-query cap "
                                         f"of {self.rules['max_epsilon']:g}")
                out = self._finish(self.clock(), paid["buyer"], paid["spec_hash"], request_id, refusal=r.to_dict())
            out["settle_tx"] = self._settle(out) if paid["status"] == "pending" else None
            return out

    def _unreleased(self, request_id: int, receipt: dict, status: str) -> EngineError:
        """The chain closed this request (expired) before our settle landed. Its answer
        never left the enclave, so drop it and give its epsilon back to the local ledger."""
        self.state.forget(request_id)
        return EngineError(410, f"request {request_id} is {status} on-chain (refunded) before it was settled, "
                                f"so its answer was never released and has been thrown away. Pay again to ask")

    def _settle(self, out: dict) -> str:
        r = out["receipt"]
        refusal = r["refusal"]["rule"] if r["refusal"] else None
        try:
            return self.chain.settle(r["request_id"], r["status"], out["receipt_hash"],
                                     to_fraction(r["epsilon_charged"]), refusal)
        except Exception as e:  # the answer is saved: retrying the same request_id is free
            raise EngineError(502, f"answered, but settle failed, so nothing is returned yet. "
                                   f"Send the same request_id again to retry. ({e})") from e

    def _answer(self, now: float, buyer: str, raw: Any, paid: dict | None = None,
                plan_id: str | None = None) -> dict:
        request_id = int(paid["id"]) if paid else None
        spec_hash = receipts.sha256_hex(raw)  # replaced by the canonical hash once the spec checks out
        try:
            limit = self.rules["rate_limit_per_hour"]
            if self.state.recent(buyer, now - HOUR) >= limit:
                raise Refused(RATE_LIMITED, f"{buyer} has asked {limit} questions in the last hour, the limit")
            cq = compile_spec(validate(raw, self.schema))
            spec_hash = receipts.spec_hash(cq.spec.canonical_json())
            eps = to_fraction(cq.spec.epsilon)

            if paid and paid["spec_hash"] != spec_hash:
                raise Refused(SPEC_MISMATCH, f"this question hashes to {spec_hash}, but request "
                                             f"{request_id} paid for {paid['spec_hash']}")
            if paid and from_millionths(paid["epsilon"]) != eps:
                raise Refused(SPEC_MISMATCH, f"the question asks for epsilon {cq.spec.epsilon:g}, but request "
                                             f"{request_id} reserved {float(from_millionths(paid['epsilon'])):g}")

            hit = self.state.cached(self.dataset, spec_hash)
            if hit and self.chain and not self._released(hit.request_id):
                hit = None  # its request expired before settling: the answer never left, so make a fresh one
            if hit:
                return self._finish(now, buyer, spec_hash, request_id, answer=hit.answer, replay_of=hit.request_id,
                                    plan_id=plan_id)

            left = self.remaining()
            if left == 0:
                raise Refused(OVER_BUDGET, "the privacy budget is used up, so this dataset is closed to new questions")
            if eps > left:
                raise Refused(OVER_BUDGET, f"epsilon {cq.spec.epsilon:g} is more than the {float(left):g} left in the budget")

            result = answer(cq, run(self.con, cq), self.rules["small_cell_threshold"])
            return self._finish(now, buyer, spec_hash, request_id, answer=result, charge=eps, left=left,
                                plan_id=plan_id)
        except Refused as r:
            return self._finish(now, buyer, spec_hash, request_id, refusal=r.to_dict())

    def _released(self, request_id: int) -> bool:
        """Chain mode: may the cached answer of request_id be replayed? Only once it is settled as
        answered on-chain. While it is still pending, the buyer is asked to retry; if it expired,
        its answer was never handed out, so it is dropped (and its epsilon given back)."""
        on_chain = self.chain.request(request_id)
        status = on_chain["status"] if on_chain else "missing"
        if status == "answered":
            return True
        if status == "pending":
            raise EngineError(409, f"the same question is being settled for request {request_id}; "
                                   f"send this again in a minute (nothing was recorded)")
        self.state.forget(request_id)
        return False

    def _finish(self, now: float, buyer: str, spec_hash: str, request_id: int | None, answer: dict | None = None,
                refusal: dict | None = None, charge: Fraction | None = None, replay_of: int | None = None,
                left: Fraction | None = None, plan_id: str | None = None) -> dict:
        status = "refused" if refusal else "answered"
        charged = charge or Fraction(0)
        left_after = (self.remaining() if left is None else left) - charged
        planned = self.plans.get(plan_id) if plan_id else None
        model_signature = planned[1] if planned and planned[0] == spec_hash else None

        def make_receipt(rid: int) -> dict:
            unsigned = receipts.build(rid, self.dataset, spec_hash, status, answer, charged, left_after,
                                      refusal, replay_of, now, self.enclave.compose_hash,
                                      model_signature)
            return receipts.sign(unsigned, self.enclave.signer)

        receipt = self.state.log(self.dataset, buyer, now, status, refusal["rule"] if refusal else None,
                                 make_receipt, (spec_hash, charge, answer) if charge else None, request_id)
        return self._response(receipt, answer)

    @staticmethod
    def _response(receipt: dict, answer: dict | None) -> dict:
        return {
            "request_id": receipt["request_id"],
            "status": receipt["status"],
            "replay_of": receipt["replay_of"],
            "answer": answer,
            "refusal": receipt["refusal"],
            "receipt": receipt,
            "receipt_hash": receipts.receipt_hash(receipt),
        }

class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=500, description="A question in plain English")


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buyer: str | None = Field(None, pattern=r"^[a-z0-9_.-]{2,64}$",
                              description="Local mode: the buyer's NEAR account, e.g. alice.testnet")
    request_id: int | None = Field(None, ge=0, description="Chain mode: the id request_query returned")
    plan_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$",
                                description="The plan_id /plan returned: puts the model's signature in the receipt")
    spec: Any = Field(description="A QuerySpec; see /schema for tables, columns and query_rules")


def create_app(engine: Blindfold | None = None) -> FastAPI:
    engine = engine or Blindfold.from_env()
    app = FastAPI(
        title="Blindfold engine",
        version="0.1.0",
        description="Answers, not data: differentially private answers with a signed receipt for every question.",
    )
    app.state.engine = engine
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"],
                       allow_headers=["content-type"])

    @app.get("/schema")
    def get_schema() -> Response:
        return Response(content=engine.schema_bytes, media_type="application/json")

    @app.get("/budget")
    def get_budget() -> dict:
        return engine.budget()

    @app.post("/plan")
    def post_plan(req: PlanRequest) -> dict:
        try:
            return engine.plan(req.question)
        except EngineError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e

    @app.post("/query")
    def post_query(req: QueryRequest) -> dict:
        try:
            return engine.query(req.buyer, req.spec, req.request_id, req.plan_id)
        except EngineError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e

    @app.get("/receipt/{request_id}")
    def get_receipt(request_id: int) -> dict:
        receipt = engine.state.receipt(request_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail=f"no request {request_id}")
        return {"receipt": receipt, "receipt_hash": receipts.receipt_hash(receipt)}

    @app.get("/attestation")
    def get_attestation() -> dict:
        return engine.enclave.attestation()

    @app.get("/info")
    def get_info() -> dict:
        return engine.info()
    
    @app.post("/bundle")
    def post_bundle() -> dict:
        try:
            with engine.lock:
                return engine.load_bundle()
        except EngineError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e

    return app