"""Accuracy benchmark: 200 questions, noisy answers against the true answers.

    make bench                        # 200 questions, ε 0.1 0.2 0.5 1, 20 draws each
    make bench EPS=0.2 TRIALS=100

1. Builds the 200 questions below (8 condition groups x 25 question shapes)
   and computes every true answer on the raw tables, outside any enclave.
   Writes them to datasets/riverside/benchmark.jsonl, so a rerun on the same
   data gives the same file.
2. At each ε, draws noisy answers with the enclave's own code (dp.answer),
   TRIALS times. It charges no budget: this measures the mechanism, offline.
   The noise uses a seeded generator so the numbers are reproducible; the
   enclave itself uses the OS's secure random source.
3. Scores every cell the buyer would see (suppressed "<20" cells aren't
   scored): relative error |noisy - true| / true, and whether the truth is
   inside the answer's 95% error bar.

Writes the full results to data/bench.json and the summary the README and
the UI chart use to docs/bench.json.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path

import duckdb

from . import load
from .compile import compile_spec, run
from .dp import answer, true_value
from .spec import validate

BENCHMARK = load.REPO_ROOT / "datasets" / "riverside" / "benchmark.jsonl"
SUMMARY = load.REPO_ROOT / "docs" / "bench.json"
FULL = load.REPO_ROOT / "data" / "bench.json"
CONDITIONS = ["diabetes", "hypertension", "copd", "heart_failure", "ckd", "asthma", "depression", "obesity"]
TESTS = {"hba1c": "HbA1c", "ldl": "LDL cholesterol", "systolic_bp": "systolic blood pressure", "bmi": "BMI"}
OLD = ["age_band", "in", ["65-79", "80+"]]
READMIT = [["admissions.encounter_class", "=", "inpatient"], ["admissions.readmit_30d", "=", True]]
INPATIENT = ["encounter_class", "=", "inpatient"]


def shapes(c: str, name: str) -> list[tuple[str, dict]]:
    """The 25 question shapes for one condition group: (question, spec without dataset)."""
    has = ["conditions.condition_group", "=", c]
    old = ["patients." + OLD[0], *OLD[1:]]
    out = [
        (f"How many patients have {name}?", {"measure": "count", "table": "patients", "filters": [has]}),
        (f"How many patients have {name}, by sex?",
         {"measure": "count", "table": "patients", "filters": [has], "group_by": ["sex"]}),
        (f"How many patients have {name}, by age band?",
         {"measure": "count", "table": "patients", "filters": [has], "group_by": ["age_band"]}),
        (f"How many patients aged 65 or older have {name}, by sex?",
         {"measure": "count", "table": "patients", "filters": [has, OLD], "group_by": ["sex"]}),
        (f"How many patients with {name} live in each 3-digit ZIP area?",
         {"measure": "count", "table": "patients", "filters": [has], "group_by": ["zip3"]}),
        (f"What share of patients with {name} were readmitted within 30 days of a hospital stay?",
         {"measure": "rate", "table": "patients", "numerator_filter": READMIT,
          "filters": [READMIT[0], has]}),
        (f"What share of patients with {name} were readmitted within 30 days, by sex?",
         {"measure": "rate", "table": "patients", "numerator_filter": READMIT,
          "filters": [READMIT[0], has], "group_by": ["sex"]}),
        (f"What share of patients 65 or older with {name} were readmitted within 30 days?",
         {"measure": "rate", "table": "patients", "numerator_filter": READMIT,
          "filters": [READMIT[0], has, OLD]}),
        (f"What fraction of hospital stays by patients with {name} end in a 30-day readmission?",
         {"measure": "rate", "table": "admissions", "numerator_filter": [["readmit_30d", "=", True]],
          "filters": [INPATIENT, has]}),
        (f"What fraction of encounters by patients with {name} are emergency visits?",
         {"measure": "rate", "table": "admissions", "numerator_filter": [["encounter_class", "=", "emergency"]],
          "filters": [has]}),
        (f"How long is the average hospital stay for patients with {name}?",
         {"measure": "mean", "table": "admissions", "column": "los_days", "filters": [INPATIENT, has]}),
        (f"How many hospital days in total did patients with {name} spend?",
         {"measure": "sum", "table": "admissions", "column": "los_days", "filters": [INPATIENT, has]}),
        (f"Which drug classes are prescribed to patients with {name}, and how often?",
         {"measure": "histogram", "table": "medications", "column": "drug_class", "filters": [has]}),
        (f"Which drug classes are prescribed to patients with {name}, by sex?",
         {"measure": "histogram", "table": "medications", "column": "drug_class", "filters": [has],
          "group_by": ["patients.sex"]}),
        (f"Which drug classes do patients 65 or older with {name} take?",
         {"measure": "histogram", "table": "medications", "column": "drug_class", "filters": [has, old]}),
    ]
    for test, label in TESTS.items():
        lab = ["test", "=", test]
        out.append((f"What is the average {label} of patients with {name}?",
                    {"measure": "mean", "table": "labs", "column": "value", "filters": [lab, has]}))
        out.append((f"What is the average {label} of patients with {name}, by sex?",
                    {"measure": "mean", "table": "labs", "column": "value", "filters": [lab, has],
                     "group_by": ["patients.sex"]}))
    out.append((f"How many patients with {name} are female or male, by age band?",
                {"measure": "count", "table": "patients", "filters": [has], "group_by": ["sex", "age_band"]}))
    out.append((f"How many patients 65 or older with {name} take a statin?",
                {"measure": "count", "table": "patients",
                 "filters": [has, OLD, ["medications.drug_class", "=", "statin"]]}))
    return out


def questions(schema: dict) -> list[dict]:
    out = []
    for c in CONDITIONS:
        for text, spec in shapes(c, c.replace("_", " ")):
            spec = {"dataset": schema["dataset"], **spec}
            validate(spec, schema)  # a broken shape is a bug here, so let it raise
            out.append({"id": len(out), "question": text, "spec": spec})
    return out

def tidy(x, digits: int):
    """Round floats to `digits` significant digits (also inside lists and dicts), so files don't change between runs."""
    if isinstance(x, float):
        return float(f"{x:.{digits}g}")
    if isinstance(x, dict):
        return {k: tidy(v, digits) for k, v in x.items()}
    if isinstance(x, list):
        return [tidy(v, digits) for v in x]
    return x

