"""Day 3: the noise sampler, the mechanisms, error bars and small-cell suppression.

The statistical tests use a seeded random.Random so they give the same result
on every run and never fail by bad luck. The engine itself uses the secure
source (secrets) unless a test passes its own rng.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from fractions import Fraction

import duckdb
import pytest

from blindfold import load
from blindfold.compile import compile_spec, run
from blindfold.dp import (SUM_GRID, answer, bernoulli_exp, discrete_laplace, half_width,
                          noise_scales, to_fraction)
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
EXAMPLES = load.REPO_ROOT / "datasets" / "riverside" / "examples"
T = SCHEMA["query_rules"]["small_cell_threshold"]
N = 20_000
needs_real_db = pytest.mark.skipif(not load.DEFAULT_DB.exists(), reason="run `make data` to check the real tables")


def seeded(seed=7):
    return random.Random(seed)


def pmf(x, b):
    """Exact probability of x under discrete Laplace with scale b."""
    q = math.exp(-1 / b)
    return (1 - q) / (1 + q) * q ** abs(x)


def tail(m, b):
    """P(X > m), which also equals P(X < -m)."""
    q = math.exp(-1 / b)
    return q ** (m + 1) / (1 + q)


def chi2_critical(df, z=3.09):
    """Chi-square value exceeded with probability about 0.001 (Wilson-Hilferty)."""
    return df * (1 - 2 / (9 * df) + z * math.sqrt(2 / (9 * df))) ** 3


def compiled(**raw):
    return compile_spec(validate({"dataset": "riverside", **raw}, SCHEMA))


# --- The exact samplers -------------------------------------------------------

@pytest.mark.parametrize("gamma", [Fraction(0), Fraction(1, 2), Fraction(1), Fraction(5, 2)])
def test_bernoulli_exp_is_true_with_probability_exp_minus_gamma(gamma):
    rng = seeded()
    hits = sum(bernoulli_exp(gamma, rng) for _ in range(N)) / N
    p = math.exp(-gamma)
    assert abs(hits - p) <= 5 * math.sqrt(p * (1 - p) / N)


@pytest.mark.parametrize("scale", [Fraction(1, 3), Fraction(1), Fraction(5, 2), Fraction(20)])
def test_discrete_laplace_matches_its_distribution(scale):
    """Chi-square test of N draws against the exact probabilities."""
    rng = seeded()
    b = float(scale)
    draws = [discrete_laplace(scale, rng) for _ in range(N)]
    m = 0
    while N * pmf(m + 1, b) >= 5 and N * tail(m + 1, b) >= 5:
        m += 1
    observed = [sum(1 for x in draws if x < -m)]
    observed += [draws.count(x) for x in range(-m, m + 1)]
    observed += [sum(1 for x in draws if x > m)]
    expected = [N * tail(m, b)] + [N * pmf(x, b) for x in range(-m, m + 1)] + [N * tail(m, b)]
    chi2 = sum((o - e) ** 2 / e for o, e in zip(observed, expected))
    assert chi2 < chi2_critical(len(observed) - 1)


def test_discrete_laplace_mean_and_variance():
    rng = seeded()
    b = 4
    draws = [discrete_laplace(Fraction(b), rng) for _ in range(N)]
    q = math.exp(-1 / b)
    variance = 2 * q / (1 - q) ** 2
    assert abs(statistics.fmean(draws)) <= 5 * math.sqrt(variance / N)
    assert abs(statistics.pvariance(draws) / variance - 1) < 0.1


def test_discrete_laplace_uses_the_secure_source_by_default():
    draws = [discrete_laplace(Fraction(2)) for _ in range(50)]
    assert all(isinstance(x, int) for x in draws)
    assert len(set(draws)) > 1


@pytest.mark.parametrize("bad", [2.0, 2, Fraction(0), Fraction(-1)])
def test_discrete_laplace_needs_a_positive_fraction(bad):
    with pytest.raises(ValueError):
        discrete_laplace(bad)


def test_to_fraction_keeps_the_decimal():
    assert to_fraction(0.2) == Fraction(1, 5)
    assert to_fraction(70.0) == 70


@pytest.mark.parametrize("b", [0.3, 1, 2, 25, 50, 70010])
def test_half_width_is_the_exact_95_percent_point(b):
    m = half_width(to_fraction(b))
    assert 2 * tail(m, b) <= 0.05
    if m > 0:
        assert 2 * tail(m - 1, b) > 0.05


def test_half_width_covers_95_percent_of_draws():
    rng = seeded()
    scale = Fraction(50)
    m = half_width(scale)
    inside = sum(abs(discrete_laplace(scale, rng)) <= m for _ in range(N)) / N
    assert inside >= 0.95 - 4 * math.sqrt(0.05 * 0.95 / N)


# --- Noise scale for each measure ---------------------------------------------

def test_noise_scales_follow_cap_epsilon_and_bounds():
    assert noise_scales(compiled(measure="count", table="admissions", epsilon=0.1)) == {"n": 50}
    assert noise_scales(compiled(measure="histogram", table="conditions", column="condition_group",
                                 epsilon=0.5)) == {"n": 16}
    # A rate's yes and no counts are disjoint, so each gets the full epsilon.
    assert noise_scales(compiled(measure="rate", table="patients", epsilon=1.0,
                                 numerator_filter=[["admissions.readmit_30d", "is", True]])) == {"yes": 1, "no": 1}
    # A mean splits epsilon in half; the sum is in 0.01 steps, plus one step for rounding.
    assert noise_scales(compiled(measure="mean", table="labs", column="value", epsilon=0.2,
                                 filters=[["test", "=", "hba1c"]])) == {"sum": (70 * 100 + 1) * 10, "n": 50}


# --- Mechanisms on hand-made true values --------------------------------------

def test_count_is_unbiased_and_the_error_bar_covers_it():
    cq = compiled(measure="count", table="patients", epsilon=1.0)
    rng = seeded()
    cells = [answer(cq, [{"cell": {}, "n": 1000}], T, rng)["cells"][0] for _ in range(2000)]
    assert abs(statistics.fmean(c["value"] for c in cells) - 1000) < 0.5
    assert sum(abs(c["value"] - 1000) <= c["plus_minus"] for c in cells) / len(cells) >= 0.93
    assert all(isinstance(c["value"], int) for c in cells)


def test_small_cells_are_suppressed():
    cq = compiled(measure="count", table="patients", epsilon=1.0)
    rng = seeded()
    small = [answer(cq, [{"cell": {}, "n": 3}], T, rng)["cells"][0] for _ in range(200)]
    assert all(c["suppressed"] and c["value"] is None and c["display"] == "<20" for c in small)
    big = [answer(cq, [{"cell": {}, "n": 1000}], T, rng)["cells"][0] for _ in range(200)]
    assert not any(c["suppressed"] for c in big)


def test_every_cell_is_returned_in_order_even_when_empty():
    cq = compiled(measure="count", table="patients", group_by=["sex"], epsilon=1.0)
    rows = [{"cell": {"patients.sex": "female"}, "n": 500}, {"cell": {"patients.sex": "male"}, "n": 0}]
    out = answer(cq, rows, T, seeded())["cells"]
    assert [c["cell"] for c in out] == [r["cell"] for r in rows]
    assert not out[0]["suppressed"] and out[1]["suppressed"]


def test_rate_stays_between_0_and_1_and_lands_near_the_truth():
    cq = compiled(measure="rate", table="patients", epsilon=1.0,
                  numerator_filter=[["admissions.readmit_30d", "is", True]])
    rng = seeded()
    cells = [answer(cq, [{"cell": {}, "numerator": 100, "denominator": 1000}], T, rng)["cells"][0]
             for _ in range(500)]
    assert all(0 <= c["value"] <= 1 and c["display"].endswith(f"± {100 * c['plus_minus']:.1f}") for c in cells)
    assert abs(statistics.median(c["value"] for c in cells) - 0.1) < 0.005


def test_rate_error_bar_covers_the_demo_sized_cell():
    """22 of 205 is the per-patient demo cell for women; the bar should cover the truth ~95% of the time."""
    cq = compiled(measure="rate", table="patients", epsilon=1.0,
                  numerator_filter=[["admissions.readmit_30d", "is", True]])
    rng = seeded()
    cells = [answer(cq, [{"cell": {}, "numerator": 22, "denominator": 205}], T, rng)["cells"][0]
             for _ in range(2000)]
    covered = sum(abs(c["value"] - 22 / 205) <= c["plus_minus"] for c in cells) / len(cells)
    assert covered >= 0.92
    assert statistics.median(c["plus_minus"] for c in cells) < 0.04


def test_rate_with_a_small_denominator_is_suppressed():
    cq = compiled(measure="rate", table="patients", epsilon=1.0,
                  numerator_filter=[["admissions.readmit_30d", "is", True]])
    out = answer(cq, [{"cell": {}, "numerator": 2, "denominator": 5}], T, seeded())["cells"][0]
    assert out["suppressed"]


def test_mean_is_clipped_to_the_tests_bounds():
    cq = compiled(measure="mean", table="labs", column="value", filters=[["test", "=", "hba1c"]], epsilon=1.0)
    rng = seeded()
    cells = [answer(cq, [{"cell": {}, "sum": 13.9 * 40, "n": 40}], T, rng)["cells"][0] for _ in range(300)]
    shown = [c for c in cells if not c["suppressed"]]
    assert shown and all(4.0 <= c["value"] <= 14.0 for c in shown)


def test_mean_of_a_big_group_is_close():
    cq = compiled(measure="mean", table="labs", column="value", filters=[["test", "=", "hba1c"]], epsilon=1.0)
    rng = seeded()
    cells = [answer(cq, [{"cell": {}, "sum": 7.0 * 5000, "n": 5000}], T, rng)["cells"][0] for _ in range(300)]
    assert abs(statistics.median(c["value"] for c in cells) - 7.0) < 0.05
    assert all(c["plus_minus"] < 0.2 for c in cells)


def test_sum_is_on_the_grid_and_has_an_error_bar():
    cq = compiled(measure="sum", table="admissions", column="los_days",
                  filters=[["encounter_class", "=", "inpatient"]], epsilon=1.0)
    out = answer(cq, [{"cell": {}, "sum": 12345.0, "n": 900}], T, seeded())
    c = out["cells"][0]
    assert Fraction(str(c["value"])) % SUM_GRID == 0
    assert c["plus_minus"] > 0 and " ± " in c["display"]
    assert out["noise_scale"]["sum"] == pytest.approx((150 / 0.01 + 1) / 0.5 * 0.01)


def test_answer_reports_its_settings():
    cq = compiled(measure="count", table="admissions", epsilon=0.1)
    out = answer(cq, [{"cell": {}, "n": 10_000}], T)
    assert out["measure"] == "count" and out["epsilon"] == 0.1 and out["level"] == 0.95
    assert out["noise_scale"] == {"n": 50.0} and out["small_cell_threshold"] == T
    assert set(out["cells"][0]) == {"cell", "suppressed", "value", "plus_minus", "display"}


def test_noise_is_fresh_on_every_call():
    cq = compiled(measure="count", table="admissions", epsilon=0.1)
    values = {answer(cq, [{"cell": {}, "n": 10_000}], T)["cells"][0]["value"] for _ in range(20)}
    assert len(values) > 1


# --- The real build (skipped until `make data` has run) -----------------------

@pytest.fixture(scope="module")
def real_con():
    c = duckdb.connect(str(load.DEFAULT_DB), read_only=True)
    yield c
    c.close()


@needs_real_db
def test_demo_question_per_patient_on_real_data(real_con):
    raw = json.loads((EXAMPLES / "readmit_patients_diabetes_65plus_by_sex.json").read_text(encoding="utf-8"))
    cq = compile_spec(validate(raw, SCHEMA))
    rows = run(real_con, cq)
    assert [r["cell"]["patients.sex"] for r in rows] == ["female", "male"]
    out = answer(cq, rows, T)
    for c in out["cells"]:
        assert not c["suppressed"]
        assert 0 <= c["value"] <= 1 and c["plus_minus"] < 0.05


@needs_real_db
def test_histogram_answers_on_real_data(real_con):
    cq = compiled(measure="histogram", table="medications", column="drug_class", epsilon=0.5)
    out = answer(cq, run(real_con, cq), T)
    assert [c["cell"]["medications.drug_class"] for c in out["cells"]] == \
        SCHEMA["tables"]["medications"]["columns"]["drug_class"]["categories"]