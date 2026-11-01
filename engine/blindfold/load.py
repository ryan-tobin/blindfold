"""Build the Riverside analytic schema in DuckDB from Synthea's raw CSV files.

    python -m blindfold.load
    python -m blindfold.load --csv DIR --db FILE

Reads patients, encounters, conditions, medications and observations CSVs
(Synthea v4.0.0 columns) and writes five tables whose columns match
datasets/riverside/schema.json exactly: patients, admissions, conditions,
medications, labs. Source identifiers (Synthea UUIDs, names, addresses,
exact birth dates, full ZIPs) never reach the output tables.
"""

from __future__ import annotations

import argparse 
import json 
from pathlib import Path 

import duckdb 

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_DIR = REPO_ROOT / "data" / "synthea" / "csv"
DEFAULT_DB = REPO_ROOT / "data" / "riverside.duckdb"
DEFAULT_SCHEMA = REPO_ROOT / "datasets" / "riverside" / "schema.json"
DEFAULT_CONDITION_MAP = REPO_ROOT / "datasets" / "riverside" / "condition_map.csv"

TABLES = ("patients", "admissions", "conditions", "medications", "labs")
RAW_FILES = ("patients", "encounters", "conditions", "medications", "observations")

# Encounter classes kept in `admissions`. Synthea also writes wellness,
# outpatient, urgentcare, home, virtual, snf and hospice; those are dropped
ENCOUNTER_CLASSES = ("inpatient", "emergency", "ambulatory")
READMIT_WINDOW_DAYS = 30

# LOINC codes Synthea uses for the four lab tests.
LAB_CODES = {
    "4548-4": "hba1c",          # Hemoglobin A1c/Hemoglobin.total in Blood (%)
    "18262-6": "ldl",           # Cholesterol in LDL, direct assay (mg/dL)
    "8480-6": "systolic_bp",    # Systolic blood pressure (mm[Hg])
    "39156-5": "bmi",           # Body mass index (kg/m2)
}
# Drug classes by ingredient name in the RxNorm description. Synthea uses a 
# separate RxNorm code for every strength and brand (130+ codes for these six
# classes), so matching the ingredient is shorter and catches them all.
# First match wins. Regexes run ont he lower-cased DESCRIPTION
DRUG_CLASSES = (
    ("metformin", r"\bmetformin\b"),
    ("insulin", r"\binsulin\b"),
    ("statin", r"\b(atorvastatin|simvastatin|pravastatin|rosuvastatin|lovastatin|pitavastatin|fluvastatin)\b"),
    ("ace_inhibitor", r"\b(lisinopril|enalapril|ramipril|captopril|benazepril|quinapril|fosinopril|perindopril|trandolapril|moexipril)\b"),
    ("beta_blocker", r"\b(metoprolol|atenolol|carvedilol|propranolol|bisoprolol|nadolol|labetalol|nebivolol)\b"),
    ("ssri", r"\b(fluoxetine|sertraline|citalopram|escitalopram|paroxetine|fluvoxamine)\b"),
)

def sql_str(value: str) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"

