#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALPHA_HOST = "www.alphavantage.co"
MARKETDATA_HOST = "api.marketdata.app"
ALLOWED_HOSTS = {ALPHA_HOST, MARKETDATA_HOST}
PUBLIC_REPOSITORY = "spoudel2010-ux/ppi-data-acquisition"
PUBLIC_REPOSITORY_ID = 1312286476
WORKFLOW_PATH = ".github/workflows/collect-r11-public-evidence.yml"
PUBLIC_CONTRACT_ID = "PPI-R11-PUBLIC-ACQUISITION-003-R1"
PRIVATE_CONTRACT_ID = "PPI-R11-BATCH-EVIDENCE-003-R1"
COLLECTOR_RELEASE_ID = "PPI-PUBLIC-COLLECTOR-003-R1"
QUEUE_RECEIPT_SHA256 = "afd074a959c2c612f6dd5f6c91ed66ce2346144167c73b4d21999005e9a049b3"
MAX_RESPONSE_BYTES = 5_000_000
MAX_YFINANCE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 25
YFINANCE_TIMEOUT_SECONDS = 40
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 30
ALPHA_MIN_INTERVAL_SECONDS = 12.5
YFINANCE_VERSION = "1.5.1"
BENCHMARK = "QQQ"


class CollectionError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CollectionError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> str:
    payload = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    return min(max((when - datetime.now(timezone.utc)).total_seconds(), 0.0), MAX_RETRY_DELAY_SECONDS)
                except (TypeError, ValueError):
                    pass
    return min((2 ** (attempt - 1)) + random.random(), MAX_RETRY_DELAY_SECONDS)


