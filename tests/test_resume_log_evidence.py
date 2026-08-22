from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collect_raw_provider_evidence_r2 as r2  # noqa: E402
import run_resumable_r2 as resume  # noqa: E402
import scan_job_log  # noqa: E402

RUN_ID = 123456
HEAD_SHA = "a" * 40
RESPONSE_SHA = "b" * 64


def operation(entity: str, category: str, origin_attempt: int = 1) -> dict:
    provider = {
        "independent_recognition": "alpha_vantage",
        "expectation_history": "yahoo_finance_via_yfinance",
        "market_time_series": "marketdata",
        "specialized_contract_data": "marketdata",
        "benchmark_market_time_series": "marketdata",
    }[category]
    return {
        "entity": entity,
        "category": category,
        "payload": {"entity": entity, "category": category, "sample": True},
        "receipt": {
            "provider": provider,
            "operation": category,
            "response_received_at_utc": "2026-08-01T00:00:01Z",
            "response_sha256": RESPONSE_SHA,
        },
        "origin_attempt": origin_attempt,
    }


def checkpoint(operations: list[dict], attempt: int = 1) -> dict:
    value = {
        "schema_version": resume.CHECKPOINT_SCHEMA,
        "status": resume.CHECKPOINT_STATUS,
        "repository": r2.PUBLIC_REPOSITORY,
        "workflow_run_id": RUN_ID,
        "workflow_run_attempt": attempt,
        "head_sha": HEAD_SHA,
        "collection_started_at_utc": "2026-08-01T00:00:00Z",
        "resumed_from_attempt": None,
        "resume_policy": resume.RESUME_POLICY,
        "operations": operations,
        "authorized_actions": [],
        "checkpoint_sha256": "0" * 64,
    }
    value["checkpoint_sha256"] = resume.checkpoint_digest(value)
    return value


def shard_operations(shard_id: int) -> list[dict]:
    tickers = dict(r2.SHARDS)[shard_id]
    categories = (
        "independent_recognition",
        "expectation_history",
        "market_time_series",
        "specialized_contract_data",
    )
    return [operation(ticker, category) for ticker in tickers for category in categories]


class ResumeCheckpointTests(unittest.TestCase):
    def test_complete_shard_is_reusable(self) -> None:
        value = checkpoint(shard_operations(0))
        prior, started, reusable, reused_shards, benchmark_reused = resume.validate_checkpoint(
            value, current_run_id=RUN_ID, current_attempt=2, current_head_sha=HEAD_SHA
        )
        self.assertEqual(prior, 1)
        self.assertEqual(started, "2026-08-01T00:00:00Z")
        self.assertEqual(reused_shards, [0])
        self.assertEqual(len(reusable), 12)
        self.assertFalse(benchmark_reused)

    def test_partial_shard_is_never_reused(self) -> None:
        value = checkpoint(shard_operations(0)[:-1])
        _, _, reusable, reused_shards, _ = resume.validate_checkpoint(
            value, current_run_id=RUN_ID, current_attempt=2, current_head_sha=HEAD_SHA
        )
        self.assertEqual(reused_shards, [])
        self.assertEqual(reusable, {})

    def test_tampered_checkpoint_fails_closed(self) -> None:
        value = checkpoint(shard_operations(0))
        value["operations"][0]["payload"]["tampered"] = True
        with self.assertRaises(resume.ResumeError):
            resume.validate_checkpoint(
                value, current_run_id=RUN_ID, current_attempt=2, current_head_sha=HEAD_SHA
            )

    def test_wrong_run_head_or_attempt_fails_closed(self) -> None:
        value = checkpoint(shard_operations(0))
        for kwargs in (
            {"current_run_id": RUN_ID + 1, "current_attempt": 2, "current_head_sha": HEAD_SHA},
            {"current_run_id": RUN_ID, "current_attempt": 1, "current_head_sha": HEAD_SHA},
            {"current_run_id": RUN_ID, "current_attempt": 2, "current_head_sha": "f" * 40},
        ):
            with self.assertRaises(resume.ResumeError):
                resume.validate_checkpoint(value, **kwargs)

    def test_session_starts_with_reused_operations_and_zero_network_calls(self) -> None:
        prior = checkpoint(shard_operations(0))
        _, started, reusable, reused_shards, benchmark_reused = resume.validate_checkpoint(
            prior, current_run_id=RUN_ID, current_attempt=2, current_head_sha=HEAD_SHA
        )
        with tempfile.TemporaryDirectory() as tmp:
            session = resume.CheckpointSession(
                output=Path(tmp) / "current.json",
                run_id=RUN_ID,
                attempt=2,
                head_sha=HEAD_SHA,
                started=started,
                prior_attempt=1,
                reusable=reusable,
                reused_shards=reused_shards,
                benchmark_reused=benchmark_reused,
            )
            self.assertEqual(session.network_calls, 0)
            self.assertIsNotNone(session.cached(("AAPL", "expectation_history")))
            persisted = json.loads((Path(tmp) / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["workflow_run_attempt"], 2)
            self.assertEqual(resume.checkpoint_digest(persisted), persisted["checkpoint_sha256"])


class JobLogLeakTests(unittest.TestCase):
    ENV = {
        "PPI_ALPHA_VANTAGE_API_KEY": "alpha-secret-12345",
        "PPI_MARKETDATA_TOKEN": "market-secret-67890",
        "PPI_PRIVATE_HANDOFF_TOKEN": "private-handoff-secret-abcdefghijkl",
        "GITHUB_REPOSITORY": "MarketMakingLFG/ppi-data-acquisition",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": HEAD_SHA,
    }

    def scan_bytes(self, payload: bytes) -> dict:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, self.ENV, clear=False):
            path = Path(tmp) / "job.log"
            path.write_bytes(payload)
            return scan_job_log.scan(path)

    def test_masked_job_log_passes(self) -> None:
        result = self.scan_bytes(b"request completed\nAuthorization: Bearer ***\nsecret masked as ***\n")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["logs_scanned"])
        self.assertEqual(result["exact_secret_matches"], 0)
        self.assertEqual(result["encoded_secret_matches"], 0)

    def test_exact_secret_in_job_log_fails(self) -> None:
        result = self.scan_bytes(b"oops alpha-secret-12345 leaked\n")
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["exact_secret_matches"], 0)

    def test_encoded_secret_in_job_log_fails(self) -> None:
        encoded = __import__("base64").b64encode(self.ENV["PPI_MARKETDATA_TOKEN"].encode())
        result = self.scan_bytes(b"encoded=" + encoded + b"\n")
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["encoded_secret_matches"], 0)

    def test_unmasked_authorization_header_fails(self) -> None:
        result = self.scan_bytes(b"Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n")
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["authorization_header_matches"], 0)


class WorkflowWiringTests(unittest.TestCase):
    def test_workflow_persists_private_checkpoint_and_scans_completed_job_log(self) -> None:
        text = (ROOT / ".github/workflows/collect-r11-public-evidence.yml").read_text(encoding="utf-8")
        self.assertIn("actions: read", text)
        self.assertIn("src/run_resumable_r2.py", text)
        self.assertIn("src/private_checkpoint_store.py restore", text)
        self.assertIn("src/private_checkpoint_store.py publish", text)
        self.assertIn("if: always()", text)
        self.assertIn("scan-job-log:", text)
        self.assertIn("src/scan_job_log.py", text)
        self.assertIn("actions/jobs/${job_id}/logs", text)
        self.assertNotIn("runtime/r11-batch3-private-checkpoint/", text.split("Retain public safe success metadata", 1)[1])


if __name__ == "__main__":
    unittest.main()
