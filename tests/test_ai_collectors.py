"""Tests for the LLM / AI cost sources (Cursor, Anthropic) and the AI grouping.

Network is fully mocked — no real Cursor / Anthropic API calls are made.
"""
from __future__ import annotations

import logging
import os
import unittest
from datetime import date
from unittest import mock

from src.models import CostRecord


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Returns queued payloads for successive get/post calls."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.auth = None
        self.calls: list[dict] = []

    def _next(self, **kwargs):
        payload = self._payloads.pop(0)   # raises IndexError when exhausted
        self.calls.append(kwargs)
        return _FakeResp(payload)

    def post(self, *args, **kwargs):
        return self._next(**kwargs)

    def get(self, *args, **kwargs):
        return self._next(**kwargs)


class CursorCollectorTests(unittest.TestCase):
    START, END = date(2026, 6, 1), date(2026, 7, 1)

    def setUp(self):
        # Neutralize any real key in the environment so cfg-provided keys win.
        self._env = mock.patch.dict(os.environ, {"CURSOR_API_KEY": ""}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_no_key_skips(self):
        from src.collectors.cursor import collect_cursor

        with mock.patch.dict(os.environ, {"CURSOR_API_KEY": ""}, clear=False):
            self.assertEqual(collect_cursor({}, self.START, self.END), [])

    def test_aggregates_per_model_user_tokens_across_pages(self):
        from src.collectors import cursor

        pages = [
            {
                "usageEvents": [
                    {"model": "claude-4.5-sonnet", "chargedCents": 2136.232,
                     "userEmail": "alex@example.com",
                     "tokenUsage": {"inputTokens": 100, "outputTokens": 50,
                                    "cacheWriteTokens": 10, "cacheReadTokens": 40}},
                    {"model": "gpt-5", "chargedCents": 100.0, "userEmail": "sam@example.com"},
                ],
                "pagination": {"hasNextPage": True},
            },
            {
                "usageEvents": [
                    {"model": "claude-4.5-sonnet", "chargedCents": 863.768,
                     "userEmail": "alex@example.com",
                     "tokenUsage": {"inputTokens": 200, "outputTokens": 0}},
                    # events without a charge and no tokens are ignored
                    {"model": "gpt-5", "chargedCents": None, "userEmail": "sam@example.com"},
                ],
                "pagination": {"hasNextPage": False},
            },
        ]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test"}}
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        by_model = {}
        for r in recs:
            by_model[r.service] = by_model.get(r.service, 0.0) + r.cost
        self.assertEqual(round(by_model["claude-4.5-sonnet"], 2), 30.0)   # (2136.232 + 863.768)/100
        self.assertEqual(round(by_model["gpt-5"], 2), 1.0)
        self.assertTrue(all(r.cloud == "Cursor" for r in recs))

        # Per-user attribution + tokens are captured.
        sonnet = next(r for r in recs if r.service == "claude-4.5-sonnet")
        self.assertEqual(sonnet.user, "alex@example.com")
        self.assertEqual(sonnet.tokens, 400)   # 100+50+10+40 + 200+0
        self.assertEqual({r.user for r in recs}, {"alex@example.com", "sam@example.com"})

        self.assertEqual(len(fake.calls), 2)          # two pages fetched
        self.assertEqual(fake.auth, ("key_test", ""))  # HTTP Basic: key as user

    def test_seat_lines_from_live_member_count_plus_bugbot(self):
        from src.collectors import cursor

        pages = [
            # usage page (empty), then the /teams/members roster.
            {"usageEvents": [], "pagination": {"hasNextPage": False}},
            {"teamMembers": [
                {"role": "owner"}, {"role": "member"}, {"role": "member"},
                {"role": "removed"},   # excluded — not a billable seat
            ]},
        ]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test", "seats": {
            "enabled": True, "seat_price": 40.0, "tax_rate": 10.3, "bugbot_seats": 8,
        }}}
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        seat_recs = {r.service: r for r in recs if r.usage_unit == "seats"}
        # 3 active members (owner + 2 members); 'removed' excluded.
        teams = seat_recs["Cursor Teams (3 seats)"]
        self.assertEqual(teams.cost, 120.0)          # 3 x $40
        self.assertEqual(teams.tax, 12.36)           # 10.3% of 120
        self.assertEqual(teams.tokens, 3)
        bugbot = seat_recs["Bugbot (8 seats)"]
        self.assertEqual(bugbot.cost, 320.0)         # 8 x $40
        self.assertEqual(bugbot.tax, 32.96)          # 10.3% of 320
        self.assertTrue(all(r.cloud == "Cursor" for r in seat_recs.values()))

    def test_billed_mode_live_factor_when_report_is_current_cycle(self):
        from datetime import datetime, timezone
        from src.collectors import cursor

        # Live fallback only applies when the report cycle == Cursor's current
        # cycle, so the mocked cycle start must match the report window start.
        cycle_start_ms = int(datetime(self.START.year, self.START.month, self.START.day,
                                      tzinfo=timezone.utc).timestamp() * 1000)
        pages = [
            # 1) report-window usage: list value $40 (claude $30 + gpt $10)
            {"usageEvents": [
                {"model": "claude", "chargedCents": 3000, "userEmail": "a@example.com"},
                {"model": "gpt", "chargedCents": 1000, "userEmail": "b@example.com"},
            ], "pagination": {"hasNextPage": False}},
            # 2) /teams/spend: billed $20 for the current cycle
            {"subscriptionCycleStart": cycle_start_ms, "totalPages": 1,
             "teamMemberSpend": [{"spendCents": 2000}]},
            # 3) current-cycle usage window: list value $40 -> factor = 20/40 = 0.5
            {"usageEvents": [{"chargedCents": 4000}], "pagination": {"hasNextPage": False}},
        ]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test", "billing_mode": "billed"}}
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        by_model = {r.service: r.cost for r in recs}
        self.assertEqual(by_model["claude"], 15.0)   # 30 × 0.5
        self.assertEqual(by_model["gpt"], 5.0)       # 10 × 0.5

    def test_billed_mode_uses_stored_snapshot(self):
        from src.collectors import cursor
        from src import store as store_mod

        # report usage: list value $40 (claude $30 + gpt $10)
        pages = [{"usageEvents": [
            {"model": "claude", "chargedCents": 3000, "userEmail": "a@example.com"},
            {"model": "gpt", "chargedCents": 1000, "userEmail": "b@example.com"},
        ], "pagination": {"hasNextPage": False}}]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test", "billing_mode": "billed"}}

        fake_store = mock.Mock()
        fake_store.get_snapshot.return_value = {"billed_cents": 2000}  # $20 billed
        with mock.patch.object(cursor.requests, "Session", return_value=fake), \
             mock.patch.object(store_mod, "CursorSpendStore", return_value=fake_store):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        by_model = {r.service: r.cost for r in recs}
        self.assertEqual(by_model["claude"], 15.0)   # 30 × (20/40)
        self.assertEqual(by_model["gpt"], 5.0)
        self.assertEqual(len(fake.calls), 1)          # snapshot used -> no /teams/spend call

    def test_billed_mode_past_cycle_without_snapshot_uses_list_value(self):
        from src.collectors import cursor
        from src import store as store_mod

        pages = [
            {"usageEvents": [
                {"model": "claude", "chargedCents": 3000, "userEmail": "a@example.com"},
            ], "pagination": {"hasNextPage": False}},
            # current cycle is a DIFFERENT (later) cycle than the report window
            {"subscriptionCycleStart": 9_000_000_000_000, "totalPages": 1,
             "teamMemberSpend": [{"spendCents": 2000}]},
            {"usageEvents": [{"chargedCents": 4000}], "pagination": {"hasNextPage": False}},
        ]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test", "billing_mode": "billed"}}
        fake_store = mock.Mock()
        fake_store.get_snapshot.return_value = None
        with mock.patch.object(cursor.requests, "Session", return_value=fake), \
             mock.patch.object(store_mod, "CursorSpendStore", return_value=fake_store):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        # No snapshot and not the current cycle -> unscaled list value.
        self.assertEqual({r.service: r.cost for r in recs}["claude"], 30.0)

    def test_list_mode_is_default_and_unscaled(self):
        from src.collectors import cursor

        pages = [{"usageEvents": [{"model": "claude", "chargedCents": 3000, "userEmail": "a@example.com"}],
                  "pagination": {"hasNextPage": False}}]
        fake = _FakeSession(pages)
        cfg = {"cursor": {"api_key": "key_test"}}   # no billing_mode -> list
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        self.assertEqual({r.service: r.cost for r in recs}["claude"], 30.0)
        self.assertEqual(len(fake.calls), 1)   # no /teams/spend call

    def test_seat_lines_skipped_when_disabled(self):
        from src.collectors import cursor

        fake = _FakeSession([{"usageEvents": [], "pagination": {"hasNextPage": False}}])
        cfg = {"cursor": {"api_key": "key_test"}}   # no seats block
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            recs = cursor.collect_cursor(cfg, self.START, self.END)

        self.assertEqual([r for r in recs if r.usage_unit == "seats"], [])
        self.assertEqual(len(fake.calls), 1)   # members endpoint not hit

    def test_month_window_bounds_are_inclusive_epoch_ms(self):
        from src.collectors import cursor

        fake = _FakeSession([{"usageEvents": [], "pagination": {"hasNextPage": False}}])
        cfg = {"cursor": {"api_key": "key_test"}}
        with mock.patch.object(cursor.requests, "Session", return_value=fake):
            cursor.collect_cursor(cfg, self.START, self.END)

        from datetime import datetime, timezone

        def ms(y, m, d):
            return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)

        payload = fake.calls[0]["json"]
        # start = 2026-06-01T00:00:00Z, end (exclusive) = 2026-07-01 -> endDate is last ms.
        self.assertEqual(payload["startDate"], ms(2026, 6, 1))
        self.assertEqual(payload["endDate"], ms(2026, 7, 1) - 1)


