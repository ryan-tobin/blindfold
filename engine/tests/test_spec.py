"""Tests for blindfold.spec: valid QuerySpecs pass, bad ones are refused with a reason.

    cd engine && python -m pytest -q tests/test_spec.py
"""

from __future__ import annotations

import copy
import json

import pytest

from blindfold import load
from blindfold.refuse import NOT_AGGREGATE, OFF_SCHEMA, OVER_BUDGET, RULES, TOO_FINE, Refused
from blindfold.spec import QuerySpec, validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
EXAMPLES = load.REPO_ROOT / "datasets" / "riverside" / "examples"

# The demo question: 30-day readmission rate for diabetic inpatients over 65, by sex.
DEMO = json.loads((EXAMPLES / "readmit_diabetes_65plus_by_sex.json").read_text(encoding="utf-8"))

COUNT = {"dataset": "riverside", "measure": "count", "table": "patients"}


def spec(base: dict = COUNT, **changes) -> dict:
    out = copy.deepcopy(base)
    out.update(changes)
    return out


def refused(raw: dict) -> Refused:
    with pytest.raises(Refused) as e:
        validate(raw, SCHEMA)
    return e.value


# --- Valid specs -------------------------------------------------------------

def test_demo_spec_is_valid():
    v = validate(DEMO, SCHEMA)
    assert v.measure == "rate" and v.table == "admissions"
    assert [c.ref for c in v.group_by] == ["patients.sex"]
    assert v.epsilon == 0.2
    assert v.rows_cap == 5
    assert [(f.column.ref, f.op, f.value) for f in v.numerator] == [("admissions.readmit_30d", "is", True)]


def test_default_epsilon_by_measure():
    defaults = SCHEMA["query_rules"]["default_epsilon"]
    assert validate(COUNT, SCHEMA).epsilon == defaults["count"] == 0.1
    no_eps = {k: v for k, v in DEMO.items() if k != "epsilon"}
    assert validate(no_eps, SCHEMA).epsilon == defaults["rate"] == 0.2


def test_same_question_same_canonical_form():
    a = spec(filters=[["sex", "=", "female"], ["age_band", "in", ["80+", "65-79"]]])
    b = spec(filters=[["patients.age_band", "in", ["65-79", "80+", "65-79"]], ["patients.sex", "in", ["female"]]],
             epsilon=0.1)
    assert validate(a, SCHEMA).canonical_json() == validate(b, SCHEMA).canonical_json()


def test_different_questions_differ():
    a = validate(spec(filters=[["sex", "=", "female"]]), SCHEMA)
    b = validate(spec(filters=[["sex", "=", "male"]]), SCHEMA)
    assert a.canonical_json() != b.canonical_json()


def test_pydantic_model_is_accepted():
    model = QuerySpec(dataset="riverside", measure="count", table="patients", group_by=["sex"])
    assert [c.ref for c in validate(model, SCHEMA).group_by] == ["patients.sex"]


def test_mean_of_lab_value_uses_that_tests_bounds():
    v = validate(spec(measure="mean", table="labs", column="value", filters=[["test", "=", "hba1c"]]), SCHEMA)
    assert v.value_bounds == (4.0, 14.0)
    v = validate(spec(measure="sum", table="admissions", column="los_days"), SCHEMA)
    assert v.value_bounds == (0.0, 30.0)


def test_histogram_column_is_the_last_dimension():
    v = validate(spec(measure="histogram", table="conditions", column="condition_group", group_by=["patients.sex"]),
                 SCHEMA)
    assert [c.ref for c in v.group_by] == ["patients.sex", "conditions.condition_group"]
    assert v.canonical()["group_by"] == ["patients.sex"]
    assert v.canonical()["column"] == "conditions.condition_group"


