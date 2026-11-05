# Blindfold. Run `make help` for the list of targets.
# Python commands use the active virtualenv (see README Quickstart).

PYTHON ?= python3

SPEC ?= datasets/riverside/examples/readmit_patients_diabetes_65plus_by_sex.json
TRIALS ?= 0
BUYER ?= alice.testnet
URL ?= http://127.0.0.1:8000
NEAR_NETWORK ?= testnet
CONTRACT_ID ?=
OWNER ?=
ADMIN ?=
REQUEST ?=
COMPOSE ?=
ALLOW_SIM ?=
Q?= What is the 30-day readmission rate for diabetic patients aged 65 and older, by sex?
MODELS ?=
GOAL ?=
EPS ?= 0.1,0.2,0.5,1
DATA_URL ?= https://github.com/ryan-tobin/blindfold/releases/download/riverside-data-v1/riverside.duckdb
DATA_SHA256 ?= 9509ce8ae957b677399716ca41f4c47881e0f80daa1bb70ade9e3ef1a0ce8bc6

.PHONY: help data test sql ask serve query reset contract register up register-enclave pay verify demo bench bundle buy ui data-quick

help:
	@echo "make data    generate 10,000 Synthea patients and build the 5 analytic tables"
	@echo "make test    run the engine and contract unit tests"
	@echo "make sql     check a QuerySpec, print its SQL and true values (SPEC=file.json)"
	@echo "make ask     answer a QuerySpec with noise, as a buyer sees it (SPEC=file.json, TRIALS=200 for accuracy)"
	@echo "make serve   start the engine's HTTP service on $(URL) (docs at $(URL)/docs)"
	@echo "make query   send a QuerySpec to the running service (SPEC=file.json BUYER=alice.testnet)"
	@echo "make reset   forget the local budget ledger, replay cache and receipts"
	@echo "make up      run the engine in a simulated enclave with Docker (dstack simulator)"
	@echo "make register-enclave  register the running engine's enclave (CONTRACT_ID=... ADMIN=...)"
	@echo "make ask-question	plan Q, pay for it on-chain, get the answer (CONTRACT_ID=... BUYTER=...)"
	@echo "make pay     pay for SPEC on-chain, then get the answer (CONTRACT_ID=... BUYER=...)"
	@echo "make verify  check a receipt's signature, enclave and on-chain record (REQUEST=id)"
	@echo "make contract  build the NEAR contract to contract/target/near/blindfold.wasm"
	@echo "make register  list Riverside on the contract (CONTRACT_ID=... OWNER=...)"
	@echo "make demo    start local mode (not built yet)"
	@echo "make bench   accuracy of 200 benchmark questions at each epsilon (EPS=0.1,0.2,0.5,1 TRIALS=20)"
	@echo "make deploy  deploy contract and enclave (not built yet)"
	@echo "make bundle  check the enclave, then encrypt the tables to its key (URL=... CONTRACT_ID=... OWNER=...)"
	@echo "make plan	turn a plain-English question into a QuerySpec, free (Q=\"...\")"
	@echo "make plan-bench	score the planner on the 25 test questions (MODELS=a,b or MODELS=stub)"
	@echo "make buy		the buyer agent: plan, pay with its function-call key, answer, report (BUYER=... Q=... or GOAL=...)"
	@echo "make ui		the browser UI on http://localhost:5173 (needs Node 20+)"
	@echo "make data-quick  download the same tables, prebuilt (4 MB, checked by SHA-256)"
	@echo "make demo    the whole local flow in one command: plan, answer, replay, refusal, verify"

data:
	bash scripts/generate_data.sh
	cd engine && $(PYTHON) -m blindfold.load

data-quick:
	mkdir -p data
	curl -fsSL --retry 3 -o data/riverside.duckdb.part $(DATA_URL)
	$(PYTHON) -c "import hashlib,sys; h=hashlib.sha256(open('data/riverside.duckdb.part','rb').read()).hexdigest(); sys.exit(0 if h=='$(DATA_SHA256)' else 'SHA-256 mismatch: '+h)"
	mv data/riverside.duckdb.part data/riverside.duckdb
	@echo "data/riverside.duckdb ready: the 5 tables make data builds (SHA-256 $(DATA_SHA256) checked)"

test:
	cd engine && $(PYTHON) -m pytest -q
	$(PYTHON) -m pytest -q agent/tests
	@if command -v cargo >/dev/null; then cd contract && cargo test -q; \
	else echo "cargo not found: skipped the 19 contract tests (install Rust: https://rustup.rs)"; fi

sql:
	cd engine && $(PYTHON) -m blindfold.compile $(abspath $(SPEC)) --run

ask:
	cd engine && $(PYTHON) -m blindfold.dp $(abspath $(SPEC)) --trials $(TRIALS)

serve:
	cd engine && $(PYTHON) -m uvicorn blindfold.app:create_app --factory --host 127.0.0.1 --port 8000

plan:
	@curl -s -X POST $(URL)/plan -H 'content-type: application/json' \
		-d "$$($(PYTHON) -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$(Q)")" \
		| $(PYTHON) -c 'import json,sys; d=json.load(sys.stdin); d.pop("evidence",None); print(json.dumps(d,indent=2,ensure_ascii=False))'

plan-bench:
	cd engine && $(PYTHON) -m blindfold.plan_bench $(if $(MODELS),--models "$(MODELS)")

query:
	@curl -s -X POST $(URL)/query -H 'content-type: application/json' \
		-d "{\"buyer\": \"$(BUYER)\", \"spec\": $$(cat $(SPEC))}" | $(PYTHON) -m json.tool --no-ensure-ascii

reset:
	rm -f data/state*.sqlite

contract:
	cd contract && cargo near build non-reproducible-wasm

register:
	$(PYTHON) owner/register.py --contract $(CONTRACT_ID) --owner $(OWNER) --network $(NEAR_NETWORK)

up:
	docker compose up --build

register-enclave:
	$(PYTHON) owner/register_enclave.py --contract $(CONTRACT_ID) --admin $(ADMIN) --engine $(URL) --network $(NEAR_NETWORK)

bundle:
	$(PYTHON) owner/encrypt.py --engine $(URL) $(if $(CONTRACT_ID),--contract $(CONTRACT_ID)) $(if $(OWNER),--owner $(OWNER)) --network $(NEAR_NETWORK) $(if $(ALLOW_SIM),--allow-simulated)

ask-question:
	$(PYTHON) agent/pay_and_ask.py --contract $(CONTRACT_ID) --buyer $(BUYER) --question "$(Q)" --engine $(URL) --network $(NEAR_NETWORK)

pay:
	$(PYTHON) agent/pay_and_ask.py --contract $(CONTRACT_ID) --buyer $(BUYER) --spec $(SPEC) --engine $(URL) --network $(NEAR_NETWORK)

buy:
	$(PYTHON) agent/buyer.py --buyer $(BUYER) --engine $(URL) --network $(NEAR_NETWORK) $(if $(CONTRACT_ID),--contract $(CONTRACT_ID)) $(if $(GOAL),--goal "$(GOAL)","$(Q)")

verify:
	$(PYTHON) verify/verify.py --engine $(URL) --request $(REQUEST) $(if $(CONTRACT_ID),--contract $(CONTRACT_ID)) $(if $(COMPOSE),--compose $(COMPOSE)) --network $(NEAR_NETWORK)

ui:
	cd ui && npm install && npm run dev

bench:
	cd engine && $(PYTHON) -m blindfold.bench --eps $(EPS) --trials $(if $(filter 0,$(TRIALS)),20,$(TRIALS))

demo:
	$(PYTHON) scripts/demo.py