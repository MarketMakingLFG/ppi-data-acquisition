#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import collect_raw_provider_evidence as collector
import collect_raw_provider_evidence_r2 as r2

CHECKPOINT_SCHEMA = "1.0.0"
CHECKPOINT_STATUS = "r11_private_checkpoint_material"
RESUME_POLICY = "reuse_verified_completed_shards_only"
BENCHMARK_KEY = (collector.BENCHMARK, "benchmark_market_time_series")
EXPECTED_SHARD_KEYS = {
    shard_id: {(ticker, category) for ticker in tickers for category in (
        "independent_recognition", "expectation_history", "market_time_series", "specialized_contract_data"
    )}
    for shard_id, tickers in r2.SHARDS
}
ALL_EXPECTED_KEYS = set().union(*EXPECTED_SHARD_KEYS.values()) | {BENCHMARK_KEY}


class ResumeError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ResumeError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def checkpoint_digest(value: dict[str, Any]) -> str:
    zeroed = dict(value)
    zeroed["checkpoint_sha256"] = "0" * 64
    return hashlib.sha256(canonical_json(zeroed)).hexdigest()


def operation_key(value: dict[str, Any]) -> tuple[str, str]:
    return str(value.get("entity", "")), str(value.get("category", ""))


def validate_checkpoint(
    value: dict[str, Any], *, current_run_id: int, current_attempt: int, current_head_sha: str
) -> tuple[int, str, dict[tuple[str, str], dict[str, Any]], list[int], bool]:
    require(value.get("schema_version") == CHECKPOINT_SCHEMA, "checkpoint schema mismatch")
    require(value.get("status") == CHECKPOINT_STATUS, "checkpoint status mismatch")
    require(value.get("repository") == r2.PUBLIC_REPOSITORY, "checkpoint repository mismatch")
    require(value.get("workflow_run_id") == current_run_id, "checkpoint run ID mismatch")
    prior_attempt = value.get("workflow_run_attempt")
    require(isinstance(prior_attempt, int) and 0 < prior_attempt < current_attempt, "checkpoint attempt is not prior to current attempt")
    require(value.get("head_sha") == current_head_sha, "checkpoint head SHA mismatch")
    started = value.get("collection_started_at_utc")
    require(isinstance(started, str) and started.endswith("Z"), "checkpoint collection start missing")
    require(value.get("authorized_actions") == [], "checkpoint authorized_actions must be empty")
    digest = value.get("checkpoint_sha256")
    require(isinstance(digest, str) and len(digest) == 64, "checkpoint digest missing")
    require(checkpoint_digest(value) == digest, "checkpoint digest mismatch")

    operations = value.get("operations")
    require(isinstance(operations, list), "checkpoint operations must be a list")
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for operation in operations:
        require(isinstance(operation, dict), "checkpoint operation must be an object")
        require(set(operation) == {"entity", "category", "payload", "receipt", "origin_attempt"}, "checkpoint operation fields drift")
        key = operation_key(operation)
        require(key in ALL_EXPECTED_KEYS, f"unexpected checkpoint operation: {key}")
        require(key not in by_key, f"duplicate checkpoint operation: {key}")
        require(isinstance(operation.get("payload"), dict), f"checkpoint payload must be an object: {key}")
        receipt = operation.get("receipt")
        require(isinstance(receipt, dict), f"checkpoint receipt must be an object: {key}")
        origin_attempt = operation.get("origin_attempt")
        require(isinstance(origin_attempt, int) and 0 < origin_attempt <= prior_attempt, "checkpoint origin attempt invalid")
        response_sha = receipt.get("response_sha256")
        require(isinstance(response_sha, str) and len(response_sha) == 64 and all(c in "0123456789abcdef" for c in response_sha), f"checkpoint response digest invalid: {key}")
        by_key[key] = operation

    reusable_shards = [
        shard_id for shard_id, expected in EXPECTED_SHARD_KEYS.items() if expected.issubset(by_key)
    ]
    reusable: dict[tuple[str, str], dict[str, Any]] = {}
    for shard_id in reusable_shards:
        for key in EXPECTED_SHARD_KEYS[shard_id]:
            reusable[key] = by_key[key]
    benchmark_reused = BENCHMARK_KEY in by_key
    if benchmark_reused:
        reusable[BENCHMARK_KEY] = by_key[BENCHMARK_KEY]
    return prior_attempt, started, reusable, reusable_shards, benchmark_reused


