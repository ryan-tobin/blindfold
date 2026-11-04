"""Score the planner on the 25 test questions (5 demo questions + 20 paraphrases).

    make plan-bench                                   # NEAR_AI_MODEL, or the default model
    make plan-bench MODELS="zai-org/GLM-5.1-FP8,other/model"
    make plan-bench MODELS=stub                       # the offline stub: 5/25 by design

A question passes when its plan validates to the same canonical QuerySpec as
one of the expected specs in datasets/riverside/planner_questions.json, or,
for the attack questions, when the plan is refused (by the model or by the
engine's rules). Writes data/plan_bench.json for the README.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time

from . import load
from .planner import DEFAULT_MODEL, NearAI, PlanError, Planner
from .planner_stub import QUESTIONS, StubPlanner
from .spec import validate


def cases(schema: dict) -> list[dict]:
    out = []
    for g in json.loads(QUESTIONS.read_text())["groups"]:
        want = "refused" if g["expected"] == "refused" else [validate(e, schema).canonical_json() for e in g["expected"]]
        for i, q in enumerate([g["question"]] + g["paraphrases"]):
            out.append({"id": f"{g['id']}/{i}", "question": q, "want": want})
    return out


def score(planner, case: dict) -> dict:
    t = time.perf_counter()
    try:
        plan = planner.plan(case["question"])
        refused = plan.refusal is not None
        got = plan.valid.canonical_json() if plan.valid else None
        ok = refused if case["want"] == "refused" else (not refused and got in case["want"])
        note = plan.to_dict()["summary"] or f"refused: {plan.refusal['rule']}"
        tries = plan.tries
    except (PlanError, OSError) as e:
        ok, note, tries = False, f"no plan: {type(e).__name__} {e}"[:120], 0
    return {**case, "ok": ok, "note": note, "tries": tries, "seconds": round(time.perf_counter() - t, 2)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=os.environ.get("NEAR_AI_MODEL") or DEFAULT_MODEL,
                   help="comma-separated model ids, or 'stub'")
    a = p.parse_args()
    schema = load.load_schema(load.DEFAULT_SCHEMA)
    results = {}
    for model in [m.strip() for m in a.models.split(",") if m.strip()]:
        if model == "stub":
            planner = StubPlanner(schema)
        else:
            key = os.environ.get("NEAR_AI_API_KEY")
            if not key:
                raise SystemExit("set NEAR_AI_API_KEY (or use MODELS=stub)")
            planner = Planner(NearAI(key, model), schema)
        print(f"\n{model}")
        rows = []
        for case in cases(schema):
            r = score(planner, case)
            rows.append(r)
            print(f"  {'✓' if r['ok'] else '✗'} {r['id']:<24} {r['seconds']:>5}s  {r['note']}")
        passed = sum(r["ok"] for r in rows)
        secs = [r["seconds"] for r in rows if r["tries"]]
        retried = sum(r["tries"] > 1 for r in rows)
        summary = {"passed": passed, "total": len(rows), "retried": retried,
                   "median_seconds": statistics.median(secs) if secs else None}
        print(f"  {passed}/{len(rows)} correct, {retried} needed a retry, median {summary['median_seconds']}s")
        results[model] = {"summary": summary, "rows": rows}

    print("\nmodel                                     correct")
    for model, r in results.items():
        s = r["summary"]
        print(f"{model:<40}  {s['passed']:>2}/{s['total']}  ({100 * s['passed'] / s['total']:.0f}%)")
    out = load.REPO_ROOT / "data" / "plan_bench.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()