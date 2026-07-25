#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALPHA_HOST = "www.alphavantage.co"
MARKETDATA_HOST = "api.marketdata.app"
ALLOWED_HOSTS = {ALPHA_HOST, MARKETDATA_HOST}
MAX_RESPONSE_BYTES = 5_000_000
REQUEST_TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 2


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> str:
    payload = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


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
    query = urlencode({key: str(value) for key, value in params.items() if value is not None})
    request = Request(
        f"https://{host}{path}?{query}",
        headers={"Accept": "application/json", **(headers or {})},
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", response.getcode()))
                require(status in {200, 203}, f"{provider} returned HTTP {status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            require(len(raw) <= MAX_RESPONSE_BYTES, f"{provider} response exceeded size limit")
            payload = json.loads(raw.decode("utf-8"))
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
                "response_bytes": len(raw),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
            }
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code < 500 and exc.code != 429:
                break
            if isinstance(exc, RuntimeError):
                break
            if attempt < MAX_ATTEMPTS:
                time.sleep(float(attempt))
    raise RuntimeError(f"{provider} request failed: {last_error}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect raw public R11 provider evidence")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--observed-at")
    args = parser.parse_args()

    scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    require(scope["contract_id"] == "PPI-R11-BATCH-EVIDENCE-003-R1", "unexpected private contract binding")
    require(scope["batch_sequence"] == 3, "unexpected batch sequence")
    require(scope["expected_bundle_count"] == 48, "unexpected bundle count")
    require(scope["authorized_actions"] == [], "collection scope grants downstream authority")

    alpha_key = os.environ.get("PPI_ALPHA_VANTAGE_API_KEY", "").strip()
    marketdata_token = os.environ.get("PPI_MARKETDATA_TOKEN", "").strip()
    require(len(alpha_key) >= 8, "PPI_ALPHA_VANTAGE_API_KEY is missing")
    require(len(marketdata_token) >= 8, "PPI_MARKETDATA_TOKEN is missing")

    observed_at = args.observed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    require(observed_at.endswith("Z"), "observed-at must be UTC")
    output_root = Path(args.output_root)
    entries: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    for ticker in scope["cumulative_tickers"]:
        requests = [
            ("expectation_history", "alpha_vantage", ALPHA_HOST, "/query", {"function": "EARNINGS_ESTIMATES", "symbol": ticker, "apikey": alpha_key}, None),
            ("independent_recognition", "alpha_vantage", ALPHA_HOST, "/query", {"function": "NEWS_SENTIMENT", "tickers": ticker, "limit": 50, "sort": "LATEST", "apikey": alpha_key}, None),
            ("market_time_series", "marketdata", MARKETDATA_HOST, f"/v1/stocks/candles/D/{ticker}/", {"countback": 65, "adjustsplits": "true"}, {"Authorization": f"Bearer {marketdata_token}"}),
            ("specialized_contract_data", "marketdata", MARKETDATA_HOST, f"/v1/options/chain/{ticker}/", {"dte": 45, "side": "call", "strikeLimit": 3, "minOpenInterest": 1, "nonstandard": "false"}, {"Authorization": f"Bearer {marketdata_token}"}),
        ]
        for category, provider, host, path, params, headers in requests:
            payload, receipt = request_json(provider=provider, host=host, path=path, params=params, headers=headers)
            receipt = {**receipt, "category": category, "entity": ticker}
            value = {
                "schema_version": "1.0.0",
                "contract_id": scope["contract_id"],
                "contract_sha256": scope["contract_sha256"],
                "queue_receipt_sha256": scope["queue_receipt_sha256"],
                "batch_sequence": 3,
                "category": category,
                "entity": ticker,
                "observed_at_utc": observed_at,
                "source_kind": "external_provider_raw_candidate",
                "source_content_modified": False,
                "synthetic_content_used": False,
                "provider_receipt": receipt,
                "raw_provider_payload": payload,
                "private_review_status": "not_performed",
                "scoring_status": "not_performed",
                "authorized_actions": [],
            }
            relative = Path("bundles") / category / f"{ticker}.json"
            artifact_sha = write_json(output_root / relative, value)
            entries.append({
                "entity": ticker,
                "category": category,
                "artifact_path": relative.as_posix(),
                "artifact_sha256": artifact_sha,
                "provider": provider,
                "response_sha256": receipt["response_sha256"],
            })
            receipts.append(receipt)

    require(len(entries) == 48, "bundle count mismatch")
    expected_pairs = {(ticker, category) for ticker in scope["cumulative_tickers"] for category in scope["categories"]}
    actual_pairs = {(entry["entity"], entry["category"]) for entry in entries}
    require(actual_pairs == expected_pairs, "bundle coverage mismatch")

    manifest = {
        "schema_version": "1.0.0",
        "status": "raw_provider_candidate_collected",
        "contract_id": scope["contract_id"],
        "contract_sha256": scope["contract_sha256"],
        "queue_receipt_sha256": scope["queue_receipt_sha256"],
        "batch_sequence": 3,
        "observed_at_utc": observed_at,
        "cumulative_tickers": scope["cumulative_tickers"],
        "new_candidate_tickers": scope["new_candidate_tickers"],
        "categories": scope["categories"],
        "bundle_count": 48,
        "bundles": entries,
        "private_review_status": "not_performed",
        "scoring_status": "not_performed",
        "authorized_actions": [],
    }
    manifest_sha = write_json(output_root / "cumulative-manifest.json", manifest)

    collection_receipt = {
        "schema_version": "1.0.0",
        "status": "immutable_collection_receipt",
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
        "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
        "head_sha": os.environ.get("GITHUB_SHA", "").lower(),
        "workflow_event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "observed_at_utc": observed_at,
        "contract_id": scope["contract_id"],
        "contract_sha256": scope["contract_sha256"],
        "queue_receipt_sha256": scope["queue_receipt_sha256"],
        "batch_sequence": 3,
        "provider_request_count": len(receipts),
        "provider_requests": receipts,
        "bundle_count": 48,
        "manifest_sha256": manifest_sha,
        "private_repository_dispatched": False,
        "registry_mutation_authorized": False,
        "scoring_authorized": False,
        "production_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }
    write_json(output_root / "collection-receipt.json", collection_receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