def request_key(provider: str, path: str, params: dict[str, Any]) -> tuple[str, str]:
    if provider == "alpha_vantage":
        require(params.get("function") == "NEWS_SENTIMENT", "unexpected Alpha Vantage operation")
        ticker = str(params.get("tickers", ""))
        key = (ticker, "independent_recognition")
    elif provider == "marketdata" and path.startswith("/v1/stocks/candles/D/"):
        ticker = path.rstrip("/").split("/")[-1]
        key = BENCHMARK_KEY if ticker == collector.BENCHMARK else (ticker, "market_time_series")
    elif provider == "marketdata" and path.startswith("/v1/options/chain/"):
        ticker = path.rstrip("/").split("/")[-1]
        key = (ticker, "specialized_contract_data")
    else:
        raise ResumeError(f"unexpected provider operation: {provider} {path}")
    require(key in ALL_EXPECTED_KEYS, f"provider operation outside frozen scope: {key}")
    return key


class CheckpointSession:
    def __init__(
        self,
        *,
        output: Path,
        run_id: int,
        attempt: int,
        head_sha: str,
        started: str,
        prior_attempt: int | None,
        reusable: dict[tuple[str, str], dict[str, Any]],
        reused_shards: list[int],
        benchmark_reused: bool,
    ) -> None:
        self.output = output
        self.run_id = run_id
        self.attempt = attempt
        self.head_sha = head_sha
        self.started = started
        self.prior_attempt = prior_attempt
        self.reused_shards = sorted(reused_shards)
        self.benchmark_reused = benchmark_reused
        self.operations: dict[tuple[str, str], dict[str, Any]] = {
            key: copy.deepcopy(operation) for key, operation in reusable.items()
        }
        self.network_calls = 0
        self.persist()

    def persist(self) -> None:
        value: dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA,
            "status": CHECKPOINT_STATUS,
            "repository": r2.PUBLIC_REPOSITORY,
            "workflow_run_id": self.run_id,
            "workflow_run_attempt": self.attempt,
            "head_sha": self.head_sha,
            "collection_started_at_utc": self.started,
            "resumed_from_attempt": self.prior_attempt,
            "resume_policy": RESUME_POLICY,
            "operations": [self.operations[key] for key in sorted(self.operations)],
            "authorized_actions": [],
            "checkpoint_sha256": "0" * 64,
        }
        value["checkpoint_sha256"] = checkpoint_digest(value)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(canonical_json(value))

    def cached(self, key: tuple[str, str]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        operation = self.operations.get(key)
        if operation is None:
            return None
        return copy.deepcopy(operation["payload"]), copy.deepcopy(operation["receipt"])

    def record(self, key: tuple[str, str], payload: dict[str, Any], receipt: dict[str, Any]) -> None:
        require(key not in self.operations, f"provider operation repeated within attempt: {key}")
        self.operations[key] = {
            "entity": key[0],
            "category": key[1],
            "payload": copy.deepcopy(payload),
            "receipt": copy.deepcopy(receipt),
            "origin_attempt": self.attempt,
        }
        self.network_calls += 1
        self.persist()


def load_prior_checkpoint(path: Path | None, *, run_id: int, attempt: int, head_sha: str) -> tuple[int | None, str, dict[tuple[str, str], dict[str, Any]], list[int], bool]:
    started = collector.utc_now()
    if path is None or not path.is_file():
        return None, started, {}, [], False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeError("prior checkpoint is not valid JSON") from exc
    require(isinstance(value, dict), "prior checkpoint must be an object")
    prior_attempt, prior_started, reusable, reused_shards, benchmark_reused = validate_checkpoint(
        value, current_run_id=run_id, current_attempt=attempt, current_head_sha=head_sha
    )
    return prior_attempt, prior_started, reusable, reused_shards, benchmark_reused


def patch_receipts(
    output_root: Path,
    shard_receipt_output: Path,
    *,
    started: str,
    prior_attempt: int | None,
    reused_shards: list[int],
    benchmark_reused: bool,
    network_calls: int,
) -> None:
    collection_path = output_root / "collection-receipt.json"
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    require(isinstance(collection, dict), "collection receipt malformed")
    collection["collection_started_at_utc"] = started
    collection_path.write_bytes(collector.canonical_json(collection))

    shard_receipt = json.loads(shard_receipt_output.read_text(encoding="utf-8"))
    require(isinstance(shard_receipt, dict), "shard receipt malformed")
    shard_receipt["resumed_from_attempt"] = prior_attempt if reused_shards else None
    shard_receipt["resume_policy"] = RESUME_POLICY
    shard_receipt["reused_shards"] = sorted(reused_shards)
    shard_receipt["recomputed_shards"] = [shard_id for shard_id, _ in r2.SHARDS if shard_id not in reused_shards]
    shard_receipt["reused_provider_request_count"] = 12 * len(reused_shards) + (1 if benchmark_reused else 0)
    shard_receipt["provider_calls_this_attempt"] = network_calls
    shard_receipt["benchmark_reused"] = benchmark_reused
    shard_receipt_output.write_bytes(collector.canonical_json(shard_receipt))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the exact R11 R2 collector with verified cross-attempt checkpoint reuse")
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--failure-output", type=Path, required=True)
    parser.add_argument("--shard-receipt-output", type=Path, required=True)
    parser.add_argument("--checkpoint-input", type=Path)
    parser.add_argument("--checkpoint-output", type=Path, required=True)
    args = parser.parse_args()

    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    head_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    require(run_id > 0 and attempt > 0, "workflow run identity is invalid")
    require(len(head_sha) == 40 and all(c in "0123456789abcdef" for c in head_sha), "workflow head SHA invalid")

    collector.PUBLIC_REPOSITORY = r2.PUBLIC_REPOSITORY
    collector.PUBLIC_REPOSITORY_ID = r2.PUBLIC_REPOSITORY_ID
    collector.PUBLIC_CONTRACT_ID = r2.PUBLIC_CONTRACT_ID
    collector.COLLECTOR_RELEASE_ID = r2.COLLECTOR_RELEASE_ID

    prior_attempt, started, reusable, reused_shards, benchmark_reused = load_prior_checkpoint(
        args.checkpoint_input, run_id=run_id, attempt=attempt, head_sha=head_sha
    )
    session = CheckpointSession(
        output=args.checkpoint_output,
        run_id=run_id,
        attempt=attempt,
        head_sha=head_sha,
        started=started,
        prior_attempt=prior_attempt,
        reusable=reusable,
        reused_shards=reused_shards,
        benchmark_reused=benchmark_reused,
    )

    original_request_json: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = collector.request_json
    original_yfinance: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] = collector.fetch_yfinance_expectations

    def resumable_request_json(*, provider: str, host: str, path: str, params: dict[str, Any], headers: dict[str, str] | None = None, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        key = request_key(provider, path, params)
        cached = session.cached(key)
        if cached is not None:
            return cached
        payload, receipt = original_request_json(provider=provider, host=host, path=path, params=params, headers=headers, **kwargs)
        session.record(key, payload, receipt)
        return payload, receipt

    def resumable_yfinance(entity: str) -> tuple[dict[str, Any], dict[str, Any]]:
        key = (entity, "expectation_history")
        require(key in ALL_EXPECTED_KEYS, "Yahoo Finance entity outside frozen scope")
        cached = session.cached(key)
        if cached is not None:
            return cached
        payload, receipt = original_yfinance(entity)
        session.record(key, payload, receipt)
        return payload, receipt

    collector.request_json = resumable_request_json  # type: ignore[assignment]
    collector.fetch_yfinance_expectations = resumable_yfinance  # type: ignore[assignment]

    try:
        scope = json.loads(args.scope.read_text(encoding="utf-8"))
        require(isinstance(scope, dict), "scope must be an object")
        result = r2.collect_sharded(scope, args.output_root, args.request_id, args.shard_receipt_output)
        patch_receipts(
            args.output_root,
            args.shard_receipt_output,
            started=started,
            prior_attempt=prior_attempt,
            reused_shards=reused_shards,
            benchmark_reused=benchmark_reused,
            network_calls=session.network_calls,
        )
        print(json.dumps({
            "status": result["status"],
            "bundle_count": result["bundle_count"],
            "reused_shards": reused_shards,
            "provider_calls_this_attempt": session.network_calls,
        }, sort_keys=True))
        return 0
    except Exception as exc:
        safe = {
            "schema_version": "1.0.0",
            "status": "collection_failed_closed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "repository": r2.PUBLIC_REPOSITORY,
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
            "head_sha": head_sha,
            "resumed_from_attempt": prior_attempt,
            "reused_shards": reused_shards,
            "provider_calls_this_attempt": session.network_calls,
            "authorized_actions": [],
        }
        collector.write_json(args.failure_output, safe)
        raise
    finally:
        collector.request_json = original_request_json  # type: ignore[assignment]
        collector.fetch_yfinance_expectations = original_yfinance  # type: ignore[assignment]


if __name__ == "__main__":
    raise SystemExit(main())
