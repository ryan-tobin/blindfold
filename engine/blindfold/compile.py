"""Compile a checked QuerySpec into parameterized DuckDB SQL.

    python -m blindfold.compile SPEC.json          # check the spec, print its SQL
    python -m blindfold.compile SPEC.json --run    # also print the TRUE values (dev only)

The SQL is built from a fixed template. Table and column names come only
from schema.json (spec.py has already checked them) and every value is a `?`
parameter, so nothing a buyer types is ever pasted into SQL.

Two things the template always does, because the noise added on Day 3 relies
on them:

1. Contribution cap. After the filters, each patient keeps at most k rows
   (max_rows_per_patient in schema.json, e.g. 5 admissions). Which k rows is
   a fixed pseudo-random pick (ordered by a hash of the row), so the same
   question always sees the same rows, and no one patient can move a count
   by more than k, or a sum by more than k * max(|low|, |high|).
2. Every cell, even empty ones. With group_by, the output has one row for
   every combination of categories, in schema order, with 0 where nothing
   matched. Leaving empty cells out would itself reveal something.

The values returned here are the true, un-noised aggregates. They never leave
the enclave: dp.py adds the noise before anything is answered.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import load
from .refuse import Refused
from .spec import Column, Filter, ValidSpec, validate

OUTPUTS = {
    "count": ("n",),
    "histogram": ("n",),
    "rate": ("numerator", "denominator"),
    "sum": ("sum","n"), # n is only used to supress small cells
    "mean": ("sum", "n"),
}


@dataclass(frozen=True)
class CompiledQuery:
    spec: ValidSpec
    sql: str
    params: tuple
    cells: tuple[tuple, ...]        # group values of each output row, in order; ((),) without group_by
    outputs: tuple[str, ...]        # aggregate columns after the group columns
    sensitivity: dict[str, float]   # most one patient can change each output, summed over all cells


def compile_spec(spec: ValidSpec) -> CompiledQuery:
    builder = _Builder(spec)

    # Section order matches the order of `?` in the final SQL.
    select = ["t.patient_id", "hash(t) AS row_hash"]
    select += [f"{builder.alias(c)}.{_q(c.name)} AS g{i}" for i, c in enumerate(spec.group_by)]
    select_params: list = []
    if spec.measure in ("sum", "mean"):
        select.append(f"least(greatest(CAST(t.{_q(spec.column.name)} AS DOUBLE), ?), ?) AS v")
        select_params += list(spec.value_bounds)
    if spec.measure == "rate":
        num_sql, num_params = builder.conditions(spec.numerator)
        select.append(f"({num_sql}) AS num")
        select_params += num_params
    where_sql, where_params = builder.conditions(spec.filters)

    join = ""
    if builder.uses_patients:
        join = "\n    JOIN patients p ON p.patient_id = t.patient_id"

    aggs = {
        "n": "count(*)",
        "numerator": "count(*) FILTER (WHERE num)",
        "denominator": "count(*)",
        "sum": "CAST(coalesce(sum(v), 0) AS DOUBLE)",
    }
    outputs = OUTPUTS[spec.measure]
    agg_list = ", ".join(f"{aggs[o]} AS {o}" for o in outputs)

    sql = f"""WITH base AS (
    SELECT {', '.join(select)}
    FROM {_q(spec.table)} t{join}
    WHERE {where_sql}
),
capped AS (
    SELECT * FROM (
        SELECT *, row_number() OVER (PARTITION BY patient_id ORDER BY row_hash) AS rn FROM base
    ) WHERE rn <= ?
)"""
    params = select_params + where_params + [spec.rows_cap]

    if not spec.group_by:
        sql += f"\nSELECT {agg_list} FROM capped"
        cells: tuple[tuple, ...] = ((),)
    else:
        groups = [f"g{i}" for i in range(len(spec.group_by))]
        cells = tuple(itertools.product(*(_values(c) for c in spec.group_by)))
        casts = [f"CAST(? AS {'BOOLEAN' if c.kind == 'boolean' else 'VARCHAR'})" for c in spec.group_by]
        row = "(?, " + ", ".join(casts) + ")"
        for i, cell in enumerate(cells):
            params += [i, *cell]
        sql += f""",
