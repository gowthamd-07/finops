# Example FinOps — Cloud & AI Cost Reporter

**One-page overview for stakeholders** · Last updated Aug 2026

---

## What it is

A single web dashboard that pulls the **actual monthly bill** from every cloud and AI
provider Example uses — **AWS, Azure, GCP, Cursor, and Claude (Anthropic)** — reconciles
each against the real invoice, and presents one trusted number for **charges, credits,
tax, and amount due**. It replaces the manual, spreadsheet-based report that took days
to assemble and was hard to audit.

> One place to see *what we spent, where it went, and where it's heading* — for both
> cloud infrastructure and AI/LLM tooling.

## Why it matters (impact)

- **From days to minutes.** The monthly cost report is generated on demand instead of
  hand-built from provider consoles and exports.
- **One source of truth.** Every figure is reconciled to the provider invoice, so
  Finance, Engineering, and leadership review the same numbers.
- **AI spend is finally visible.** Cursor, Claude, and Google Gemini/Vertex spend is
  tracked per model and per user/key — previously invisible in the cloud reports.
- **A shared basis for action.** Month-over-month movers, workload attribution, and a
  next-month forecast turn the report from a record-keeping exercise into a tool the
  group can use to **drive costs down and audit spend together**.

## What you can do with it

| Capability | Answers the question |
|---|---|
| Overall + per-cloud tabs | *What did we pay in total, and to whom?* |
| Environment split (prod / non-prod / shared) | *How much is production vs. everything else?* |
| Workload attribution (Ecommerce, Lab, AI, Data, Shared) | *Which product line drives Azure spend?* |
| AI / LLM tab | *What are we spending on AI, by model and by person?* |
| Filters + drill-down charts | *Slice spend by cloud, subscription, resource group, service.* |
| Historical trends + forecast | *Are we trending up or down? What's next month?* |
| Credit burn-down & forecast | *How long do our prepaid credits last?* |
| PDF / CSV export, email distribution | *Share the report with the group each month.* |

## Data validation (how we know the numbers are right)

- **Invoice reconciliation** — each cloud's totals are checked against the authoritative
  invoice; Azure credit and tax are read from the Billing API (not the usage API).
- **GCP source auto-selection** — BigQuery billing export is used for SKU-level detail
  only when it reconciles to the console cost table within tolerance; otherwise the
  console CSV is used.
- **Breakdown reconciliation** — every detail table is checked to sum back to its
  section total, so drill-downs never disagree with the headline number.
- **Read-only access everywhere** — the tool only *reads* billing data; it cannot change
  any cloud resource or spend.

## Known issue being fixed

> **AI cost figures shown in the portal are currently inaccurate and are being corrected.**
> Cursor bills on a cycle that doesn't align to the calendar month and exposes only
> current-cycle "billed" spend, and Anthropic does not attribute cost per API key. We are
> reconciling these to the provider invoices. Treat AI dollar amounts as directional until
> this note is removed; cloud (AWS/Azure/GCP) figures are invoice-reconciled.

*(Screenshots in this document use illustrative demo data, not live billing.)*

## The dashboard at a glance

**Overview** — KPIs, per-cloud spend, environment split, filters, charts, month-over-month movers, and credit forecast:

![Overview dashboard](screenshots/01-overview-dashboard.png)

**AI / LLM** — spend by provider, model, project, and top users/keys:

![AI / LLM tab](screenshots/05-ai-llm.png)

**Historical trends & forecast** — stacked spend by cloud with a next-month projection:

![Historical trends](screenshots/07-trends.png)

## Where we go next (for the group)

1. Finalize AI-cost reconciliation (remove the caveat above).
2. Use the monthly review to agree on **cost-down actions** by workload owner.
3. Set alert thresholds so anomalies (e.g., a Gemini spike) are flagged automatically.
