#!/usr/bin/env python3
"""Prepare, attest, and publish one exact private handoff archive without rebuilding it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:
    from .publish_private_handoff import (
        API_ROOT, PRIVATE_REPOSITORY, PRIVATE_REPOSITORY_ID, api, canonical_json,
        deterministic_zip, ensure_release, expected_paths, require, upload_asset,
    )
except ImportError:
    from publish_private_handoff import (  # type: ignore[no-redef]
        API_ROOT, PRIVATE_REPOSITORY, PRIVATE_REPOSITORY_ID, api, canonical_json,
        deterministic_zip, ensure_release, expected_paths, require, upload_asset,
    )

PREPARED_STATUS = "private_handoff_archive_prepared_for_attestation"
PUBLISHED_STATUS = "attested_private_release_handoff_complete"


def workflow_identity() -> tuple[int, int, str]:
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    head_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    require(run_id > 0 and attempt > 0, "workflow identity is invalid")
    require(len(head_sha) == 40 and all(c in "0123456789abcdef" for c in head_sha), "workflow head SHA is invalid")
    return run_id, attempt, head_sha


def sha256_file(path: Path) -> tuple[str, int]:
    require(path.is_file() and not path.is_symlink(), "prepared private handoff archive is missing or unsafe")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    require(size > 0, "prepared private handoff archive is empty")
    return digest.hexdigest(), size


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"JSON document must be an object: {path}")
    return value


def prepare(package_root: Path, archive: Path, summary_path: Path) -> dict[str, Any]:
    run_id, attempt, head_sha = workflow_identity()
    archive_sha, archive_size = deterministic_zip(package_root, archive)
    paths = expected_paths(package_root)
    summary = {
        "schema_version": "1.0.0",
        "status": PREPARED_STATUS,
        "public_repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "public_run_id": run_id,
        "public_run_attempt": attempt,
        "public_head_sha": head_sha,
        "private_repository": PRIVATE_REPOSITORY,
        "private_asset_name": f"ppi-r11-public-package-{run_id}-{attempt}.zip",
        "private_asset_sha256": archive_sha,
        "private_asset_bytes": archive_size,
        "package_file_count": len(paths),
        "bundle_file_count": sum(p.startswith("bundles/") and p.endswith(".json") for p in paths),
        "attestation_required_before_publication": True,
        "private_release_published": False,
        "authorized_actions": [],
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(canonical_json(summary))
    print(json.dumps(summary, sort_keys=True))
    return summary


def publish(package_root: Path, archive: Path, preparation_summary_path: Path, summary_path: Path, *,
            attestation_id: str, attestation_url: str) -> dict[str, Any]:
    token = os.environ.get("PPI_PRIVATE_HANDOFF_TOKEN", "").strip()
    require(token, "PPI_PRIVATE_HANDOFF_TOKEN is missing")
    run_id, attempt, head_sha = workflow_identity()
    prep = load_json(preparation_summary_path)
    require(prep.get("status") == PREPARED_STATUS, "private handoff preparation status is invalid")
    require(prep.get("public_run_id") == run_id, "prepared archive run ID mismatch")
    require(prep.get("public_run_attempt") == attempt, "prepared archive run attempt mismatch")
    require(prep.get("public_head_sha") == head_sha, "prepared archive head SHA mismatch")
    require(prep.get("private_repository") == PRIVATE_REPOSITORY, "prepared archive target repository mismatch")
    require(prep.get("package_file_count") == 50, "prepared archive file count mismatch")
    require(prep.get("bundle_file_count") == 48, "prepared archive bundle count mismatch")
    require(prep.get("attestation_required_before_publication") is True, "prepared archive does not require attestation")
    require(prep.get("private_release_published") is False, "prepared archive was already marked published")

    archive_sha, archive_size = sha256_file(archive)
    require(prep.get("private_asset_sha256") == archive_sha, "prepared archive digest changed after attestation")
    require(prep.get("private_asset_bytes") == archive_size, "prepared archive size changed after attestation")
    require(len(expected_paths(package_root)) == 50, "package inventory changed before publication")

    attestation_id = attestation_id.strip()
    attestation_url = attestation_url.strip()
    require(attestation_id, "GitHub attestation ID is missing")
    require(attestation_url.startswith("https://github.com/"), "GitHub attestation URL is invalid")

    _, repository = api("GET", f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}", token=token)
    require(isinstance(repository, dict), "unexpected private repository response")
    require(int(repository.get("id", 0)) == PRIVATE_REPOSITORY_ID, "private repository ID mismatch")
    require(repository.get("private") is True, "private handoff repository must remain private")

    tag = f"ppi-r11-public-{run_id}-{attempt}"
    asset_name = f"ppi-r11-public-package-{run_id}-{attempt}.zip"
    require(prep.get("private_asset_name") == asset_name, "prepared archive asset name mismatch")
    release = ensure_release(tag, token, run_id, attempt, head_sha)
    asset = upload_asset(release, archive, asset_name, token)

    summary = {
        "schema_version": "1.0.0",
        "status": PUBLISHED_STATUS,
        "public_run_id": run_id,
        "public_run_attempt": attempt,
        "public_head_sha": head_sha,
        "private_repository": PRIVATE_REPOSITORY,
        "private_repository_id": PRIVATE_REPOSITORY_ID,
        "private_release_id": int(release.get("id", 0) or 0),
        "private_release_tag": tag,
        "private_asset_id": int(asset.get("id", 0) or 0),
        "private_asset_name": asset_name,
        "private_asset_sha256": archive_sha,
        "private_asset_bytes": archive_size,
        "github_attestation_id": attestation_id,
        "github_attestation_url": attestation_url,
        "attestation_verified_before_publication": True,
        "public_raw_artifact_uploaded": False,
        "private_repository_dispatched": False,
        "registry_mutation_authorized": False,
        "production_authorized": False,
        "publication_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(canonical_json(summary))
    print(json.dumps(summary, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--package-root", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--summary", type=Path, required=True)
    q = subs.add_parser("publish")
    q.add_argument("--package-root", type=Path, required=True)
    q.add_argument("--archive", type=Path, required=True)
    q.add_argument("--preparation-summary", type=Path, required=True)
    q.add_argument("--summary", type=Path, required=True)
    q.add_argument("--attestation-id", required=True)
    q.add_argument("--attestation-url", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.package_root, args.archive, args.summary)
    else:
        publish(args.package_root, args.archive, args.preparation_summary, args.summary,
                attestation_id=args.attestation_id, attestation_url=args.attestation_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
