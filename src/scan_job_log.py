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
ANSI_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(rb"\x1b\](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\)")


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


def normalize_ansi(payload: bytes) -> tuple[bytes, int]:
    """Remove only recognized terminal formatting and reject unknown ESC material."""
    normalized, csi_count = ANSI_CSI_RE.subn(b"", payload)
    normalized, osc_count = ANSI_OSC_RE.subn(b"", normalized)
    if b"\x1b" in normalized:
        raise ValueError("job log contains unsupported terminal escape material")
    return normalized, csi_count + osc_count


def max_scan_count(raw: bytes, normalized: bytes, matcher) -> int:
    return max(matcher(raw), matcher(normalized))


def scan(log_path: Path) -> dict:
    if not log_path.is_file() or log_path.is_symlink():
        raise ValueError("job log is missing or unsafe")
    payload = log_path.read_bytes()
    if not payload:
        raise ValueError("job log is empty")
    normalized, ansi_sequences_removed = normalize_ansi(payload)
    secrets = [os.environ.get(name, "").encode() for name in SECRET_ENV]
    if any(len(secret) < 8 for secret in secrets):
        raise ValueError("required secret value unavailable to log scanner")

    exact_secret_matches = max_scan_count(
        payload,
        normalized,
        lambda value: sum(value.count(secret) for secret in secrets),
    )
    encoded_secret_matches = max_scan_count(
        payload,
        normalized,
        lambda value: sum(count_occurrences(value, list(variants(secret))[1:]) for secret in secrets),
    )
    authorization_header_matches = max_scan_count(
        payload,
        normalized,
        lambda value: len(AUTH_HEADER_RE.findall(value)),
    )
    credential_query_matches = max_scan_count(
        payload,
        normalized,
        lambda value: len(CREDENTIAL_QUERY_RE.findall(value)),
    )
    status = "pass" if not any((
        exact_secret_matches,
        encoded_secret_matches,
        authorization_header_matches,
        credential_query_matches,
    )) else "fail"
    return {
        "schema_version": "1.1.0",
        "status": status,
        "logs_scanned": True,
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow_run_id": int(os.environ.get("GITHUB_RUN_ID", "0") or 0),
        "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0") or 0),
        "head_sha": os.environ.get("GITHUB_SHA", "").strip().lower(),
        "job_log_sha256": hashlib.sha256(payload).hexdigest(),
        "job_log_bytes": len(payload),
        "ansi_normalized_sha256": hashlib.sha256(normalized).hexdigest(),
        "ansi_normalized_bytes": len(normalized),
        "ansi_sequences_removed": ansi_sequences_removed,
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
        "credential_query_matches", "encoded_secret_matches", "ansi_sequences_removed"
    )}, sort_keys=True))
    if result["status"] != "pass":
        raise SystemExit("job log secret scan failed closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
