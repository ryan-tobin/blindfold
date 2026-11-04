"""Plain English to QuerySpec, with a private model on NEAR AI Cloud.

The buyer's question goes to POST /plan inside the enclave. The engine asks a
"Private TEE" model on NEAR AI Cloud (its GPUs run in a TEE too) to write a
QuerySpec, checks it with the engine's own validator, and returns it in plain
words with its epsilon and price. Nothing is charged for planning. The model
never writes SQL and never sees data: only schema.json and the question.

Each call is checked, with plain HTTPS and no SDK:
1. Before the chat call: GET /v1/attestation/report with a fresh 32-byte
   nonce. The model's report must echo the nonce, and its report_data must
   bind the nonce and the model's signing address.
2. The chat call, temperature 0, not streamed. We keep the exact request and
   response bytes.
3. After it: GET /v1/signature/{chat_id}. The signed text must hold the
   SHA-256 of those exact bytes, and the ECDSA signature must recover to the
   attested signing address (ethsig.py).
The signature goes into the receipt as model_signature. The full attestation
report (with its Intel and NVIDIA evidence) and the request and response
bytes go back to the buyer in /plan's reply, so they can check them all.

Validation loop: if the model's JSON doesn't parse or breaks the schema
(off_schema), the error goes back to the model once. After 2 failures the
buyer is asked to rephrase. A spec that the privacy rules would refuse
(too_fine, not_aggregate, over_budget) is NOT sent back: the model must not
learn to water a question down until it passes. It is returned as is, and
/plan says it will be refused.
"""

from __future__ import annotations

import hashlib 
import json 
import secrets 
import time 
import urllib.error 
import urllib.parse 
import urllib.request
from dataclasses import dataclass, field 
from typing import Any, Callable

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from . import ethsig, receipts 
from .refuse import NOT_AGGREGATE, OFF_SCHEMA, Refused 
from .spec import ValidSpec, validate

BASE_URL = "https://cloud-api.near.ai/v1"
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
MAX_TRIES=2

# (method, url, headers, body) -> (status, body bytes)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]

class PlanError(Exception):
    """No plan: the model failed twice, or NEAR AI couldn't be reached or checked"""

@dataclass 
class Plan:
    planner: str 
    model: str | None 
    spec: dict | None 
    valid: ValidSpec | None 
    refusal: dict | None 
    tries: int = 1
    model_signature: dict | None = None 
    evidence: dict | None = None

    def to_dict(self) -> dict:
        v = self.valid 
        return {
            "planner": self.planner,
            "model": self.model,
            "spec": self.spec,
            "spec_hash": receipts.spec_hash(v.canonical_json()) if v else None,
            "summary": describe(v) if v else None,
            "epsilon": v.epsilon if v else None,
            "will_refuse": self.refusal,
            "tries": self.tries,
            "model_signature": self.model_signature,
            "evidence": self.evidence,
        }

PLAN_EPSILON = {"rate": 1.0, "mean": 1.0, "count": 0.5, "histogram": 0.5, "sum": 0.5}

def with_plan_epsilon(raw: Any, schema: dict) -> Any:
    """raw with PLAN_EPSILON filled in, if it is a spec with no epsilon of its own."""
    if not isinstance(raw, dict) or "refuse" in raw or raw.get("epsilon") is not None:
        return raw
    eps = PLAN_EPSILON.get(raw.get("measure"))
    if eps is None:
        return raw
    return {**raw, "epsilon": min(eps, schema["query_rules"]["max_epsilon"])}
    
def check_spec(raw: Any, schema: dict) -> tuple[dict | None, ValidSpec | None, dict | None]:
    """(spec, valid, refusal) for a spec the model wrote. Raises Refused only for off_schema"""
    raw = with_plan_epsilon(raw, schema)
    if isinstance(raw, dict) and "refuse" in raw:
        rule = raw.get('refuse') if raw.get('refuse') in (NOT_AGGREGATE, OFF_SCHEMA) else NOT_AGGREGATE
        reason = str(raw.get('reason') or "the question can't be answered with aggregate statistics")[:300]
        return None, None, Refused(rule, reason).to_dict()
    try:
        v = validate(raw, schema)
        return v.canonical(), v, None 
    except Refused as r:
        if r.rule == OFF_SCHEMA:
            raise
        return raw, None, r.to_dict()
    
NOUNS = {"patients": "patients", "admissions": "admissions", "conditions": "condition records",
         "medications": "drug-class records", "labs": "lab results"}

