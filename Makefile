PYTHON ?= python3

.PHONY: test lint demo

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

# Embedded CSMS + the default fleet, with the live status table. Logs go to logs/.
demo:
	$(PYTHON) -m ocpp_ops.fleet --fleet fleet.yaml --log-dir logs
