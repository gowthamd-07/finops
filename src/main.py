"""CLI entrypoint: collect -> aggregate -> render PDF -> email.

Used for local runs and ad-hoc generation. The scheduled monthly run and the
web UI share the same engine in src/service.py.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config
from .delivery import send_email
from .service import generate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cloud-cost-reporter")


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-cloud monthly cost report")
    parser.add_argument("--month", help="Target month YYYY-MM (default: previous month)")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--output", default="/tmp/cloud-cost-report.pdf")
    parser.add_argument("--csv", help="Also write line items to this CSV path")
    parser.add_argument("--no-email", action="store_true", help="Skip email delivery")
    parser.add_argument("--no-persist", action="store_true", help="Do not save to the report store")
    parser.add_argument("--force", action="store_true", help="Re-collect from clouds even if month is already stored")
    parser.add_argument("--clouds", default="aws,azure,gcp", help="Subset of clouds to collect")
    parser.add_argument(
        "--cursor-spend-snapshot",
        action="store_true",
        help="Capture the current Cursor billing cycle's billed spend and exit "
             "(run daily so the monthly report can value usage at the invoiced amount)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.cursor_spend_snapshot:
        from .collectors.cursor import snapshot_cursor_billed_spend

        snap = snapshot_cursor_billed_spend(cfg)
        if snap is None:
            log.error("Cursor billed-spend snapshot failed (API or DB unavailable)")
            return 1
        log.info("Cursor billed-spend snapshot captured for cycle %s", snap["cycle_start_date"])
        return 0

    clouds = {c.strip().lower() for c in args.clouds.split(",")} if args.clouds else None

    try:
        result = generate(
            cfg, month=args.month, clouds=clouds,
            persist=not args.no_persist, force=args.force,
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    except RuntimeError as exc:
        log.error("%s. Aborting.", exc)
        return 1

    if result.cached:
        log.info("Loaded existing report for %s from store (use --force to refresh)", result.month_key)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_bytes(result.pdf_bytes)
    log.info("PDF written to %s (grand total %.2f)", args.output, result.report.grand_total)

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        Path(args.csv).write_bytes(result.csv_bytes)
        log.info("CSV written to %s", args.csv)

    if not args.no_email:
        send_email(
            cfg,
            result.pdf_bytes,
            Path(args.output).name,
            result.month_name,
            result.year,
            increases=result.report.key_increases,
            decreases=result.report.savings,
            currency=result.report.currency,
            mom=result.report.mom,
            summaries=[
                {"cloud": s.cloud, "total": s.total, "subtotal": s.subtotal,
                 "credits": s.credits, "tax": s.tax}
                for s in result.report.summaries
            ],
            curr_total=result.report.grand_total,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
