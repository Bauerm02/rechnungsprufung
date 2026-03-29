PYTHON ?= python

.PHONY: install test run

install:
	$(PYTHON) -m pip install -e .[dev]

test:
	pytest

run:
	uvicorn invoice_automation.api.app:app --reload

