"""Differential privacy: the noise, the error bars and small-cell suppression.

    python -m blindfold.dp SPEC.json                 # the noisy answer a buyer would get
    python -m blindfold.dp SPEC.json --trials 200    # dev only: accuracy over 200 noisy draws

compile.py gives the true aggregates and how far one patient can move them
(the sensitivity). This file turns them into the answer that leaves the
enclave:

1. Noise. Every count gets integer noise from the discrete Laplace
   distribution, P(x) proportional to exp(-|x| / b), with scale
   b = sensitivity / epsilon. It is sampled exactly, with whole numbers and
   fractions only, from the OS's secure random source (`secrets`).
   Floating-point Laplace samplers leak through rounding (Mironov 2012), so
   no floats are used to draw noise. Algorithm: Canonne, Kamath and Steinke,
   "The Discrete Gaussian for Differential Privacy" (2020), Algorithms 1-2.
2. Budget split per measure (the whole answer costs epsilon once):
   count, histogram: each cell gets scale k / eps. The cells are disjoint and
     a patient's k rows are capped across all of them, so the whole grid
     costs eps once (parallel composition).
   rate: noise "yes" and "no" counts, each at scale k / eps. They are
     disjoint too, so no split is needed; denominator = yes + no.
   sum, mean: eps is split in half between the clipped sum and the count.
     Sums are rounded to a 0.01 grid and get grid-sized integer noise.
3. Error bars. Every value comes with a 95% interval for the noise
   (exact for counts and sums; a standard approximation for rates and means).
   It covers the noise only, not sampling error.
4. Small cells. A cell whose noisy count is below small_cell_threshold (20)
   is suppressed and shown as "<20". Deciding from a noisy count is itself
   private, so this costs nothing extra.

Everything after the noise is drawn (clamping, ratios, intervals) only uses
noisy values and public settings, so it costs no extra privacy.
"""

from __future__ import annotations

import argparse 
import json 
import math 
import random 
import secrets
import statistics
import sys 
from fractions import Fraction
from pathlib import Path 

import duckdb 

from . import load 
from .compile import CompiledQuery, compile_spec, run 
from .refuse import Refused 
from .spec import validate

SUM_GRID = Fraction(1, 100)
LEVEL = 0.95
_SECURE = secrets.SystemRandom()

def bernoulli(p: Fraction, rng: random.Random) -> bool:
    return rng.randrange(p.denominator) < p.numerator

def bernoulli_exp(gamma: Fraction, rng: random.Random) -> bool:
    while gamma > 1:
        if not bernoulli_exp(Fraction(1), rng):
            return False 
        gamma -= 1
    k = 1
    while bernoulli(gamma / k, rng):
        k += 1
    return k % 2 == 1

def discrete_laplace(scale: Fraction, rng: random.Random | None = None) -> int:
    rng = rng or _SECURE
    if not isinstance(scale, Fraction) or scale <= 0:
        raise ValueError("scale must be a positive Fraction.")
    t, s = scale.numerator, scale.denominator
    while True:
        u = rng.randrange(t)
        if not bernoulli_exp(Fraction(u, t), rng):
            continue 
        v = 0
        while bernoulli_exp(Fraction(1), rng):
            v += 1
        y = (u + t * v) // s
        negative = rng.randrange(2) == 1
        if negative and y == 0:
            continue 
        return -y if negative else y
    
def half_width(scale: Fraction,level: float = LEVEL) -> int:
    q = math.exp(-1 / float(scale))
    m = math.ceil(math.log((1 - level) * (1 + q) / 2) / math.log(q)) - 1
    return max(m, 0)

def to_fraction(x: float) -> Fraction:
    return Fraction(str(x))

def noise_scales(cq: CompiledQuery) -> dict[str, Fraction]:
    eps = to_fraction(cq.spec.epsilon)
    k = to_fraction(cq.sensitivity["n" if "n" in cq.sensitivity else "denominator"])
    measure = cq.spec.measure
    if measure in ("count", "histogram"):
        return {"n": k / eps}
    if measure in "rate":
        return {"yes": k / eps, "no": k / eps}
    half = eps / 2
    sum_units = to_fraction(cq.sensitivity["sum"]) / SUM_GRID + 1
    return {"sum": sum_units / half, "n": k / half}

