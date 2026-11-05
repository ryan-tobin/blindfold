# Blindfold

> Work in progress for NEAR HACKATHON (Nov 1 to Nov 14, 2026). Sections marked _TBD_ are filled in as the build lands.

Blindfold is a private data room where AI agents pay per question for differentially private answers from encrypted data held in a TEE, with payments, limits and receipts on NEAR. **Answers, not data.**

_TBD: 30-second GIF and demo video link._

## Tracks entered and why

- **Privacy & Private AI (primary):** _TBD_
- **Agent Economy:** _TBD_
- **AI Security & Defense:** _TBD_

## Quickstart

Prerequisites: Python 3.11+, Java 17+ (for Synthea), make and curl, plus [Rust](https://rustup.rs) and [cargo-near](https://github.com/near/cargo-near) 0.22 for the contract (`contract/rust-toolchain.toml` installs Rust 1.93.0 and the wasm target on first use), Docker for the simulated enclave, and Node 20+ for the browser UI.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e "./engine[dev]"
make data   # downloads Synthea v4.0.0, generates 10,000 synthetic patients, builds 5 tables
make test   # engine and contract unit tests
make sql    # check the demo question and show the SQL it compiles to
make ask    # answer the demo question with differential-privacy noise
make serve  # start the engine's HTTP service on http://127.0.0.1:8000 (leave it running)
make query  # in a second terminal: ask the running service, get the answer and a signed receipt
make verify REQUEST=1  # check that receipt's signature and the enclave that signed it
make plan   # turn the demo question, in plain English, into a QuerySpec (free; no key needed for the 5 demo questions)
make buy BUYER=alice.testnet Q="How many patients have hypertension, by age band?"  # the buyer agent, local and unpaid
make ui     # the browser UI on http://localhost:5173 (?net=local talks to make serve; default is testnet)
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

`make test` then prints `230 passed` for the engine, `13 passed` for the buyer's scripts in `agent/`, then `test result: ok. 19 passed` for the contract. Before `make data` the engine part prints `209 passed, 21 skipped`, because the checks on the real tables need the database. The first contract run downloads Rust 1.93.0 and the crates, so it takes a few minutes.

The demo question is `datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json`: of diabetic patients aged 65+ with an inpatient stay, what share were readmitted within 30 days, by sex? It counts each patient once, at ε 1.0.

`make sql` checks it, prints the parameterized SQL it compiles to, and runs it. It prints the **true** answers (women 22 of 205, men 23 of 191). That's a developer check on the raw data; the engine never returns these.

`make ask` gives the answer a buyer would get: the same question with differential-privacy noise, a 95% error bar and small-cell suppression. The numbers change on every run, for example:

```text
"display": "10.7% ± 1.3"     (women; true 10.7%)
"display": "12.4% ± 1.4"     (men; true 12.0%)
```

`make ask TRIALS=500` (dev only) draws the noise 500 times and reports the typical error against the true values. At ε 1.0 the median error is about 0.3 percentage points and the ± bar holds the true value about 95% of the time.

To see a refusal instead, run `make sql SPEC=datasets/riverside/examples/attack_one_patient.json`: it prints the refusal as JSON and exits with code 2.

`make serve` starts the engine as a web service. Open http://127.0.0.1:8000/docs for the interactive API docs. It has eight endpoints:

| Endpoint | What it does |
| --- | --- |
| `GET /schema` | The public schema: tables, columns, categories and the query rules |
| `GET /budget` | The privacy budget: total (10), spent, remaining, and whether the dataset is closed |
| `POST /plan` | `{"question": "..."}`: the QuerySpec it becomes, in plain words, with its ε and price, and whether the engine would refuse it. Free |
| `POST /query` | `{"buyer": "alice.testnet", "spec": {...}}` (or `{"request_id": 7, ...}` in chain mode): a noisy answer or a refusal, always with a signed receipt |
| `GET /receipt/{id}` | The receipt of an earlier request |
| `GET /attestation` | The enclave's mode, public keys, report_data and quote |
| `GET /info` | Mode, app id, compose hash, NEAR account, contract, and where the data came from |
| `POST /bundle` | Load the encrypted bundle from `BUNDLE_URL` now (if it wasn't ready at startup) |

With the service running, `make query` (in a second terminal) sends the demo question as `alice.testnet` and prints the answer and its receipt. The receipt shows `"epsilon_charged": 1.0` and `"epsilon_remaining": 9.0`. Run `make query` again and you get the exact same numbers back, with `"replay_of": 1` and `"epsilon_charged": 0.0`: a repeated question costs nothing and can't be averaged. `make query SPEC=datasets/riverside/examples/attack_one_patient.json` is refused (`too_fine`) and costs nothing. Every receipt is signed by the enclave's ed25519 key. `make verify REQUEST=1` checks it: the receipt hash, the signature, that the key is the engine's, and that the attestation binds the keys to the schema. Without Docker the engine runs in **dev** mode (a local key in `data/dev_enclave.key`, no quote), and the verifier marks it in yellow.

The budget, the replay cache and the receipts live in `data/state.sqlite`, so they survive a restart. `make reset` deletes that file to start the budget again at 10.

### The enclave and the chain

`make up` runs the engine next to Phala's dstack simulator, which gives it the same API as a real Intel TDX enclave. The engine derives two keys from dstack (`engine/blindfold/enclave.py`): an ed25519 **signer** that signs every receipt and is also the enclave's own NEAR account (the implicit account named by its public key), and an X25519 **encryption** key the data owner encrypts the bundle to. It puts `sha256(signer key + encryption key + schema hash)` into the attestation quote's report_data, so the quote binds both keys and the schema to the code. In the simulator the quote is a fixed sample, so `/attestation` says `"simulated": true` and the verifier shows it in yellow.

With `CONTRACT_ID` set (in `.env` for Docker, or `make serve CONTRACT_ID=...`), the engine runs in **chain mode**:

1. The buyer calls `request_query(dataset_id, spec_hash, epsilon)` on the contract, which escrows the price and returns a request id.
2. The buyer sends `{"request_id": 7, "spec": {...}}` to `/query`. The engine reads the request on-chain and refuses the question (rule 6, `spec_mismatch`) if its canonical hash or ε isn't the one paid for.
3. The engine answers or refuses, signs the receipt, and calls `settle` with the receipt hash, signed by its own account (`engine/blindfold/chain.py`, `engine/blindfold/near.py`). Answered pays the owner and charges ε; refused refunds the buyer.

The engine reads `epsilon_total` and `epsilon_spent` from the contract and uses the larger of the contract's and its own ledger, so neither a restart nor a lost transaction can overspend. If `settle` fails, the engine keeps the answer and returns an error; sending the same request id again settles it without charging ε twice.

`make register-enclave CONTRACT_ID=... ADMIN=...` funds the enclave's account with 1 NEAR for gas and registers it on the contract. `make pay CONTRACT_ID=... BUYER=...` pays for the demo question on-chain and asks the engine (the buyer's key comes from near-cli's legacy keychain). `make verify REQUEST=<id> CONTRACT_ID=...` then also checks that the enclave is the registered one and that the request was settled with this exact receipt hash.

### The encrypted bundle and the real enclave

The data reaches the enclave only as an encrypted **bundle** (`engine/blindfold/bundle.py`). `make bundle URL=<engine>` first checks the enclave's attestation (`engine/blindfold/attest.py`), then packs the five tables as Parquet in one archive and seals it to the enclave's X25519 key, writing `data/riverside.bundle`. With `CONTRACT_ID` and `OWNER` it also lists the bundle's SHA-256 on the contract (`update_dataset`). The ciphertext is safe to publish: only the enclave can open it.

The engine fetches the bundle from `BUNDLE_URL`, refuses it unless its SHA-256 is the one listed on-chain (or `BUNDLE_HASH` without a contract), decrypts it in memory, and loads it into an in-memory DuckDB. The Parquet files sit in RAM (tmpfs) only while DuckDB reads them, then DuckDB's file access is switched off for good. Until a bundle loads, the engine still serves `/attestation` (the owner needs its key first) and `/query` answers 503; `POST /bundle` retries.

To try it locally with the simulator: `make up`, then `make bundle URL=http://127.0.0.1:8000 ALLOW_SIM=1`, then restart with `BUNDLE_URL=/app/data/riverside.bundle BUNDLE_HASH=<the printed hash> docker compose up`.

The real enclave runs on Phala Cloud in an Intel TDX VM, from `docker-compose.tee.yml`. That file is measured into the quote, so it pins the engine image by digest, names the contract and the bundle URL, and takes no env vars or secrets. The image is built by GitHub Actions (`.github/workflows/engine-image.yml`) with a signed build provenance, so anyone can check it came from this repo:

```bash
gh attestation verify oci://ghcr.io/ryan-tobin/blindfold-engine@sha256:e908c0d71eca45be286829608ba987ac11e3bd02d877563c0d9f580b0cdfca11 --repo ryan-tobin/blindfold
```

Pointed at the real enclave, `make verify` checks the whole chain: the quote is signed by a genuine Intel TDX CPU (dcap-qvl, up to Intel's root CA, with Phala's PCCS for revocation lists and TCB status), report_data binds the keys and schema, the event log replays to the quote's RTMR0-3 (dstack 0.5.12 leaves the RTMR3 digests empty, so the verifier computes each one from its event), its compose hash is `sha256(app_compose)`, and app_compose holds this repo's `docker-compose.tee.yml` byte for byte. The OS image hash is in the quote too; the image itself is dstack's, which Phala's key service checks before releasing keys, and the verifier doesn't rebuild it.

### Plain English to QuerySpec

Buyers don't write QuerySpecs by hand. `POST /plan {"question": "..."}` turns a question into one, shows it in plain words with its ε and price, and says if the engine would refuse it, all before anyone pays (`engine/blindfold/planner.py`). With `NEAR_AI_API_KEY` set, the question goes from the enclave to a **Private TEE** model on NEAR AI Cloud (by default `z-ai/glm-5.3-flash`), over plain HTTPS with no SDK. The model sees only the public `schema.json`, the rules and 10 worked examples, and must reply with JSON at temperature 0. The engine's own validator checks that JSON. A malformed spec goes back to the model once with the error; after two failures the buyer is asked to rephrase. A spec the privacy rules would refuse is never sent back to be "fixed", so the model can't learn to widen a question until it passes: it comes back marked `will_refuse`.

Every planning call is checked. Before it, the engine fetches NEAR AI's attestation with a fresh nonce and checks that the model's report binds the nonce and the model's signing address. After it, the engine fetches the response signature and checks it covers the SHA-256 of the exact request and response bytes and recovers to that attested address (`engine/blindfold/ethsig.py`: keccak-256 and secp256k1 recovery, written from scratch). The signature goes into the receipt as `model_signature`, and `make verify` checks it. With `NEAR_AI_API_KEY` set, the verifier also confirms that NEAR AI still attests the key and checks the model's Intel quote. So one receipt shows that both the model and the engine ran in hardware enclaves.

The planner, not the model, picks ε when the question doesn't name one: 1.0 for rates and means, 0.5 for counts, sums and histograms (`PLAN_EPSILON` in `engine/blindfold/planner.py`, never above the schema's cap of 1.0). The prompt also asks for readmission rates per patient (each patient counts once) unless the question counts stays. One patient then moves the answer by at most 1 instead of up to 5, so the demo question gets ±1.3 points instead of ±11 to ±16. The tradeoff is budget: a rate question spends a tenth of Riverside's ε 10, so the dataset answers about 10 new rate questions before it closes. Repeats are free.

Without a key, `/plan` falls back to an offline stub that knows exactly the 5 demo questions (`engine/blindfold/planner_stub.py`), so local mode needs no keys. `make plan-bench` scores a model on 25 questions: the 5 demo questions plus 4 rewordings of each (`datasets/riverside/planner_questions.json`). See Benchmark results.

### Payments and the buyer agent

A buyer pays in USDC (Circle's native USDC on NEAR, 6 decimals). They deposit once into the contract and set two caps, then hand their agent a key that can do one thing: call `request_query`. The scripts in `agent/` run on the buyer's machine, never in the enclave, so changing them doesn't change the enclave image.

`agent/wallet.py` is the human side. Everything that signs goes through near-cli-rs with the buyer's full key from the legacy keychain (`~/.near-credentials`), prints the command first and asks before sending (`--yes` skips the question, `--dry-run` only prints):

```bash
python agent/wallet.py status       --buyer alice.ryan-tobin.testnet   # wallet, contract balance, caps, agent key (reads only)
python agent/wallet.py register     --buyer alice.ryan-tobin.testnet   # NEP-145 storage with USDC for the contract and the buyer, 0.00125 NEAR each, once
python agent/wallet.py deposit 2    --buyer alice.ryan-tobin.testnet   # ft_transfer_call into the contract
python agent/wallet.py limits 0.25 1 --buyer alice.ryan-tobin.testnet  # at most 0.25 USDC a question and 1 USDC a UTC day
python agent/wallet.py agent-key    --buyer alice.ryan-tobin.testnet   # a function-call key for request_query only
python agent/wallet.py revoke-agent --buyer alice.ryan-tobin.testnet   # delete that key
python agent/wallet.py withdraw 0.25 --buyer alice.ryan-tobin.testnet  # take USDC back out (buyers' change, owners' earnings)
```

`agent-key` makes an ed25519 key in `~/.blindfold/agent-keys/<network>/<account>.json` (chmod 600) and adds it to the account as a function-call key: receiver the Blindfold contract, method `request_query`, a NEAR gas allowance (0.25 NEAR by default) and no deposits. Because `set_limits` and `withdraw` need 1 yoctoNEAR attached, the agent can spend within the caps and can't touch anything else.

`agent/buyer.py` (`make buy`) is the agent. It refuses to run unless its key is exactly that kind of key. For each question it:

1. plans it with `POST /plan` (free) and prints the plan in plain words;
2. checks the dataset is open, the privacy budget left, the price against both caps and the balance, the same checks the contract makes, so a payment that would fail is never sent;
3. pays with `request_query`, signed by the agent key;
4. gets the answer from `POST /query`, then reads the request on-chain and checks it was settled with the same receipt hash.

Questions the engine would refuse are skipped and not paid for. `--goal "..."` (with `NEAR_AI_API_KEY`) asks a NEAR AI model for up to 3 questions that serve the goal; `--plan-only` plans and prices without paying. At the end it writes a report to `data/reports/` with every answer, price, transaction and receipt, and saves each receipt to `data/receipts/` for `make verify`.

```bash
make buy CONTRACT_ID=blindfold.ryan-tobin.testnet URL=<testnet enclave URL> BUYER=alice.ryan-tobin.testnet \
    Q="What is the 30-day readmission rate for diabetic patients aged 65 and older, by sex?"
make buy CONTRACT_ID=blindfold.ryan-tobin.testnet URL=<testnet enclave URL> BUYER=alice.ryan-tobin.testnet \
    GOAL="Is diabetes linked to readmissions in older patients?"
```

On testnet, `alice.ryan-tobin.testnet` paid for requests 3 and 4 this way, and the agent skipped an attack question without paying. Circle's testnet faucet only accepts 64-hex implicit accounts, so the testnet USDC came to an implicit account first and was sent on with `ft_transfer`.

### Funding from another chain with NEAR Intents

`agent/fund.py` brings USDC from Base or Arbitrum to a NEAR mainnet account through the [NEAR Intents 1Click API](https://docs.near-intents.org/). It never holds the buyer's keys: it gets a quote with a one-time deposit address, the buyer sends USDC to it from their own wallet, and the script watches until it lands.

```bash
python agent/fund.py quote --amount 1 --to you.near --refund-to 0xYourBaseWallet        # dry quote: fees and time, nothing moves
python agent/fund.py quote --amount 1 --to you.near --refund-to 0xYourBaseWallet --go   # real quote with a deposit address, then watch
python agent/fund.py status 0xDepositAddress --to you.near [--tx 0xYourBaseTxHash]      # watch an earlier deposit again
```

Before quoting it checks that the account exists and is registered with the USDC contract, because a transfer to an unregistered account would fail. A real quote is saved with 1Click's signature to `data/intents/<deposit address>.json`, in case anything needs to be disputed. Set `ONECLICK_API_KEY` (a Distribution Channel key from partners.near-intents.org) to avoid 1Click's extra 0.25% fee. Intents has no testnet, so this always moves real money.

Run on Nov 4, 2026: 1 USDC sent from Base landed as 0.999798 USDC in `ryan-tobin.near` about 2 minutes later ([NEAR transaction](https://nearblocks.io/txns/5YPrshVTBq2CNar6RKeWyEM85yNKYomd4e1S6VCeYtF5)). That USDC paid for the first mainnet question.

### The browser UI

`make ui` starts the UI (`ui/`, React and Vite, plain CSS and hand-drawn SVG, no component library) on http://localhost:5173. The dropdown in the header picks the network: `testnet` (the default), `mainnet`, or `local` for `make serve` with nothing paid.

- **Dataset card.** Riverside's title, source, price and owner, whether the engine is online in a TDX enclave, and whether the `schema.json` the UI shows hashes to the `schema_hash` on-chain.
- **Privacy meter.** A ring from ε 10 down to 0, read from the contract's `get_dataset`, with ε held by questions being answered shown in amber. It drops as answers settle.
- **Ask.** Type a question or pick a demo one, then **Plan it (free)** shows the QuerySpec in plain words, its ε, its price, and which TEE model planned it. **Pay and ask** has the buyer's NEAR wallet (connected with [NEAR Connect](https://github.com/hot-dao/near-selector), for example Meteor Wallet) sign `request_query`. The engine then answers and settles, and the UI draws the answer as bars with 95% error bars, plus the receipt hash and links to the payment and settlement transactions. Before paying, it runs the same checks as the contract (balance, both caps, budget left) and explains why it won't pay if one fails. A question the engine would refuse can't be paid for.
- **Feed.** The last 15 questions from the contract's `list_recent`, answered, refused (with the rule, refunded), pending or expired, refreshed every 8 seconds.

The UI reads the chain with plain JSON-RPC view calls to FastNEAR's public RPC and talks to the engine over HTTPS. The engine allows any origin to read its replies (CORS), since every endpoint is public and nothing uses cookies. Depositing and setting caps stay in `agent/wallet.py` for now. Run on Nov 4, 2026 in local mode, the demo question came back as women 9.8% ± 1.3, men 11.6% ± 1.4, and the hypertension count by age band as ± 6 per band.

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
- **The question stays private too.** It goes to `/plan` inside the enclave, then to a Private TEE model on NEAR AI Cloud whose attestation and response signature the engine checks. The question is never stored or logged; only the model's signature is kept, in memory, to put in the receipt (`engine/blindfold/planner.py`, `engine/blindfold/app.py`). Planning is limited to 120 calls an hour (`PLAN_LIMIT_PER_HOUR`).
- **Payments, limits and the budget are public, on NEAR.** `contract/src/lib.rs` holds each dataset's price and privacy budget, escrows the fee for every question, and records ε spent and the receipt hash when the enclave settles. A refused question refunds the fee and is logged on-chain with its rule. Pending questions reserve their ε, so they can't overspend the budget together, and the contract won't let the enclave charge more ε than the buyer asked for.
- **An agent can buy answers but can't drain the wallet.** The buyer deposits USDC once and sets a per-question and a daily cap. Changing caps and withdrawing need 1 yoctoNEAR attached, which a NEAR function-call key can't attach, so an agent holding only a key for `request_query` can spend within the caps and nothing else.
- **Every receipt is signed inside the enclave.** The signer key is derived by dstack from the app's identity and never leaves the enclave. The quote binds it, the encryption key and the schema hash together, and the contract only accepts `settle` from that key's account, so a receipt hash on-chain means the enclave signed it (`engine/blindfold/enclave.py`, `engine/blindfold/receipts.py`, `verify/verify.py`).
- **The engine can only charge what was paid for.** In chain mode a question must match the spec hash and ε of a paid request, or it is refused and refunded (`engine/blindfold/app.py`). A request is answered and settled once.

## Benchmark results

### Planner: plain English to QuerySpec

`make plan-bench` plans 25 questions (the 5 demo questions and 4 rewordings of each, `datasets/riverside/planner_questions.json`) through NEAR AI Cloud, with the attestation and signature checked on every call. A question is correct when its spec is the same canonical QuerySpec as an expected one (the 15 readmission, hypertension and heart-failure questions each accept either of two equivalent specs), or, for the 5 attack questions, when it is refused. Run on Nov 4 and 5, 2026:

| Private TEE model on NEAR AI Cloud | Prompt | Correct | Needed a retry | Median time |
| --- | --- | --- | --- | --- |
| `z-ai/glm-5.3-flash` (default) | current (per-patient rates) | 25/25 | 1 | 4.2 s |
| `z-ai/glm-5.3-flash` | Day 8 | 25/25 | 0 | 3.0 s |
| `deepseek-ai/DeepSeek-V4.1-Flash-1017` | Day 8 | 25/25 | 2 | 2.8 s |
| `Qwen/Qwen3.6-35B-A3B-FP8` | Day 8 | 25/25 | 1 | 4.8 s |
| `Qwen/Qwen3.8-27B` | Day 8 | dropped: over 120 s per question | | |

With the current prompt, `z-ai/glm-5.3-flash` planned 4 of the 5 readmission questions per patient. It planned the fifth ("what fraction of hospital admissions…") per stay, as the prompt asks when a question counts stays. Attack questions are the slowest to plan, up to 86 s.

On the Day 8 prompt, none of the 15 attack questions (5 per model) got an answer: the model refused 9 as asking about one person, and the engine refused 6 as `too_fine`. On the current prompt, the 5 attack questions were refused 2 times by the model and 3 times by the engine. `z-ai/glm-5.3` was not tested because its attestation didn't echo our nonce, so the engine won't use it.

_TBD: accuracy vs ε from `make bench` (Day 11)._

## Tests

`make test` runs the unit tests:

- `engine/tests/test_load.py` (30): table schemas, identifier removal, `readmit_30d`, category values and lab bounds.
- `engine/tests/test_spec.py` (45): valid QuerySpecs pass and normalise to one canonical form; 33 bad specs are each refused with the right rule and reason.
- `engine/tests/test_compile.py` (22): exact answers on a hand-made database, the per-patient cap, empty cells, clipping, parameterized SQL, and runs on the real tables.
- `engine/tests/test_dp.py` (36): chi-square tests of the noise sampler against the exact distribution, its mean and variance, exact 95% error bars and their coverage, the noise scale for each measure, suppression, clipping, and noisy answers on the real tables.
- `engine/tests/test_budget.py` (8): exact budget sums, the replay cache, the rate-limit counter, state that survives a restart, rollback on failure, and receipt hashes.
- `engine/tests/test_app.py` (17): the HTTP service end to end: answers with receipts, CORS for the browser UI, free identical replays, the averaging attack, a refusal for each rule, the budget running out, the rate limit, a restart, and the demo question on the real tables.
- `engine/tests/test_enclave.py` (14): keys from a fake dstack agent over a unix socket and HTTP, report_data in the quote, dev-mode keys, and signed receipts that break when any field changes.
- `engine/tests/test_chain.py` (15): chain mode against a fake contract: paid questions settle, replays cost no ε, mismatched or bad specs are refused and refunded, a request is answered once, a failed settle retries for free, the budget follows the contract, plus NEAR transaction bytes checked against py-near and base58.
- `engine/tests/test_bundle.py` (10): bundle round trip with every row and type intact, no file access after loading, fresh ciphertext that reveals nothing, the wrong hash, key, schema or archive entries refused, an engine that starts without data and loads the bundle later, chain mode loading only the listed bundle, and the real tables.
- `engine/tests/test_attest.py` (11): a made-up TDX quote and event log: every check passes, the compose file named in the label, dstack's digest formula, digests computed when dstack leaves them empty, a forged event log, a different compose file, keys not in the quote, operator env vars, the NEAR AI key as the one allowed secret, and simulated or dev enclaves shown as warnings.
- `engine/tests/test_planner.py` (18): the planner against a fake NEAR AI server that signs with a real secp256k1 key: a checked plan end to end, one retry for bad JSON, rephrase after two failures, privacy refusals never retried, model refusals, a missing nonce, the wrong signer, a signature over other bytes, a late signature, prompt examples kept apart from the test questions, the planner's ε defaults, per-patient readmission rates in the prompt, the stub, `/plan`, its rate limit, and the model signature in the receipt.
- `engine/tests/test_ethsig.py` (4): keccak-256 and Ethereum signature recovery against values from eth-account.
- `agent/tests/test_agent.py` (13): USDC and NEAR amounts to and from text, agent keys that are refused (full access, the wrong contract, other methods, not on the account), the pre-payment checks for each cap, the balance and the budget, the 1Click quote body, and the report.
- `contract/src/tests.rs` (19): answered questions pay the owner and spend ε; refusals refund and cost nothing; a free replay; only the enclave settles, once, and never for more ε than requested; per-question and daily caps and the daily reset; an agent key can't raise its caps; pending questions reserve the budget; closed datasets; owner-only updates; expiry refunds; USDC-only deposits; unique dataset ids; the recent-queries feed.

## Contract and enclave measurement

The same contract runs on two networks, each with its own TDX enclave on Phala Cloud:

| | Testnet (demo fallback) | Mainnet (real USDC) |
| --- | --- | --- |
| Contract | [`blindfold.ryan-tobin.testnet`](https://testnet.nearblocks.io/address/blindfold.ryan-tobin.testnet) | [`blindfold.ryan-tobin.near`](https://nearblocks.io/address/blindfold.ryan-tobin.near) |
| Code hash | `E6cjptwZoGVmyhaxjL2bJ8ScUJeNipJK5sjAYebC82Hd` | `E6cjptwZoGVmyhaxjL2bJ8ScUJeNipJK5sjAYebC82Hd` (same wasm) |
| USDC | Circle's testnet USDC `3e2210e1…b8af` | Circle's USDC `17208628…33a1` |
| Riverside price | 0.25 USDC | 0.05 USDC |
| Enclave compose file | `docker-compose.tee.yml` | `docker-compose.mainnet.yml` |

Build the contract with `make contract`; cargo-near prints the wasm's SHA-256. The mainnet contract was deployed in [this transaction](https://nearblocks.io/txns/8XZ2zLKNjTtJJcWPt63FSZm1J2ELZf9PZAqM9QYxgSJE), with `ryan-tobin.near` as admin and an expiry of 1000 blocks.

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

Both enclaves run the same engine image, on Phala Cloud (Intel TDX, dstack 0.5.12, production OS image). Both compose files pass `NEAR_AI_API_KEY` as Phala's encrypted secret, the one secret the verifier allows:

| | Testnet enclave | Mainnet enclave |
| --- | --- | --- |
| URL (started for demos) | `https://87eb46e50a2a931b1f84dfeff6cb89bf77e9cff0-8000.dstack-pha-prod5.phala.network` | `https://ad57f4701c05bfdc2fe85c8afbaf8d046499a8d5-8000.dstack-pha-prod9.phala.network` |
| App id | `87eb46e50a2a931b1f84dfeff6cb89bf77e9cff0` | `ad57f4701c05bfdc2fe85c8afbaf8d046499a8d5` |
| Compose hash (registered on-chain) | `91b520758464d565…` (Day 10 image; was `2df57d50…292a`) | `8380732b7ee494b4…` (Day 10 image; was `a12ccebe…4b16`) |
| Engine image | `ghcr.io/ryan-tobin/blindfold-engine@sha256:e908c0d71eca45be286829608ba987ac11e3bd02d877563c0d9f580b0cdfca11` | the same |
| Enclave NEAR account | `b654d7e23d33e9bcc3096295ee4c85877f9e6c6dfc5b0f02960dd263dba91146` | `e637acc95c667ca5cfc7806a1d1459f7ece3dbe0dd26ed92403f0b48cec9bd6d` |
| Signer key | `ed25519:DGkEji4hQLxUYbDCYKFA5RwMJ8wfAjWKaREnwX6TpbuX` | `ed25519:GVg1ZbpCZDLUirwVcBtvXMezeJEo62gfKtK9PVzDHYdA` |
| Quote hash (registered on-chain) | `TESTNET_QUOTE_HASH` | `MAINNET_QUOTE_HASH` |
| Encrypted bundle | [`riverside-v1`](https://github.com/ryan-tobin/blindfold/releases/tag/riverside-v1), SHA-256 `f67024c6e0678a34f02b1b0bc99ff9d796169ba9fc343fd3dfab211211b039ce` | [`riverside-mainnet-v1`](https://github.com/ryan-tobin/blindfold/releases/tag/riverside-mainnet-v1), SHA-256 `5a094f8d205aafa5c95d940886234b20cc155607ecf8442847f1443d7f2252c0` |

The mainnet enclave is a separate Phala app, so the key service gave it its own keys, and the owner encrypted a second bundle to them. Check the engine image came from this repo:

```bash
gh attestation verify oci://ghcr.io/ryan-tobin/blindfold-engine@sha256:e908c0d71eca45be286829608ba987ac11e3bd02d877563c0d9f580b0cdfca11 --repo ryan-tobin/blindfold
```

Check them yourself while they're running:

```bash
make verify REQUEST=3 CONTRACT_ID=blindfold.ryan-tobin.testnet URL=<testnet URL>
make verify REQUEST=0 CONTRACT_ID=blindfold.ryan-tobin.near NEAR_NETWORK=mainnet URL=<mainnet URL> COMPOSE=docker-compose.mainnet.yml
```

Testnet request 3 was paid by the buyer agent and answered and settled inside the enclave; every check passes. Mainnet request 0 is the first question paid with real USDC: `ryan-tobin.near` deposited 0.9 of the bridged USDC, set caps of 0.05 a question and 0.5 a day, and its agent key paid 0.05 USDC in [this transaction](https://nearblocks.io/txns/GiyUrmw7F69j9tiXFgoeJvFAEtk2MF7YBvu9TWXF2ZDq). Its receipt hash is `05a6f0f9e1d9f0070fec1553fac35a2e5480df775d7978e3e726b5e93e679837`, and all 24 checks pass. The Day 7 testnet run (request 2, settled in [this transaction](https://testnet.nearblocks.io/txns/8B2Aape5LfR7dQ3Gih3Wj76zDehCxPGVpJ3bDdqqBxbA)) was signed by the same testnet enclave key under the earlier compose hash `b6f5b259…b583`. Requests answered before Day 10 name their own compose hashes (`2df57d50…` testnet, `a12ccebe…` mainnet), so `make verify` on them now shows the compose check in yellow: the enclave has since been upgraded and re-registered under the Day 10 image, with the same keys.

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
| [dcap-qvl](https://github.com/Phala-Network/dcap-qvl) 0.7.0 | Checks Intel TDX quotes up to Intel's root CA (`engine/blindfold/attest.py`, dev and verify only) | Apache 2.0 |
| [Phala Cloud](https://phala.com/) with dstack 0.5.12 | Runs the real enclave: the TDX VM, its OS image and its key service (KMS) | service; dstack is Apache 2.0 |
| GitHub Actions (`actions/checkout`, `docker/*-action`, `actions/attest-build-provenance`) | Builds and signs the engine image | MIT or Apache 2.0 |
| [pytest](https://pytest.org/) | Tests | MIT |
| [near-sdk-rs](https://github.com/near/near-sdk-rs) 5.29.1 | The NEAR contract | MIT or Apache 2.0 |
| [cargo-near](https://github.com/near/cargo-near) 0.22.0 | Builds the contract; project layout from `cargo near new` | MIT or Apache 2.0 |
| [near-cli-rs](https://github.com/near/near-cli-rs) 0.30.1 | Creates accounts, deploys, and signs calls from `owner/register.py` | MIT or Apache 2.0 |
| [NEAR Intents 1Click API](https://docs.near-intents.org/) | Bridges USDC from Base or Arbitrum to NEAR (`agent/fund.py`), called over plain HTTPS | service |
| [Circle USDC](https://www.circle.com/usdc) on NEAR | The payment token, testnet and mainnet | service |
| [NEAR AI Cloud](https://docs.near.ai/cloud/) | Private TEE model inference for `/plan`, with attestation and response signatures, called over plain HTTPS | service |
| [React](https://react.dev/) 19.3.1 and [Vite](https://vite.dev/) 8.3.4 with `@vitejs/plugin-react` 6.1.2 | The browser UI | MIT |
| [NEAR Connect](https://github.com/hot-dao/near-selector) (`@hot-labs/near-connect`) 0.11.4 | Connects the buyer's NEAR wallet in the UI and has it sign `request_query` | MIT |
| [FastNEAR](https://fastnear.com/) public RPC | The UI's and scripts' view calls and transactions | service |
| Python standard library | Everything else in the engine, including SQLite for the budget ledger | PSF |
| Claude (Anthropic), via Claude Code | AI assistance: project brief, planning, and docs | n/a |

## Known limits and what's next

- All data is synthetic (Synthea). No real person's data is used, even for testing.
- Synthea has no ZIP code for some towns; those patients get `zip3 = "unknown"`.
- Contribution caps trade accuracy for privacy: a patient with more than 5 matching admissions or lab results counts only 5 (a fixed pseudo-random pick), so totals over heavy users are lower than the raw data. The benchmark measures this.
- Depression and heart failure are rare in Synthea's living population (well under 1% of patients), so most questions about them will be suppressed as small cells.
- Error bars cover the noise only, not sampling error. For rates and means they use a standard approximation; the tests check they hold the true value about 95% of the time.
- `readmit_30d` near the end of the data (after Oct 1, 2026) can miss readmissions that happen after the data ends.
- The simulated enclave's keys come from the simulator's public sample key file, so anyone can derive them, so it only ever runs locally.
- Phala's key service (KMS) derives the enclave's keys from its app id. Redeploying the same app keeps them; a new app gets new keys, and the owner re-encrypts the bundle (`make bundle`).
- Without `CONTRACT_ID` the engine runs in local mode: the buyer in `/query` is a name the caller types and nobody pays. With a contract, buyers pay in USDC from a prepaid balance (see Payments and the buyer agent).
- The agent's key is only as safe as the file it lives in (`~/.blindfold/agent-keys`, chmod 600). Whoever holds it can spend up to the caps, and burn its NEAR gas allowance, until the buyer runs `revoke-agent`.
- Planned questions now default to ε 1.0 (rates, means) or 0.5 (counts, sums, histograms), for usable error bars. That spends Riverside's budget of 10 in about 10 to 20 new questions; a real owner would set a larger budget or a lower default.
- The UI has no automated tests; its wallet flow is checked by hand. Deposits and caps are set with `agent/wallet.py`, not in the UI yet.
- Both enclaves are started only for demos, to keep the cost down (about $0.06 an hour each). While one is stopped, its contract still takes questions, and `expire` refunds any that nobody settles.
- NEAR Intents has no testnet, so `agent/fund.py` only runs on mainnet with real money. On testnet the USDC comes from Circle's faucet.
- Trust model: the contract doesn't check the Intel TDX quote itself, which is too heavy for a contract. It trusts one registered enclave account, whose key exists only inside the enclave, and stores the quote hash so anyone can check it off-chain.
- Trust in Phala: the enclave's keys come from Phala's key service (KMS), which only releases them to a VM whose quote and compose hash it accepts, and the OS image is dstack's. The verifier checks the OS image hash is in the quote but doesn't rebuild the image. A KMS run on-chain (dstack supports one on Base) would remove that trust.
- The contract account pays NEAR storage for every question it records (a few hundred bytes each).
- The planner can misread a question. That is why `/plan` shows the spec in plain words before paying, and why the engine re-checks every spec. `make plan-bench` measures how often it is right.
- The engine checks NEAR AI's attestation nonce and signature on every call, but not the model's Intel and NVIDIA quotes (too slow per question). `make verify` checks the Intel quote with `NEAR_AI_API_KEY` set; the NVIDIA GPU evidence is passed to the buyer in `/plan`'s `evidence` but not yet checked.

## License

MIT. See [LICENSE](LICENSE).