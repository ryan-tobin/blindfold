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

.PHONY: help data test sql ask serve query reset contract register demo bench deploy

help:
	@echo "make data    generate 10,000 Synthea patients and build the 5 analytic tables"
	@echo "make test    run the engine and contract unit tests"
	@echo "make sql     check a QuerySpec, print its SQL and true values (SPEC=file.json)"
	@echo "make ask     answer a QuerySpec with noise, as a buyer sees it (SPEC=file.json, TRIALS=200 for accuracy)"
	@echo "make serve   start the engine's HTTP service on $(URL) (docs at $(URL)/docs)"
	@echo "make query   send a QuerySpec to the running service (SPEC=file.json BUYER=alice.testnet)"
	@echo "make reset   forget the local budget ledger, replay cache and receipts"
	@echo "make contract  build the NEAR contract to contract/target/near/blindfold.wasm"
	@echo "make register  list Riverside on the contract (CONTRACT_ID=... OWNER=...)"
	@echo "make demo    start local mode (not built yet)"
	@echo "make bench   accuracy vs epsilon benchmark (not built yet)"
	@echo "make deploy  deploy contract and enclave (not built yet)"

data:
	bash scripts/generate_data.sh
	cd engine && $(PYTHON) -m blindfold.load

test:
	cd engine && $(PYTHON) -m pytest -q
	cd contract && cargo test -q

sql:
	cd engine && $(PYTHON) -m blindfold.compile $(abspath $(SPEC)) --run

ask:
	cd engine && $(PYTHON) -m blindfold.dp $(abspath $(SPEC)) --trials $(TRIALS)

serve:
	cd engine && $(PYTHON) -m uvicorn blindfold.app:create_app --factory --host 127.0.0.1 --port 8000

query:
	@curl -s -X POST $(URL)/query -H 'content-type: application/json' \
		-d "{\"buyer\": \"$(BUYER)\", \"spec\": $$(cat $(SPEC))}" | $(PYTHON) -m json.tool --no-ensure-ascii

reset:
	rm -f data/state.sqlite

contract:
	cd contract && cargo near build non-reproducible-wasm

register:
	$(PYTHON) owner/register.py --contract $(CONTRACT_ID) --owner $(OWNER) --network $(NEAR_NETWORK)

demo bench deploy:
	@echo "make $@: not built yet"