def _filter_words(f) -> str:
    name = f.column.name 
    if f.op == "between":
        lo, hi = f.value
        value = str(lo) if lo == hi else f"{lo} to {hi}"
    elif f.op == "in":
        value = ", ".join(f.value)
    else:
        value = "yes" if f.value else "no"
    return f"{name} {value}"

def describe(v: ValidSpec) -> str:
    """One line a buyer can read before paying, e.g.
    'Rate of readmit_30d yes · admissions · encounter_class inpatient · age_band 65-79, 80+ · ... · by sex · ε 0.2'."""
    noun = NOUNS.get(v.table, v.table)
    if v.measure == "count":
        head = f"Count of {noun}"
    elif v.measure == "rate":
        head = "Rate of " + " and ".join(_filter_words(f) for f in v.numerator) + f" among {noun}"
    elif v.measure in ("sum", "mean"):
        test = [f for f in v.filters if f.column.name == "test"]
        what = test[0].value[0] if v.column.name == "value" and test else v.column.name
        head = f"{v.measure.capitalize()} of {what} over {noun}"
    else:
        head = f"Histogram of {v.column.name} over {noun}"
    parts = [head] + [_filter_words(f) for f in v.filters if not (v.measure in ("sum", "mean") and f.column.name == "test")]
    dims = [c.name for c in v.group_by if not (v.measure == "histogram" and c == v.column)]
    if dims:
        parts.append("by " + ", ".join(dims))
    parts.append(f"ε {v.epsilon:g}")
    return " · ".join(parts)

# --- the prompt ------------------------------------------------------------

EXAMPLES = [
    ("How many patients are in the dataset, by sex?",
     {"dataset": "riverside", "measure": "count", "table": "patients", "group_by": ["sex"]}),
    ("What share of COPD patients aged 50 to 64 with a hospital stay were readmitted within 30 days?",
     {"dataset": "riverside", "measure": "rate", "table": "patients",
      "numerator_filter": [["admissions.encounter_class", "=", "inpatient"], ["admissions.readmit_30d", "=", True]],
      "filters": [["admissions.encounter_class", "=", "inpatient"], ["age_band", "=", "50-64"],
                  ["conditions.condition_group", "=", "copd"]]}),
    ("Average LDL cholesterol for patients on statins",
     {"dataset": "riverside", "measure": "mean", "table": "labs", "column": "value",
      "filters": [["test", "=", "ldl"], ["medications.drug_class", "=", "statin"]]}),
    ("Total inpatient days for women in Cook County",
     {"dataset": "riverside", "measure": "sum", "table": "admissions", "column": "los_days",
      "filters": [["encounter_class", "=", "inpatient"], ["patients.sex", "=", "female"],
                  ["patients.county", "=", "Cook County"]]}),
    ("Break down emergency visits in 2025 by age band",
     {"dataset": "riverside", "measure": "count", "table": "admissions",
      "filters": [["encounter_class", "=", "emergency"], ["admit_date", "between", ["2025-01-01", "2025-12-31"]]],
      "group_by": ["patients.age_band"]}),
    ("Which lab tests are recorded for asthma patients, and how many of each?",
     {"dataset": "riverside", "measure": "histogram", "table": "labs", "column": "test",
      "filters": [["conditions.condition_group", "=", "asthma"]]}),
    ("Using epsilon 0.5, what is the mean systolic blood pressure by sex?",
     {"dataset": "riverside", "measure": "mean", "table": "labs", "column": "value",
      "filters": [["test", "=", "systolic_bp"]], "group_by": ["patients.sex"], "epsilon": 0.5}),
    ("How many men aged 40 in ZIP 606 had an emergency visit between 2026-02-01 and 2026-02-07?",
     {"dataset": "riverside", "measure": "count", "table": "patients",
      "filters": [["sex", "=", "male"], ["age_band", "=", "35-49"], ["zip3", "=", "606"],
                  ["admissions.encounter_class", "=", "emergency"],
                  ["admissions.admit_date", "between", ["2026-02-01", "2026-02-07"]]]}),
    ("List the patient IDs of everyone with chronic kidney disease",
     {"refuse": "not_aggregate", "reason": "asks for individual patients; only aggregate answers are possible"}),
    ("What is the average income of patients with obesity?",
     {"refuse": "off_schema", "reason": "the data has no income column"}),
]