def build(con, schema: dict) -> list[dict]:
    """The questions with their true answers, written to BENCHMARK."""
    items = questions(schema)
    with open(BENCHMARK, "w", encoding="utf-8") as f:
        for q in items:
            cq = compile_spec(validate(q["spec"], schema))
            q["truth"] = [{"cell": r["cell"], "value": tidy(true_value(cq, r), 10)} for r in run(con, cq)]
            f.write(json.dumps(q, ensure_ascii=False, sort_keys=True) + "\n")
    return items


def score(items: list[dict], con, schema: dict, eps: float, trials: int, rng: random.Random) -> dict:
    threshold = schema["query_rules"]["small_cell_threshold"]
    per_measure: dict[str, list[float]] = {}
    errors, covered, shown, cells, per_question = [], 0, 0, 0, []
    for q in items:
        cq = compile_spec(validate({**q["spec"], "epsilon": eps}, schema))
        rows = run(con, cq)
        q_errors = []
        for _ in range(trials):
            for got, truth in zip(answer(cq, rows, threshold, rng)["cells"], q["truth"]):
                cells += 1
                if got["suppressed"]:
                    continue
                shown += 1
                miss = abs(got["value"] - truth["value"])
                covered += miss <= got["plus_minus"]
                if truth["value"]:
                    rel = miss / abs(truth["value"])
                    errors.append(rel)
                    q_errors.append(rel)
                    per_measure.setdefault(cq.spec.measure, []).append(rel)
        per_question.append(statistics.median(q_errors) if q_errors else None)
    scored = [e for e in per_question if e is not None]
    return {
        "epsilon": eps,
        "median_error": statistics.median(errors),
        "p90_error": statistics.quantiles(errors, n=10)[-1],
        "inside_error_bar": covered / shown,
        "suppressed": 1 - shown / cells,
        "questions_within_10pct": sum(e <= 0.10 for e in scored) / len(scored),
        "by_measure": {m: statistics.median(v) for m, v in sorted(per_measure.items())},
        "per_question": per_question,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--eps", default="0.1,0.2,0.5,1", help="comma-separated epsilons")
    p.add_argument("--trials", type=int, default=20, help="noisy draws per question and epsilon")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--db", type=Path, default=load.DEFAULT_DB)
    a = p.parse_args()

    schema = load.load_schema(load.DEFAULT_SCHEMA)
    if not a.db.exists():
        raise SystemExit(f"{a.db} not found: run `make data` first")
    con = duckdb.connect(str(a.db), read_only=True)
    items = build(con, schema)
    print(f"{len(items)} questions, true answers in {BENCHMARK.relative_to(load.REPO_ROOT)}; "
          f"{a.trials} noisy draws each\n")
    rng = random.Random(a.seed)
    results = [score(items, con, schema, float(e), a.trials, rng) for e in a.eps.split(",")]
    con.close()

    measures = sorted({m for r in results for m in r["by_measure"]})
    print(f"{'ε':>5}  {'median error':>12}  {'90% under':>9}  {'≤10% (q)':>8}  {'inside ±':>8}  "
          f"{'suppressed':>10}  " + "  ".join(f"{m:>9}" for m in measures))
    for r in results:
        print(f"{r['epsilon']:>5g}  {r['median_error']:>12.1%}  {r['p90_error']:>9.1%}  "
              f"{r['questions_within_10pct']:>8.0%}  {r['inside_error_bar']:>8.1%}  {r['suppressed']:>10.1%}  "
              + "  ".join(f"{r['by_measure'].get(m, float('nan')):>9.1%}" for m in measures))
    print("\nmedian error: half of all shown cells are closer than this to the truth (relative error)")
    print("≤10% (q): questions whose typical cell is within 10%; inside ±: truth inside the 95% error bar")

    FULL.parent.mkdir(exist_ok=True)
    FULL.write_text(json.dumps(tidy({"questions": [q["question"] for q in items], "results": results}, 8), indent=1))
    summary = {"questions": len(items), "trials": a.trials, "seed": a.seed,
               "results": [{k: v for k, v in r.items() if k != "per_question"} for r in results]}
    SUMMARY.parent.mkdir(exist_ok=True)
    SUMMARY.write_text(json.dumps(tidy(summary, 8), indent=2) + "\n")
    print(f"\nwrote {SUMMARY.relative_to(load.REPO_ROOT)} and {FULL.relative_to(load.REPO_ROOT)}")


if __name__ == "__main__":
    main()