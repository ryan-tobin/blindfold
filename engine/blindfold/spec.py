"""QuerySpec: the only thing the engine accepts, and the rules that check it.

A buyer (or the planner, on their behalf) sends a small JSON object such as

    {"dataset": "riverside", "measure": "rate", "table": "admissions",
     "numerator_filter": [["readmit_30d", "=", true]],
     "filters": [["encounter_class", "=", "inpatient"],
                 ["patients.age_band", "in", ["65-79", "80+"]],
                 ["conditions.condition_group", "=", "diabetes"]],
     "group_by": ["patients.sex"], "epsilon": 0.2}

validate() checks it against the public schema.json and returns a ValidSpec
(every column resolved, every value checked, defaults filled in), or raises
Refused with a rule code and a plain reason. Nothing here touches the data.

Column names: a bare name is a column of `table`; "patients.<col>" is the
patient's own row; "<other table>.<col>" means "the patient has at least one
row in that table matching all the filters given on that table".
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .refuse import NOT_AGGREGATE, OFF_SCHEMA, OVER_BUDGET, TOO_FINE, Refused

# Keys that only make sense for row-level output. Refused as rule 1, before
# the generic "unknown field" check, so the reason says what's wrong.
ROW_LEVEL_KEYS = {"sql", "select", "rows", "records", "columns", "fields", "limit", "raw", "where"}


class QuerySpec(BaseModel):
    """The JSON shape. Meaning is checked in validate()."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dataset: str
    measure: str
    table: str
    column: str | None = None
    filters: list[list[Any]] = []
    numerator_filter: list[list[Any]] = []
    group_by: list[str] = []
    epsilon: float | None = None


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    type: str
    kind: str  # id, category, boolean, number, date
    categories: tuple | None = None
    bounds: tuple[float, float] | None = None
    bounds_by_test: dict | None = None
    quasi_identifier: bool = False

    @property
    def ref(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class Filter:
    column: Column
    op: str  # normalised: "in" (categories), "between" (numbers, dates), "is" (booleans)
    value: Any  # tuple of categories, (lo, hi), or bool

    def to_json(self) -> list:
        if self.op == "between":
            lo, hi = self.value
            if self.column.kind == "date":
                lo, hi = lo.isoformat(), hi.isoformat()
            return [self.column.ref, "between", [lo, hi]]
        if self.op == "in":
            return [self.column.ref, "in", list(self.value)]
        return [self.column.ref, "is", self.value]


@dataclass(frozen=True)
class ValidSpec:
    dataset: str
    measure: str
    table: str
    column: Column | None          # summed/averaged column, or the histogram column
    filters: tuple[Filter, ...]
    numerator: tuple[Filter, ...]  # rate only
    group_by: tuple[Column, ...]   # output dimensions; a histogram column comes last
    epsilon: float
    rows_cap: int                  # each patient contributes at most this many rows
    value_bounds: tuple[float, float] | None  # clip range for sum and mean

    def canonical(self) -> dict:
        """Same question, same dict: filters sorted, defaults filled in."""
        out = {
            "dataset": self.dataset,
            "measure": self.measure,
            "table": self.table,
            "filters": sorted((f.to_json() for f in self.filters), key=json.dumps),
            "group_by": [c.ref for c in self.group_by],
            "epsilon": self.epsilon,
        }
        if self.column is not None:
            out["column"] = self.column.ref
            if self.measure == "histogram":
                out["group_by"] = out["group_by"][:-1]
        if self.numerator:
            out["numerator_filter"] = sorted((f.to_json() for f in self.numerator), key=json.dumps)
        return out

    def canonical_json(self) -> str:
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))