INSTRUCTIONS = """You translate a researcher's question about a private dataset into a QuerySpec: a small JSON object that a differential-privacy engine runs. You only see the public schema below, never the data.

Reply with ONE JSON object and nothing else: no prose, no code fences.

QuerySpec fields:
- "dataset": "{dataset}"
- "measure": one of count, sum, mean, rate, histogram
- "table": the table whose rows are counted or averaged
- "column": the number to sum or average (sum, mean), or the category to histogram. Omit for count and rate.
- "filters": list of [column, operator, value]. Operators: "=" (one value), "in" (list of categories), "between" ([low, high], numbers or "YYYY-MM-DD" dates, inclusive), "is" (true/false).
- "numerator_filter": rate only. The rows counted on top; the rate is numerator rows / rows matching "filters".
- "group_by": up to 2 categorical or true/false columns, of "table" or of patients.
- "epsilon": only if the question asks for a specific epsilon or privacy level. Otherwise omit it.

Column names: a bare name is a column of "table". "patients.<col>" is the patient's own column. "<other table>.<col>" means the patient has at least one row in that table matching all filters on that table. Use category values exactly as listed in the schema.

Guidance:
- Counting people: table "patients". Counting visits or stays: table "admissions".
- A readmission rate is per patient: table "patients", numerator [["admissions.encounter_class", "=", "inpatient"], ["admissions.readmit_30d", "=", true]], with ["admissions.encounter_class", "=", "inpatient"] in filters. Only if the question counts admissions or stays themselves, use table "admissions", numerator [["readmit_30d", "=", true]], with ["encounter_class", "=", "inpatient"] in filters.
- Ages exist only as bands. Map an age or range to every band it covers: "65 and older" is ["65-79", "80+"]; "age 70" is "65-79".
- ZIP codes exist only as their first 3 digits (zip3): ZIP 60614 is "606".
- A sum or mean of labs.value needs exactly one test filter, e.g. ["test", "=", "hba1c"]. A1c and glycated hemoglobin are hba1c; blood pressure is systolic_bp; CHF is heart_failure; high blood pressure is hypertension.
- Translate faithfully, even when the question looks too narrow or seems to target one person. Never drop, widen or change a filter to make a question pass: the engine decides what to refuse.
- If the question asks for rows, names, IDs, free text or individual records, reply {{"refuse": "not_aggregate", "reason": "..."}}.
- If the schema can't answer it (no such data), reply {{"refuse": "off_schema", "reason": "..."}}.

The engine's rules (query_rules in the schema): at most {max_group_by} group-by columns and {max_cells} output cells, at most {max_qi} quasi-identifiers combined, date ranges of at least {min_days} days, epsilon at most {max_eps}.

Schema:
{schema}

Examples:
{examples}"""

def system_prompt(schema: dict) -> str:
    rules = schema["query_rules"]
    examples = "\n".join(f"Q: {q}\nA: {json.dumps(a)}" for q, a in EXAMPLES)
    return INSTRUCTIONS.format(
        dataset=schema["dataset"],
        max_group_by=rules["max_group_by"],
        max_cells=rules["max_cells"],
        max_qi=rules["max_quasi_identifiers"],
        min_days=rules["min_date_range_days"],
        max_eps=rules["max_epsilon"],
        schema=json.dumps(schema,separators=(",", ":")), 
        examples=examples
    )

