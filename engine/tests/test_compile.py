"""Tests for blindfold.compile: checked specs become safe SQL with exact true answers.

A tiny hand-made database in the analytic schema gives answers we can work
out by hand. A few checks also run on the real data/riverside.duckdb when
`make data` has built it, and are skipped otherwise.

    cd engine && python -m pytest -q tests/test_compile.py
"""

from __future__ import annotations

import datetime as dt
import json

import duckdb
import pytest

from blindfold import load
from blindfold.compile import compile_spec, run
from blindfold.spec import validate

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)
EXAMPLES = load.REPO_ROOT / "datasets" / "riverside" / "examples"
needs_real_db = pytest.mark.skipif(not load.DEFAULT_DB.exists(), reason="run `make data` to check the real tables")

PATIENTS = [
    # patient_id, sex, age_band, zip3, county
    (1, "female", "65-79", "606", "Cook County"),
    (2, "male", "80+", "606", "Cook County"),
    (3, "female", "18-34", "627", "Sangamon County"),
    (4, "male", "65-79", "unknown", "Champaign County"),
]

D = dt.date


def _admissions():
    rows, n = [], 0
    # Patient 1: 7 inpatient stays, all followed by a readmission (cap keeps 5).
    for i in range(7):
        n += 1
        rows.append((n, 1, D(2025, 1, 1 + 4 * i), D(2025, 1, 3 + 4 * i), "inpatient", 2, True))
    # Patient 2: 2 inpatient (1 readmission), 1 emergency with a 40-day stay (clipped to 30).
    rows += [
        (n + 1, 2, D(2025, 3, 1), D(2025, 3, 5), "inpatient", 4, True),
        (n + 2, 2, D(2025, 3, 20), D(2025, 3, 22), "inpatient", 2, False),
        (n + 3, 2, D(2025, 6, 1), D(2025, 7, 11), "emergency", 40, False),
    ]
    # Patient 3: 1 ambulatory visit. Patient 4: none.
    rows.append((n + 4, 3, D(2025, 5, 5), D(2025, 5, 5), "ambulatory", 0, False))
    return rows


ADMISSIONS = _admissions()

CONDITIONS = [
    # patient_id, condition_group, onset_year
    (1, "diabetes", 2010),
    (2, "diabetes", 2020),
    (2, "hypertension", 2012),
    (3, "asthma", 2015),
]

MEDICATIONS = [(1, "metformin", 2011)]

LABS = [
    # patient_id, test, value, year (20.0 is above the HbA1c bound and clips to 14.0)
    (1, "hba1c", 7.0, 2024),
    (1, "hba1c", 8.0, 2025),
    (2, "hba1c", 20.0, 2025),
    (2, "bmi", 31.0, 2025),
]


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    path = tmp_path_factory.mktemp("compile") / "tiny.duckdb"
    c = duckdb.connect(str(path))
    data = {"patients": PATIENTS, "admissions": ADMISSIONS, "conditions": CONDITIONS,
            "medications": MEDICATIONS, "labs": LABS}
    for table, rows in data.items():
        cols = SCHEMA["tables"][table]["columns"]
        defs = ", ".join(f"{name} {col['type']}" for name, col in cols.items())
        c.execute(f"CREATE TABLE {table} ({defs})")
        c.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", rows)
    yield c
    c.close()


@pytest.fixture(scope="module")
def real_con():
    c = duckdb.connect(str(load.DEFAULT_DB), read_only=True)
    yield c
    c.close()


def answer(con, **raw):
    raw = {"dataset": "riverside", **raw}
    return run(con, compile_spec(validate(raw, SCHEMA)))


def cells(rows, *outputs):
    return [(tuple(r["cell"].values()), *(r[o] for o in outputs)) for r in rows]


# --- Exact answers on the tiny database ---------------------------------------

def test_count_all_patients(con):
    assert answer(con, measure="count", table="patients") == [{"cell": {}, "n": 4}]


def test_group_by_lists_every_cell_in_schema_order(con):
    rows = answer(con, measure="count", table="patients", group_by=["age_band"])
    assert cells(rows, "n") == [(("0-17",), 0), (("18-34",), 1), (("35-49",), 0), (("50-64",), 0),
                                (("65-79",), 2), (("80+",), 1)]


def test_two_group_bys_give_the_full_grid(con):
    rows = answer(con, measure="count", table="patients", group_by=["sex", "age_band"])
    assert len(rows) == 12
    assert rows[0]["cell"] == {"patients.sex": "female", "patients.age_band": "0-17"}
    assert sum(r["n"] for r in rows) == 4


def test_contribution_cap_limits_each_patient(con):
    # Patient 1 has 7 admissions but counts as 5; patient 2 has 3, patient 3 has 1.
    assert answer(con, measure="count", table="admissions")[0]["n"] == 5 + 3 + 1


def test_cap_applies_after_filters(con):
    rows = answer(con, measure="count", table="admissions", filters=[["encounter_class", "=", "emergency"]])
    assert rows[0]["n"] == 1


def test_filter_on_another_table_means_has_a_matching_row(con):
    rows = answer(con, measure="count", table="patients", filters=[["conditions.condition_group", "=", "diabetes"]])
    assert rows[0]["n"] == 2


def test_filters_on_the_same_other_table_match_one_row(con):
    # Patient 2 has diabetes (2020) and hypertension (2012); neither row is "diabetes before 2015".
    rows = answer(con, measure="count", table="patients",
                  filters=[["conditions.condition_group", "=", "diabetes"], ["conditions.onset_year", "between", [2000, 2014]]])
    assert rows[0]["n"] == 1