class AnthropicCollectorTests(unittest.TestCase):
    START, END = date(2026, 6, 1), date(2026, 7, 1)

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {"ANTHROPIC_ADMIN_KEY": "", "ANTHROPIC_ADMIN_API_KEY": ""},
            clear=False,
        )
        self._env.start()
        # Cost-only tests don't queue usage payloads; the usage call fails and is
        # caught + logged. Silence that expected noise.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        self._env.stop()
        logging.disable(logging.NOTSET)

    def test_no_key_skips(self):
        from src.collectors.anthropic import collect_anthropic

        with mock.patch.dict(os.environ, {"ANTHROPIC_ADMIN_KEY": "", "ANTHROPIC_ADMIN_API_KEY": ""}, clear=False):
            self.assertEqual(collect_anthropic({}, self.START, self.END), [])

    def test_sums_daily_buckets_per_model(self):
        from src.collectors import anthropic

        body = {
            "data": [
                {"starting_at": "2026-06-01T00:00:00Z", "results": [
                    {"amount": "1000.0", "model": "claude-sonnet-4-5", "currency": "USD"},
                    {"amount": "500", "model": "claude-haiku-4-5", "currency": "USD"},
                ]},
                {"starting_at": "2026-06-02T00:00:00Z", "results": [
                    {"amount": "250", "model": "claude-sonnet-4-5"},
                    # no model -> falls back to description
                    {"amount": "75", "description": "web_search"},
                ]},
            ],
            "has_more": False,
        }
        fake = _FakeSession([body])
        cfg = {"anthropic": {"admin_key": "sk-ant-admin01-test"}}
        with mock.patch.object(anthropic.requests, "Session", return_value=fake):
            recs = anthropic.collect_anthropic(cfg, self.START, self.END)

        by_model = {r.service: r.cost for r in recs}
        self.assertEqual(by_model["claude-sonnet-4-5"], 12.5)   # (1000 + 250)/100
        self.assertEqual(by_model["claude-haiku-4-5"], 5.0)     # 500/100
        self.assertEqual(by_model["web_search"], 0.75)          # 75/100
        self.assertTrue(all(r.cloud == "Claude" for r in recs))
        # Admin key + version headers were sent.
        headers = fake.calls[0]["headers"]
        self.assertEqual(headers["x-api-key"], "sk-ant-admin01-test")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_tax_rate_grosses_up_usage(self):
        from src.collectors import anthropic

        body = {
            "data": [{"results": [{"amount": "10000", "model": "claude-sonnet-4-5"}]}],
            "has_more": False,
        }
        fake = _FakeSession([body])
        cfg = {"anthropic": {"admin_key": "sk-ant-admin01-test", "tax_rate": 10.3}}
        with mock.patch.object(anthropic.requests, "Session", return_value=fake):
            recs = anthropic.collect_anthropic(cfg, self.START, self.END)

        rec = next(r for r in recs if r.service == "claude-sonnet-4-5")
        self.assertEqual(rec.cost, 100.0)     # 10000 cents, pre-tax subtotal
        self.assertEqual(rec.tax, 10.3)       # 10.3% tax carried separately
        self.assertEqual(rec.total, 110.3)    # what is actually paid

    def test_no_tax_rate_leaves_usage_pretax(self):
        from src.collectors import anthropic

        body = {"data": [{"results": [{"amount": "10000", "model": "m"}]}], "has_more": False}
        fake = _FakeSession([body])
        cfg = {"anthropic": {"admin_key": "sk-ant-admin01-test"}}   # no tax_rate
        with mock.patch.object(anthropic.requests, "Session", return_value=fake):
            recs = anthropic.collect_anthropic(cfg, self.START, self.END)

        rec = next(r for r in recs if r.service == "m")
        self.assertEqual(rec.tax, 0.0)
        self.assertEqual(rec.total, 100.0)

    def test_follows_pagination_next_page(self):
        from src.collectors import anthropic

        pages = [
            {"data": [{"results": [{"amount": "100", "model": "m"}]}], "has_more": True, "next_page": "tok"},
            {"data": [{"results": [{"amount": "400", "model": "m"}]}], "has_more": False},
        ]
        fake = _FakeSession(pages)
        cfg = {"anthropic": {"admin_key": "sk-ant-admin01-test"}}
        with mock.patch.object(anthropic.requests, "Session", return_value=fake):
            recs = anthropic.collect_anthropic(cfg, self.START, self.END)

        self.assertEqual({r.service: r.cost for r in recs}, {"m": 5.0})  # (100 + 400)/100
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["params"]["page"], "tok")

    def test_usage_adds_tokens_and_api_key_owner(self):
        from src.collectors import anthropic

        cost_body = {"data": [{"results": [{"amount": "1000", "model": "claude-sonnet-4-5"}]}],
                     "has_more": False}
        api_keys_body = {"data": [{"id": "key_1", "name": "ci-bot"},
                                  {"id": "key_2", "name": "data-team"}], "has_more": False}
        usage_body = {"data": [{"results": [
            {"model": "claude-sonnet-4-5", "api_key_id": "key_1",
             "uncached_input_tokens": 1000, "output_tokens": 500},
            {"model": "claude-sonnet-4-5", "api_key_id": "key_2",
             "uncached_input_tokens": 200, "output_tokens": 100},
        ]}], "has_more": False}
        fake = _FakeSession([cost_body, api_keys_body, usage_body])
        cfg = {"anthropic": {"admin_key": "sk-ant-admin01-test"}}
        with mock.patch.object(anthropic.requests, "Session", return_value=fake):
            recs = anthropic.collect_anthropic(cfg, self.START, self.END)

        # Cost record (from cost_report) + two usage records (tokens only).
        cost_recs = [r for r in recs if r.cost]
        usage_recs = [r for r in recs if not r.cost and r.tokens]
        self.assertEqual(len(cost_recs), 1)
        self.assertEqual(cost_recs[0].cost, 10.0)
        self.assertEqual({r.user for r in usage_recs}, {"ci-bot", "data-team"})
        ci = next(r for r in usage_recs if r.user == "ci-bot")
        self.assertEqual(ci.tokens, 1500)   # 1000 + 500
        self.assertEqual(ci.model, "claude-sonnet-4-5")


