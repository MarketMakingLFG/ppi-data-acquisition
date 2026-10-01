from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collect_raw_provider_evidence_batch4 as batch4  # noqa: E402
import run_resumable_batch4 as resume4  # noqa: E402
import publish_private_handoff_batch4 as handoff4  # noqa: E402
import private_checkpoint_store_batch4 as checkpoint4  # noqa: E402


class BatchFourCandidateTests(unittest.TestCase):
    def test_exact_scope_and_counts(self) -> None:
        scope = json.loads((ROOT / "config/r11_batch_004.json").read_text())
        expected = ["AAPL","MU","NVDA","AMD","AVGO","INTC","TSM","ARM","QCOM","MRVL","GFS","TXN","STM","ON","NXPI","MCHP"]
        self.assertEqual(scope["batch_sequence"], 4)
        self.assertEqual(scope["cumulative_tickers"], expected)
        self.assertEqual(scope["new_candidate_tickers"], ["STM","ON","NXPI","MCHP"])
        self.assertEqual(scope["expected_bundle_count"], 64)
        self.assertEqual(scope["expected_path_count"], 66)
        self.assertEqual(scope["expected_provider_request_count"], 65)
        self.assertEqual(scope["expected_alpha_vantage_request_count"], 16)
        self.assertEqual([ticker for _, tickers in batch4.SHARDS for ticker in tickers], expected)
        self.assertEqual(len(resume4.ALL_EXPECTED_KEYS), 65)

    def test_collector_identity_is_batch_four_only(self) -> None:
        self.assertEqual(batch4.PUBLIC_CONTRACT_ID, "PPI-R11-PUBLIC-ACQUISITION-004-R1")
        self.assertEqual(batch4.PRIVATE_CONTRACT_ID, "PPI-R11-BATCH-EVIDENCE-004-R1")
        self.assertEqual(batch4.COLLECTOR_RELEASE_ID, "PPI-PUBLIC-COLLECTOR-004-R1")
        self.assertEqual(batch4.WORKFLOW_PATH, ".github/workflows/collect-r11-public-evidence-batch4.yml")
        self.assertEqual([len(tickers) for _, tickers in batch4.SHARDS], [4,4,4,4])

    def test_resume_scope_rejects_outside_ticker(self) -> None:
        with self.assertRaises(resume4.ResumeError):
            resume4.request_key("alpha_vantage", "/query", {"function":"NEWS_SENTIMENT","tickers":"NOT-IN-SCOPE"})

    def test_batch_four_handoff_requires_66_paths_and_64_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(64):
                p = root / "bundles" / "fixture" / f"{i:02d}.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}")
            (root / "cumulative-manifest.json").write_text("{}")
            (root / "collection-receipt.json").write_text("{}")
            paths = handoff4.expected_paths(root)
            self.assertEqual(len(paths), 66)

    def test_batch_four_checkpoint_namespace_is_isolated(self) -> None:
        digest = "a" * 64
        tag = "b" * 64
        name = f"ppi-r11-batch4-checkpoint-123-2-{digest}-{tag}.json"
        self.assertIsNotNone(checkpoint4.ASSET_RE.fullmatch(name))
        self.assertIsNone(checkpoint4.ASSET_RE.fullmatch(f"ppi-r11-checkpoint-123-2-{digest}-{tag}.json"))
        self.assertTrue(checkpoint4.RELEASE_PREFIX.startswith("ppi-r11-batch4-"))
        self.assertIn(b"BATCH4", checkpoint4.AUTH_DOMAIN)

    def test_batch_four_workflow_is_manual_protected_and_non_writing(self) -> None:
        text = (ROOT / ".github/workflows/collect-r11-public-evidence-batch4.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("\n  schedule:", text)
        self.assertNotIn("\n  push:", text)
        self.assertNotIn("\n  pull_request:", text)
        self.assertIn("COLLECT-R11-BATCH-4", text)
        self.assertEqual(text.count("environment: r11-public-acquisition-protected"), 2)
        self.assertIn("src/run_resumable_batch4.py", text)
        self.assertIn("src/private_checkpoint_store_batch4.py", text)
        self.assertIn("src/publish_prepared_private_handoff_batch4.py", text)
        self.assertIn("src/scan_job_log_batch4.py", text)
        self.assertIn("config/r11_batch_004.json", text)
        for forbidden in ("contents: write","actions: write","pull-requests: write","git push","gh pr create","gh pr merge"):
            self.assertNotIn(forbidden, text)

    def test_batch_three_files_remain_present(self) -> None:
        self.assertTrue((ROOT / "config/r11_batch_003.json").is_file())
        self.assertTrue((ROOT / "src/collect_raw_provider_evidence_r2.py").is_file())
        self.assertTrue((ROOT / ".github/workflows/collect-r11-public-evidence.yml").is_file())


if __name__ == "__main__":
    unittest.main()
