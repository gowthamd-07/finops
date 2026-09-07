VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
DATA ?= ./localdata
MONTH ?=

# Load .env if present.
ifneq (,$(wildcard ./.env))
include .env
export
endif

.PHONY: help venv install test run-web run clean docker-up docker-down docker-logs docker-cli docker-prod-up

help:
	@echo "make install      - create venv and install deps"
	@echo "make test         - run the unit test suite"
	@echo "make run-web      - start the web app at http://localhost:8080"
	@echo "make run MONTH=2026-05 - CLI run for a month (no email)"
	@echo "make docker-up    - run web + Postgres in Docker (http://localhost:8080)"
	@echo "make docker-prod-up - run the production compose stack (Key Vault)"
	@echo "make docker-down  - stop and remove the Docker stack"
	@echo "make docker-logs  - tail the web container logs"
	@echo "make docker-cli MONTH=2026-05 - run a CLI generation inside Docker"

venv:
	python3 -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "If WeasyPrint fails: brew install cairo pango gdk-pixbuf libffi"

test:
	$(PY) -m unittest discover -s tests -v

# run-web + run require DATABASE_URL (from .env or your shell).
run-web:
	CONFIG_PATH=config/config.yaml \
	$(VENV)/bin/uvicorn src.web.app:app --reload --port 8080

run:
	CONFIG_PATH=config/config.yaml \
	$(PY) -m src.main $(if $(MONTH),--month $(MONTH),) --no-email --output ./report.pdf
	@echo "Wrote ./report.pdf"

docker-up:
	docker compose up --build -d
	@echo "Web at http://localhost:8080 (login uses BASIC_AUTH_* from .env)"

docker-prod-up:
	docker compose -f docker-compose.prod.yml up --build -d
	@echo "Production stack up. Secrets/config are pulled from Azure Key Vault."

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f web

# Generate a month via the CLI inside the running stack (uses the compose DB).
docker-cli:
	docker compose run --rm web python -m src.main $(if $(MONTH),--month $(MONTH),) --no-email --output /tmp/report.pdf

clean:
	rm -rf $(VENV) report.pdf
