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
ALLOW_SIM ?=
Q?= What is the 30-day readmission rate for diabetic patients aged 65 and older, by sex?
MODELS ?=
GOAL ?=

.PHONY: help data test sql ask serve query reset contract register up register-enclave pay verify demo bench deploy bundle buy

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
	@echo "make bench   accuracy vs epsilon benchmark (not built yet)"
	@echo "make deploy  deploy contract and enclave (not built yet)"
	@echo "make bundle  check the enclave, then encrypt the tables to its key (URL=... CONTRACT_ID=... OWNER=...)"
	@echo "make plan	turn a plain-English question into a QuerySpec, free (Q=\"...\")"
	@echo "make plan-bench	score the planner on the 25 test questions (MODELS=a,b or MODELS=stub)"
	@echo "make buy		the buyer agent: plan, pay with its function-call key, answer, report (BUYER=... Q=... or GOAL=...)"

data:
	bash scripts/generate_data.sh
	cd engine && $(PYTHON) -m blindfold.load

test:
	cd engine && $(PYTHON) -m pytest -q
	$(PYTHON) -m pytest -q agent/tests
	cd contract && cargo test -q

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
	$(PYTHON) verify/verify.py --engine $(URL) --request $(REQUEST) $(if $(CONTRACT_ID),--contract $(CONTRACT_ID)) --network $(NEAR_NETWORK)

demo bench deploy:
	@echo "make $@: not built yet"