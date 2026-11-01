# Blindfold

> Work in progress for NEAR HACKATHON (Nov 1 to Nov 14, 2026). Sections marked _TBD_ are filled in as the build lands.

Blindfold is a private data room where AI agents pay per question for differentially private answers from encrypted data held in a TEE, with payments, limits and receipts on NEAR. **Answers, not data.**

_TBD: 30-second GIF and demo video link._

## Tracks entered and why

- **Privacy & Private AI (primary):** _TBD_
- **Agent Economy:** _TBD_
- **AI Security & Defense:** _TBD_

## Quickstart

Prerequisites: Python 3.11+, Java 17+ (for Synthea), make. Docker and Node arrive with later days.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e "./engine[dev]"
make data   # downloads Synthea v4.0.0, generates 10,000 synthetic patients, builds 5 tables
make test   # unit tests
make sql    # check the demo question and show the SQL it compiles to
make ask    # answer the demo question with differential-privacy noise
```

## What you should see

`make data` takes about 15–25 minutes the first time: it downloads Synthea (191 MB), writes about 1.2 GB of CSVs to `data/synthea/csv/`, then builds the five analytic tables and prints their row counts. Synthea runs with a fixed seed, reference and end date, and time zone (UTC), so you should get these exact numbers on any machine, on any day:

```text
Built .../data/riverside.duckdb
  patients         10,000 rows
  admissions      270,648 rows
  conditions        8,524 rows
  medications       5,402 rows
  labs            325,355 rows
```

`make test` then prints `133 passed`. Before `make data` it prints `114 passed, 19 skipped`, because the checks on the real tables need the database.

The demo question is `datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json`: of diabetic patients aged 65+ with an inpatient stay, what share were readmitted within 30 days, by sex? It counts each patient once, at ε 1.0.

`make sql` checks it, prints the parameterized SQL it compiles to, and runs it. It prints the **true** answers (women 22 of 205, men 23 of 191). That's a developer check on the raw data; the engine never returns these.

`make ask` gives the answer a buyer would get: the same question with differential-privacy noise, a 95% error bar and small-cell suppression. The numbers change on every run, for example:

```text
"display": "10.7% ± 1.3"     (women; true 10.7%)
"display": "12.4% ± 1.4"     (men; true 12.0%)
```

`make ask TRIALS=500` (dev only) draws the noise 500 times and reports the typical error against the true values. At ε 1.0 the median error is about 0.3 percentage points and the ± bar holds the true value about 95% of the time.

To see a refusal instead, run `make sql SPEC=datasets/riverside/examples/attack_one_patient.json`: it prints the refusal as JSON and exits with code 2.

_TBD: expected output for each demo step._

## Architecture and data flow

_TBD_

## Privacy guarantees and how each is enforced

- **Only aggregate questions in a fixed format.** The engine accepts a QuerySpec (measure, table, filters, group-by, ε), never SQL. `engine/blindfold/spec.py` checks every name and value against `datasets/riverside/schema.json` and refuses anything else with a rule and a reason (`engine/blindfold/refuse.py`).
- **No SQL injection by construction.** `engine/blindfold/compile.py` builds SQL from a fixed template; table and column names come only from the schema, and every value is a `?` parameter.
- **One patient can move an answer only so far.** After filters, each patient keeps at most `max_rows_per_patient` rows (5 admissions, 5 lab results, …) before anything is counted, and every group-by cell is returned, including empty ones. `compile.py` reports the resulting sensitivity for the noise step.
- **No narrowing in on a person.** At most 2 group-by columns and 50 cells, at most 3 quasi-identifiers (sex, age band, ZIP3, county, admission and discharge dates) per question, and date ranges of at least 30 days.
- **Every answer is differentially private.** `engine/blindfold/dp.py` adds discrete Laplace noise with scale sensitivity / ε to every count, drawn exactly with whole numbers and fractions from the OS's secure random source (`secrets`), so there is no floating-point rounding leak. Group-by cells and a rate's yes/no counts are disjoint, so a whole answer costs its ε once. Sums and means clip each value to the schema bounds and split ε between the sum and the count.
- **Small groups are hidden.** A cell whose noisy count is below 20 is shown as `<20`.
- **Every answer has an error bar.** A 95% interval for the noise comes with each value.
- _TBD: privacy budget and replay cache (Day 4), enclave and receipts (Days 6–7)._

## Benchmark results

_TBD: table from `make bench`._

## Tests

`make test` runs the unit tests:

- `engine/tests/test_load.py` (30): table schemas, identifier removal, `readmit_30d`, category values and lab bounds.
- `engine/tests/test_spec.py` (45): valid QuerySpecs pass and normalise to one canonical form; 33 bad specs are each refused with the right rule and reason.
- `engine/tests/test_compile.py` (22): exact answers on a hand-made database, the per-patient cap, empty cells, clipping, parameterized SQL, and runs on the real tables.
- `engine/tests/test_dp.py` (36): chi-square tests of the noise sampler against the exact distribution, its mean and variance, exact 95% error bars and their coverage, the noise scale for each measure, suppression, clipping, and noisy answers on the real tables.

_TBD: budget, contract._

## Contract and enclave measurement

_TBD: testnet contract address and enclave code hash._

## Built during the hackathon

Everything in this repository was written during NEAR HACKATHON. The first commit was made on Nov 1, 2026, inside the build window that opened with the announcement on Oct 19, 2026, and there is no code from before it. That is why there is no `pre-hackathon` tag. The final submitted state is tagged `hackathon-submission`.

## Third-party code and AI assistance

_Kept up to date as the build goes._

| What | Used for | License |
| --- | --- | --- |
| [Synthea](https://github.com/synthetichealth/synthea) v4.0.0 | Generates the synthetic patients. Downloaded at build time by `scripts/generate_data.sh`, not vendored | Apache 2.0 |
| [DuckDB](https://duckdb.org/) (Python package) | Analytic tables and queries | MIT |
| [pydantic](https://docs.pydantic.dev/) | Checks the shape of incoming QuerySpecs; FastAPI uses it too (Day 4) | MIT |
| [pytest](https://pytest.org/) | Tests | MIT |
| Python standard library | Everything else in the engine | PSF |
| Claude (Anthropic), via Claude Code | AI assistance: project brief, planning, and docs | n/a |

## Known limits and what's next

- All data is synthetic (Synthea). No real person's data is used, even for testing.
- Synthea has no ZIP code for some towns; those patients get `zip3 = "unknown"`.
- Contribution caps trade accuracy for privacy: a patient with more than 5 matching admissions or lab results counts only 5 (a fixed pseudo-random pick), so totals over heavy users are lower than the raw data. The benchmark measures this.
- Depression and heart failure are rare in Synthea's living population (well under 1% of patients), so most questions about them will be suppressed as small cells.
- Error bars cover the noise only, not sampling error. For rates and means they use a standard approximation; the tests check they hold the true value about 95% of the time.
- `readmit_30d` near the end of the data (after Oct 1, 2026) can miss readmissions that happen after the data ends.

## License

MIT. See [LICENSE](LICENSE).