def answer(cq: CompiledQuery, rows: list[dict], threshold: int, rng: random.Random | None = None) -> dict:
    rng = rng or _SECURE
    scales = noise_scales(cq)
    hw = {name: half_width(b) for name, b in scales.items()}
    measure = cq.spec.measure
    lo, hi = cq.spec.value_bounds or (None, None)
    out = []
    for row in rows:
        if measure in ("count", "histogram"):
            n = row["n"] + discrete_laplace(scales["n"], rng)
            value, pm, size = max(n, 0), hw["n"], n
        elif measure == "rate":
            yes = max(row["numerator"] + discrete_laplace(scales["yes"], rng), 0)
            no = max(row["denominator"] - row["numerator"] + discrete_laplace(scales["no"], rng), 0)
            size = yes + no 
            value = yes / size if size else 0.0
            pm = hw["yes"] * math.hypot(yes, no) / size**2 if size else 1.0
        else:
            units = round(Fraction(row["sum"]) / SUM_GRID) + discrete_laplace(scales["sum"], rng)
            noisy_sum = float(units * SUM_GRID)
            n = row["n"] + discrete_laplace(scales["n"], rng)
            size = n 
            sum_pm = float(hw["sum"] * SUM_GRID)
            if measure == "sum":
                value, pm = noisy_sum, sum_pm 
            else:
                value = min(max(noisy_sum / n, lo), hi) if n > 0 else lo 
                pm = math.hypot(sum_pm, value * hw["n"]) / n if n > 0 else hi - lo 
        out.append(_cell(row["cell"], measure, value, pm, size < threshold, threshold))
    return {
        "measure": measure,
        "epsilon": cq.spec.epsilon,
        "level": LEVEL,
        "noise_scale": {name: float(b * SUM_GRID if name == "sum" else b) for name, b in scales.items()},
        "small_cell_threshold": threshold,
        "cells": out,
    }

def _cell(cell: dict, measure: str, value: float, pm: float, suppressed: bool, threshold: int) -> dict:
    if suppressed:
        return {
            "cell": cell,
            "suppressed": True, 
            "value": None,
            "plus_minus": None,
            "display": f"<{threshold}"
        }
    if measure in ("count", "histogram"):
        value, pm = int(value), int(pm)
        display = f"{value:,} ± {pm:,}"
    elif measure == "rate":
        value, pm = round(value, 4), round(min(pm, 1.0), 4)
        display = f"{100 * value:.1f}% ± {100 * pm:.1f}"
    else:
        value, pm = round(value, 2), round(pm, 2)
        display = f"{value:,.2f} ± {pm:,.2f}"
    return {
        "cell": cell,
        "suppressed": False,
        "value": value,
        "plus_minus": pm, 
        "display": display
    }

def true_value(cq: CompiledQuery, row: dict) -> float:
    m = cq.spec.measure 
    if m in ("count", "histogram"):
        return row["n"]
    if m == "rate":
        return row["numerator"] / row["denominator"] if row["denominator"] else 0.0
    if m == "sum":
        return row["sum"]
    return row["sum"] / row["n"] if row["n"] else 0.0

def main() -> None:
    parser = argparse.ArgumentParser(description="Answer a QuerySpec with differential privacy.")
    parser.add_argument("spec", type=Path, help="QuerySpec JSON file")
    parser.add_argument("--trials", type=int, default=0, help="dev only: compare N noisy draws with the true values")
    parser.add_argument("--db", type=Path, default=load.DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=load.DEFAULT_SCHEMA)
    args = parser.parse_args()

    schema = load.load_schema(args.schema)
    with open(args.spec, encoding="utf-8") as f:
        raw = json.load(f)
    try:
        cq = compile_spec(validate(raw, schema))
    except Refused as r:
        print(json.dumps(r.to_dict(), indent=2))
        sys.exit(2)

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        rows = run(con, cq)
    finally:
        con.close()
    threshold = schema["query_rules"]["small_cell_threshold"]

    if not args.trials:
        print(json.dumps(answer(cq, rows, threshold), indent=2, ensure_ascii=False))
        return

    print(f"{args.trials} noisy draws at epsilon {cq.spec.epsilon} (dev only: uses the TRUE values)")
    draws = [answer(cq, rows, threshold)["cells"] for _ in range(args.trials)]
    for i, row in enumerate(rows):
        truth = true_value(cq, row)
        cells = [d[i] for d in draws]
        shown = [c for c in cells if not c["suppressed"]]
        name = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in row["cell"].items()) or "all"
        if not shown:
            print(f"  {name:<24} true {truth:.4g}   suppressed in every draw")
            continue
        errors = [abs(c["value"] - truth) for c in shown]
        covered = sum(abs(c["value"] - truth) <= c["plus_minus"] for c in shown) / len(shown)
        print(f"  {name:<24} true {truth:.4g}   median error {statistics.median(errors):.4g}   "
              f"typical ± {statistics.median(c['plus_minus'] for c in shown):.4g}   "
              f"inside ± {100 * covered:.0f}%   suppressed {100 * (1 - len(shown) / len(cells)):.0f}%")


if __name__ == "__main__":
    main()