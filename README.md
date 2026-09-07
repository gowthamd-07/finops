# FinOps Cloud Cost Reporter

Collects read-only spend from AWS, Azure, GCP, Cursor, and Anthropic, then
builds one monthly report (web UI + PDF).

This is a **sanitized public snapshot**. Company identifiers, credentials,
subscription IDs, and internal catalogs were removed. Point `config/config.yaml`
and `.env` at your own accounts before running.

## Quick start

```bash
cp .env.example .env
make docker-up
```

App: http://localhost:8080

- Secrets stay in `.env` / Key Vault — never in Git
- Collectors are read-only (Cost Explorer, Cost Management, BigQuery billing export)
- Optional monthly CronJob + email via Azure Communication Services

## Layout

- `src/collectors/` — AWS / Azure / GCP / Cursor / Anthropic
- `src/web/` — FastAPI dashboard
- `k8s/manifest.yaml` — Deployment + monthly CronJob
- `config/config.yaml` — non-secret settings