agg AS (
    SELECT {', '.join(groups)}, {agg_list} FROM capped GROUP BY {', '.join(groups)}
),
cells(i, {', '.join(groups)}) AS (
    VALUES {', '.join([row] * len(cells))}
)
SELECT {', '.join('c.' + g for g in groups)}, {', '.join(f'coalesce(a.{o}, 0) AS {o}' for o in outputs)}
FROM cells c LEFT JOIN agg a ON {' AND '.join(f'a.{g} = c.{g}' for g in groups)}
ORDER BY c.i"""

    k = spec.rows_cap
    sensitivity = {"n": k, "numerator": k, "denominator": k}
    if spec.value_bounds:
        sensitivity["sum"] = k * max(abs(b) for b in spec.value_bounds)
    return CompiledQuery(
        spec=spec,
        sql=sql,
        params=tuple(params),
        cells=cells,
        outputs=outputs,
        sensitivity={o: float(sensitivity[o]) for o in outputs},
    )


def run(con: duckdb.DuckDBPyConnection, cq: CompiledQuery) -> list[dict]:
    """Execute and return one dict per cell: {"cell": {column: value}, output: true value}."""
    rows = con.execute(cq.sql, list(cq.params)).fetchall()
    width = len(cq.spec.group_by)
    out = []
    for row in rows:
        cell = {c.ref: v for c, v in zip(cq.spec.group_by, row[:width])}
        out.append({"cell": cell, **dict(zip(cq.outputs, row[width:]))})
    return out


class _Builder:
    """Turns filters into SQL conditions on t (the table), p (patients) or EXISTS."""

    def __init__(self, spec: ValidSpec):
        self.table = spec.table
        cols = [f.column for f in spec.filters + spec.numerator] + list(spec.group_by)
        self.uses_patients = spec.table != "patients" and any(c.table == "patients" for c in cols)
        self._exists = 0

    def alias(self, c: Column) -> str:
        return "t" if c.table == self.table else "p"

    def conditions(self, filters: tuple[Filter, ...]) -> tuple[str, list]:
        if not filters:
            return "TRUE", []
        parts, params = [], []
        direct = [f for f in filters if f.column.table in (self.table, "patients")]
        for f in direct:
            sql, p = _condition(self.alias(f.column), f)
            parts.append(sql)
            params += p
        # Other tables: "the patient has a row there matching all its filters".
        others = list(dict.fromkeys(f.column.table for f in filters if f not in direct))
        for table in others:
            a = f"x{self._exists}"
            self._exists += 1
            inner, inner_params = [], []
            for f in filters:
                if f.column.table == table:
                    sql, p = _condition(a, f)
                    inner.append(sql)
                    inner_params += p
            parts.append(
                f"EXISTS (SELECT 1 FROM {_q(table)} {a} WHERE {a}.patient_id = t.patient_id AND {' AND '.join(inner)})"
            )
            params += inner_params
        return " AND ".join(parts), params


def _condition(alias: str, f: Filter) -> tuple[str, list]:
    col = f"{alias}.{_q(f.column.name)}"
    if f.op == "in":
        return f"{col} IN ({', '.join('?' * len(f.value))})", list(f.value)
    if f.op == "between":
        return f"{col} BETWEEN ? AND ?", list(f.value)
    return f"{col} = ?", [f.value]


def _values(c: Column) -> tuple:
    return (False, True) if c.kind == "boolean" else c.categories


def _q(name: str) -> str:
    """Quote an identifier. Names come from schema.json, never from the buyer."""
    return '"' + name.replace('"', '""') + '"'


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a QuerySpec and print its SQL.")
    parser.add_argument("spec", type=Path, help="QuerySpec JSON file")
    parser.add_argument("--run", action="store_true", help="also run it and print the TRUE values (dev only)")
    parser.add_argument("--db", type=Path, default=load.DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=load.DEFAULT_SCHEMA)
    args = parser.parse_args()

    schema = load.load_schema(args.schema)
    with open(args.spec, encoding="utf-8") as f:
        raw = json.load(f)
    try:
        spec = validate(raw, schema)
    except Refused as r:
        print(json.dumps(r.to_dict(), indent=2))
        sys.exit(2)

    cq = compile_spec(spec)
    print("Spec (canonical):")
    print("  " + spec.canonical_json())
    print(f"Contribution cap: {spec.rows_cap} rows per patient; sensitivity {cq.sensitivity}")
    print("SQL:")
    print("  " + cq.sql.replace("\n", "\n  "))
    print("Params:", [str(p) for p in cq.params])
    if args.run:
        con = duckdb.connect(str(args.db), read_only=True)
        try:
            rows = run(con, cq)
        finally:
            con.close()
        print("TRUE values (no noise; dev only, never returned by the engine):")
        for r in rows:
            cell = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in r["cell"].items()) or "all"
            vals = ", ".join(f"{o}={r[o]:g}" if isinstance(r[o], float) else f"{o}={r[o]}" for o in cq.outputs)
            print(f"  {cell:<32} {vals}")


if __name__ == "__main__":
    main()