def request_json(
    *,
    provider: str,
    host: str,
    path: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    require(host in ALLOWED_HOSTS, f"provider host is not allowlisted: {host}")
    require(path.startswith("/"), "provider path must be absolute")
    clean_params = {key: str(value) for key, value in params.items() if value is not None}
    request = Request(
        f"https://{host}{path}?{urlencode(clean_params)}",
        headers={"Accept": "application/json", **(headers or {})},
        method="GET",
    )
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request_started = utc_now()
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", response.getcode()))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            response_received = utc_now()
            require(status in {200, 203}, f"{provider} returned HTTP {status}")
            require(len(raw) <= MAX_RESPONSE_BYTES, f"{provider} response exceeded size limit")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CollectionError(f"{provider} returned malformed JSON") from exc
            require(isinstance(payload, dict), f"{provider} response must be a JSON object")
            if provider == "alpha_vantage":
                for key in ("Error Message", "Information", "Note"):
                    if payload.get(key):
                        raise CollectionError(f"Alpha Vantage returned provider field {key}")
            if provider == "marketdata":
                require(payload.get("s") == "ok", f"MarketData returned status {payload.get('s')!r}")
            return payload, {
                "provider": provider,
                "host": host,
                "path": path,
                "attempt": attempt,
                "http_status": status,
                "request_started_at_utc": request_started,
                "response_received_at_utc": response_received,
                "response_bytes": len(raw),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
            }
        except (HTTPError, URLError, TimeoutError, OSError, CollectionError) as exc:
            errors.append(f"attempt={attempt}:{type(exc).__name__}:{exc}")
            retryable = not isinstance(exc, CollectionError)
            if isinstance(exc, HTTPError):
                retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt == MAX_ATTEMPTS:
                break
            sleep_fn(retry_delay(exc, attempt))
    raise CollectionError(f"{provider} request failed after bounded retries: {' | '.join(errors)}")


def fetch_yfinance_expectations(entity: str) -> tuple[dict[str, Any], dict[str, Any]]:
    helper = Path(__file__).with_name("fetch_yfinance_expectations.py")
    require(helper.is_file(), "public Yahoo expectation helper is missing")
    started = utc_now()
    try:
        completed = subprocess.run(
            [sys.executable, str(helper), "--entity", entity],
            capture_output=True,
            check=False,
            timeout=YFINANCE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectionError(f"Yahoo Finance expectation fetch timed out for {entity}") from exc
    except OSError as exc:
        raise CollectionError(f"Yahoo Finance expectation helper could not start for {entity}") from exc
    received = utc_now()
    stdout = bytes(completed.stdout or b"")
    stderr = bytes(completed.stderr or b"")
    require(len(stdout) <= MAX_YFINANCE_BYTES, "Yahoo Finance expectation response exceeded size limit")
    require(completed.returncode == 0, f"Yahoo Finance expectation fetch failed for {entity} with exit code {completed.returncode}")
    require(stdout, f"Yahoo Finance expectation fetch returned no data for {entity}")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Yahoo Finance expectation fetch returned malformed JSON for {entity}") from exc
    require(isinstance(payload, dict), "Yahoo Finance expectation payload must be an object")
    require(payload.get("entity") == entity, "Yahoo Finance expectation entity mismatch")
    require(payload.get("yfinance_version") == YFINANCE_VERSION, "Yahoo Finance helper version mismatch")
    return payload, {
        "provider": "yahoo_finance_via_yfinance",
        "operation": "expectation_history",
        "entity": entity,
        "attempt": 1,
        "request_started_at_utc": started,
        "response_received_at_utc": received,
        "response_bytes": len(stdout),
        "response_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "package_version": YFINANCE_VERSION,
        "timeout_seconds": YFINANCE_TIMEOUT_SECONDS,
    }


def provider_event_time(category: str, payload: dict[str, Any], fallback: str) -> str:
    if category == "independent_recognition":
        feed = payload.get("feed")
        if isinstance(feed, list):
            times = [str(item.get("time_published", "")) for item in feed if isinstance(item, dict)]
            times = [value for value in times if value]
            if times:
                try:
                    parsed = datetime.strptime(max(times), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                    return parsed.isoformat().replace("+00:00", "Z")
                except ValueError:
                    pass
    if category == "market_time_series":
        values = payload.get("t")
        if isinstance(values, list) and values:
            try:
                return datetime.fromtimestamp(float(values[-1]), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                pass
    return fallback


def expected_paths(scope: dict[str, Any]) -> set[str]:
    return {
        *(f"bundles/{category}/{ticker}.json" for category in scope["categories"] for ticker in scope["cumulative_tickers"]),
        "cumulative-manifest.json",
        "collection-receipt.json",
    }


def collect(scope: dict[str, Any], output_root: Path, request_id: str) -> dict[str, Any]:
    require(scope["contract_id"] == PUBLIC_CONTRACT_ID, "unexpected public contract binding")
    require(scope["private_contract_id"] == PRIVATE_CONTRACT_ID, "unexpected private contract binding")
    require(scope["collector_release_id"] == COLLECTOR_RELEASE_ID, "unexpected collector release binding")
    require(scope["batch_sequence"] == 3, "unexpected batch sequence")
    require(scope["expected_bundle_count"] == 48, "unexpected bundle count")
    require(scope["expected_path_count"] == 50, "unexpected path count")
    require(scope["expected_provider_request_count"] == 49, "unexpected provider operation count")
    require(scope["authorized_actions"] == [], "collection scope grants downstream authority")
    require(not output_root.exists() or not any(output_root.rglob("*")), "output root must be empty")

    alpha_key = os.environ.get("PPI_ALPHA_VANTAGE_API_KEY", "").strip()
    marketdata_token = os.environ.get("PPI_MARKETDATA_TOKEN", "").strip()
    require(len(alpha_key) >= 8, "PPI_ALPHA_VANTAGE_API_KEY is missing")
    require(len(marketdata_token) >= 8, "PPI_MARKETDATA_TOKEN is missing")

    collection_started = utc_now()
    request_receipts: list[dict[str, Any]] = []
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    receipts: dict[tuple[str, str], dict[str, Any]] = {}

    # Probe and complete all Alpha Vantage work first. Provider quota responses
    # fail before Yahoo/MarketData work is spent. Twelve calls remain below the
    # documented 25-request free daily allowance.
    for index, ticker in enumerate(scope["cumulative_tickers"]):
        if index:
            time.sleep(ALPHA_MIN_INTERVAL_SECONDS)
        payload, receipt = request_json(
            provider="alpha_vantage",
            host=ALPHA_HOST,
            path="/query",
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "limit": 50,
                "sort": "LATEST",
                "apikey": alpha_key,
            },
        )
        receipt = {**receipt, "operation": "NEWS_SENTIMENT", "category": "independent_recognition", "entity": ticker}
        payloads[(ticker, "independent_recognition")] = payload
        receipts[(ticker, "independent_recognition")] = receipt
        request_receipts.append(receipt)

    for ticker in scope["cumulative_tickers"]:
        payload, receipt = fetch_yfinance_expectations(ticker)
        payloads[(ticker, "expectation_history")] = payload
        receipts[(ticker, "expectation_history")] = receipt
        request_receipts.append({**receipt, "category": "expectation_history"})

    benchmark_payload, benchmark_receipt = request_json(
        provider="marketdata",
        host=MARKETDATA_HOST,
        path=f"/v1/stocks/candles/D/{BENCHMARK}/",
        params={"countback": 65, "adjustsplits": "true"},
        headers={"Authorization": f"Bearer {marketdata_token}"},
    )
    benchmark_receipt = {**benchmark_receipt, "operation": "daily_candles", "category": "benchmark_market_time_series", "entity": BENCHMARK}
    request_receipts.append(benchmark_receipt)

    for ticker in scope["cumulative_tickers"]:
        candles, candle_receipt = request_json(
            provider="marketdata",
            host=MARKETDATA_HOST,
            path=f"/v1/stocks/candles/D/{ticker}/",
            params={"countback": 65, "adjustsplits": "true"},
            headers={"Authorization": f"Bearer {marketdata_token}"},
        )
        candle_receipt = {**candle_receipt, "operation": "daily_candles", "category": "market_time_series", "entity": ticker}
        payloads[(ticker, "market_time_series")] = candles
        receipts[(ticker, "market_time_series")] = candle_receipt
        request_receipts.append(candle_receipt)

        options, option_receipt = request_json(
            provider="marketdata",
            host=MARKETDATA_HOST,
            path=f"/v1/options/chain/{ticker}/",
            params={"dte": 45, "side": "call", "strikeLimit": 3, "minOpenInterest": 1, "nonstandard": "false"},
            headers={"Authorization": f"Bearer {marketdata_token}"},
        )
        option_receipt = {**option_receipt, "operation": "option_chain", "category": "specialized_contract_data", "entity": ticker}
        payloads[(ticker, "specialized_contract_data")] = options
        receipts[(ticker, "specialized_contract_data")] = option_receipt
        request_receipts.append(option_receipt)

    require(len(request_receipts) == 49, "provider operation count mismatch")
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
                "queue_receipt_sha256": QUEUE_RECEIPT_SHA256,
                "batch_sequence": 3,
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
                value["benchmark"] = BENCHMARK
                value["benchmark_provider_payload"] = benchmark_payload
                value["benchmark_provider_receipt"] = benchmark_receipt
            relative = Path("bundles") / category / f"{ticker}.json"
            artifact_sha = write_json(output_root / relative, value)
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

    require(len(entries) == 48, "bundle count mismatch")
    expected_pairs = {(ticker, category) for ticker in scope["cumulative_tickers"] for category in scope["categories"]}
    require({(item["entity"], item["category"]) for item in entries} == expected_pairs, "bundle coverage mismatch")

    manifest = {
        "schema_version": "1.0.0",
        "status": "private_handoff_candidate_complete",
        "contract_id": PUBLIC_CONTRACT_ID,
        "private_contract_id": PRIVATE_CONTRACT_ID,
        "collector_release_id": COLLECTOR_RELEASE_ID,
        "queue_receipt_sha256": QUEUE_RECEIPT_SHA256,
        "batch_sequence": 3,
        "cumulative_tickers": scope["cumulative_tickers"],
        "new_candidate_tickers": scope["new_candidate_tickers"],
        "categories": scope["categories"],
        "bundle_count": 48,
        "exact_path_count": 50,
        "provider_operation_count": 49,
        "alpha_vantage_request_count": 12,
        "bundles": entries,
        "public_raw_storage_authorized": False,
        "private_review_status": "not_performed",
        "scoring_status": "not_performed",
        "authorized_actions": [],
    }
    manifest_sha = write_json(output_root / "cumulative-manifest.json", manifest)
    collection_completed = utc_now()
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
        "queue_receipt_sha256": QUEUE_RECEIPT_SHA256,
        "batch_sequence": 3,
        "provider_request_count": 49,
        "alpha_vantage_request_count": 12,
        "provider_requests": request_receipts,
        "bundle_count": 48,
        "exact_path_count": 50,
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
    write_json(output_root / "collection-receipt.json", collection_receipt)
    actual_paths = {path.relative_to(output_root).as_posix() for path in output_root.rglob("*") if path.is_file()}
    require(actual_paths == expected_paths(scope), f"unexpected success package paths: {sorted(actual_paths)}")
    return collection_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect the exact R11 batch-3 public provider package")
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--failure-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        scope = json.loads(args.scope.read_text(encoding="utf-8"))
        result = collect(scope, args.output_root, args.request_id)
        print(json.dumps({"status": result["status"], "bundle_count": result["bundle_count"]}, sort_keys=True))
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
        write_json(args.failure_output, safe)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
