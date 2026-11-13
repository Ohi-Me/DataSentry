# Convenience targets. Everything here is a thin wrapper over `ds`, which is
# the real entry point -- so a machine without make loses nothing.
#
# On Windows: `py ds.py <command>` or `.\make.ps1 <target>`.

PY ?= python

.PHONY: help setup doctor demo seed inject detect correlate rca execute verify serve eval test lint typecheck status reset clean docker docker-run

help:           ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:          ## venv, dependencies, .env, then check the environment
	./scripts/dev.sh --setup-only

doctor:         ## is this machine able to run DataSentry, and why not
	$(PY) ds.py doctor

demo:           ## the whole loop end to end, scored against ground truth
	$(PY) ds.py demo

seed:           ## build the synthetic warehouse and its history
	$(PY) ds.py seed --fresh

inject:         ## inject a fault: make inject TYPE=JOIN_SEMANTICS
	$(PY) ds.py inject $(TYPE) --advance 18

detect:         ## score the recent cycles
	$(PY) ds.py detect --cycles 18

correlate:      ## group anomalies into incidents
	$(PY) ds.py correlate

rca:            ## run the reasoning loop
	$(PY) ds.py rca --verbose

execute:        ## run approved actions
	$(PY) ds.py execute

verify:         ## check whether remediations cleared
	$(PY) ds.py verify

serve:          ## API and console on http://localhost:8787
	$(PY) ds.py serve --open

eval:           ## score against ground truth over several seeds; non-zero on regression
	$(PY) ds.py eval

test:           ## unit and integration tests
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy packages services tools

status:         ## where everything stands
	$(PY) ds.py status

reset:          ## drop the control plane
	$(PY) ds.py reset

docker:         ## build the image
	docker build -t datasentry:local .

docker-run:     ## run the image, console on http://localhost:8787
	docker compose up --build

clean:
	rm -rf var/*.db var/*.db-wal var/*.db-shm evals/results/*.json
