# Cloud Cost Reporter — Overview & Proposal

*Status: public snapshot · Last updated: Sep 2026*

> A short doc to explain **why** this was built, **what** it does today, the
> **constraints** it operates under, and **how it can evolve**.

---

## 1. TL;DR

Cloud Cost Reporter is an internal tool that pulls Example's **AWS + Azure**
bills automatically and produces **one monthly cost report** (web page + PDF),
including a month-over-month executive summary. What used to be a manual,
multi-hour, error-prone spreadsheet exercise is now **a single click** (or a
fully automated job on the 5th of every month).

- **Time:** ~2 days to build (with Cursor).
- **Built by:** Gowtham D.
- **Repo:** https://github.com/gowthamd-07/finops

### Access the app

| | |
|---|---|
| **URL** | http://localhost:8080 (local) or your own ingress host |
| **Network** | Bind to localhost or an internal network |
| **Username** | `admin` |
| **Password** | set in `.env` (`BASIC_AUTH_PASSWORD`) |
| **Role** | `admin` (full access). Additional `viewer` accounts can be created from the **Admin** page once logged in. |

> **Security note:** never commit `.env`. Rotate bootstrap credentials and keep
> production secrets in Key Vault.

---

## 2. The problem (what was painful before)

Every month, cloud spend was reconciled **by hand** across multiple billing
portals and copied into a spreadsheet. Concretely, the pain points were:

1. **Manual, repetitive collection.** Someone had to log into separate billing
   portals (AWS, Azure), read costs per service, and copy numbers into a sheet.
2. **Slow service-vs-resource-group reconciliation.** In the tracking sheet,
   walking each service line (e.g. **rows ~52 → ~74**) and matching billing
   against the right resource group took a long time, every single month.
3. **Manual executive summary.** Writing "what went up / down vs last month"
   was a separate manual analysis on top of the data gathering.
4. **Human error.** Copy/paste and manual tallying meant mistakes were easy and
   hard to catch.
5. **No single source of truth.** Numbers lived in a spreadsheet; comparing
   months or filtering by environment/workload was awkward.

**Net effect:** a recurring, low-leverage task that consumed hours each month
and still carried a risk of being wrong.

---

## 3. What we built (the solution)

A small service that does the whole job end-to-end:

- **Collects** AWS (Cost Explorer) and Azure (Cost Management + Billing invoice)
  costs automatically, **read-only** — it can never change resources or spend
  money.
- **Aggregates** everything into one report: per-cloud totals (charges, credits,
  tax), a **breakdown by service**, and a split across
  **Production / Non-Production / Shared** environments and workloads
  (Ecommerce, Shared, VRE, AI, Bioinfo, Others).
- **Compares to last month** automatically — top increases and savings as an
  **executive summary**.
- **Two ways to run, same engine:**
  1. On demand via the **web app** (pick a month, pick clouds, generate).
  2. A **scheduled job** that runs on the **5th of each month** with no human
     involved and emails the report.
- **Keeps history** in a PostgreSQL database so any past month can be reopened,
  compared, and downloaded as a PDF.
- **Filtering is easy** — everything for all clouds is in one place, filterable
  by cloud / environment / workload, instead of scattered across portals.

### Before vs. After

| | Before (manual) | After (Cloud Cost Reporter) |
|---|---|---|
| Data collection | Log into each portal, copy by hand | Automatic, read-only API pulls |
| Service ↔ resource-group reconciliation | Manual, line-by-line (rows 52→74) | Automatic grouping |
| Month-over-month summary | Written by hand | Generated automatically |
| Errors | Easy to introduce | Eliminated (no manual entry) |
| Effort per month | Hours | One click / fully automated |
| Source of truth | A spreadsheet | One web app + PDF + history DB |

---

## 4. Screenshots

> _Placeholders — to be filled in before sharing. Suggested captures below._

- **Dashboard** — summary cards, "cost by cloud" doughnut, spend-trend line,
  per-cloud table, list of saved reports.
  `![Dashboard](images/dashboard.png)`
- **Generate** — month picker + cloud selection + "email report" option.
  `![Generate report](images/generate.png)`
- **Report / Executive summary** — per-cloud totals, service breakdown by
  environment, and top increases/decreases vs last month.
  `![Executive summary](images/report-exec-summary.png)`
- **PDF export** — the downloadable monthly report.
  `![PDF report](images/pdf.png)`

---

## 5. How it works (the big picture)

```
                 ┌──────────── Web app ──────────────────────┐
You (browser) ─► │ dashboard · generate report · view/download │
                 └─────────────────────────────────────────────┘
                                   │ (same shared "engine")
Automatic        run on a schedule │
monthly job ────────────────────► ▼
        read the 2 cloud bills → add them up → make a PDF → save it + email it
```

- **Backend:** FastAPI (Python), PDF via WeasyPrint, data in PostgreSQL.
- **Auth:** role-based — `admin` (generate, manage users/data) and `viewer`
  (read + download only).
- **Secrets:** never in code; pulled from Azure Key Vault (or `.env` locally).
- **Deploy:** containerized (Docker), runs on Kubernetes; the monthly run is a
  CronJob.

---

## 6. Constraints & current limitations (be honest)

- **Two clouds today:** AWS + Azure. (GCP / other providers not yet covered.)
- **Azure scope:** reads four specific subscriptions (Production,
  NonProduction, Connectivity, Management). New subscriptions need to be added
  to config.
- **Read-only access required:** needs `Cost Management Reader` + `Billing
  account reader` on Azure and Cost Explorer read perms on AWS.
- **Workload mapping** (RG → Ecommerce/VRE/AI/etc.) is derived from
  resource-group naming conventions; misnamed groups can land in "Others".
- **Not yet on GitHub** — code still local; needs to be pushed before others
  can review/contribute.
- **Single maintainer** so far; no formal review/ownership across the team yet.
- **Reporting, not optimization:** it reports spend; it doesn't yet recommend
  savings or enforce budgets.

---

## 7. Now → Future (how this can evolve)

**Now (works today)**
- Automated AWS + Azure monthly report (web + PDF + email).
- Month-over-month executive summary.
- History + filtering in one place; one-click generation.

**Next (near term)**
- Push to GitHub; add CI, tests, and proper access/ownership.
- Screenshots + onboarding doc for the team.
- Validate workload/RG mappings with finance + infra owners.

**Later (if adopted)**
- Budgets & alerts (flag when a service/environment exceeds a threshold).
- Anomaly detection (call out unusual spikes automatically).
- Cost-saving recommendations (idle/underused resources, commitment coverage).
- More providers/sources (GCP, SaaS spend) for a true single pane of glass.
- Self-serve dashboards / Slack digest instead of just email.
- Tag/showback by team or product line for accountability.

---

## 8. Feedback we're looking for

1. Is a unified monthly multi-cloud report useful for your workflow?
2. Is the **environment / workload breakdown** the right way to slice spend?
3. What's missing for this to replace a spreadsheet entirely?
4. Which "Later" items would deliver the most value first (budgets? alerts?
   recommendations?)?

---

## 9. Links

- **Local app:** http://localhost:8080
- **Repo:** https://github.com/gowthamd-07/finops
- **README (setup/run/deploy):** `../README.md`