def load_schema(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
    
def build(
        csv_dir: Path = DEFAULT_CSV_DIR,
        db_path: Path = DEFAULT_DB,
        schema_path: Path = DEFAULT_SCHEMA,
        condition_map_path: Path = DEFAULT_CONDITION_MAP,
) -> dict[str, int]:
    """Build all five tables into db_path. Returns {table: row count}."""
    csv_dir, db_path = Path(csv_dir), Path(db_path)
    schema = load_schema(schema_path)
    as_of = sql_str(schema["as_of"])

    for name in RAW_FILES:
        if not (csv_dir / f"{name}.csv").exists():
            raise FileNotFoundError(f"{csv_dir / (name + '.csv')} not found. Run scripts/generate_data.sh fist.")
        
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        for name in RAW_FILES:
            path = sql_str((csv_dir / f"{name}.csv").as_posix())
            con.execute(f"CREATE TEMP VIEW raw_{name} AS SELECT * FROM read_csv({path}, header=true, all_varchar=true)")
        con.execute(
            "CREATE TEMP TABLE condition_map AS SELECT * FROM read_csv("
            f"{sql_str(Path(condition_map_path).as_posix())}, header=true, all_varchar=true)"
        )

        # Synthea UUID -> new patient number. Sorted by UUID so it is deterministic.
        con.execute(f"""
            CREATE TEMP TABLE patient_key AS
            SELECT Id AS source_id,
                   CAST(row_number() OVER (ORDER BY Id) AS INTEGER) AS patient_id,
                   CAST(BIRTHDATE AS DATE) AS birth_date,
                   GENDER, ZIP, COUNTY
            FROM raw_patients
            WHERE DEATHDATE IS NULL AND CAST(BIRTHDATE AS DATE) <= DATE {as_of}
        """)

        build_patients(con, as_of)
        build_admissions(con, as_of)
        build_conditions(con)
        build_medications(con)
        build_labs(con, schema)

        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        con.close()
    return counts 

def build_patients(con: duckdb.DuckDBPyConnection, as_of: str) -> None:
    con.execute(f"""
        CREATE TABLE patients AS
        WITH aged AS (
            SELECT *, CAST(extract(year FROM age(DATE {as_of}, birth_date)) AS INTEGER) AS age
            FROM patient_key
        )
        SELECT
            patient_id,
            CASE GENDER WHEN 'F' THEN 'female' WHEN 'M' THEN 'male' END AS sex,
            CASE
                WHEN age < 18 THEN '0-17'
                WHEN age < 35 THEN '18-34'
                WHEN age < 50 THEN '35-49'
                WHEN age < 65 THEN '50-64'
                WHEN age < 80 THEN '65-79'
                ELSE '80+'
            END AS age_band,
            CASE
                WHEN ZIP IS NULL OR NOT regexp_full_match(ZIP, '[0-9]{{3,5}}') THEN 'unknown'
                WHEN lpad(ZIP, 5, '0') = '00000' THEN 'unknown'
                ELSE substr(lpad(ZIP, 5, '0'), 1, 3)
            END AS zip3,
            COUNTY AS county
        FROM aged
        ORDER BY patient_id
    """)


def build_admissions(con: duckdb.DuckDBPyConnection, as_of: str) -> None:
    classes = ", ".join(sql_str(c) for c in ENCOUNTER_CLASSES)
    con.execute(f"""
        CREATE TABLE admissions AS
        WITH enc AS (
            SELECT
                e.Id AS source_id,
                k.patient_id,
                e.ENCOUNTERCLASS AS encounter_class,
                CAST(e.START AS TIMESTAMP) AS start_ts,
                -- Stays still open at the end of the simulation get the wall-clock
                -- time of the Synthea run as STOP; cap at as_of so reruns match.
                least(coalesce(CAST(e.STOP AS TIMESTAMP), CAST(e.START AS TIMESTAMP)),
                      CAST(DATE {as_of} AS TIMESTAMP)) AS stop_ts
            FROM raw_encounters e
            JOIN patient_key k ON k.source_id = e.PATIENT
            WHERE e.ENCOUNTERCLASS IN ({classes})
              AND CAST(e.START AS TIMESTAMP) <= CAST(DATE {as_of} AS TIMESTAMP)
        ),
        numbered AS (
            SELECT *, CAST(row_number() OVER (ORDER BY patient_id, start_ts, source_id) AS INTEGER) AS admission_id
            FROM enc
        )
        SELECT
            a.admission_id,
            a.patient_id,
            CAST(a.start_ts AS DATE) AS admit_date,
            CAST(a.stop_ts AS DATE) AS discharge_date,
            a.encounter_class,
            CAST(least(greatest(date_diff('day', CAST(a.start_ts AS DATE), CAST(a.stop_ts AS DATE)), 0), 30) AS INTEGER) AS los_days,
            EXISTS (
                SELECT 1 FROM numbered b
                WHERE b.patient_id = a.patient_id
                  AND b.admission_id <> a.admission_id
                  AND b.encounter_class = 'inpatient'
                  AND b.start_ts >= a.stop_ts
                  AND b.start_ts <= a.stop_ts + INTERVAL {READMIT_WINDOW_DAYS} DAY
            ) AS readmit_30d
        FROM numbered a
        ORDER BY a.admission_id
    """)


def build_conditions(con: duckdb.DuckDBPyConnection) -> None:
    # One row per patient per group, with the year of the earliest diagnosis.
    con.execute("""
        CREATE TABLE conditions AS
        SELECT
            k.patient_id,
            m.condition_group,
            CAST(min(year(CAST(c.START AS DATE))) AS INTEGER) AS onset_year
        FROM raw_conditions c
        JOIN patient_key k ON k.source_id = c.PATIENT
        JOIN condition_map m ON m.system = c.SYSTEM AND m.code = c.CODE
        GROUP BY k.patient_id, m.condition_group
        ORDER BY k.patient_id, m.condition_group
    """)


def build_medications(con: duckdb.DuckDBPyConnection) -> None:
    whens = "\n".join(
        f"WHEN regexp_matches(lower(DESCRIPTION), {sql_str(rx)}) THEN {sql_str(name)}"
        for name, rx in DRUG_CLASSES
    )
    con.execute(f"""
        CREATE TABLE medications AS
        WITH classified AS (
            SELECT PATIENT, START, CASE {whens} END AS drug_class
            FROM raw_medications
        )
        SELECT
            k.patient_id,
            c.drug_class,
            CAST(min(year(CAST(c.START AS TIMESTAMP))) AS INTEGER) AS start_year
        FROM classified c
        JOIN patient_key k ON k.source_id = c.PATIENT
        WHERE c.drug_class IS NOT NULL
        GROUP BY k.patient_id, c.drug_class
        ORDER BY k.patient_id, c.drug_class
    """)


def build_labs(con: duckdb.DuckDBPyConnection, schema: dict) -> None:
    bounds = schema["tables"]["labs"]["columns"]["value"]["bounds_by_test"]
    rows = ", ".join(
        f"({sql_str(code)}, {sql_str(test)}, {float(bounds[test][0])}, {float(bounds[test][1])})"
        for code, test in LAB_CODES.items()
    )
    con.execute(f"""
        CREATE TABLE labs AS
        WITH lab_code(code, test, lo, hi) AS (VALUES {rows})
        SELECT
            k.patient_id,
            l.test,
            CAST(least(greatest(TRY_CAST(o.VALUE AS DOUBLE), l.lo), l.hi) AS DOUBLE) AS value,
            CAST(year(CAST(o.DATE AS TIMESTAMP)) AS INTEGER) AS year
        FROM raw_observations o
        JOIN lab_code l ON l.code = o.CODE
        JOIN patient_key k ON k.source_id = o.PATIENT
        WHERE TRY_CAST(o.VALUE AS DOUBLE) IS NOT NULL
        ORDER BY k.patient_id, l.test, year, value
    """)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Riverside analytic tables in DuckDB.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_DIR, help="folder with Synthea CSV files")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB file to (re)create")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--condition-map", type=Path, default=DEFAULT_CONDITION_MAP)
    args = parser.parse_args()

    counts = build(args.csv, args.db, args.schema, args.condition_map)
    print(f"Built {args.db}")
    for table in TABLES:
        print(f"  {table:<12} {counts[table]:>10,} rows")


if __name__ == "__main__":
    main()