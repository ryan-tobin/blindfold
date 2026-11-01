# Blindfold. Run `make help` for the list of targets.
# Python commands use the active virtualenv (see README Quickstart).

PYTHON ?= python3

SPEC ?= datasets/riverside/examples/readmit_diabetes_65plus_by_sex.json

.PHONY: help data test sql demo bench deploy

help:
	@echo "make data    generate 10,000 Synthea patients and build the 5 analytic tables"
	@echo "make test    run the engine unit tests"
	@echo "make sql     check a QuerySpec, print its SQL and true values (SPEC=file.json)"
	@echo "make demo    start local mode (not built yet)"
	@echo "make bench   accuracy vs epsilon benchmark (not built yet)"
	@echo "make deploy  deploy contract and enclave (not built yet)"

data:
	bash scripts/generate_data.sh
	cd engine && $(PYTHON) -m blindfold.load

test:
	cd engine && $(PYTHON) -m pytest -q

sql:
	cd engine && $(PYTHON) -m blindfold.compile $(abspath $(SPEC)) --run

demo bench deploy:
	@echo "make $@: not built yet"