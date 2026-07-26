from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicBoundaryTests(unittest.TestCase):
    def test_scope_is_exact_batch_three_r2(self) -> None:
        scope = json.loads((ROOT / "config/r11_batch_003.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["contract_id"], "PPI-R11-PUBLIC-ACQUISITION-003-R2")
        self.assertEqual(scope["private_contract_id"], "PPI-R11-BATCH-EVIDENCE-003-R1")
        self.assertEqual(scope["collector_release_id"], "PPI-PUBLIC-COLLECTOR-003-R2")
        self.assertEqual(scope["batch_sequence"], 3)
        self.assertEqual(
            scope["cumulative_tickers"],
            ["AAPL", "MU", "NVDA", "AMD", "AVGO", "INTC", "TSM", "ARM", "QCOM", "MRVL", "GFS", "TXN"],
        )
        self.assertEqual(scope["new_candidate_tickers"], ["QCOM", "MRVL", "GFS", "TXN"])
        self.assertEqual(
            scope["categories"],
            ["expectation_history", "independent_recognition", "market_time_series", "specialized_contract_data"],
        )
        self.assertEqual(scope["expectation_provider"], "yahoo_finance_via_yfinance")
        self.assertEqual(scope["pinned_yfinance_version"], "1.5.1")
        self.assertEqual(scope["expected_alpha_vantage_request_count"], 12)
        self.assertEqual(scope["expected_bundle_count"], 48)
        self.assertEqual(scope["expected_path_count"], 50)
        self.assertEqual(scope["expected_provider_request_count"], 49)
        self.assertFalse(scope["public_raw_storage_authorized"])
        self.assertTrue(scope["private_release_handoff_required"])
        self.assertEqual(scope["authorized_actions"], [])

    def test_workflow_is_manual_dispatch_read_only_and_private_handoff_only(self) -> None:
        text = (ROOT / ".github/workflows/collect-r11-public-evidence.yml").read_text(encoding="utf-8")
        self.assertIn("\n  workflow_dispatch:\n", text)
        self.assertNotIn("\n  schedule:\n", text)
        self.assertNotIn("\n  push:\n", text)
        self.assertNotIn("\n  pull_request:\n", text)
        self.assertNotIn("\n  repository_dispatch:\n", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("PRIVATE_RELEASE_HANDOFF_ENABLED: true", text)
        self.assertIn("ppi-r11-public-success-", text)
        self.assertIn("ppi-r11-public-failure-", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("'yfinance==1.5.1'", text)
        self.assertIn("src/fetch_yfinance_expectations.py", text)
        self.assertIn("src/collect_raw_provider_evidence_r2.py", text)
        self.assertIn("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", text)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", text)
        self.assertNotIn("actions/checkout@v", text)
        self.assertNotIn("actions/upload-artifact@v", text)
        self.assertNotIn("runtime/r11-batch3-private-package/", text.split("Retain public safe success metadata", 1)[1])
        for forbidden in (
            "contents:" + " write",
            "actions:" + " write",
            "pull-requests:" + " write",
            "git " + "push",
            "gh pr " + "create",
            "gh pr " + "merge",
        ):
            self.assertNotIn(forbidden, text)

    def test_collector_keeps_calculation_private_and_limits_alpha_calls(self) -> None:
        text = (ROOT / "src/collect_raw_provider_evidence.py").read_text(encoding="utf-8")
        entrypoint = (ROOT / "src/collect_raw_provider_evidence_r2.py").read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in (
            "relative_strength",
            "sma_50",
            "average_volume_20",
            "entity_return_20",
            "countability",
            "registry append",
            "place_order",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn('"provider": "yahoo_finance_via_yfinance"', text)
        self.assertIn('"function": "NEWS_SENTIMENT"', text)
        self.assertNotIn('"function": "EARNINGS_ESTIMATES"', text)
        self.assertIn("ALPHA_MIN_INTERVAL_SECONDS = 12.5", text)
        self.assertIn('"alpha_vantage_request_count": 12', text)
        self.assertIn('PUBLIC_CONTRACT_ID = "PPI-R11-PUBLIC-ACQUISITION-003-R2"', entrypoint)
        self.assertIn('COLLECTOR_RELEASE_ID = "PPI-PUBLIC-COLLECTOR-003-R2"', entrypoint)

    def test_public_yahoo_helper_is_bounded_and_pinned(self) -> None:
        text = (ROOT / "src/fetch_yfinance_expectations.py").read_text(encoding="utf-8")
        self.assertIn('EXPECTED_YFINANCE_VERSION = "1.5.1"', text)
        self.assertEqual(text.count("SUPPORTED_ENTITIES"), 2)
        self.assertIn("get_eps_trend", text)
        self.assertIn("get_eps_revisions", text)
        self.assertIn("get_earnings_estimate", text)

    def test_private_handoff_does_not_dispatch_or_mutate_registry(self) -> None:
        text = (ROOT / "src/publish_private_handoff.py").read_text(encoding="utf-8")
        self.assertIn('PRIVATE_REPOSITORY = "musksuman3/ai-signal-engine"', text)
        self.assertIn('"private_repository_dispatched": False', text)
        self.assertIn('"registry_mutation_authorized": False', text)
        for forbidden in ("/dispatches", "gh pr ", "git push"):
            self.assertNotIn(forbidden, text)

    def test_licensing_forbids_public_raw_storage(self) -> None:
        value = json.loads((ROOT / "config/provider_licensing_dispositions.json").read_text(encoding="utf-8"))
        self.assertEqual(value["contract_id"], "PPI-R11-PUBLIC-ACQUISITION-003-R2")
        self.assertEqual(value["public_raw_artifact_policy"], "public_storage_prohibited")
        self.assertTrue(value["dispositions"])
        self.assertEqual(value["dispositions"][0]["provider"], "yahoo_finance_via_yfinance")
        self.assertEqual(value["dispositions"][1]["maximum_batch_requests"], 12)
        for item in value["dispositions"]:
            self.assertEqual(item["disposition"], "private_repository_handoff")
            self.assertEqual(item["public_retention"], "hash_and_metadata_only")

    def test_contract_lineage_preserves_r1_and_freezes_r2(self) -> None:
        acquisition_r1 = json.loads((ROOT / "contracts/PPI-R11-PUBLIC-ACQUISITION-003-R1.json").read_text(encoding="utf-8"))
        acquisition_r2 = json.loads((ROOT / "contracts/PPI-R11-PUBLIC-ACQUISITION-003-R2.json").read_text(encoding="utf-8"))
        collector_r1 = json.loads((ROOT / "contracts/PPI-PUBLIC-COLLECTOR-003-R1.json").read_text(encoding="utf-8"))
        collector_r2 = json.loads((ROOT / "contracts/PPI-PUBLIC-COLLECTOR-003-R2.json").read_text(encoding="utf-8"))
        self.assertEqual(acquisition_r1["collector_release_id"], "PPI-PUBLIC-COLLECTOR-003-R1")
        self.assertEqual(collector_r1["provider_operations"][0], "alpha_vantage:EARNINGS_ESTIMATES")
        self.assertEqual(acquisition_r2["status"], "frozen")
        self.assertEqual(collector_r2["status"], "frozen")
        self.assertEqual(acquisition_r2["supersedes"], acquisition_r1["contract_id"])
        self.assertEqual(collector_r2["supersedes"], collector_r1["contract_id"])
        self.assertEqual(acquisition_r2["private_contract_id"], "PPI-R11-BATCH-EVIDENCE-003-R1")
        self.assertEqual(acquisition_r2["collector_release_id"], collector_r2["contract_id"])
        self.assertEqual(acquisition_r2["category_providers"]["expectation_history"], "yahoo_finance_via_yfinance:1.5.1")
        self.assertEqual(collector_r2["alpha_vantage_batch_request_count"], 12)
        self.assertEqual(acquisition_r2["exact_success_package"]["path_count"], 50)
        self.assertFalse(acquisition_r2["private_dispatch_authorized"])
        self.assertEqual(acquisition_r2["authorized_actions"], [])


if __name__ == "__main__":
    unittest.main()