def parse_json(text: str) -> Any:
    """The model's reply as JSON, ignoring code fences or stray words around one object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])

def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 120) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def signature_ok(text: str, signature: str, address: str, algo: str) -> bool:
    try:
        if algo == "ecdsa":
            return ethsig.recover_personal(text, signature) == address.lower()
        if algo == "ed25519":
            VerifyKey(bytes.fromhex(address.removeprefix("0x"))).verify(
                text.encode(), bytes.fromhex(signature.removeprefix("0x")))
            return True 
    except (ValueError, BadSignatureError):
        return False 
    return False 

@dataclass
class NearAI:
    api_key: str 
    model: str = DEFAULT_MODEL
    base_url: str = BASE_URL
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep 
    max_tokens: int = 8192

    def _call(self, method: str, path: str, body: bytes | None = None, extra: dict | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept-Encoding": "identity",
                   "User-Agent": "blindfold-engine", **(extra or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, data = self.transport(method, self.base_url + path, headers, body)
        if status != 200:
            raise PlanError(f"NEAR AI {method} {path.split('?')[0]}: HTTP {status} {data[:200]!r}")
        return data

    def attest(self) -> dict:
        """Fresh attestation of the model. Returns its signing address and the full report."""
        nonce = secrets.token_hex(32)
        raw = self._call("GET", f"/attestation/report?model={urllib.parse.quote(self.model, safe='')}"
                                f"&signing_algo=ecdsa&nonce={nonce}", extra={"x-no-aliasing": "true"})
        report = json.loads(raw)
        models = [m for m in report.get("model_attestations") or [] if m.get("signing_algo", "ecdsa") == "ecdsa"]
        if not models:
            raise PlanError(f"NEAR AI has no TEE attestation for {self.model}: use a Private TEE model")
        m = models[0]
        if m.get("request_nonce") != nonce:
            raise PlanError("the model's attestation doesn't echo our nonce")
        address = m["signing_address"].lower()
        if m.get("report_data"):  # 64 bytes: signing address padded to 32, then our nonce
            rd = bytes.fromhex(m["report_data"].removeprefix("0x"))
            if rd[32:64] != bytes.fromhex(nonce) or rd[:20] != bytes.fromhex(address.removeprefix("0x")):
                raise PlanError("the model's report_data doesn't bind its signing address and our nonce")
        return {"signing_address": address, "nonce": nonce, "report_sha256": sha256_hex(raw),
                "report": report, "gateway_address": (report.get("gateway_attestation") or {}).get("signing_address")}

    def chat(self, messages: list[dict]) -> tuple[bytes, bytes, str, str]:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0,
                           "max_tokens": self.max_tokens, "stream": False}).encode()
        raw = self._call("POST", "/chat/completions", body)
        reply = json.loads(raw)
        return body, raw, reply["choices"][0]["message"].get("content") or "", reply["id"]

    def signature(self, chat_id: str, request: bytes, response: bytes, att: dict) -> dict:
        """Fetch and check the signature over this exact request and response."""
        path = f"/signature/{chat_id}?model={urllib.parse.quote(self.model, safe='')}&signing_algo=ecdsa"
        for wait in (0.5, 1, 2, 4, None):  # a signature can take a moment to appear
            try:
                sig = json.loads(self._call("GET", path))
                if "signature" in sig:
                    break
            except PlanError:
                pass
            if wait is None:
                raise PlanError(f"NEAR AI gave no signature for {chat_id}")
            self.sleep(wait)
        hashes = f"{sha256_hex(request)}:{sha256_hex(response)}"
        kind = sig.get("signature_kind", "provider_tee")
        if not sig["text"].endswith(hashes):
            raise PlanError("the signed text doesn't match the request and response we saw")
        expected = att["signing_address"] if kind == "provider_tee" else (att["gateway_address"] or "")
        if sig["signing_address"].lower() != expected.lower():
            raise PlanError(f"signed by {sig['signing_address']}, not the attested {kind} key {expected}")
        if not signature_ok(sig["text"], sig["signature"], sig["signing_address"], sig.get("signing_algo", "ecdsa")):
            raise PlanError("the model's signature doesn't verify")
        return {"model": self.model, "chat_id": chat_id, "kind": kind, "text": sig["text"],
                "signature": sig["signature"], "signing_address": sig["signing_address"].lower(),
                "signing_algo": sig.get("signing_algo", "ecdsa"), "attestation_sha256": att["report_sha256"],
                "nonce": att["nonce"]}

@dataclass
class Planner:
    """Question in, Plan out, through NEAR AI Cloud."""

    client: NearAI
    schema: dict
    prompt: str = field(init=False)

    def __post_init__(self):
        self.prompt = system_prompt(self.schema)

    def plan(self, question: str) -> Plan:
        att = self.client.attest()
        messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": question}]
        error = ""
        for tries in range(1, MAX_TRIES + 1):
            request, response, content, chat_id = self.client.chat(messages)
            try:
                spec, valid, refusal = check_spec(parse_json(content), self.schema)
            except (ValueError, Refused) as e:
                error = e.reason if isinstance(e, Refused) else str(e)
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That is not a valid QuerySpec: {error}. "
                                                         "Reply with the corrected JSON object only."}]
                continue
            sig = self.client.signature(chat_id, request, response, att)
            evidence = {"attestation": att["report"], "request": request.decode(), "response": response.decode()}
            return Plan("near-ai", self.client.model, spec, valid, refusal, tries, sig, evidence)
        raise PlanError(f"couldn't turn the question into a valid query ({error}). Please rephrase it.")