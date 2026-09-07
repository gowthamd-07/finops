"""Capture UI screenshots of the running FinOps web app with Playwright.

Uses the system Google Chrome (channel="chrome") so no browser download is
needed. Logs in, then captures each report tab plus the generate/trends pages.

    .venv/bin/python scripts/shots.py
"""
from __future__ import annotations

import os
import pathlib

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
USER = os.environ.get("BASIC_AUTH_USER", "admin")
PWD = os.environ.get("BASIC_AUTH_PASSWORD", "demo1234")
MONTH = os.environ.get("SHOT_MONTH", "2026-06")
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

TABS = [
    ("overview", "01-overview-dashboard.png"),
    ("azure", "02-azure.png"),
    ("aws", "03-aws.png"),
    ("gcp", "04-gcp.png"),
    ("ai", "05-ai-llm.png"),
]
PAGES = [
    (f"{BASE}/generate", "06-generate.png"),
    (f"{BASE}/trends", "07-trends.png"),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  device_scale_factor=2)
        page = ctx.new_page()

        page.goto(f"{BASE}/login", wait_until="networkidle")
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PWD)
        page.click("button[type=submit], input[type=submit]")
        page.wait_for_load_state("networkidle")
        print("logged in:", page.url)

        # Load the report once; overview charts render on load. Tabs are
        # display:none panels toggled by clicking a .tab-btn (a hash-only URL
        # change would NOT reload the page or re-run showTab).
        page.goto(f"{BASE}/reports/{MONTH}", wait_until="networkidle")
        try:
            page.wait_for_selector(".chart-card.loaded", timeout=8000)
        except Exception:
            pass

        for tab, fname in TABS:
            page.click(f".tab-btn[data-tab='{tab}']")
            page.wait_for_selector(f".tab-panel[data-tab='{tab}'].active", timeout=5000)
            page.wait_for_timeout(1000)  # let charts/animations settle
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(OUT / fname), full_page=True)
            print("saved", fname)

        for url, fname in PAGES:
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / fname), full_page=True)
            print("saved", fname)

        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