class AiBreakdownTests(unittest.TestCase):
    def _records(self):
        return [
            CostRecord(cloud="Cursor", service="claude-4.5-sonnet", cost=142.5, environment="production"),
            CostRecord(cloud="Cursor", service="gpt-5", cost=17.25, environment="non-production"),
            CostRecord(cloud="Claude", service="claude-sonnet-4.5", cost=880.0, environment="production"),
            # Google spend lives in GCP; should be folded into the AI section.
            CostRecord(cloud="GCP", service="Vertex AI", cost=320.0, environment="production"),
            CostRecord(cloud="GCP", service="Generative Language API", cost=45.5, environment="non-production"),
            CostRecord(cloud="GCP", service="Vertex AI", cost=0.0, credits=12.0, environment="production"),
            # Non-AI GCP spend must be excluded.
            CostRecord(cloud="GCP", service="Compute Engine", cost=500.0, environment="production"),
        ]

    def test_groups_providers_including_google_from_gcp(self):
        from src.report.groups import ai_breakdown

        ai = ai_breakdown(self._records())
        self.assertEqual(set(ai["provider_names"]), {"Cursor", "Claude", "Google"})

        by_provider = {p["provider"]: p for p in ai["providers"]}
        self.assertEqual(by_provider["Google"]["subtotal"], 365.5)   # 320 + 45.5
        self.assertEqual(by_provider["Google"]["credits"], 12.0)
        self.assertEqual(by_provider["Google"]["total"], 353.5)
        self.assertEqual(by_provider["Cursor"]["subtotal"], 159.75)

        services = {r["service"] for r in ai["rows"]}
        self.assertNotIn("Compute Engine", services)
        self.assertTrue(ai["has_env_split"])

    def test_tokens_and_top_users(self):
        from src.report.groups import ai_breakdown

        records = [
            # Cursor: per-user cost + tokens -> ranks users by spend.
            CostRecord(cloud="Cursor", service="claude-4.5-sonnet", model="claude-4.5-sonnet",
                       cost=100.0, tokens=5000, usage_unit="tokens", user="alex@example.com", environment="production"),
            CostRecord(cloud="Cursor", service="claude-4.5-sonnet", model="claude-4.5-sonnet",
                       cost=40.0, tokens=2000, usage_unit="tokens", user="sam@example.com", environment="production"),
            # Claude: usage-only rows (cost 0) -> ranks users by tokens.
            CostRecord(cloud="Claude", service="claude-sonnet-4.5", model="claude-sonnet-4.5",
                       cost=880.0, environment="production"),
            CostRecord(cloud="Claude", service="claude-sonnet-4.5", model="claude-sonnet-4.5",
                       cost=0.0, tokens=90000, usage_unit="tokens", user="ci-bot", environment="production"),
            # Google: two SKUs with DIFFERENT units (count + hour) -> "mixed".
            CostRecord(cloud="GCP", service="Gemini API", model="Generate content input token count",
                       cost=320.0, tokens=1000000, usage_unit="count", scope="ml-prod", environment="production"),
            CostRecord(cloud="GCP", service="Gemini API", model="Cached content storage token hours",
                       cost=8000.0, tokens=8_344_000_000, usage_unit="hour", scope="ml-prod", environment="production"),
        ]
        ai = ai_breakdown(records)
        self.assertTrue(ai["has_tokens"])
        self.assertTrue(ai["has_projects"])

        # Cursor top users ranked by cost.
        cursor_tu = ai["top_users"]["Cursor"]
        self.assertEqual(cursor_tu["by"], "cost")
        self.assertEqual(cursor_tu["rows"][0]["user"], "alex@example.com")
        # Claude top users ranked by tokens (no per-key cost).
        self.assertEqual(ai["top_users"]["Claude"]["by"], "tokens")
        self.assertEqual(ai["top_users"]["Claude"]["rows"][0]["user"], "ci-bot")
        # Google has no per-user attribution.
        self.assertNotIn("Google", ai["top_users"])

        # Per-row unit labels are preserved.
        by_provider = {p["provider"]: p for p in ai["providers"]}
        self.assertEqual(by_provider["Cursor"]["unit"], "tokens")
        self.assertEqual(by_provider["Cursor"]["tokens"], 7000)   # 5000 + 2000
        # Google mixes hour + count -> provider token total suppressed.
        self.assertEqual(by_provider["Google"]["unit"], "mixed")
        self.assertEqual(by_provider["Google"]["tokens"], 0.0)

        google_rows = {r["model"]: r for r in ai["rows"] if r["provider"] == "Google"}
        self.assertEqual(google_rows["Cached content storage token hours"]["unit"], "hour")
        self.assertEqual(google_rows["Generate content input token count"]["unit"], "count")
        self.assertEqual(google_rows["Generate content input token count"]["project"], "ml-prod")

        # By Project: Google SKUs roll up under the GCP project; Cursor/Claude
        # (no project) fall back to the provider name so all spend is attributed.
        proj = {p["project"]: p for p in ai["projects"]}
        self.assertEqual(proj["ml-prod"]["total"], 8320.0)   # 320 + 8000
        self.assertEqual(proj["ml-prod"]["providers"], ["Google"])
        self.assertEqual(proj["ml-prod"]["unit"], "mixed")
        self.assertEqual(proj["Cursor"]["total"], 140.0)           # 100 + 40
        self.assertEqual(proj["Cursor"]["tokens"], 7000)
        self.assertEqual(proj["Claude"]["total"], 880.0)
        # Highest-cost project is listed first.
        self.assertEqual(ai["projects"][0]["project"], "ml-prod")

    def test_custom_gcp_patterns_from_config(self):
        from src.report.groups import ai_breakdown

        records = [
            CostRecord(cloud="GCP", service="Gemini API", cost=30.0, environment="production"),
            CostRecord(cloud="GCP", service="Vertex AI", cost=99.0, environment="production"),
        ]
        cfg = {"ai": {"gcp_service_patterns": ["gemini"]}}
        ai = ai_breakdown(records, cfg)
        services = {r["service"] for r in ai["rows"]}
        self.assertIn("Gemini API", services)
        self.assertNotIn("Vertex AI", services)   # excluded by the narrowed pattern

    def test_empty_when_no_ai_spend(self):
        from src.report.groups import ai_breakdown

        ai = ai_breakdown([CostRecord(cloud="AWS", service="EC2", cost=10.0)])
        self.assertEqual(ai["rows"], [])
        self.assertEqual(ai["total"], 0.0)
        self.assertEqual(ai["provider_names"], [])

    def test_view_does_not_change_gcp_totals(self):
        """The AI section is a cross-cutting view; GCP still counts its AI lines."""
        from src.report.groups import ai_breakdown, gcp_breakdown

        records = self._records()
        ai_breakdown(records)  # must not mutate records
        g = gcp_breakdown(records)
        gcp_services = {s["service"] for s in g["services"]}
        self.assertIn("Vertex AI", gcp_services)
        self.assertIn("Generative Language API", gcp_services)
        # GCP subtotal includes the AI service charges (320 + 45.5 + 500).
        self.assertEqual(g["subtotal"], 865.5)


if __name__ == "__main__":
    unittest.main()
