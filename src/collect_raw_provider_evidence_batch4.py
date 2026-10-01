#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import collect_raw_provider_evidence as collector

PUBLIC_REPOSITORY = "MarketMakingLFG/ppi-data-acquisition"
PUBLIC_REPOSITORY_ID = 1312286476
PUBLIC_CONTRACT_ID = "PPI-R11-PUBLIC-ACQUISITION-004-R1"
PRIVATE_CONTRACT_ID = "PPI-R11-BATCH-EVIDENCE-004-R1"
COLLECTOR_RELEASE_ID = "PPI-PUBLIC-COLLECTOR-004-R1"
WORKFLOW_PATH = ".github/workflows/collect-r11-public-evidence-batch4.yml"
SHARDS = (
    (0, ["AAPL", "MU", "NVDA", "AMD"]),
    (1, ["AVGO", "INTC", "TSM", "ARM"]),
    (2, ["QCOM", "MRVL", "GFS", "TXN"]),
    (3, ["STM", "ON", "NXPI", "MCHP"]),
)


def checkpoint_digest(rows: list[dict[str, Any]]) -> str:
    payload = collector.canonical_json(sorted(rows, key=lambda item: (item["entity"], item["category"])))
    return hashlib.sha256(payload).hexdigest()


def historical_option_date(payload: dict[str, Any]) -> str:
    values = payload.get("t")
    collector.require(isinstance(values, list), "MarketData candle payload has no timestamps")
    try:
        ordered = sorted({float(value) for value in values})
        collector.require(len(ordered) >= 2, "MarketData candle payload lacks a prior closed session")
        # Use the second-latest distinct trading session. Sorting makes the
        # safety property independent of response ordering.
        return datetime.fromtimestamp(ordered[-2], tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError) as exc:
        raise collector.CollectionError("MarketData candle timestamp is invalid") from exc


def require_marketdata_preflight(receipt: dict[str, Any], remaining_calls: int) -> None:
    collector.require(remaining_calls >= 0, "remaining MarketData call count is invalid")
    required = ("rate_limit_limit", "rate_limit_remaining", "rate_limit_consumed", "rate_limit_reset_epoch")
    missing = [field for field in required if not isinstance(receipt.get(field), int)]
    collector.require(not missing, f"MarketData quota preflight headers missing: {missing}")
    consumed = int(receipt["rate_limit_consumed"])
    remaining = int(receipt["rate_limit_remaining"])
    reset_epoch = int(receipt["rate_limit_reset_epoch"])
    collector.require(0 <= consumed <= 1, f"MarketData QQQ preflight consumed unexpected credits: {consumed}")
    collector.require(reset_epoch > int(collector.time.time()), "MarketData quota preflight reset timestamp is stale")
    collector.require(
        remaining >= remaining_calls,
        f"MarketData credit budget insufficient: remaining={remaining}, required_after_current={remaining_calls}, reset_epoch={reset_epoch}",
    )


