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
```

## What you should see

`make data` ends by printing the five analytic tables and their row counts:

```text
Built .../data/riverside.duckdb
  patients         10,000 rows
  admissions          ... rows
  conditions          ... rows
  medications         ... rows
  labs                ... rows
```

_TBD: expected output for each demo step._

## Architecture and data flow

_TBD_

## Privacy guarantees and how each is enforced

_TBD_

## Benchmark results

_TBD: table from `make bench`._

## Tests

`make test` runs the unit tests (today: the data loader, `engine/tests/test_load.py`). _TBD: DP mechanisms, refusal rules, compiler, contract._

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
| [pytest](https://pytest.org/) | Tests | MIT |
| Python standard library | Everything else in the engine | PSF |
| Claude (Anthropic), via Claude Code | AI assistance: project brief, planning, and drafts of code, tests and docs, reviewed and typed in by the author | n/a |

## Known limits and what's next

- All data is synthetic (Synthea). No real person's data is used, even for testing.
- Synthea has no ZIP code for some towns; those patients get `zip3 = "unknown"`.
- `readmit_30d` near the end of the data (after Oct 1, 2026) can miss readmissions that happen after the data ends.

## License

MIT. See [LICENSE](LICENSE).