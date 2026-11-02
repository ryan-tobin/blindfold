# Blindfold

> Work in progress for NEAR HACKATHON (Nov 1 to Nov 14, 2026). Sections marked _TBD_ are filled in as the build lands.

Blindfold is a private data room where AI agents pay per question for differentially private answers from encrypted data held in a TEE, with payments, limits and receipts on NEAR. **Answers, not data.**

_TBD: 30-second GIF and demo video link._

## Tracks entered and why

- **Privacy & Private AI (primary):** _TBD_
- **Agent Economy:** _TBD_
- **AI Security & Defense:** _TBD_

## Quickstart

Prerequisites: Python 3.11+, Java 17+ (for Synthea), make and curl, plus [Rust](https://rustup.rs) and [cargo-near](https://github.com/near/cargo-near) 0.22 for the contract (`contract/rust-toolchain.toml` installs Rust 1.93.0 and the wasm target on first use), and Docker for the simulated enclave. Node arrives with the UI.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e "./engine[dev]"
make data   # downloads Synthea v4.0.0, generates 10,000 synthetic patients, builds 5 tables
make test   # engine and contract unit tests
make sql    # check the demo question and show the SQL it compiles to
make ask    # answer the demo question with differential-privacy noise
make serve  # start the engine's HTTP service on http://127.0.0.1:8000 (leave it running)
make query  # in a second terminal: ask the running service, get the answer and a signed receipt
make verify REQUEST=1  # check that receipt's signature and the enclave that signed it
```

To run the engine in a simulated enclave instead, use Docker: `make up` (the first build compiles Phala's dstack simulator, 10–20 minutes), then the same `make query` and `make verify REQUEST=1`. Docker reads `CONTRACT_ID` from `.env`; leave it blank (or run `CONTRACT_ID= make up`) for this local run.

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

`make test` then prints `186 passed` for the engine, then `test result: ok. 19 passed` for the contract. Before `make data` the engine part prints `166 passed, 20 skipped`, because the checks on the real tables need the database. The first contract run downloads Rust 1.93.0 and the crates, so it takes a few minutes.

The demo question is `datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json`: of diabetic patients aged 65+ with an inpatient stay, what share were readmitted within 30 days, by sex? It counts each patient once, at ε 1.0.

`make sql` checks it, prints the parameterized SQL it compiles to, and runs it. It prints the **true** answers (women 22 of 205, men 23 of 191). That's a developer check on the raw data; the engine never returns these.

`make ask` gives the answer a buyer would get: the same question with differential-privacy noise, a 95% error bar and small-cell suppression. The numbers change on every run, for example:

```text
"display": "10.7% ± 1.3"     (women; true 10.7%)
"display": "12.4% ± 1.4"     (men; true 12.0%)
```

`make ask TRIALS=500` (dev only) draws the noise 500 times and reports the typical error against the true values. At ε 1.0 the median error is about 0.3 percentage points and the ± bar holds the true value about 95% of the time.

To see a refusal instead, run `make sql SPEC=datasets/riverside/examples/attack_one_patient.json`: it prints the refusal as JSON and exits with code 2.

`make serve` starts the engine as a web service. Open http://127.0.0.1:8000/docs for the interactive API docs. It has six endpoints:

| Endpoint | What it does |
| --- | --- |
| `GET /schema` | The public schema: tables, columns, categories and the query rules |
| `GET /budget` | The privacy budget: total (10), spent, remaining, and whether the dataset is closed |
| `POST /query` | `{"buyer": "alice.testnet", "spec": {...}}` (or `{"request_id": 7, ...}` in chain mode): a noisy answer or a refusal, always with a signed receipt |
| `GET /receipt/{id}` | The receipt of an earlier request |
| `GET /attestation` | The enclave's mode, public keys, report_data and quote |
| `GET /info` | Mode, app id, compose hash, NEAR account and contract |

With the service running, `make query` (in a second terminal) sends the demo question as `alice.testnet` and prints the answer and its receipt. The receipt shows `"epsilon_charged": 1.0` and `"epsilon_remaining": 9.0`. Run `make query` again and you get the exact same numbers back, with `"replay_of": 1` and `"epsilon_charged": 0.0`: a repeated question costs nothing and can't be averaged. `make query SPEC=datasets/riverside/examples/attack_one_patient.json` is refused (`too_fine`) and costs nothing. Every receipt is signed by the enclave's ed25519 key. `make verify REQUEST=1` checks it: the receipt hash, the signature, that the key is the engine's, and that the attestation binds the keys to the schema. Without Docker the engine runs in **dev** mode (a local key in `data/dev_enclave.key`, no quote), and the verifier marks it in yellow.

The budget, the replay cache and the receipts live in `data/state.sqlite`, so they survive a restart. `make reset` deletes that file to start the budget again at 10.

### The enclave and the chain

`make up` runs the engine next to Phala's dstack simulator, which gives it the same API as a real Intel TDX enclave. The engine derives two keys from dstack (`engine/blindfold/enclave.py`): an ed25519 **signer** that signs every receipt and is also the enclave's own NEAR account (the implicit account named by its public key), and an X25519 **encryption** key the data owner encrypts the bundle to (Day 7). It puts `sha256(signer key + encryption key + schema hash)` into the attestation quote's report_data, so the quote binds both keys and the schema to the code. In the simulator the quote is a fixed sample, so `/attestation` says `"simulated": true` and the verifier shows it in yellow.

With `CONTRACT_ID` set (in `.env` for Docker, or `make serve CONTRACT_ID=...`), the engine runs in **chain mode**:

1. The buyer calls `request_query(dataset_id, spec_hash, epsilon)` on the contract, which escrows the price and returns a request id.
2. The buyer sends `{"request_id": 7, "spec": {...}}` to `/query`. The engine reads the request on-chain and refuses the question (rule 6, `spec_mismatch`) if its canonical hash or ε isn't the one paid for.
3. The engine answers or refuses, signs the receipt, and calls `settle` with the receipt hash, signed by its own account (`engine/blindfold/chain.py`, `engine/blindfold/near.py`). Answered pays the owner and charges ε; refused refunds the buyer.

The engine reads `epsilon_total` and `epsilon_spent` from the contract and uses the larger of the contract's and its own ledger, so neither a restart nor a lost transaction can overspend. If `settle` fails, the engine keeps the answer and returns an error; sending the same request id again settles it without charging ε twice.

`make register-enclave CONTRACT_ID=... ADMIN=...` funds the enclave's account with 1 NEAR for gas and registers it on the contract. `make pay CONTRACT_ID=... BUYER=...` pays for the demo question on-chain and asks the engine (the buyer's key comes from near-cli's legacy keychain). `make verify REQUEST=<id> CONTRACT_ID=...` then also checks that the enclave is the registered one and that the request was settled with this exact receipt hash.

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
- **A total privacy budget.** The dataset has a budget of ε 10. Every new answer spends its ε, and costs add up. Spending is kept in exact fractions, so 100 questions at 0.1 spend exactly 10. A question that needs more ε than is left is refused, and at zero the dataset closes to new questions (`engine/blindfold/budget.py`, `engine/blindfold/app.py`).
- **No averaging attack.** The same question asked again, by anyone, returns the stored noisy answer at no cost (the replay cache). Asking 1,000 times gives the same noise 1,000 times. Questions are matched on their canonical form, so rewording the JSON doesn't get fresh noise.
- **A rate limit.** At most 30 requests per buyer in any rolling hour, refusals and repeats included.
- **A receipt for every request.** Answered or refused, each request gets a receipt with the hash of the question, the hash of the answer, the ε charged and the ε left (`engine/blindfold/receipts.py`). Refusals cost no budget.
- **Payments, limits and the budget are public, on NEAR.** `contract/src/lib.rs` holds each dataset's price and privacy budget, escrows the fee for every question, and records ε spent and the receipt hash when the enclave settles. A refused question refunds the fee and is logged on-chain with its rule. Pending questions reserve their ε, so they can't overspend the budget together, and the contract won't let the enclave charge more ε than the buyer asked for.
- **An agent can buy answers but can't drain the wallet.** The buyer deposits USDC once and sets a per-question and a daily cap. Changing caps and withdrawing need 1 yoctoNEAR attached, which a NEAR function-call key can't attach, so an agent holding only a key for `request_query` can spend within the caps and nothing else.
- **Every receipt is signed inside the enclave.** The signer key is derived by dstack from the app's identity and never leaves the enclave. The quote binds it, the encryption key and the schema hash together, and the contract only accepts `settle` from that key's account, so a receipt hash on-chain means the enclave signed it (`engine/blindfold/enclave.py`, `engine/blindfold/receipts.py`, `verify/verify.py`).
- **The engine can only charge what was paid for.** In chain mode a question must match the spec hash and ε of a paid request, or it is refused and refunded (`engine/blindfold/app.py`). A request is answered and settled once.

## Benchmark results

_TBD: table from `make bench`._

## Tests

`make test` runs the unit tests:

- `engine/tests/test_load.py` (30): table schemas, identifier removal, `readmit_30d`, category values and lab bounds.
- `engine/tests/test_spec.py` (45): valid QuerySpecs pass and normalise to one canonical form; 33 bad specs are each refused with the right rule and reason.
- `engine/tests/test_compile.py` (22): exact answers on a hand-made database, the per-patient cap, empty cells, clipping, parameterized SQL, and runs on the real tables.
- `engine/tests/test_dp.py` (36): chi-square tests of the noise sampler against the exact distribution, its mean and variance, exact 95% error bars and their coverage, the noise scale for each measure, suppression, clipping, and noisy answers on the real tables.
- `engine/tests/test_budget.py` (8): exact budget sums, the replay cache, the rate-limit counter, state that survives a restart, rollback on failure, and receipt hashes.
- `engine/tests/test_app.py` (16): the HTTP service end to end: answers with receipts, free identical replays, the averaging attack, a refusal for each rule, the budget running out, the rate limit, a restart, and the demo question on the real tables.
- `engine/tests/test_enclave.py` (14): keys from a fake dstack agent over a unix socket and HTTP, report_data in the quote, dev-mode keys, and signed receipts that break when any field changes.
- `engine/tests/test_chain.py` (15): chain mode against a fake contract: paid questions settle, replays cost no ε, mismatched or bad specs are refused and refunded, a request is answered once, a failed settle retries for free, the budget follows the contract, plus NEAR transaction bytes checked against py-near and base58.
- `contract/src/tests.rs` (19): answered questions pay the owner and spend ε; refusals refund and cost nothing; a free replay; only the enclave settles, once, and never for more ε than requested; per-question and daily caps and the daily reset; an agent key can't raise its caps; pending questions reserve the budget; closed datasets; owner-only updates; expiry refunds; USDC-only deposits; unique dataset ids; the recent-queries feed.

## Contract and enclave measurement

Contract on NEAR testnet: [`blindfold.ryan-tobin.testnet`](https://testnet.nearblocks.io/address/blindfold.ryan-tobin.testnet). Build it with `make contract`; cargo-near prints the wasm's SHA-256. The enclave registered there today is the simulated one (Day 6); the real TDX enclave replaces it on Day 7.

| Method | Who calls it | What it does |
| --- | --- | --- |
| `new(admin, usdc_contract, expiry_blocks)` | once, at deploy | Sets the admin, the USDC token and how long a question can stay unsettled |
| `register_enclave(...)` | admin | Records the enclave's account, public keys, compose hash and quote hash (`make register-enclave`) |
| `register_dataset(id, schema_hash, bundle_hash, price, epsilon_total)` | data owner | Lists a dataset (`make register`) |
| `update_dataset(id, bundle_hash?, price?, active?)` | data owner | Sets the bundle hash, price, or closes the dataset. The budget can't change |
| `ft_on_transfer` | USDC, after the buyer's `ft_transfer_call` with `{"action":"deposit"}` | Credits the buyer |
| `set_limits(per_query_max, daily_max)` | buyer, full key, 1 yoctoNEAR | Agent spending caps |
| `request_query(dataset_id, spec_hash, epsilon)` | buyer's agent, function-call key | Checks caps, balance and budget, escrows the price, returns the request id |
| `settle(request_id, status, receipt_hash, epsilon_charged, refusal?)` | enclave account only | Answered pays the owner and spends ε; refused refunds |
| `expire(request_id)` | anyone, after `expiry_blocks` | Refunds a question nobody settled |
| `withdraw(amount)` | buyer or owner, 1 yoctoNEAR | Sends USDC out; puts it back if the transfer fails |
| `get_config`, `get_dataset`, `list_datasets`, `get_enclave`, `get_request`, `get_balance`, `list_recent` | anyone | Views |

Units: amounts are USDC's smallest unit (6 decimals, so $0.25 = `"250000"`) and ε is in millionths (1.0 = `1000000`), so the budget adds up exactly. Every change emits a NEP-297 event (`EVENT_JSON:{"standard":"blindfold",...}`).

Enclave code hash: _TBD (Day 7)._

## Built during the hackathon

Everything in this repository was written during NEAR HACKATHON. The first commit was made on Nov 1, 2026, inside the build window that opened with the announcement on Oct 19, 2026, and there is no code from before it. That is why there is no `pre-hackathon` tag. The final submitted state is tagged `hackathon-submission`.

## Third-party code and AI assistance

_Kept up to date as the build goes._

| What | Used for | License |
| --- | --- | --- |
| [Synthea](https://github.com/synthetichealth/synthea) v4.0.0 | Generates the synthetic patients. Downloaded at build time by `scripts/generate_data.sh`, not vendored | Apache 2.0 |
| [DuckDB](https://duckdb.org/) (Python package) | Analytic tables and queries | MIT |
| [pydantic](https://docs.pydantic.dev/) | Checks the shape of incoming QuerySpecs and requests | MIT |
| [FastAPI](https://fastapi.tiangolo.com/) and [Starlette](https://starlette.dev/) | The engine's HTTP service and its docs page | MIT, BSD-3-Clause |
| [Uvicorn](https://uvicorn.dev/) | Runs the HTTP service | BSD-3-Clause |
| [httpx2](https://github.com/pydantic/httpx2) (tests only) | FastAPI's test client needs it | BSD-3-Clause |
| [PyNaCl](https://github.com/pyca/pynacl) 1.6.2 | ed25519 receipt and transaction signatures, X25519 keys | Apache 2.0 |
| [dstack](https://github.com/Dstack-TEE/dstack) v0.6.1 simulator | The simulated enclave. Built from source by `docker/simulator/Dockerfile`, adapted from dstack's own simulator Dockerfile | Apache 2.0 |
| [pytest](https://pytest.org/) | Tests | MIT |
| [near-sdk-rs](https://github.com/near/near-sdk-rs) 5.29.1 | The NEAR contract | MIT or Apache 2.0 |
| [cargo-near](https://github.com/near/cargo-near) 0.22.0 | Builds the contract; project layout from `cargo near new` | MIT or Apache 2.0 |
| [near-cli-rs](https://github.com/near/near-cli-rs) 0.30.1 | Creates accounts, deploys, and signs calls from `owner/register.py` | MIT or Apache 2.0 |
| Python standard library | Everything else in the engine, including SQLite for the budget ledger | PSF |
| Claude (Anthropic), via Claude Code | AI assistance: project brief, planning, and docs | n/a |

## Known limits and what's next

- All data is synthetic (Synthea). No real person's data is used, even for testing.
- Synthea has no ZIP code for some towns; those patients get `zip3 = "unknown"`.
- Contribution caps trade accuracy for privacy: a patient with more than 5 matching admissions or lab results counts only 5 (a fixed pseudo-random pick), so totals over heavy users are lower than the raw data. The benchmark measures this.
- Depression and heart failure are rare in Synthea's living population (well under 1% of patients), so most questions about them will be suppressed as small cells.
- Error bars cover the noise only, not sampling error. For rates and means they use a standard approximation; the tests check they hold the true value about 95% of the time.
- `readmit_30d` near the end of the data (after Oct 1, 2026) can miss readmissions that happen after the data ends.
- The simulated enclave's keys come from the simulator's public sample key file, so anyone can derive them. A simulated enclave registered on testnet proves nothing; the real TDX deployment (Day 7) replaces it.
- Without `CONTRACT_ID` the engine runs in local mode: the buyer in `/query` is a name the caller types and nobody pays. Buyers pay with USDC from Day 9; until then `make pay` works on a dataset whose price is 0.
- Trust model: the contract doesn't check the Intel TDX quote itself, which is too heavy for a contract. It trusts one registered enclave account, whose key exists only inside the enclave, and stores the quote hash so anyone can check it off-chain.
- The contract account pays NEAR storage for every question it records (a few hundred bytes each).

## License

MIT. See [LICENSE](LICENSE).