from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicBoundaryTests(unittest.TestCase):
    def test_scope_is_exact_batch_three(self) -> None:
        scope = json.loads((ROOT / "config/r11_batch_003.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["batch_sequence"], 3)
        self.assertEqual(
            scope["cumulative_tickers"],
            ["AAPL", "MU", "NVDA", "AMD", "AVGO", "INTC", "TSM", "ARM", "QCOM", "MRVL", "GFS", "TXN"],
        )
        self.assertEqual(scope["new_candidate_tickers"], ["QCOM", "MRVL", "GFS", "TXN"])
        self.assertEqual(scope["expected_bundle_count"], 48)
        self.assertEqual(scope["authorized_actions"], [])

    def test_workflow_is_manual_only_and_read_only(self) -> None:
        text = (ROOT / ".github/workflows/collect-r11-public-evidence.yml").read_text(encoding="utf-8")
        self.assertIn("\n  workflow_dispatch:\n", text)
        self.assertNotIn("\n  schedule:\n", text)
        self.assertNotIn("\n  push:\n", text)
        self.assertNotIn("\n  pull_request:\n", text)
        self.assertNotIn("\n  repository_dispatch:\n", text)
        self.assertIn("permissions:\n  contents: read", text)
        for forbidden in (
            "contents:" + " write",
            "actions:" + " write",
            "pull-requests:" + " write",
            "git " + "push",
            "gh pr " + "create",
            "gh pr " + "merge",
        ):
            self.assertNotIn(forbidden, text)

    def test_collector_contains_no_private_calculation(self) -> None:
        text = (ROOT / "src/collect_raw_provider_evidence.py").read_text(encoding="utf-8").lower()
        for forbidden in (
            "relative_strength",
            "sma_50",
            "average_volume_20",
            "entity_return_20",
            "countability",
            "registry append",
            "place_order",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