def test_limits_are_inclusive():
    validate(spec(epsilon=1.0), SCHEMA)  # exactly the per-query cap
    validate(spec(table="admissions", filters=[["admit_date", "between", ["2025-01-01", "2025-01-30"]]]),
             SCHEMA)  # exactly 30 days
    validate(spec(filters=[["sex", "=", "female"], ["age_band", "=", "80+"], ["zip3", "=", "606"]]),
             SCHEMA)  # 3 quasi-identifiers


def test_numbers_and_dates_are_normalised():
    v = validate(spec(table="conditions", filters=[["onset_year", "=", 2015]]), SCHEMA)
    assert (v.filters[0].op, v.filters[0].value) == ("between", (2015.0, 2015.0))
    v = validate(spec(table="admissions", filters=[["admit_date", "between", ["2025-01-01", "2025-12-31"]]]), SCHEMA)
    assert v.canonical()["filters"] == [["admissions.admit_date", "between", ["2025-01-01", "2025-12-31"]]]


# --- Refusals: each bad spec gets the right rule and a reason ---------------

DATE = "admissions.admit_date"

BAD = [
    # rule 1: not an aggregate
    ("raw sql", spec(sql="SELECT * FROM patients"), NOT_AGGREGATE, "asks for rows"),
    ("row output", spec(measure="rows"), NOT_AGGREGATE, "measure 'rows'"),
    ("group by patient", spec(group_by=["patient_id"]), NOT_AGGREGATE, "identifies a person"),
    ("filter on row id", spec(table="admissions", filters=[["admission_id", "=", 7]]), NOT_AGGREGATE, "identifies"),
    # rule 2: off-schema
    ("unknown field", spec(note="hi"), OFF_SCHEMA, "unknown field 'note'"),
    ("other dataset", spec(dataset="hospital"), OFF_SCHEMA, "unknown dataset"),
    ("unknown table", spec(table="notes"), OFF_SCHEMA, "unknown table"),
    ("unknown column", spec(filters=[["income", "=", "high"]]), OFF_SCHEMA, "unknown column 'income'"),
    ("unknown operator", spec(filters=[["sex", "like", "f%"]]), OFF_SCHEMA, "unknown operator 'like'"),
    ("not a category", spec(filters=[["sex", "=", "martian"]]), OFF_SCHEMA, "not one of its categories"),
    ("free text", spec(table="conditions", filters=[["condition_group", "=", "diabetes' OR 1=1"]]),
     OFF_SCHEMA, "not one of its categories"),
    ("between on category", spec(filters=[["sex", "between", ["a", "z"]]]), OFF_SCHEMA, "'in' with a list"),
    ("in on number", spec(table="admissions", filters=[["los_days", "in", [1, 2]]]), OFF_SCHEMA, "'between'"),
    ("bool as text", spec(table="admissions", filters=[["readmit_30d", "=", "yes"]]), OFF_SCHEMA, "true or false"),
    ("bad date", spec(table="admissions", filters=[["admit_date", "between", ["March 3", "2026-04-03"]]]),
     OFF_SCHEMA, "YYYY-MM-DD"),
    ("low above high", spec(table="admissions", filters=[["los_days", "between", [9, 2]]]), OFF_SCHEMA, "low is above"),
    ("filter not a triple", spec(filters=[["sex", "female"]]), OFF_SCHEMA, "[column, operator, value]"),
    ("rate without numerator", spec(measure="rate", table="admissions"), OFF_SCHEMA, "needs a numerator_filter"),
    ("numerator on count", spec(numerator_filter=[["sex", "=", "female"]]), OFF_SCHEMA, "only used with"),
    ("sum without column", spec(measure="sum", table="admissions"), OFF_SCHEMA, "needs a column"),
    ("mean of category", spec(measure="mean", column="sex"), OFF_SCHEMA, "not a bounded number"),
    ("lab mean, no test", spec(measure="mean", table="labs", column="value"), OFF_SCHEMA, "exactly one test"),
    ("group by date", spec(table="admissions", group_by=["admit_date"]), OFF_SCHEMA, "only categorical"),
    ("group by other table", spec(table="admissions", group_by=["conditions.condition_group"]),
     OFF_SCHEMA, "'admissions' or 'patients' only"),
    ("epsilon zero", spec(epsilon=0.0), OFF_SCHEMA, "positive"),
    ("epsilon as text", spec(epsilon="0.2"), OFF_SCHEMA, "epsilon"),
    # rule 3: too fine
    ("three group-bys", spec(group_by=["sex", "age_band", "zip3"]), TOO_FINE, "at most 2"),
    ("histogram plus two", spec(measure="histogram", table="conditions", column="condition_group",
                                group_by=["patients.sex", "patients.age_band"]), TOO_FINE, "at most 2"),
    ("62 cells", spec(group_by=["zip3", "sex"]), TOO_FINE, "62 output cells"),
    ("county cells", spec(group_by=["county"]), TOO_FINE, "output cells"),
    ("four quasi-ids", spec(table="admissions", filters=[["patients.sex", "=", "female"], ["patients.age_band", "=", "65-79"],
                                                         ["patients.zip3", "=", "606"],
                                                         [DATE, "between", ["2026-01-01", "2026-06-30"]]]),
     TOO_FINE, "combines 4 quasi-identifiers"),
    ("one-day window", spec(table="admissions", filters=[[DATE, "=", "2026-03-03"]]), TOO_FINE, "at least 30 days"),
    # rule 4: over budget (per-query cap; the running budget comes on Day 4)
    ("epsilon above cap", spec(epsilon=1.5), OVER_BUDGET, "per-query cap"),
]


