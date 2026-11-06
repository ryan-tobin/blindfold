"""A small research agent that buys answers from Blindfold with its own limited key.

    python agent/buyer.py --buyer alice.ryan-tobin.testnet \
        "What is the 30-day readmission rate for diabetic patients aged 65 and older, by sex?" \
        "How many patients have hypertension, by age band?"
    python agent/buyer.py --buyer alice.ryan-tobin.testnet --goal "Is diabetes linked to readmissions in older patients?"

For each question it:
1. plans it with the engine (POST /plan, free) and prints the plan in plain words
2. checks the price, its limits, its balance and the privacy budget left, so a
   payment that would fail never gets sent
3. pays with request_query, signed by the agent's function-call key (made by
   `python agent/wallet.py agent-key`). It refuses to run with a full-access key.
4. gets the answer (POST /query), then checks that the enclave settled it on-chain
   with the same receipt hash
Then it writes a report with every answer, price, transaction and receipt to
data/reports/, and each receipt to data/receipts/ for verify/verify.py.

--goal needs NEAR_AI_API_KEY: the agent asks a NEAR AI model for up to 3
questions the public schema can answer. Questions the engine would refuse are
skipped and not paid for. Without --contract or CONTRACT_ID it talks to a local
engine (make serve) with no payments, as --buyer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from fractions import Fraction

from common import ROOT, agent_key_problem, http_json, key_permission, load_agent_key, near_client, \
    tx_link, usdc_text
from blindfold.chain import to_millionths

MAX_QUESTIONS = 3


def propose_questions(goal: str, schema: dict, n: int = MAX_QUESTIONS) -> list[str]:
    """Ask a NEAR AI model (the buyer's own key) for questions that serve the goal."""
    from blindfold.planner import DEFAULT_MODEL, NearAI, parse_json

    key = os.environ.get("NEAR_AI_API_KEY")
    if not key:
        raise SystemExit("--goal needs NEAR_AI_API_KEY; or pass the questions yourself")
    tables = {t: sorted(c for c, col in info["columns"].items() if col.get("role") != "privacy_unit")
              for t, info in schema["tables"].items()}
    prompt = (f"You help a researcher who can only ask aggregate questions (counts, rates, sums, means, "
              f"grouped by at most 2 columns) about this synthetic patient dataset: {json.dumps(tables)}. "
              f"Write at most {n} short plain-English questions that together serve the goal. "
              f'Reply with JSON only: {{"questions": ["...", "..."]}}')
    client = NearAI(key, os.environ.get("NEAR_AI_MODEL") or DEFAULT_MODEL)
    _, _, content, _ = client.chat([{"role": "system", "content": prompt}, {"role": "user", "content": goal}])
    questions = [q for q in parse_json(content).get("questions", []) if isinstance(q, str) and q.strip()]
    if not questions:
        raise SystemExit(f"the model wrote no questions: {content[:300]}")
    return questions[:n]


def precheck(price: int, eps_millionths: int, balance: dict, dataset: dict) -> str | None:
    """Why paying would fail on-chain, or None. Mirrors the contract's request_query checks."""
    left = dataset["epsilon_total"] - dataset["epsilon_spent"] - dataset["epsilon_pending"]
    if not dataset["active"]:
        return "the dataset is closed"
    if eps_millionths > left:
        return f"the privacy budget has only {left / 1e6:g} epsilon left"
    if price == 0:
        return None
    if price > int(balance["per_query_max"]):
        return f"the price {usdc_text(price)} USDC is above the per-question limit {usdc_text(balance['per_query_max'])}"
    if int(balance["spent_today"]) + price > int(balance["daily_max"]):
        return f"the daily limit {usdc_text(balance['daily_max'])} USDC would be passed"
    if price > int(balance["amount"]):
        return f"the balance {usdc_text(balance['amount'])} USDC is below the price {usdc_text(price)}"
    return None


def answer_lines(out: dict) -> list[str]:
    if out["status"] != "answered":
        r = out["refusal"] or {}
        return [f"Refused ({r.get('rule')}): {r.get('reason')}"]
    a = out["answer"]
    lines = [f"| {' · '.join(f'{k} {v}' for k, v in c['cell'].items()) or 'all'} | {c['display']} |"
             for c in a["cells"]]
    return [f"{a['measure']}, ε {a['epsilon']:g}, {int(a['level'] * 100)}% error bars", "",
            "| group | answer |", "| --- | --- |", *lines]


def report(goal: str | None, entries: list[dict], ctx: dict) -> str:
    out = ["# Blindfold research report", "",
           f"{ctx['when']} · buyer `{ctx['buyer']}` · engine {ctx['engine']}"
           + (f" · contract `{ctx['contract']}` on {ctx['network']}" if ctx["contract"] else " · local, unpaid"), ""]
    if goal:
        out += [f"**Goal:** {goal}", ""]
    paid = sum(e.get("price", 0) for e in entries if e.get("request_id") is not None)
    eps = sum(Fraction(str(e["epsilon_charged"])) for e in entries if e.get("epsilon_charged"))
    out += [f"Asked {len(entries)} questions, paid for {sum('request_id' in e for e in entries)}: "
            f"{usdc_text(paid)} USDC and ε {float(eps):g} spent.", ""]
    for i, e in enumerate(entries, 1):
        out += [f"## {i}. {e['question']}", ""]
        if e.get("plan"):
            out += [f"Plan ({e['planner']}): {e['plan']}", ""]
        if e.get("skipped"):
            out += [f"Not paid: {e['skipped']}", ""]
            continue
        out += answer_lines(e["out"]) + [""]
        if ctx["contract"]:
            out += [f"- request {e['request_id']}, price {usdc_text(e['price'])} USDC",
                    f"- paid: {tx_link(ctx['network'], e['pay_tx'])}",
                    f"- settled: {tx_link(ctx['network'], e['out'].get('settle_tx'))} "
                    f"({'receipt hash matches on-chain' if e['onchain_ok'] else 'NOT matching on-chain'})"]
        sig = e["out"]["receipt"].get("model_signature")
        out += [f"- receipt `{e['out']['receipt_hash']}`, saved to `{e['receipt_file']}`",
                f"- spec written by {sig['model']}, signed in its TEE by `{sig['signing_address']}`" if sig
                else "- spec not from NEAR AI (stub or hand-written), so no model signature", ""]
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("questions", nargs="*", help="plain-English questions")
    p.add_argument("--goal", help="a research goal; the agent writes the questions (needs NEAR_AI_API_KEY)")
    p.add_argument("--buyer", required=True, help="the account the agent spends from")
    p.add_argument("--engine", default=os.environ.get("ENGINE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID") or None)
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"))
    p.add_argument("--plan-only", action="store_true", help="plan and price the questions, pay nothing")
    a = p.parse_args()
    engine = a.engine.rstrip("/")
    schema = http_json("GET", engine + "/schema")

    questions = list(a.questions) + (propose_questions(a.goal, schema) if a.goal else [])
    if not questions:
        raise SystemExit("give questions, or --goal")

    near = key = None
    if a.contract and not a.plan_only:
        near = near_client(a.network)
        key = load_agent_key(a.network, a.buyer)
        problem = agent_key_problem(key_permission(near, a.buyer, key), a.contract)
        if problem:
            raise SystemExit(f"won't pay with this key: {problem}")
        print(f"agent key ok: it can only call request_query on {a.contract}")

    receipts_dir, reports_dir = ROOT / "data" / "receipts", ROOT / "data" / "reports"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for q in questions:
        print(f"\n? {q}")
        e: dict = {"question": q}
        entries.append(e)
        plan = http_json("POST", engine + "/plan", {"question": q})
        e["planner"] = plan["planner"] + (f", {plan['model']}" if plan.get("model") else "")
        e["plan"] = plan.get("summary")
        print(f"  plan: {e['plan'] or plan.get('spec')}")
        if plan["will_refuse"] or not plan["spec"]:
            e["skipped"] = f"the engine would refuse it ({(plan['will_refuse'] or {}).get('reason')})"
            print(f"  skip: {e['skipped']}")
            continue
        if a.plan_only:
            e["skipped"] = "plan only"
            continue

        signed = {"plan_id": plan["plan_id"]} if plan.get("plan_id") else {}  # the model's signature goes in the receipt
        if near:
            dataset = near.view(a.contract, "get_dataset", {"id": plan["spec"]["dataset"]})
            balance = near.view(a.contract, "get_balance", {"account_id": a.buyer})
            eps = to_millionths(Fraction(str(plan["epsilon"])))
            e["price"] = int(dataset["price"])
            why = precheck(e["price"], eps, balance, dataset)
            if why:
                e["skipped"] = why
                print(f"  not paying: {why}")
                continue
            e["pay_tx"], rid = near.call(key, a.buyer, a.contract, "request_query",
                                         {"dataset_id": dataset["id"], "spec_hash": plan["spec_hash"], "epsilon": eps})
            e["request_id"] = int(rid)
            print(f"  paid {usdc_text(e['price'])} USDC: request {rid}, {tx_link(a.network, e['pay_tx'])}")
            out = http_json("POST", engine + "/query", {"request_id": e["request_id"], "spec": plan["spec"], **signed})
            onchain = near.view(a.contract, "get_request", {"id": str(e["request_id"])})
            e["onchain_ok"] = onchain["status"] == out["status"] and onchain["receipt_hash"] == out["receipt_hash"]
        else:
            out = http_json("POST", engine + "/query", {"buyer": a.buyer, "spec": plan["spec"], **signed})
        e["out"] = out
        e["epsilon_charged"] = out["receipt"]["epsilon_charged"]
        name = f"{a.contract or 'local'}-{out['request_id']}.json"
        (receipts_dir / name).write_text(json.dumps({"receipt": out["receipt"], "receipt_hash": out["receipt_hash"]},
                                                    indent=2, ensure_ascii=False))
        e["receipt_file"] = f"data/receipts/{name}"
        for line in answer_lines(out):
            print("  " + line)

    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = report(a.goal, entries, {"when": when, "buyer": a.buyer, "engine": engine, "contract": a.contract,
                                    "network": a.network})
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"report-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text)
    print(f"\nreport: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()