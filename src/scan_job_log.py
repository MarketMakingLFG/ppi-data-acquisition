#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Iterable

SECRET_ENV = (
    "PPI_ALPHA_VANTAGE_API_KEY",
    "PPI_MARKETDATA_TOKEN",
    "PPI_PRIVATE_HANDOFF_TOKEN",
)
AUTH_HEADER_RE = re.compile(
    rb"(?i)authorization\s*:\s*(?:bearer|token)\s+"
    rb"(?!\*{3,}(?:\s|$)|redacted(?:\s|$))[A-Za-z0-9_.~+/=-]{8,}"
)
CREDENTIAL_QUERY_RE = re.compile(rb"(?i)(?:apikey|api_key|access_token|auth_token|token|password)=[A-Za-z0-9_.~%+/=-]{8,}")


def variants(secret: bytes) -> Iterable[bytes]:
    yield secret
    yield base64.b64encode(secret)
    yield urllib.parse.quote_from_bytes(secret, safe="").encode("ascii")


def count_occurrences(payload: bytes, needles: Iterable[bytes]) -> int:
    total = 0
    seen: set[bytes] = set()
    for needle in needles:
        if not needle or needle in seen:
            continue
        seen.add(needle)
        total += payload.count(needle)
    return total


def scan(log_path: Path) -> dict:
    if not log_path.is_file() or log_path.is_symlink():
        raise ValueError("job log is missing or unsafe")
    payload = log_path.read_bytes()
    if not payload:
        raise ValueError("job log is empty")
    secrets = [os.environ.get(name, "").encode() for name in SECRET_ENV]
    if any(len(secret) < 8 for secret in secrets):
        raise ValueError("required secret value unavailable to log scanner")

    exact_secret_matches = sum(payload.count(secret) for secret in secrets)
    encoded_secret_matches = sum(
        count_occurrences(payload, list(variants(secret))[1:]) for secret in secrets
    )
    authorization_header_matches = len(AUTH_HEADER_RE.findall(payload))
    credential_query_matches = len(CREDENTIAL_QUERY_RE.findall(payload))
    status = "pass" if not any((
        exact_secret_matches,
        encoded_secret_matches,
        authorization_header_matches,
        credential_query_matches,
    )) else "fail"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "logs_scanned": True,
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0") or 0),
        "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0") or 0),
        "head_sha": os.environ.get("GITHUB_SHA", "").strip().lower(),
        "job_log_sha256": hashlib.sha256(payload).hexdigest(),
        "job_log_bytes": len(payload),
        "secret_values_checked": len(secrets),
        "exact_secret_matches": exact_secret_matches,
        "authorization_header_matches": authorization_header_matches,
        "credential_query_matches": credential_query_matches,
        "encoded_secret_matches": encoded_secret_matches,
        "authorized_actions": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan completed GitHub Actions producer job logs for credential leakage")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = scan(args.log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "status", "logs_scanned", "exact_secret_matches", "authorization_header_matches",
        "credential_query_matches", "encoded_secret_matches"
    )}, sort_keys=True))
    if result["status"] != "pass":
        raise SystemExit("job log secret scan failed closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