def validate(raw: dict | QuerySpec, schema: dict) -> ValidSpec:
    """Check a QuerySpec against the schema. Returns a ValidSpec or raises Refused."""
    rules = schema["query_rules"]
    if isinstance(raw, QuerySpec):
        raw = raw.model_dump(exclude_none=True)
    if not isinstance(raw, dict):
        raise Refused(OFF_SCHEMA, "a QuerySpec must be a JSON object")

    row_keys = sorted(ROW_LEVEL_KEYS & set(raw))
    if row_keys:
        raise Refused(NOT_AGGREGATE, f"'{row_keys[0]}' asks for rows; only aggregate answers are allowed")
    measure = raw.get("measure")
    if isinstance(measure, str) and measure not in rules["measures"]:
        raise Refused(NOT_AGGREGATE, f"measure '{measure}' is not allowed; use one of {', '.join(rules['measures'])}")

    try:
        spec = QuerySpec.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(p) for p in err["loc"]) or "spec"
        if err["type"] == "extra_forbidden":
            raise Refused(OFF_SCHEMA, f"unknown field '{where}'") from None
        raise Refused(OFF_SCHEMA, f"{where}: {err['msg']}") from None

    if spec.dataset != schema["dataset"]:
        raise Refused(OFF_SCHEMA, f"unknown dataset '{spec.dataset}'")
    if spec.table not in schema["tables"]:
        raise Refused(OFF_SCHEMA, f"unknown table '{spec.table}'")
    table = spec.table

    filters = tuple(_filter(f, table, schema, rules) for f in spec.filters)
    numerator = tuple(_filter(f, table, schema, rules) for f in spec.numerator_filter)
    for group in (filters, numerator):
        refs = [f.column.ref for f in group]
        twice = next((r for r in refs if refs.count(r) > 1), None)
        if twice:
            raise Refused(OFF_SCHEMA, f"'{twice}' is filtered more than once: use one filter per column "
                                      f"(one range, or 'in' with a list)")
    _check_date_overlap(filters + numerator, rules)
    group_by = [_column(ref, table, schema) for ref in spec.group_by]
    column = _column(spec.column, table, schema) if spec.column is not None else None

    if spec.measure == "rate":
        if not numerator:
            raise Refused(OFF_SCHEMA, "a rate needs a numerator_filter")
    elif numerator:
        raise Refused(OFF_SCHEMA, "numerator_filter is only used with measure 'rate'")

    value_bounds = None
    if spec.measure in ("count", "rate"):
        if column is not None:
            raise Refused(OFF_SCHEMA, f"measure '{spec.measure}' takes no column")
    elif spec.measure in ("sum", "mean"):
        if column is None:
            raise Refused(OFF_SCHEMA, f"measure '{spec.measure}' needs a column")
        if column.table != table or column.kind != "number":
            raise Refused(OFF_SCHEMA, f"'{column.ref}' is not a bounded number in table '{table}'")
        value_bounds = column.bounds or _bounds_for_test(column, filters)
    else:  # histogram
        if column is None:
            raise Refused(OFF_SCHEMA, "measure 'histogram' needs a categorical column")
        group_by.append(column)

    for c in group_by:
        if c.kind not in ("category", "boolean"):
            raise Refused(OFF_SCHEMA, f"can't group by '{c.ref}': only categorical and true/false columns")
        if c.table not in (table, "patients"):
            raise Refused(OFF_SCHEMA, f"can group by columns of '{table}' or 'patients' only, not '{c.ref}'")
    if len({c.ref for c in group_by}) != len(group_by):
        raise Refused(OFF_SCHEMA, "the same column appears twice in group_by")
    if len(group_by) > rules["max_group_by"]:
        raise Refused(TOO_FINE, f"at most {rules['max_group_by']} group-by columns (a histogram column counts as one)")
    cells = math.prod(len(_values(c)) for c in group_by)
    if cells > rules["max_cells"]:
        raise Refused(TOO_FINE, f"{cells} output cells; the limit is {rules['max_cells']}")

    qi = sorted({f.column.ref for f in filters + numerator if f.column.quasi_identifier}
                | {c.ref for c in group_by if c.quasi_identifier})
    if len(qi) > rules["max_quasi_identifiers"]:
        raise Refused(TOO_FINE, f"combines {len(qi)} quasi-identifiers ({', '.join(qi)}); "
                                f"the limit is {rules['max_quasi_identifiers']}, so the group could be a few people")

    epsilon = spec.epsilon if spec.epsilon is not None else rules["default_epsilon"][spec.measure]
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise Refused(OFF_SCHEMA, "epsilon must be a positive number")
    if epsilon > rules["max_epsilon"]:
        raise Refused(OVER_BUDGET, f"epsilon {epsilon} is above the per-query cap of {rules['max_epsilon']}")

    return ValidSpec(
        dataset=spec.dataset,
        measure=spec.measure,
        table=table,
        column=column,
        filters=filters,
        numerator=numerator,
        group_by=tuple(group_by),
        epsilon=float(epsilon),
        rows_cap=int(schema["tables"][table]["max_rows_per_patient"]),
        value_bounds=value_bounds,
    )


