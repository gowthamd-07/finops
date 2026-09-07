"""Integration and unit tests for FinOps."""
from __future__ import annotations

import os
import unittest
from unittest import mock

# Auth must be configured before the app module is imported.
os.environ.setdefault("BASIC_AUTH_PASSWORD", "admin")
os.environ.setdefault("BASIC_AUTH_USER", "admin")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient

from src.models import CostRecord
from src.report.azure_rg import rg_matches_subscription_tier
from src.report.changes import normalize_change_rows, resource_cost_changes
from src.web.app import app


class NormalizeChangeRowsTests(unittest.TestCase):
    def test_legacy_two_tuple(self):
        rows = normalize_change_rows([["Virtual Machines", 100.5]])
        self.assertEqual(rows[0]["service"], "Virtual Machines")
        self.assertEqual(rows[0]["amount"], 100.5)

    def test_legacy_four_tuple(self):
        rows = normalize_change_rows([["Azure", "rg-1", "Storage", 42.0]])
        self.assertEqual(rows[0]["cloud"], "Azure")
        self.assertEqual(rows[0]["amount"], 42.0)

    def test_dict_with_delta(self):
        rows = normalize_change_rows([{"name": "API Mgmt", "delta": 3020}])
        self.assertEqual(rows[0]["amount"], 3020.0)

    def test_dict_missing_amount_defaults_zero(self):
        rows = normalize_change_rows([{"cloud": "Azure", "service": "VM"}])
        self.assertEqual(rows[0]["amount"], 0.0)


class ResourceCostChangesTests(unittest.TestCase):
    def test_increase_and_decrease(self):
        prev = [
            CostRecord(cloud="Azure", service="A", cost=100.0, scope="rg-a"),
            CostRecord(cloud="Azure", service="B", cost=50.0, scope="rg-b"),
        ]
        curr = [
            CostRecord(cloud="Azure", service="A", cost=150.0, scope="rg-a"),
            CostRecord(cloud="Azure", service="B", cost=30.0, scope="rg-b"),
        ]
        inc, dec = resource_cost_changes(prev, curr, top_n=5)
        self.assertTrue(any(r["service"] == "A" and r["amount"] == 50.0 for r in inc))
        self.assertTrue(any(r["service"] == "B" and r["amount"] == 20.0 for r in dec))