@pytest.mark.parametrize("raw, rule, reason", [b[1:] for b in BAD], ids=[b[0] for b in BAD])
def test_bad_spec_is_refused_with_reason(raw, rule, reason):
    r = refused(raw)
    assert r.rule == rule, r.reason
    assert reason in r.reason


def test_attack_example_is_refused():
    raw = json.loads((EXAMPLES / "attack_one_patient.json").read_text(encoding="utf-8"))
    assert refused(raw).rule == TOO_FINE


def test_refusal_payload():
    d = refused(spec(measure="rows")).to_dict()
    assert d == {"refused": True, "rule": NOT_AGGREGATE, "rule_number": 1, "reason": d["reason"]}
    assert set(RULES.values()) == {1, 2, 3, 4, 5, 6}
    with pytest.raises(ValueError):
        Refused("because", "no such rule")


def test_schema_rules_are_complete():
    rules = SCHEMA["query_rules"]
    assert set(rules["default_epsilon"]) == set(rules["measures"])
    assert all(0 < e <= rules["max_epsilon"] for e in rules["default_epsilon"].values())
    qi = {f"{t}.{c}" for t, ts in SCHEMA["tables"].items() for c, cs in ts["columns"].items() if cs.get("quasi_identifier")}
    assert qi == {"patients.sex", "patients.age_band", "patients.zip3", "patients.county",
                  "admissions.admit_date", "admissions.discharge_date"}
    for table in SCHEMA["tables"].values():
        assert table["max_rows_per_patient"] >= 1

def test_two_date_windows_cant_make_a_finer_one():  # audit #7
    base = {"dataset": "riverside", "measure": "count", "table": "admissions"}
    a, b = ["admit_date", "between", ["2026-02-02", "2026-03-03"]], ["admit_date", "between", ["2026-03-03", "2026-04-01"]]
    with pytest.raises(Refused, match="more than once"):
        validate({**base, "filters": [a, b]}, SCHEMA)
    rate = {**base, "measure": "rate", "filters": [a], "numerator_filter": [b]}
    with pytest.raises(Refused, match="overlap") as e:
        validate(rate, SCHEMA)
    assert e.value.rule == "too_fine"
    validate({**rate, "numerator_filter": [a]}, SCHEMA)  # the same range in both is fine