def _column(ref: Any, base: str, schema: dict) -> Column:
    if not isinstance(ref, str) or not ref:
        raise Refused(OFF_SCHEMA, f"column names are strings, got {ref!r}")
    parts = ref.split(".")
    if len(parts) == 1:
        table, name = base, parts[0]
    elif len(parts) == 2:
        table, name = parts
    else:
        raise Refused(OFF_SCHEMA, f"unknown column '{ref}'")
    spec = schema["tables"].get(table, {}).get("columns", {}).get(name)
    if spec is None:
        raise Refused(OFF_SCHEMA, f"unknown column '{ref}'")
    if spec.get("role"):
        raise Refused(NOT_AGGREGATE, f"'{table}.{name}' identifies a person or row and can't be used in a question")
    bounds = tuple(float(b) for b in spec["bounds"]) if "bounds" in spec else None
    if spec["type"] == "BOOLEAN":
        kind = "boolean"
    elif "categories" in spec:
        kind = "category"
    elif spec["type"] == "DATE":
        kind = "date"
    elif bounds or "bounds_by_test" in spec:
        kind = "number"
    else:
        raise Refused(OFF_SCHEMA, f"'{table}.{name}' can't be queried")
    return Column(
        table=table,
        name=name,
        type=spec["type"],
        kind=kind,
        categories=tuple(spec["categories"]) if "categories" in spec else None,
        bounds=bounds,
        bounds_by_test=spec.get("bounds_by_test"),
        quasi_identifier=bool(spec.get("quasi_identifier")),
    )


def _values(c: Column) -> tuple:
    return (False, True) if c.kind == "boolean" else c.categories


def _filter(raw: Any, base: str, schema: dict, rules: dict) -> Filter:
    if not (isinstance(raw, list) and len(raw) == 3 and isinstance(raw[1], str)):
        raise Refused(OFF_SCHEMA, f"a filter is [column, operator, value], got {raw!r}")
    ref, op, value = raw
    col = _column(ref, base, schema)
    if op not in rules["operators"]:
        raise Refused(OFF_SCHEMA, f"unknown operator '{op}'; use one of {', '.join(rules['operators'])}")
    where = f"filter on '{col.ref}'"

    if col.kind == "category":
        if op == "=":
            value, op = [value], "in"
        if op != "in" or not isinstance(value, list) or not value:
            raise Refused(OFF_SCHEMA, f"{where}: use '=' with one category or 'in' with a list")
        bad = [v for v in value if not isinstance(v, str) or v not in col.categories]
        if bad:
            raise Refused(OFF_SCHEMA, f"{where}: {bad[0]!r} is not one of its categories")
        return Filter(col, "in", tuple(c for c in col.categories if c in value))

    if col.kind == "boolean":
        if op not in ("=", "is") or not isinstance(value, bool):
            raise Refused(OFF_SCHEMA, f"{where}: use 'is' (or '=') with true or false")
        return Filter(col, "is", value)

    if op == "=":
        value, op = [value, value], "between"
    if op != "between" or not isinstance(value, list) or len(value) != 2:
        raise Refused(OFF_SCHEMA, f"{where}: use '=' with one value or 'between' with [low, high]")

    if col.kind == "number":
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
            raise Refused(OFF_SCHEMA, f"{where}: values must be numbers")
        lo, hi = float(value[0]), float(value[1])
    else:  # date
        try:
            if not all(isinstance(v, str) and len(v) == 10 for v in value):
                raise ValueError
            lo, hi = dt.date.fromisoformat(value[0]), dt.date.fromisoformat(value[1])
        except ValueError:
            raise Refused(OFF_SCHEMA, f"{where}: dates are 'YYYY-MM-DD'") from None
    if lo > hi:
        raise Refused(OFF_SCHEMA, f"{where}: low is above high")
    if col.kind == "date" and (hi - lo).days + 1 < rules["min_date_range_days"]:
        raise Refused(TOO_FINE, f"{where}: date ranges must cover at least {rules['min_date_range_days']} days")
    return Filter(col, "between", (lo, hi))


def _check_date_overlap(filters: tuple[Filter, ...], rules: dict) -> None:
    """A date column filtered in both filters and numerator_filter: the rows that match both
    fall in the overlap of the two ranges, so the overlap must cover min_date_range_days too."""
    spans: dict[str, tuple[dt.date, dt.date]] = {}
    for f in filters:
        if f.column.kind != "date":
            continue
        lo, hi = f.value
        if f.column.ref in spans:
            lo, hi = max(lo, spans[f.column.ref][0]), min(hi, spans[f.column.ref][1])
            if (hi - lo).days + 1 < rules["min_date_range_days"]:
                raise Refused(TOO_FINE, f"the ranges on '{f.column.ref}' overlap by less than "
                                        f"{rules['min_date_range_days']} days")
        spans[f.column.ref] = (lo, hi)


def _bounds_for_test(col: Column, filters: tuple[Filter, ...]) -> tuple[float, float]:
    """labs.value has bounds per test, so a sum or mean must pick one test."""
    tests = [f.value for f in filters if f.column.table == col.table and f.column.name == "test"]
    if len(tests) != 1 or len(tests[0]) != 1:
        raise Refused(OFF_SCHEMA, f"a sum or mean of '{col.ref}' needs exactly one test, e.g. [\"test\", \"=\", \"hba1c\"]")
    lo, hi = col.bounds_by_test[tests[0][0]]
    return float(lo), float(hi)