def provider_event_time(category: str, payload: dict[str, Any], fallback: str) -> str:
    if category == "specialized_contract_data":
        values = payload.get("updated")
        if isinstance(values, list) and values:
            try:
                latest = max(float(value) for value in values)
                return datetime.fromtimestamp(latest, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                pass
    return collector.provider_event_time(category, payload, fallback)


def collect_sharded(scope: dict[str, Any], output_root: Path, request_id: str, shard_receipt_output: Path) -> dict[str, Any]:
    collector.require(scope["contract_id"] == PUBLIC_CONTRACT_ID, "unexpected public contract binding")
    collector.require(scope["private_contract_id"] == PRIVATE_CONTRACT_ID, "unexpected private contract binding")
    collector.require(scope["collector_release_id"] == COLLECTOR_RELEASE_ID, "unexpected collector release binding")
    collector.require(scope["batch_sequence"] == 4, "unexpected batch sequence")
    collector.require(scope["expected_bundle_count"] == 64, "unexpected bundle count")
    collector.require(scope["expected_path_count"] == 66, "unexpected path count")
    collector.require(scope["expected_provider_request_count"] == 65, "unexpected provider operation count")
    collector.require(scope["cumulative_tickers"] == [ticker for _, tickers in SHARDS for ticker in tickers], "shard partition drift")
    collector.require(scope["authorized_actions"] == [], "collection scope grants downstream authority")
    collector.require(not output_root.exists() or not any(output_root.rglob("*")), "output root must be empty")

    alpha_key = os.environ.get("PPI_ALPHA_VANTAGE_API_KEY", "").strip()
    marketdata_token = os.environ.get("PPI_MARKETDATA_TOKEN", "").strip()
    collector.require(len(alpha_key) >= 8, "PPI_ALPHA_VANTAGE_API_KEY is missing")
    collector.require(len(marketdata_token) >= 8, "PPI_MARKETDATA_TOKEN is missing")

    collection_started = collector.utc_now()
    request_receipts: list[dict[str, Any]] = []
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    receipts: dict[tuple[str, str], dict[str, Any]] = {}
    shard_rows: list[dict[str, Any]] = []
    alpha_index = 0

    # Spend the required QQQ benchmark call first so its rate-limit headers act
    # as a real quota preflight before Alpha Vantage/Yahoo work is consumed.
    benchmark_payload, benchmark_receipt = collector.request_json(
        provider="marketdata",
        host=collector.MARKETDATA_HOST,
        path=f"/v1/stocks/candles/D/{collector.BENCHMARK}/",
        params={"countback": 65, "adjustsplits": "true"},
        headers={"Authorization": f"Bearer {marketdata_token}"},
    )
    benchmark_receipt = {
        **benchmark_receipt,
        "operation": "daily_candles",
        "category": "benchmark_market_time_series",
        "entity": collector.BENCHMARK,
    }
    request_receipts.append(benchmark_receipt)
    remaining_marketdata_calls = 32  # 16 ticker candles + 16 historical option chains
    require_marketdata_preflight(benchmark_receipt, remaining_marketdata_calls)

    # Execute the sixteen ticker/provider operations as four deterministic shards.
    # Each shard performs 4 tickers x 4 provider categories = 16 operations and
    # records a checkpoint digest from the actual provider response receipts.
    for shard_id, tickers in SHARDS:
        checkpoint_rows: list[dict[str, Any]] = []

        for ticker in tickers:
            if alpha_index:
                collector.time.sleep(collector.ALPHA_MIN_INTERVAL_SECONDS)
            payload, receipt = collector.request_json(
                provider="alpha_vantage",
                host=collector.ALPHA_HOST,
                path="/query",
                params={
                    "function": "NEWS_SENTIMENT",
                    "tickers": ticker,
                    "limit": 50,
                    "sort": "LATEST",
                    "apikey": alpha_key,
                },
            )
            alpha_index += 1
            receipt = {**receipt, "operation": "NEWS_SENTIMENT", "category": "independent_recognition", "entity": ticker}
            payloads[(ticker, "independent_recognition")] = payload
            receipts[(ticker, "independent_recognition")] = receipt
            request_receipts.append(receipt)
            checkpoint_rows.append({
                "entity": ticker,
                "category": "independent_recognition",
                "provider": receipt["provider"],
                "operation": receipt["operation"],
                "response_sha256": receipt["response_sha256"],
            })

        for ticker in tickers:
            payload, receipt = collector.fetch_yfinance_expectations(ticker)
            enriched = {**receipt, "category": "expectation_history"}
            payloads[(ticker, "expectation_history")] = payload
            receipts[(ticker, "expectation_history")] = receipt
            request_receipts.append(enriched)
            checkpoint_rows.append({
                "entity": ticker,
                "category": "expectation_history",
                "provider": receipt["provider"],
                "operation": receipt["operation"],
                "response_sha256": receipt["response_sha256"],
            })

        for ticker in tickers:
            candles, candle_receipt = collector.request_json(
                provider="marketdata",
                host=collector.MARKETDATA_HOST,
                path=f"/v1/stocks/candles/D/{ticker}/",
                params={"countback": 65, "adjustsplits": "true"},
                headers={"Authorization": f"Bearer {marketdata_token}"},
            )
            candle_receipt = {**candle_receipt, "operation": "daily_candles", "category": "market_time_series", "entity": ticker}
            remaining_marketdata_calls -= 1
            payloads[(ticker, "market_time_series")] = candles
            receipts[(ticker, "market_time_series")] = candle_receipt
            request_receipts.append(candle_receipt)
            checkpoint_rows.append({
                "entity": ticker,
                "category": "market_time_series",
                "provider": candle_receipt["provider"],
                "operation": candle_receipt["operation"],
                "response_sha256": candle_receipt["response_sha256"],
            })

            option_date = historical_option_date(candles)
            options, option_receipt = collector.request_json(
                provider="marketdata",
                host=collector.MARKETDATA_HOST,
                path=f"/v1/options/chain/{ticker}/",
                params={"date": option_date, "dte": 45, "side": "call", "strikeLimit": 3, "minOpenInterest": 1, "nonstandard": "false"},
                headers={"Authorization": f"Bearer {marketdata_token}"},
            )
            option_receipt = {
                **option_receipt,
                "operation": "option_chain",
                "category": "specialized_contract_data",
                "entity": ticker,
                "marketdata_pricing_mode": "historical_eod",
                "marketdata_option_date": option_date,
                "marketdata_option_date_source": "penultimate_daily_candle_session",
            }
            remaining_marketdata_calls -= 1
            payloads[(ticker, "specialized_contract_data")] = options
            receipts[(ticker, "specialized_contract_data")] = option_receipt
            request_receipts.append(option_receipt)
            checkpoint_rows.append({
                "entity": ticker,
                "category": "specialized_contract_data",
                "provider": option_receipt["provider"],
                "operation": option_receipt["operation"],
                "response_sha256": option_receipt["response_sha256"],
            })

        collector.require(len(checkpoint_rows) == 16, f"shard {shard_id} request count mismatch")
        shard_rows.append({
            "shard_id": shard_id,
            "tickers": tickers,
            "status": "complete",
            "checkpoint_sha256": checkpoint_digest(checkpoint_rows),
            "provider_request_count": 16,
        })

    collector.require(remaining_marketdata_calls == 0, "MarketData operation accounting mismatch")
    collector.require(len(request_receipts) == 65, "provider operation count mismatch")

    entries: list[dict[str, Any]] = []
    for ticker in scope["cumulative_tickers"]:
        for category in scope["categories"]:
            payload = payloads[(ticker, category)]
            receipt = receipts[(ticker, category)]
            observed_at = str(receipt["response_received_at_utc"])
            value: dict[str, Any] = {
                "schema_version": "1.0.0",
                "contract_id": PUBLIC_CONTRACT_ID,
                "private_contract_id": PRIVATE_CONTRACT_ID,
                "collector_release_id": COLLECTOR_RELEASE_ID,
                "queue_receipt_sha256": collector.QUEUE_RECEIPT_SHA256,
                "batch_sequence": 4,
                "category": category,
                "entity": ticker,
                "observed_at_utc": observed_at,
                "provider_event_at_utc": provider_event_time(category, payload, observed_at),
                "source_kind": "external_provider_private_handoff_candidate",
                "source_content_modified": False,
                "synthetic_content_used": False,
                "licensing_disposition": "private_repository_handoff",
                "provider_receipt": receipt,
                "raw_provider_payload": payload,
                "private_review_status": "not_performed",
                "scoring_status": "not_performed",
                "authorized_actions": [],
            }
            if category == "market_time_series":
                value["benchmark"] = collector.BENCHMARK
                value["benchmark_provider_payload"] = benchmark_payload
                value["benchmark_provider_receipt"] = benchmark_receipt
            relative = Path("bundles") / category / f"{ticker}.json"
            artifact_sha = collector.write_json(output_root / relative, value)
            entries.append({
                "entity": ticker,
                "category": category,
                "artifact_path": relative.as_posix(),
                "artifact_sha256": artifact_sha,
                "provider": receipt["provider"],
                "response_sha256": receipt["response_sha256"],
                "provider_event_at_utc": value["provider_event_at_utc"],
                "response_received_at_utc": observed_at,
                "licensing_disposition": "private_repository_handoff",
            })

    collector.require(len(entries) == 64, "bundle count mismatch")
    expected_pairs = {(ticker, category) for ticker in scope["cumulative_tickers"] for category in scope["categories"]}
    collector.require({(item["entity"], item["category"]) for item in entries} == expected_pairs, "bundle coverage mismatch")

    manifest = {
        "schema_version": "1.0.0",
        "status": "private_handoff_candidate_complete",
        "contract_id": PUBLIC_CONTRACT_ID,
        "private_contract_id": PRIVATE_CONTRACT_ID,
        "collector_release_id": COLLECTOR_RELEASE_ID,
        "queue_receipt_sha256": collector.QUEUE_RECEIPT_SHA256,
        "batch_sequence": 4,
        "cumulative_tickers": scope["cumulative_tickers"],
        "new_candidate_tickers": scope["new_candidate_tickers"],
        "categories": scope["categories"],
        "bundle_count": 64,
        "exact_path_count": 66,
        "provider_operation_count": 65,
        "alpha_vantage_request_count": 16,
        "bundles": entries,
        "public_raw_storage_authorized": False,
        "private_review_status": "not_performed",
        "scoring_status": "not_performed",
        "authorized_actions": [],
    }
    manifest_sha = collector.write_json(output_root / "cumulative-manifest.json", manifest)
    collection_completed = collector.utc_now()
    collection_receipt = {
        "schema_version": "1.0.0",
        "status": "collection_complete_private_handoff",
        "repository": PUBLIC_REPOSITORY,
        "repository_id": PUBLIC_REPOSITORY_ID,
        "workflow_path": WORKFLOW_PATH,
        "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
        "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
        "head_sha": os.environ.get("GITHUB_SHA", "").lower(),
        "workflow_event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "request_id": request_id,
        "collection_started_at_utc": collection_started,
        "collection_completed_at_utc": collection_completed,
        "contract_id": PUBLIC_CONTRACT_ID,
        "private_contract_id": PRIVATE_CONTRACT_ID,
        "collector_release_id": COLLECTOR_RELEASE_ID,
        "queue_receipt_sha256": collector.QUEUE_RECEIPT_SHA256,
        "batch_sequence": 4,
        "provider_request_count": 65,
        "alpha_vantage_request_count": 16,
        "provider_requests": request_receipts,
        "bundle_count": 64,
        "exact_path_count": 66,
        "manifest_sha256": manifest_sha,
        "public_raw_storage_authorized": False,
        "private_repository_dispatched": False,
        "registry_mutation_authorized": False,
        "scoring_authorized": False,
        "production_authorized": False,
        "publication_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }
    collector.write_json(output_root / "collection-receipt.json", collection_receipt)
    actual_paths = {path.relative_to(output_root).as_posix() for path in output_root.rglob("*") if path.is_file()}
    collector.require(actual_paths == collector.expected_paths(scope), f"unexpected success package paths: {sorted(actual_paths)}")

    shard_receipt = {
        "schema_version": "1.0.0",
        "status": "r11_public_shards_complete",
        "repository": PUBLIC_REPOSITORY,
        "repository_id": PUBLIC_REPOSITORY_ID,
        "workflow_path": WORKFLOW_PATH,
        "workflow_run_id": collection_receipt["workflow_run_id"],
        "workflow_run_attempt": collection_receipt["workflow_run_attempt"],
        "head_sha": collection_receipt["head_sha"],
        "shard_count": 4,
        "shards": shard_rows,
        "benchmark_request_count": 1,
        "provider_request_count": 65,
        "private_analysis_authorized": False,
        "registry_mutation_authorized": False,
        "production_authorized": False,
        "publication_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }
    collector.write_json(shard_receipt_output, shard_receipt)
    return collection_receipt


def main() -> int:
    collector.PUBLIC_REPOSITORY = PUBLIC_REPOSITORY
    collector.PUBLIC_REPOSITORY_ID = PUBLIC_REPOSITORY_ID
    collector.PUBLIC_CONTRACT_ID = PUBLIC_CONTRACT_ID
    collector.PRIVATE_CONTRACT_ID = PRIVATE_CONTRACT_ID
    collector.COLLECTOR_RELEASE_ID = COLLECTOR_RELEASE_ID
    collector.WORKFLOW_PATH = WORKFLOW_PATH

    parser = argparse.ArgumentParser(description="Collect the exact sharded R11 batch-4 public provider package")
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--failure-output", type=Path, required=True)
    parser.add_argument("--shard-receipt-output", type=Path)
    args = parser.parse_args()
    shard_output = args.shard_receipt_output or args.output_root.parent / "r11-shard-resume-receipt.json"
    try:
        scope = json.loads(args.scope.read_text(encoding="utf-8"))
        result = collect_sharded(scope, args.output_root, args.request_id, shard_output)
        print(json.dumps({"status": result["status"], "bundle_count": result["bundle_count"], "shard_count": 4}, sort_keys=True))
        return 0
    except Exception as exc:
        safe = {
            "schema_version": "1.0.0",
            "status": "collection_failed_closed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "repository": PUBLIC_REPOSITORY,
            "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
            "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
            "head_sha": os.environ.get("GITHUB_SHA", "").lower(),
            "authorized_actions": [],
        }
        collector.write_json(args.failure_output, safe)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