class AzureRgTests(unittest.TestCase):
    def test_workload_categories(self):
        from src.report.azure_rg import workload_category

        cases = {
            # Ecommerce
            "rg-app-dv-eu-01": "Ecommerce",
            "rg-app-st-eu-01": "Ecommerce",
            "rg-app-pd-eu-01": "Ecommerce",
            "rg-app-ut-eu-01": "Ecommerce",
            "MC_rg-app-dv-eu-01_aks-demo-dv-eu-01_eastus": "Ecommerce",
            "MC_rg-app-dv-eu-01_aks-demo-vit-dv-eu-01_eastus": "Ecommerce",
            "MC_rg-app-st-eu-01_aks-demo-st-eu-01_eastus": "Ecommerce",
            "MC_rg-app-st-eu-01_aks-demo-vit-st-eu-01_eastus": "Ecommerce",
            "MC_rg-app-pd-eu-01_aks-demo-pd-eu-01_eastus": "Ecommerce",
            "MC_rg-app-pd-eu-01_aks-demo-vit-pd-eu-01_eastus": "Ecommerce",
            "ME_cae-ecommerce-st-eu-01_rg-app-st-eu-01_eastus": "Ecommerce",
            "ME_cae-ecommerce-pd-eu-01_rg-app-pd-eu-01_eastus": "Ecommerce",
            # Shared
            "rg-shared-dv-cu-01": "Shared",
            "rg-shared-dv-eu-01": "Shared",
            "rg-shared-dv-eu-01-managed": "Shared",
            "rg-shared-st-cu-01": "Shared",
            "rg-shared-pd-eu-01-managed": "Shared",
            "rg-shared-ut-sc-01": "Shared",
            # Lab
            "rg-lab-dv-eu-01": "Lab",
            "rg-lab-st-eu-01": "Lab",
            "rg-lab-pd-eu-01": "Lab",
            "rg-lab-sb-eu-01": "Lab",
            "ME_cae-lab-dv-eu-01_rg-lab-dv-eu-01_eastus": "Lab",
            # AI (incl. MA_mw-ai and MC_rg-ai AKS node pools)
            "MA_mw-ai-st-eu-01_eastus_managed": "AI",
            "MA_mw-ai-pd-eu-01_eastus_managed": "AI",
            "MC_rg-ai-st-eu-01_aks-ai-st-eu-01_eastus": "AI",
            "MC_rg-ai-pd-eu-01_aks-ai-pd-eu-01_eastus": "AI",
            "rg-ai-st-eu-01": "AI",
            "rg-ai-pd-eu-01": "AI",
            # Data
            "rg-data-dv-eu-01": "Data",
            "rg-data-pd-eu-01": "Data",
            # Others
            "dashboards": "Others",
            "rg-dns-np-eu-01": "Others",
            "fabric-mvp-airflow": "Others",
        }
        for rg, expected in cases.items():
            with self.subTest(rg=rg):
                self.assertEqual(workload_category(rg), expected)

    def test_rg_matches_product_by_category(self):
        from src.report.azure_rg import rg_matches_product

        self.assertTrue(rg_matches_product("rg-app-pd-eu-01", "Ecommerce"))
        self.assertFalse(rg_matches_product("rg-app-pd-eu-01", "AI"))
        self.assertTrue(rg_matches_product("MC_rg-ai-st-eu-01_aks-ai-st-eu-01_eastus", "AI"))
        self.assertTrue(rg_matches_product("MA_mw-ai-pd-eu-01_eastus_managed", "AI"))

    def test_rg_matches_env_tier(self):
        from src.report.azure_rg import rg_matches_env_tier

        self.assertTrue(rg_matches_env_tier("rg-app-pd-eu-01", "pd"))
        self.assertTrue(rg_matches_env_tier("rg-app-dv-eu-01", "dv"))
        self.assertTrue(rg_matches_env_tier("rg-app-st-eu-01", "st"))
        self.assertTrue(rg_matches_env_tier("rg-app-ut-eu-01", "ut"))
        self.assertFalse(rg_matches_env_tier("rg-app-pd-eu-01", "st"))
        self.assertTrue(rg_matches_env_tier("MC_rg-app-pd-eu-01_aks-demo-pd-eu-01_eastus", "pd"))

    def test_filter_records_by_env_tier(self):
        from src.report.filters import filter_records

        records = [
            CostRecord(cloud="Azure", service="VM", cost=100.0, scope="rg-app-pd-eu-01"),
            CostRecord(cloud="Azure", service="VM", cost=50.0, scope="rg-app-st-eu-01"),
            CostRecord(cloud="AWS", service="EC2", cost=30.0, environment="production"),
        ]
        pd_only = filter_records(records, env_tier="pd")
        self.assertEqual(len(pd_only), 2)
        self.assertEqual(sum(r.cost for r in pd_only), 130.0)
        st_only = filter_records(records, env_tier="st")
        self.assertEqual(len(st_only), 1)
        self.assertEqual(st_only[0].scope, "rg-app-st-eu-01")

    def test_nonproduction_tier_filter(self):
        self.assertTrue(rg_matches_subscription_tier("rg-app-st-eu-01", "NonProduction"))
        self.assertFalse(rg_matches_subscription_tier("rg-app-pd-eu-01", "NonProduction"))

    def test_production_tier_filter(self):
        self.assertTrue(rg_matches_subscription_tier("rg-app-pd-eu-01", "Production"))
        self.assertFalse(rg_matches_subscription_tier("rg-app-dv-eu-01", "Production"))


class MonthValidationTests(unittest.TestCase):
    def test_rejects_current_and_future_months(self):
        from datetime import date
        from src.months import latest_allowed_month_key, validate_month_key

        today = date(2026, 6, 26)
        self.assertEqual(latest_allowed_month_key(today), "2026-05")
        self.assertEqual(validate_month_key("2026-05", today), "2026-05")
        with self.assertRaises(ValueError):
            validate_month_key("2026-06", today)
        with self.assertRaises(ValueError):
            validate_month_key("2027-01", today)


