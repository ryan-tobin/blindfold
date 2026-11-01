"""Tests for blindfold.load.

A tiny hand-made Synthea-shaped dataset is built into a temporary DuckDB file.
The schema checks also run against the real data/riverside.duckdb when
`make data` has built it, and are skipped otherwise.

    cd engine && python -m pytest -q
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

import duckdb
import pytest

from blindfold import load

SCHEMA = load.load_schema(load.DEFAULT_SCHEMA)

# Synthea v4.0.0 CSV headers (only the files load.py reads).
HEADERS = {
    "patients": "Id,BIRTHDATE,DEATHDATE,SSN,DRIVERS,PASSPORT,PREFIX,FIRST,MIDDLE,LAST,SUFFIX,MAIDEN,MARITAL,RACE,"
    "ETHNICITY,GENDER,BIRTHPLACE,ADDRESS,CITY,STATE,COUNTY,FIPS,ZIP,LAT,LON,HEALTHCARE_EXPENSES,"
    "HEALTHCARE_COVERAGE,INCOME",
    "encounters": "Id,START,STOP,PATIENT,ORGANIZATION,PROVIDER,PAYER,ENCOUNTERCLASS,CODE,DESCRIPTION,"
    "BASE_ENCOUNTER_COST,TOTAL_CLAIM_COST,PAYER_COVERAGE,REASONCODE,REASONDESCRIPTION",
    "conditions": "START,STOP,PATIENT,ENCOUNTER,SYSTEM,CODE,DESCRIPTION",
    "medications": "START,STOP,PATIENT,PAYER,ENCOUNTER,CODE,DESCRIPTION,BASE_COST,PAYER_COVERAGE,DISPENSES,"
    "TOTALCOST,REASONCODE,REASONDESCRIPTION",
    "observations": "DATE,PATIENT,ENCOUNTER,CATEGORY,CODE,DESCRIPTION,VALUE,UNITS,TYPE",
}

PA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"  # female, 76, ZIP 60614
PB = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"  # male, 16, ZIP 00000 (Synthea's "no ZIP")
PC = "cccccccc-3333-4333-8333-cccccccccccc"  # dead: must be excluded
PD = "dddddddd-4444-4444-8444-dddddddddddd"  # male, turns 86 the day after as_of

NAMES = ["Adaline", "Zyxwell", "Bertram", "Quillby", "Cordelia", "Dmitrius"]

PATIENTS = [
    # Id, BIRTHDATE, DEATHDATE, FIRST, LAST, GENDER, ADDRESS, CITY, COUNTY, ZIP
    (PA, "1950-06-15", "", "Adaline", "Zyxwell", "F", "1 Elm St", "Chicago", "Cook County", "60614"),
    (PB, "2010-01-01", "", "Bertram", "Quillby", "M", "2 Oak St", "Urbana", "Champaign County", "00000"),
    (PC, "1960-01-01", "2020-01-01", "Cordelia", "Quillby", "F", "3 Ash St", "Chicago", "Cook County", "60614"),
    (PD, "1940-11-01", "", "Dmitrius", "Zyxwell", "M", "4 Fir St", "Springfield", "Sangamon County", "62701"),
]

# (id, start, stop, patient, class, expected readmit_30d or None if dropped)
ENCOUNTERS = [
    ("e1", "2025-01-01T08:00:00Z", "2025-01-05T10:00:00Z", PA, "inpatient", True),    # e2 starts 29d 23h later
    ("e2", "2025-02-04T09:00:00Z", "2025-02-06T09:00:00Z", PA, "inpatient", False),   # e3 starts 31d 1h later
    ("e3", "2025-03-09T10:00:00Z", "2025-03-10T10:00:00Z", PA, "inpatient", False),   # no inpatient after it
    ("e4", "2025-03-15T09:00:00Z", "2025-03-15T09:30:00Z", PA, "ambulatory", False),  # nothing within 30 days
    ("e5", "2025-06-01T12:00:00Z", "2025-06-01T14:00:00Z", PA, "emergency", True),    # e6 is inpatient 9 days later
    ("e6", "2025-06-10T08:00:00Z", "2025-06-12T08:00:00Z", PA, "inpatient", False),   # e9 is another patient
    ("e7", "2025-07-01T08:00:00Z", "2025-07-01T09:00:00Z", PA, "wellness", None),     # class not kept
    ("e8", "2026-10-25T18:50:17Z", "2026-11-01T14:58:15Z", PA, "inpatient", False),   # still open: capped at as_of
    ("e9", "2025-06-13T08:00:00Z", "2025-06-14T08:00:00Z", PB, "inpatient", False),
    ("e10", "2019-05-01T08:00:00Z", "2019-05-03T08:00:00Z", PC, "inpatient", None),   # dead patient
]

CONDITIONS = [
    # START, PATIENT, SYSTEM, CODE, DESCRIPTION
    ("2010-05-01", PA, "SNOMED-CT", "44054006", "Diabetes mellitus type 2 (disorder)"),
    ("2015-02-01", PA, "SNOMED-CT", "127013003", "Disorder of kidney due to diabetes mellitus (disorder)"),
    ("2012-03-01", PA, "SNOMED-CT", "59621000", "Essential hypertension (disorder)"),
    ("2018-01-01", PA, "ICD10", "K02.9", "Dental caries  unspecified"),
    ("2015-09-01", PB, "SNOMED-CT", "233678006", "Childhood asthma (disorder)"),
    ("2001-01-01", PC, "SNOMED-CT", "59621000", "Essential hypertension (disorder)"),
]

MEDICATIONS = [
    # START, PATIENT, CODE, DESCRIPTION
    ("2011-04-01T08:00:00Z", PA, "860975", "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet"),
    ("2013-04-01T08:00:00Z", PA, "314231", "Simvastatin 10 MG Oral Tablet"),
    ("2016-04-01T08:00:00Z", PA, "314231", "Simvastatin 10 MG Oral Tablet"),
    ("2017-04-01T08:00:00Z", PA, "000000", "Nystatin 100000 UNT/ML Oral Suspension"),  # not a statin
    ("2020-04-01T08:00:00Z", PB, "000001", "Acetaminophen 325 MG Oral Tablet"),
]

OBSERVATIONS = [
    # DATE, PATIENT, CODE, VALUE, TYPE
    ("2024-01-01T08:00:00Z", PA, "4548-4", "25.0", "numeric"),     # HbA1c above bound -> 14.0
    ("2025-01-01T08:00:00Z", PA, "4548-4", "6.1", "numeric"),
    ("2024-01-01T08:00:00Z", PA, "39156-5", "5.0", "numeric"),     # BMI below bound -> 12.0
    ("2024-01-01T08:00:00Z", PA, "8480-6", "120.0", "numeric"),
    ("2024-01-01T08:00:00Z", PA, "18262-6", "N/A", "text"),        # not a number -> dropped
    ("2024-01-01T08:00:00Z", PA, "8462-4", "80.0", "numeric"),     # diastolic BP: not one of the 4 tests
    ("2024-06-01T08:00:00Z", PB, "39156-5", "18.5", "numeric"),
]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def write_csv(path: Path, header: str, rows: list[dict]) -> None:
    cols = header.split(",")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in cols})


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("synthea")
    csv_dir = tmp / "csv"
    csv_dir.mkdir()
    write_csv(csv_dir / "patients.csv", HEADERS["patients"], [
        dict(Id=i, BIRTHDATE=b, DEATHDATE=d, FIRST=f, LAST=l, GENDER=g, ADDRESS=a, CITY=c,
             STATE="Illinois", COUNTY=co, ZIP=z, SSN="999-12-3456")
        for i, b, d, f, l, g, a, c, co, z in PATIENTS
    ])
    write_csv(csv_dir / "encounters.csv", HEADERS["encounters"], [
        dict(Id=i, START=s, STOP=e, PATIENT=p, ENCOUNTERCLASS=k) for i, s, e, p, k, _ in ENCOUNTERS
    ])
    write_csv(csv_dir / "conditions.csv", HEADERS["conditions"], [
        dict(START=s, PATIENT=p, SYSTEM=sy, CODE=c, DESCRIPTION=d) for s, p, sy, c, d in CONDITIONS
    ])
    write_csv(csv_dir / "medications.csv", HEADERS["medications"], [
        dict(START=s, PATIENT=p, CODE=c, DESCRIPTION=d) for s, p, c, d in MEDICATIONS
    ])
    write_csv(csv_dir / "observations.csv", HEADERS["observations"], [
        dict(DATE=d, PATIENT=p, CODE=c, VALUE=v, TYPE=t) for d, p, c, v, t in OBSERVATIONS
    ])
    db = tmp / "riverside.duckdb"
    load.build(csv_dir, db)
    return db


@pytest.fixture
def fixture_con(fixture_db):
    con = duckdb.connect(str(fixture_db), read_only=True)
    yield con
    con.close()


def _db_params():
    params = ["fixture"]
    marks = [] if load.DEFAULT_DB.exists() else [pytest.mark.skip(reason="run `make data` to check the real tables")]
    params.append(pytest.param("real", marks=marks))
    return params


@pytest.fixture(params=_db_params())
def con(request, fixture_db):
    path = fixture_db if request.param == "fixture" else load.DEFAULT_DB
    c = duckdb.connect(str(path), read_only=True)
    yield c
    c.close()


def columns(con, table) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in con.execute(f"DESCRIBE {table}").fetchall()]


# --- Schema checks: run on the fixture and on the real build ---------------

def test_all_five_tables_exist(con):
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert tables == set(load.TABLES) == set(SCHEMA["tables"])


@pytest.mark.parametrize("table", load.TABLES)
def test_columns_match_schema(con, table):
    expected = [(name, spec["type"]) for name, spec in SCHEMA["tables"][table]["columns"].items()]
    assert columns(con, table) == expected


def test_no_identifiers_beyond_patient_id(con):
    allowed_ids = {"patient_id", "admission_id"}  # admission_id is a fresh row number, per the brief's schema
    banned = re.compile(r"(name|first|last|ssn|address|birth|zip$|lat|lon|drivers|passport|uuid|encounter$|^id$)", re.I)
    for table in load.TABLES:
        for name, _ in columns(con, table):
            assert not (name.endswith("_id") and name not in allowed_ids), f"{table}.{name}"
            assert not banned.search(name), f"{table}.{name}"
    for table in load.TABLES:
        for name, typ in columns(con, table):
            if typ != "VARCHAR":
                continue
            values = [r[0] for r in con.execute(f"SELECT DISTINCT {name} FROM {table}").fetchall()]
            assert not any(v and UUID_RE.search(v) for v in values), f"{table}.{name} holds source IDs"


def test_categorical_values_are_in_schema(con):
    for table, tspec in SCHEMA["tables"].items():
        for name, spec in tspec["columns"].items():
            if "categories" not in spec:
                continue
            values = {r[0] for r in con.execute(f"SELECT DISTINCT {name} FROM {table}").fetchall()}
            assert values <= set(spec["categories"]), f"{table}.{name}: {values - set(spec['categories'])}"


def test_numeric_values_are_within_bounds(con):
    for table, tspec in SCHEMA["tables"].items():
        for name, spec in tspec["columns"].items():
            if "bounds" in spec:
                lo, hi = spec["bounds"]
                bad = con.execute(f"SELECT count(*) FROM {table} WHERE {name} < ? OR {name} > ?", [lo, hi]).fetchone()[0]
                assert bad == 0, f"{table}.{name} outside [{lo}, {hi}]"


def test_lab_values_are_within_test_bounds(con):
    bounds = SCHEMA["tables"]["labs"]["columns"]["value"]["bounds_by_test"]
    for test, (lo, hi) in bounds.items():
        bad = con.execute(
            "SELECT count(*) FROM labs WHERE test = ? AND (value < ? OR value > ? OR value IS NULL)", [test, lo, hi]
        ).fetchone()[0]
        assert bad == 0, f"{test} outside [{lo}, {hi}]"


def test_patient_ids_are_unique_and_referenced(con):
    n, distinct = con.execute("SELECT count(*), count(DISTINCT patient_id) FROM patients").fetchone()
    assert n == distinct
    for table in ("admissions", "conditions", "medications", "labs"):
        orphans = con.execute(
            f"SELECT count(*) FROM {table} t ANTI JOIN patients p USING (patient_id)"
        ).fetchone()[0]
        assert orphans == 0, table


# --- Exact checks on the hand-made fixture ----------------------------------

def test_no_source_names_or_ids_anywhere(fixture_con):
    secrets = NAMES + [p[0] for p in PATIENTS] + ["999-12-3456", "Elm St", "60614", "1950-06-15"]
    for table in load.TABLES:
        for row in fixture_con.execute(f"SELECT * FROM {table}").fetchall():
            text = " ".join(str(v) for v in row)
            for s in secrets:
                assert s not in text, f"{s!r} leaked into {table}"


def test_patients_derived_fields(fixture_con):
    rows = fixture_con.execute("SELECT sex, age_band, zip3, county FROM patients ORDER BY patient_id").fetchall()
    # patient_id follows the sorted Synthea Id: PA, PB, PD (PC is dead)
    assert rows == [
        ("female", "65-79", "606", "Cook County"),
        ("male", "0-17", "unknown", "Champaign County"),
        ("male", "80+", "627", "Sangamon County"),
    ]


def test_readmit_30d_on_fixture(fixture_con):
    got = fixture_con.execute(
        "SELECT admit_date, encounter_class, readmit_30d FROM admissions ORDER BY admission_id"
    ).fetchall()
    kept = [e for e in ENCOUNTERS if e[5] is not None]
    kept.sort(key=lambda e: ([p[0] for p in PATIENTS].index(e[3]), e[1]))
    expected = [(dt.date.fromisoformat(e[1][:10]), e[4], e[5]) for e in kept]
    assert got == expected


def test_open_stay_is_capped_at_as_of(fixture_con):
    row = fixture_con.execute(
        "SELECT discharge_date, los_days FROM admissions WHERE admit_date = DATE '2026-10-25'"
    ).fetchone()
    assert row == (dt.date.fromisoformat(SCHEMA["as_of"]), 6)


def test_conditions_grouped_once_per_patient(fixture_con):
    rows = fixture_con.execute(
        "SELECT patient_id, condition_group, onset_year FROM conditions ORDER BY patient_id, condition_group"
    ).fetchall()
    assert rows == [(1, "diabetes", 2010), (1, "hypertension", 2012), (2, "asthma", 2015)]


def test_medications_grouped_by_class(fixture_con):
    rows = fixture_con.execute(
        "SELECT patient_id, drug_class, start_year FROM medications ORDER BY patient_id, drug_class"
    ).fetchall()
    assert rows == [(1, "metformin", 2011), (1, "statin", 2013)]


def test_labs_clipped_and_filtered(fixture_con):
    rows = fixture_con.execute("SELECT patient_id, test, value, year FROM labs ORDER BY patient_id, test, year").fetchall()
    assert rows == [
        (1, "bmi", 12.0, 2024),
        (1, "hba1c", 14.0, 2024),
        (1, "hba1c", 6.1, 2025),
        (1, "systolic_bp", 120.0, 2024),
        (2, "bmi", 18.5, 2024),
    ]


def test_schema_file_is_consistent():
    assert SCHEMA["privacy_unit"] == "patient_id"
    with open(load.DEFAULT_CONDITION_MAP, encoding="utf-8") as f:
        groups = {row["condition_group"] for row in csv.DictReader(f)}
    assert groups == set(SCHEMA["tables"]["conditions"]["columns"]["condition_group"]["categories"])
    assert {name for name, _ in load.DRUG_CLASSES} == set(
        SCHEMA["tables"]["medications"]["columns"]["drug_class"]["categories"]
    )
    assert set(load.LAB_CODES.values()) == set(SCHEMA["tables"]["labs"]["columns"]["value"]["bounds_by_test"])
    script = (load.REPO_ROOT / "scripts" / "generate_data.sh").read_text(encoding="utf-8")
    assert f"REFERENCE_DATE={SCHEMA['as_of'].replace('-', '')}" in script
    json.dumps(SCHEMA)  # serialisable