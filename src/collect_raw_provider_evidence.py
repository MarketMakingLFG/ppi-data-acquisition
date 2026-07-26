#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
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
REQUEST_TIMEOUT_SECONDS = 25
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 30
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
                    require(not payload.get(key), f"Alpha Vantage returned provider field {key}")
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
            time.sleep(retry_delay(exc, attempt))
    raise CollectionError(f"{provider} request failed after bounded retries: {' | '.join(errors)}")


def provider_event_time(category: str, payload: dict[str, Any], fallback: str) -> str:
    if category == "independent_recognition":
        feed = payload.get("feed")
        if isinstance(feed, list):
            times = [str(item.get("time_published", "")) for item in feed if isinstance(item, dict)]
            times = [value for value in times if value]
            if times:
                value = max(times)
                try:
                    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
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
    require(scope["authorized_actions"] == [], "collection scope grants downstream authority")

    alpha_key = os.environ.get("PPI_ALPHA_VANTAGE_API_KEY", "").strip()
    marketdata_token = os.environ.get("PPI_MARKETDATA_TOKEN", "").strip()
    require(len(alpha_key) >= 8, "PPI_ALPHA_VANTAGE_API_KEY is missing")
    require(len(marketdata_token) >= 8, "PPI_MARKETDATA_TOKEN is missing")

    collection_started = utc_now()
    benchmark_payload, benchmark_receipt = request_json(
        provider="marketdata",
        host=MARKETDATA_HOST,
        path=f"/v1/stocks/candles/D/{BENCHMARK}/",
        params={"countback": 65, "adjustsplits": "true"},
        headers={"Authorization": f"Bearer {marketdata_token}"},
    )

    entries: list[dict[str, Any]] = []
    request_receipts: list[dict[str, Any]] = [dict(benchmark_receipt, category="benchmark_market_time_series", entity=BENCHMARK)]
    for ticker in scope["cumulative_tickers"]:
        requests = (
            ("expectation_history", "alpha_vantage", ALPHA_HOST, "/query",
             {"function": "EARNINGS_ESTIMATES", "symbol": ticker, "apikey": alpha_key}, None),
            ("independent_recognition", "alpha_vantage", ALPHA_HOST, "/query",
             {"function": "NEWS_SENTIMENT", "tickers": ticker, "limit": 50, "sort": "LATEST", "apikey": alpha_key}, None),
            ("market_time_series", "marketdata", MARKETDATA_HOST, f"/v1/stocks/candles/D/{ticker}/",
             {"countback": 65, "adjustsplits": "true"}, {"Authorization": f"Bearer {marketdata_token}"}),
            ("specialized_contract_data", "marketdata", MARKETDATA_HOST, f"/v1/options/chain/{ticker}/",
             {"dte": 45, "side": "call", "strikeLimit": 3, "minOpenInterest": 1, "nonstandard": "false"},
             {"Authorization": f"Bearer {marketdata_token}"}),
        )
        for category, provider, host, path, params, headers in requests:
            payload, receipt = request_json(provider=provider, host=host, path=path, params=params, headers=headers)
            receipt = {**receipt, "category": category, "entity": ticker}
            request_receipts.append(receipt)
            observed_at = receipt["response_received_at_utc"]
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
                "provider": provider,
                "response_sha256": receipt["response_sha256"],
                "provider_event_at_utc": value["provider_event_at_utc"],
                "response_received_at_utc": receipt["response_received_at_utc"],
                "licensing_disposition": "private_repository_handoff",
            })

    require(len(entries) == 48, "bundle count mismatch")
    expected_pairs = {(ticker, category) for ticker in scope["cumulative_tickers"] for category in scope["categories"]}
    actual_pairs = {(entry["entity"], entry["category"]) for entry in entries}
    require(actual_pairs == expected_pairs, "bundle coverage mismatch")

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
        "provider_request_count": len(request_receipts),
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