class AwsRecordTypeFilterTests(unittest.TestCase):
    def test_exclude_tax_and_credit(self):
        from src.collectors.aws import _SERVICE_RECORD_TYPES_EXCLUDE, _record_type_filter

        self.assertEqual(_SERVICE_RECORD_TYPES_EXCLUDE, ("Tax", "Credit"))
        filt = _record_type_filter(exclude_record_types=list(_SERVICE_RECORD_TYPES_EXCLUDE))
        self.assertEqual(
            filt,
            {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Tax", "Credit"]}}},
        )

    def test_include_tax_only(self):
        from src.collectors.aws import _record_type_filter

        filt = _record_type_filter(include_record_types=["Tax"])
        self.assertEqual(
            filt,
            {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Tax"]}},
        )


class BillingTotalsTests(unittest.TestCase):
    def test_aws_breakdown_matches_summary(self):
        from src.aggregator import build_report
        from src.report.groups import aws_breakdown

        records = [
            CostRecord(cloud="AWS", service="Route 53", cost=15.0),
            CostRecord(cloud="AWS", service="EC2", cost=100.0),
            CostRecord(cloud="AWS", service="EC2", cost=0.0, credits=10.0),
            CostRecord(cloud="AWS", service="Tax", cost=0.0, tax=5.0),
        ]
        report = build_report(records, month="May", year=2026, currency="USD")
        aws = aws_breakdown(records)
        summary = next(s for s in report.summaries if s.cloud == "AWS")
        self.assertEqual(aws["subtotal"], 115.0)
        self.assertEqual(aws["credits"], 10.0)
        self.assertEqual(aws["tax"], 5.0)
        self.assertEqual(aws["total"], summary.total)
        self.assertEqual(report.amount_due, 110.0)

    def test_azure_charge_credit_split(self):
        from src.billing import aggregate_charge_credit_rows

        rows = aggregate_charge_credit_rows([
            ("Virtual Machines", "rg-a", 100.0),
            ("Virtual Machines", "rg-a", -20.0),
            ("Storage", "rg-b", -5.0),
        ])
        self.assertEqual(rows, [
            ("Storage", "rg-b", 0.0, 5.0),
            ("Virtual Machines", "rg-a", 100.0, 20.0),
        ])
        records = [
            CostRecord(cloud="Azure", service=s, cost=c, credits=cr, scope=rg)
            for s, rg, c, cr in rows
        ]
        from src.aggregator import build_report
        report = build_report(records, month="May", year=2026, currency="USD")
        azure = next(s for s in report.summaries if s.cloud == "Azure")
        self.assertEqual(azure.subtotal, 100.0)
        self.assertEqual(azure.credits, 25.0)
        self.assertEqual(azure.total, 75.0)

    def test_credits_projection_hidden_without_config(self):
        from src.analytics import credits_configured, credits_projection

        self.assertFalse(credits_configured({"credits": {"enabled": False}}))
        self.assertFalse(credits_configured({"credits": {"enabled": True, "current_balance": 0}}))
        self.assertIsNone(credits_projection({"credits": {"enabled": False}}, [{"subtotal": 100, "grand_total": 90}]))

    def test_azure_invoice_overlay_matches_bill(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from src.aggregator import build_report
        from src.collectors.azure_invoice import apply_azure_invoice_overlay

        with tempfile.TemporaryDirectory() as d, \
                mock.patch("src.azure_catalog.load_invoices_db", return_value={}):
            csv_path = Path(d) / "azure_invoices.csv"
            # gross from API differs slightly (100000) from invoiced charges.
            csv_path.write_text(
                "month,charges,other_credits,azure_credit,subtotal,tax,total\n"
                "2026-05,100469.94,0,97403.70,3066.24,315.83,3382.07\n"
            )
            cfg = {"azure": {"invoices_csv": str(csv_path)}}
            records = [
                CostRecord(cloud="Azure", service="Compute", cost=60000.0, scope="rg-a"),
                CostRecord(cloud="Azure", service="Storage", cost=40000.0, scope="rg-b"),
            ]
            overlaid = apply_azure_invoice_overlay(records, cfg, "2026-05")
            report = build_report(overlaid, month="May", year=2026, currency="USD")
            azure = next(s for s in report.summaries if s.cloud == "Azure")
            # Charges normalized to the invoice; credit & tax applied; due == invoice total.
            self.assertEqual(azure.subtotal, 100469.94)
            self.assertEqual(azure.credits, 97403.70)
            self.assertEqual(azure.tax, 315.83)
            self.assertEqual(azure.total, 3382.07)

    def test_azure_invoice_overlay_noop_without_invoice(self):
        from unittest import mock

        from src.collectors.azure_invoice import apply_azure_invoice_overlay

        cfg = {"azure": {"invoices_csv": "/nonexistent/azure_invoices.csv"}}
        records = [CostRecord(cloud="Azure", service="Compute", cost=100.0)]
        with mock.patch("src.azure_catalog.load_invoices_db", return_value={}):
            # No DB row and no CSV file -> unchanged.
            self.assertEqual(apply_azure_invoice_overlay(records, cfg, "2026-05"), records)

    def test_azure_billing_invoice_mapping(self):
        from datetime import date
        from types import SimpleNamespace

        from src.collectors.azure_billing import _invoice_month, _normalize

        class Amount(SimpleNamespace):
            pass

        inv = SimpleNamespace(
            name="G162265600",
            invoice_period_start_date=date(2026, 5, 1),
            azure_prepayment_applied=Amount(value=97403.70),
            free_azure_credit_applied=Amount(value=0.0),
            credit_amount=Amount(value=0.0),
            sub_total=Amount(value=3066.24),
            tax_amount=Amount(value=315.83),
            total_amount=Amount(value=3382.07),
        )
        self.assertEqual(_invoice_month(inv), "2026-05")
        summary = _normalize(inv)
        self.assertEqual(summary["invoice_number"], "G162265600")
        self.assertEqual(summary["azure_credit"], 97403.70)
        self.assertEqual(summary["tax"], 315.83)
        self.assertEqual(summary["total"], 3382.07)
        # Charges reconstructed = subtotal + azure_credit + other_credits.
        self.assertEqual(summary["charges"], 100469.94)

    def test_azure_billing_negative_credit_amount_is_positive_magnitude(self):
        """credit_amount is reported negative; it must become a positive credit so
        gross == subtotal + azure_credit + other_credits and net stays correct."""
        from datetime import date
        from types import SimpleNamespace

        from src.collectors.azure_billing import _normalize

        class Amount(SimpleNamespace):
            pass

        # Mirrors the real March monthly invoice G150116279.
        inv = SimpleNamespace(
            name="G150116279",
            invoice_period_start_date=date(2026, 3, 1),
            azure_prepayment_applied=Amount(value=129597.89),
            free_azure_credit_applied=Amount(value=0.0),
            credit_amount=Amount(value=-5586.63),
            sub_total=Amount(value=3731.03),
            tax_amount=Amount(value=384.29),
            total_amount=Amount(value=4115.32),
        )
        summary = _normalize(inv)
        self.assertEqual(summary["other_credits"], 5586.63)
        self.assertEqual(summary["azure_credit"], 129597.89)
        # gross == real billed_amount (138915.55)
        self.assertEqual(summary["charges"], 138915.55)
        net = round(
            summary["charges"] - summary["azure_credit"]
            - summary["other_credits"] + summary["tax"],
            2,
        )
        self.assertEqual(net, summary["total"])

    def test_azure_billing_fetch_without_account(self):
        from src.collectors.azure_billing import fetch_invoice

        self.assertIsNone(fetch_invoice({"azure": {}}, "2026-05"))

    def test_credits_projection_uses_credit_drawdown(self):
        from src.analytics import credits_projection

        cfg = {"credits": {"enabled": True, "current_balance": 300.0, "burn_window_months": 3}}
        points = [
            {"month": "2026-03", "subtotal": 1000.0, "grand_total": 50.0, "credits": 90.0},
            {"month": "2026-04", "subtotal": 1000.0, "grand_total": 50.0, "credits": 100.0},
            {"month": "2026-05", "subtotal": 1000.0, "grand_total": 50.0, "credits": 110.0},
        ]
        cp = credits_projection(cfg, points)
        # Uses applied credits (avg 100), not gross subtotal (1000).
        self.assertEqual(cp["this_month_usage"], 110.0)
        self.assertEqual(cp["avg_monthly_burn"], 100.0)
        self.assertEqual(cp["months_remaining"], 3.0)

    def test_product_groups_include_negative_rg_costs(self):
        from src.report.azure_rg import group_rgs_by_product

        groups = group_rgs_by_product(
            [("rg-app-pd-eu-01", 100.0), ("rg-app-pd-eu-02", -5.0)],
            is_production=True,
        )
        self.assertEqual(groups[0].production, 95.0)
        self.assertEqual(len(groups[0].production_rgs), 2)

    def test_product_groups_sorted_by_spend_desc(self):
        from src.report.azure_rg import group_rgs_by_product

        groups = group_rgs_by_product(
            [
                ("rg-shared-pd-eu-01", 50.0),
                ("rg-app-pd-eu-01", 300.0),
                ("rg-ai-pd-eu-01", 120.0),
            ],
            is_production=True,
        )
        totals = [g.total for g in groups]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertEqual(groups[0].product, "Ecommerce")


class GcpCollectorTests(unittest.TestCase):
    _CSV = (
        "Invoice number,123,\n"
        "Billing account ID,000000-000000-000000,\n"
        "Currency,USD,\n"
        "Total amount due,$95.00,\n"
        "Billing account name,Billing account ID,Project name,Project ID,Project hierarchy,"
        "Service description,Service ID,SKU description,SKU ID,Consumption model description,"
        "Credit type,Cost type,Usage start date,Usage end date,Usage amount,Usage unit,"
        "Unrounded Cost ($),Cost ($)\n"
        # prod project charges
        "Example Billing Account,000000-000000-000000,Example - PROD,billing-prod,example.com,"
        "BigQuery,S1,Analysis,K1,Default,,Usage,2026-01-01,2026-01-31,\"1,000\",count,60.00,60.00\n"
        "Example Billing Account,000000-000000-000000,Example - PROD,billing-prod,example.com,"
        "Cloud Storage,S2,Standard,K2,Default,,Usage,2026-01-01,2026-01-31,10,gibibyte,40.00,40.00\n"
        # dev project charge + a spending-based discount (credit)
        "Example Billing Account,000000-000000-000000,ml-dev,ml-dev,example.com,"
        "Cloud Run,S3,CPU,K3,Default,,Usage,2026-01-01,2026-01-31,5,hour,10.00,10.00\n"
        "Example Billing Account,000000-000000-000000,ml-dev,ml-dev,example.com,"
        "Cloud Run,S3,CPU,K3,,SPENDING_BASED_DISCOUNT,Usage,2026-01-01,2026-01-31,,,-15.00,-15.00\n"
        # footer rows (must be ignored)
        ",,,,,,,,,,,Rounding error,,,,,-0.00,-0.00\n"
        ",,,,,,,,,,,Total,,,,,95.00,95.00\n"
    )

    def _write_csv(self, d: str) -> None:
        from pathlib import Path

        name = "Example Billing Account_Cost table, 2026-01-01 — 2026-01-31.csv"
        Path(d, name).write_text(self._CSV, encoding="utf-8")

    def test_csv_parse_charges_credits_and_env(self):
        import tempfile
        from datetime import date

        from src.collectors.gcp import collect_gcp

        with tempfile.TemporaryDirectory() as d:
            self._write_csv(d)
            cfg = {"gcp": {"source": "csv", "csv_dir": d, "default_environment": "production",
                           "project_patterns": [
                               {"pattern": "(^|[-_])dev([-_]|$)", "environment": "non-production"}]}}
            recs = collect_gcp(cfg, date(2026, 1, 1), date(2026, 2, 1))

        charges = round(sum(r.cost for r in recs), 2)
        credits = round(sum(r.credits for r in recs), 2)
        self.assertEqual(charges, 110.0)          # 60 + 40 + 10
        self.assertEqual(credits, 15.0)           # spending-based discount
        self.assertEqual(round(charges - credits, 2), 95.0)  # == invoice total
        self.assertTrue(all(r.cloud == "GCP" for r in recs))
        prod = {r.scope for r in recs if r.environment == "production"}
        nonprod = {r.scope for r in recs if r.environment == "non-production"}
        self.assertIn("Example - PROD", prod)
        self.assertIn("ml-dev", nonprod)

    def test_csv_missing_month_returns_empty(self):
        import tempfile
        from datetime import date

        from src.collectors.gcp import collect_gcp

        with tempfile.TemporaryDirectory() as d:
            self._write_csv(d)
            cfg = {"gcp": {"source": "csv", "csv_dir": d}}
            self.assertEqual(collect_gcp(cfg, date(2026, 3, 1), date(2026, 4, 1)), [])


class GcpGroupingTests(unittest.TestCase):
    def _records(self):
        return [
            CostRecord(cloud="GCP", service="BigQuery", cost=60.0, environment="production",
                       scope="Example - PROD"),
            CostRecord(cloud="GCP", service="Cloud Run", cost=10.0, credits=15.0,
                       environment="non-production", scope="ml-dev"),
            CostRecord(cloud="GCP", service="Cloud Storage", cost=40.0, environment="production",
                       scope="Example - PROD"),
        ]

    def test_gcp_breakdown_totals(self):
        from src.report.groups import gcp_breakdown

        g = gcp_breakdown(self._records())
        self.assertEqual(g["subtotal"], 110.0)
        self.assertEqual(g["credits"], 15.0)
        self.assertEqual(g["total"], 95.0)

    def test_gcp_by_project_matrix(self):
        from src.report.groups import gcp_by_project

        g = gcp_by_project(self._records())
        self.assertEqual(g["project_names"], ["Example - PROD", "ml-dev"])
        self.assertEqual(g["totals"]["Example - PROD"], 100.0)
        self.assertEqual(g["totals"]["ml-dev"], -5.0)  # 10 charge - 15 credit
        self.assertEqual(g["total"], 95.0)

    def test_gcp_included_in_summary_order(self):
        from src.aggregator import build_report

        records = self._records() + [CostRecord(cloud="AWS", service="EC2", cost=5.0)]
        report = build_report(records, month="January", year=2026, currency="USD")
        clouds = [s.cloud for s in report.summaries]
        self.assertEqual(clouds, ["AWS", "GCP"])  # AWS(0) before GCP(2)
        gcp = next(s for s in report.summaries if s.cloud == "GCP")
        self.assertEqual(gcp.total, 95.0)


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        self.client.cookies.clear()

    def _login(self):
        return self.client.post(
            "/login",
            data={"username": "admin", "password": "admin"},
            follow_redirects=False,
        )

    def test_healthz_public(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_unauthenticated_redirect(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (303, 307))
        self.assertIn("/login", r.headers.get("location", ""))

    def test_unauthenticated_api_401(self):
        r = self.client.get("/api/reports")
        self.assertEqual(r.status_code, 401)

    def test_login_logout_flow(self):
        r = self._login()
        self.assertEqual(r.status_code, 303)
        r = self.client.get("/generate")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"FinOps", r.content)
        r = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_reports_list_and_detail(self):
        self._login()
        r = self.client.get("/api/reports")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("months", data)
        for mk in data["months"]:
            r = self.client.get(f"/reports/{mk}")
            self.assertEqual(r.status_code, 200, mk)
            self.assertIn(b"Executive Summary", r.content)
            self.assertIn(b"monthSearch", r.content)
            r = self.client.get(f"/reports/{mk}/pdf")
            self.assertEqual(r.status_code, 200, mk)
            self.assertTrue(r.headers["content-type"].startswith("application/pdf"))

    def test_chart_and_trend_apis(self):
        self._login()
        r = self.client.get("/api/reports")
        months = r.json().get("months", [])
        if not months:
            self.skipTest("no stored reports")
        mk = months[0]
        r = self.client.get(f"/api/reports/{mk}/chart?dimension=service")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("series", body)
        self.assertIn("total", body)
        r = self.client.get("/api/trend/filtered")
        self.assertEqual(r.status_code, 200)
        self.assertIn("trend", r.json())

    def test_chart_filters(self):
        self._login()
        r = self.client.get("/api/reports")
        months = r.json().get("months", [])
        if not months:
            self.skipTest("no stored reports")
        mk = months[0]
        r = self.client.get(f"/api/reports/{mk}/chart?dimension=service&cloud=Azure")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/trend/filtered?cloud=Azure&product_group=ecommerce")
        self.assertEqual(r.status_code, 200)

    def test_report_not_found(self):
        self._login()
        r = self.client.get("/reports/1900-01")
        self.assertEqual(r.status_code, 404)

    def test_build_report_from_records(self):
        from src.aggregator import build_report

        records = [
            CostRecord(cloud="AWS", service="Elastic Compute Cloud", cost=120.0,
                       environment="production"),
            CostRecord(cloud="AWS", service="Tax", cost=0.0, tax=9.5,
                       environment="production"),
            CostRecord(cloud="Azure", service="Storage", cost=300.0,
                       environment="production", scope="rg-app-pd-01"),
            CostRecord(cloud="Azure", service="Storage", cost=-25.0, credits=25.0,
                       environment="production", scope="rg-app-pd-01"),
        ]
        report = build_report(records, month="January", year=2026, currency="USD")
        self.assertGreater(report.grand_total, 0)
        self.assertEqual({s.cloud for s in report.summaries}, {"AWS", "Azure"})


class AuthRolesTests(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        from src.web.users import hash_password, verify_password

        h = hash_password("s3cret-pw")
        self.assertTrue(verify_password("s3cret-pw", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertFalse(verify_password("s3cret-pw", "not-a-hash"))

    def test_verify_login_bootstrap_admin(self):
        from src.web.auth import verify_login

        self.assertEqual(verify_login("admin", "admin"), "admin")
        self.assertIsNone(verify_login("admin", "wrong-pw"))

    def test_admin_can_open_admin_page(self):
        client = TestClient(app)
        r = client.post(
            "/login", data={"username": "admin", "password": "admin"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        resp = client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Users", resp.content)
        self.assertIn(b"Azure invoices", resp.content)
        client.cookies.clear()

    def test_viewer_blocked_from_admin_and_generate(self):
        from src.web import users as users_mod

        users_mod.add_user(None, "viewer_test", "pw123456", "viewer")
        try:
            client = TestClient(app)
            r = client.post(
                "/login", data={"username": "viewer_test", "password": "pw123456"},
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertEqual(client.get("/admin").status_code, 403)
            self.assertEqual(
                client.post(
                    "/generate", data={"month": "2026-05"}, follow_redirects=False
                ).status_code,
                403,
            )
            # Viewers can still view reports and download PDFs.
            months = client.get("/api/reports").json().get("months", [])
            if months:
                self.assertEqual(client.get(f"/reports/{months[0]}/pdf").status_code, 200)
            client.cookies.clear()
        finally:
            users_mod.delete_user(None, "viewer_test")


class KeyVaultTests(unittest.TestCase):
    def test_secret_name_to_env(self):
        from src.keyvault import secret_name_to_env

        self.assertEqual(secret_name_to_env("database-url"), "DATABASE_URL")
        self.assertEqual(
            secret_name_to_env("acs-connection-string"), "ACS_CONNECTION_STRING"
        )
        self.assertEqual(secret_name_to_env("app-config"), "APP_CONFIG")
        self.assertEqual(
            secret_name_to_env("FINOPS-DATABASE-URL", "FINOPS-"), "DATABASE_URL"
        )

    def test_prod_session_secret_required(self):
        from src.web.auth import session_secret

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"SESSION_SECRET", "BASIC_AUTH_PASSWORD"}
        }
        env["APP_ENV"] = "production"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                session_secret()

    def test_dev_session_secret_falls_back(self):
        from src.web.auth import session_secret

        env = {k: v for k, v in os.environ.items() if k != "SESSION_SECRET"}
        env["APP_ENV"] = "development"
        env["BASIC_AUTH_PASSWORD"] = "dev-pass"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(session_secret(), "dev-pass")

    def test_vault_url_from_name_and_url(self):
        from src import keyvault

        saved = {k: os.environ.get(k) for k in ("AZURE_KEY_VAULT_URL", "AZURE_KEY_VAULT_NAME")}
        try:
            os.environ.pop("AZURE_KEY_VAULT_URL", None)
            os.environ["AZURE_KEY_VAULT_NAME"] = "finops-kv"
            self.assertEqual(keyvault.vault_url(), "https://finops-kv.vault.azure.net/")
            os.environ["AZURE_KEY_VAULT_URL"] = "https://x.vault.azure.net"
            self.assertEqual(keyvault.vault_url(), "https://x.vault.azure.net/")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_load_noop_without_config(self):
        from src import keyvault

        saved = {k: os.environ.get(k) for k in ("AZURE_KEY_VAULT_URL", "AZURE_KEY_VAULT_NAME")}
        try:
            os.environ.pop("AZURE_KEY_VAULT_URL", None)
            os.environ.pop("AZURE_KEY_VAULT_NAME", None)
            self.assertFalse(keyvault.load_secrets_into_env(force=True))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class EmailDeliveryTests(unittest.TestCase):
    def test_email_disabled_skips_send(self):
        from src.delivery import email_sender

        cfg = {"email": {"enabled": False, "sender": "x@y.com", "recipients": ["a@b.com"]}}
        with mock.patch.dict(os.environ, {"EMAIL_ENABLED": "false"}), \
                mock.patch.object(email_sender, "_build_client") as build:
            email_sender.send_email(cfg, b"%PDF-", "r.pdf", "January", 2026)
            build.assert_not_called()

    def test_email_sends_via_acs(self):
        import base64

        from src.delivery import email_sender

        cfg = {
            "email": {
                "enabled": True,
                "sender": "noreply@example.com",
                "recipients": ["finops@example.com", "ops@example.com"],
            }
        }

        captured = {}

        class _Poller:
            def result(self):
                return {"id": "msg-123", "status": "Succeeded"}

        class _Client:
            def begin_send(self, message):
                captured["message"] = message
                return _Poller()

        increases = [
            {"cloud": "Azure", "resource": "rg-ecom-pd-01", "service": "Virtual Machines",
             "environment": "Production (pd)", "amount": 1234.5},
        ]
        decreases = [
            {"cloud": "AWS", "resource": "111122223333", "service": "EC2",
             "environment": "Non-Production", "amount": 50.0},
        ]
        with mock.patch.dict(os.environ, {"EMAIL_ENABLED": "true"}), \
                mock.patch.object(email_sender, "_build_client", return_value=_Client()):
            email_sender.send_email(
                cfg, b"%PDF-bytes", "report.pdf", "January", 2026,
                increases=increases, decreases=decreases, currency="USD",
            )

        msg = captured["message"]
        self.assertEqual(msg["senderAddress"], "noreply@example.com")
        self.assertEqual(
            [r["address"] for r in msg["recipients"]["to"]],
            ["finops@example.com", "ops@example.com"],
        )
        att = msg["attachments"][0]
        self.assertEqual(att["name"], "report.pdf")
        self.assertEqual(att["contentType"], "application/pdf")
        self.assertEqual(base64.b64decode(att["contentInBase64"]), b"%PDF-bytes")
        self.assertIn("January 2026", msg["content"]["subject"])
        # Cost-change summary: increases + decreases with service and environment.
        text = msg["content"]["plainText"]
        self.assertIn("Increases", text)
        self.assertIn("Decreases", text)
        self.assertIn("Virtual Machines", text)
        self.assertIn("Production (pd)", text)
        self.assertIn("EC2", text)
        self.assertIn("Non-Production", text)
        self.assertIn("Environment", msg["content"]["html"])
        self.assertIn("Production (pd)", msg["content"]["html"])

    def test_email_no_recipients_skips(self):
        from src.delivery import email_sender

        cfg = {"email": {"enabled": True, "sender": "x@y.com", "recipients": []}}
        with mock.patch.dict(os.environ, {"EMAIL_ENABLED": "true"}), \
                mock.patch.object(email_sender, "_build_client") as build:
            email_sender.send_email(cfg, b"%PDF-", "r.pdf", "January", 2026)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