def test_demo_rate_by_sex(con):
    raw = json.loads((EXAMPLES / "readmit_diabetes_65plus_by_sex.json").read_text(encoding="utf-8"))
    rows = run(con, compile_spec(validate(raw, SCHEMA)))
    # Female: patient 1, 7 inpatient stays capped to 5, all readmitted.
    # Male: patient 2 (80+, diabetic), 2 inpatient stays, 1 readmitted. Patient 4 has no stays.
    assert cells(rows, "numerator", "denominator") == [(("female",), 5, 5), (("male",), 1, 2)]


def test_mean_clips_to_the_tests_bounds(con):
    rows = answer(con, measure="mean", table="labs", column="value", filters=[["test", "=", "hba1c"]])
    assert rows == [{"cell": {}, "sum": 7.0 + 8.0 + 14.0, "n": 3}]


def test_sum_clips_length_of_stay(con):
    rows = answer(con, measure="sum", table="admissions", column="los_days", filters=[["patients.sex", "=", "male"]])
    assert rows[0]["sum"] == 4 + 2 + 30


def test_histogram_over_condition_groups(con):
    rows = answer(con, measure="histogram", table="conditions", column="condition_group")
    got = {r["cell"]["conditions.condition_group"]: r["n"] for r in rows}
    assert list(got) == SCHEMA["tables"]["conditions"]["columns"]["condition_group"]["categories"]
    assert got == {"diabetes": 2, "hypertension": 1, "copd": 0, "heart_failure": 0, "ckd": 0, "asthma": 1,
                   "depression": 0, "obesity": 0}


def test_group_by_boolean(con):
    rows = answer(con, measure="count", table="admissions", group_by=["readmit_30d"])
    assert [r["cell"]["admissions.readmit_30d"] for r in rows] == [False, True]
    assert sum(r["n"] for r in rows) == 9


def test_empty_result_still_returns_zero(con):
    rows = answer(con, measure="mean", table="labs", column="value", filters=[["test", "=", "ldl"]])
    assert rows == [{"cell": {}, "sum": 0.0, "n": 0}]


# --- Safety properties of the SQL ---------------------------------------------

def test_values_are_parameters_not_sql_text(con):
    cq = compile_spec(validate({"dataset": "riverside", "measure": "count", "table": "patients",
                                "filters": [["conditions.condition_group", "=", "diabetes"], ["zip3", "=", "606"]]},
                               SCHEMA))
    assert "diabetes" not in cq.sql and "606" not in cq.sql
    assert {"diabetes", "606"} <= set(cq.params)
    assert cq.sql.count("?") == len(cq.params)


def test_compile_is_deterministic(con):
    raw = {"dataset": "riverside", "measure": "rate", "table": "admissions",
           "numerator_filter": [["readmit_30d", "=", True]], "group_by": ["patients.sex"]}
    a, b = compile_spec(validate(raw, SCHEMA)), compile_spec(validate(raw, SCHEMA))
    assert (a.sql, a.params) == (b.sql, b.params)
    assert run(con, a) == run(con, b)


def test_sensitivity_matches_cap_and_bounds():
    def sens(**raw):
        return compile_spec(validate({"dataset": "riverside", **raw}, SCHEMA)).sensitivity

    assert sens(measure="count", table="patients") == {"n": 1.0}
    assert sens(measure="count", table="admissions") == {"n": 5.0}
    assert sens(measure="rate", table="admissions", numerator_filter=[["readmit_30d", "is", True]]) == {
        "numerator": 5.0, "denominator": 5.0}
    assert sens(measure="mean", table="labs", column="value", filters=[["test", "=", "hba1c"]]) == {
        "sum": 5 * 14.0, "n": 5.0}


# --- The real build (skipped until `make data` has run) -----------------------

@needs_real_db
def test_demo_question_on_real_data(real_con):
    raw = json.loads((EXAMPLES / "readmit_diabetes_65plus_by_sex.json").read_text(encoding="utf-8"))
    rows = run(real_con, compile_spec(validate(raw, SCHEMA)))
    assert [r["cell"]["patients.sex"] for r in rows] == ["female", "male"]
    for r in rows:
        assert 0 <= r["numerator"] <= r["denominator"]
        assert r["denominator"] > 0


@needs_real_db
def test_cap_holds_on_real_data(real_con):
    n_patients = real_con.execute("SELECT count(*) FROM patients").fetchone()[0]
    for table in ("admissions", "labs"):
        k = SCHEMA["tables"][table]["max_rows_per_patient"]
        n = answer(real_con, measure="count", table=table)[0]["n"]
        raw = real_con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert n <= k * n_patients < raw


@needs_real_db
@pytest.mark.parametrize("raw", [
    {"measure": "count", "table": "patients", "group_by": ["sex", "age_band"]},
    {"measure": "sum", "table": "admissions", "column": "los_days", "filters": [["encounter_class", "=", "inpatient"]]},
    {"measure": "mean", "table": "labs", "column": "value", "filters": [["test", "=", "hba1c"]], "group_by": ["patients.sex"]},
    {"measure": "histogram", "table": "medications", "column": "drug_class"},
], ids=["count", "sum", "mean", "histogram"])
def test_every_measure_runs_on_real_data(real_con, raw):
    rows = answer(real_con, **raw)
    assert rows and all(isinstance(v, (int, float)) for r in rows for k, v in r.items() if k != "cell")