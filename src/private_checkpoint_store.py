#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from publish_private_handoff import API_ROOT, PRIVATE_REPOSITORY, PRIVATE_REPOSITORY_ID, UPLOAD_ROOT, api, canonical_json, require

ASSET_RE = re.compile(r"^ppi-r11-checkpoint-(\d+)-(\d+)-([0-9a-f]{64})-([0-9a-f]{64})\.json$")
CHECKPOINT_STATUS = "r11_private_checkpoint_material"
RELEASE_PREFIX = "ppi-r11-checkpoints-"
AUTH_SCHEME = "hmac-sha256-domain-separated-v1"
AUTH_DOMAIN = b"PPI-R11-BATCH3-R2/private-checkpoint/v1\0"


def workflow_identity() -> tuple[int, int, str]:
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    head_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    require(run_id > 0 and attempt > 0, "workflow identity is invalid")
    require(len(head_sha) == 40 and all(c in "0123456789abcdef" for c in head_sha), "workflow head SHA is invalid")
    return run_id, attempt, head_sha


def token() -> str:
    value = os.environ.get("PPI_PRIVATE_HANDOFF_TOKEN", "").strip()
    require(value, "PPI_PRIVATE_HANDOFF_TOKEN is missing")
    return value


def checkpoint_auth_tag(raw: bytes, auth: str) -> str:
    require(bool(auth), "checkpoint authentication credential is missing")
    key = hashlib.sha256(AUTH_DOMAIN + auth.encode("utf-8")).digest()
    return hmac.new(key, AUTH_DOMAIN + raw, hashlib.sha256).hexdigest()


def verify_private_repository(auth: str) -> None:
    _, repository = api("GET", f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}", token=auth)
    require(isinstance(repository, dict), "unexpected private repository response")
    require(int(repository.get("id", 0)) == PRIVATE_REPOSITORY_ID, "private repository ID mismatch")
    require(repository.get("private") is True, "checkpoint repository must remain private")


def release_tag(run_id: int) -> str:
    return f"{RELEASE_PREFIX}{run_id}"


def release_by_tag(run_id: int, auth: str) -> dict[str, Any] | None:
    tag = urllib.parse.quote(release_tag(run_id), safe="")
    status, value = api(
        "GET",
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/tags/{tag}",
        token=auth,
        allowed=(200, 404),
    )
    if status == 404:
        return None
    require(isinstance(value, dict), "unexpected checkpoint release response")
    return value


def ensure_release(run_id: int, head_sha: str, auth: str) -> dict[str, Any]:
    existing = release_by_tag(run_id, auth)
    if existing is not None:
        require(existing.get("draft") is False, "existing checkpoint release unexpectedly draft")
        return existing
    _, value = api(
        "POST",
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases",
        token=auth,
        payload={
            "tag_name": release_tag(run_id),
            "target_commitish": "main",
            "name": f"PPI R11 resumable checkpoint {run_id}",
            "body": (
                "Temporary private checkpoint material for a single public R11 acquisition run. "
                f"Source head {head_sha}. No private analysis, registry, production, publication, trading, or R12 authority."
            ),
            "draft": False,
            "prerelease": True,
        },
    )
    require(isinstance(value, dict), "unexpected checkpoint release creation response")
    return value


def checkpoint_bytes(path: Path, *, expected_run_id: int, expected_attempt: int, expected_head_sha: str) -> bytes:
    require(path.is_file() and not path.is_symlink(), "checkpoint file is missing or unsafe")
    raw = path.read_bytes()
    require(raw and len(raw) <= 150_000_000, "checkpoint size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint is not valid JSON") from exc
    require(isinstance(value, dict), "checkpoint must be a JSON object")
    require(value.get("status") == CHECKPOINT_STATUS, "checkpoint status mismatch")
    require(value.get("workflow_run_id") == expected_run_id, "checkpoint run ID mismatch")
    require(value.get("workflow_run_attempt") == expected_attempt, "checkpoint attempt mismatch")
    require(value.get("head_sha") == expected_head_sha, "checkpoint head SHA mismatch")
    require(value.get("authorized_actions") == [], "checkpoint must remain non-authorizing")
    return raw


def upload_checkpoint(path: Path, auth: str) -> dict[str, Any]:
    run_id, attempt, head_sha = workflow_identity()
    verify_private_repository(auth)
    raw = checkpoint_bytes(path, expected_run_id=run_id, expected_attempt=attempt, expected_head_sha=head_sha)
    digest = hashlib.sha256(raw).hexdigest()
    auth_tag = checkpoint_auth_tag(raw, auth)
    asset_name = f"ppi-r11-checkpoint-{run_id}-{attempt}-{digest}-{auth_tag}.json"
    release = ensure_release(run_id, head_sha, auth)
    assets = release.get("assets")
    require(isinstance(assets, list), "checkpoint release assets missing")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == asset_name]
    if matches:
        require(len(matches) == 1, "duplicate checkpoint assets")
        require(int(matches[0].get("size", 0) or 0) == len(raw), "existing checkpoint asset size mismatch")
        asset = matches[0]
    else:
        release_id = int(release.get("id", 0) or 0)
        require(release_id > 0, "checkpoint release ID invalid")
        query = urllib.parse.urlencode({"name": asset_name})
        _, asset = api(
            "POST",
            f"{UPLOAD_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/{release_id}/assets?{query}",
            token=auth,
            body=raw,
            content_type="application/json",
            allowed=(201,),
        )
        require(isinstance(asset, dict), "unexpected checkpoint upload response")
    return {
        "schema_version": "1.0.0",
        "status": "private_checkpoint_persisted",
        "public_run_id": run_id,
        "public_run_attempt": attempt,
        "public_head_sha": head_sha,
        "private_repository": PRIVATE_REPOSITORY,
        "private_release_tag": release_tag(run_id),
        "private_asset_name": asset_name,
        "private_asset_sha256": digest,
        "private_asset_bytes": len(raw),
        "checkpoint_authentication": AUTH_SCHEME,
        "public_raw_artifact_uploaded": False,
        "private_analysis_authorized": False,
        "registry_mutation_authorized": False,
        "production_authorized": False,
        "publication_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }


def download_asset(asset: dict[str, Any], auth: str) -> bytes:
    asset_id = int(asset.get("id", 0) or 0)
    require(asset_id > 0, "checkpoint asset ID invalid")
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/assets/{asset_id}",
        method="GET",
        headers={
            "Authorization": f"Bearer {auth}",
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PPI private checkpoint restore",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(150_000_001)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError("private checkpoint download failed") from exc
    require(0 < len(raw) <= 150_000_000, "downloaded checkpoint size invalid")
    return raw


def restore_checkpoint(output: Path, auth: str) -> dict[str, Any]:
    run_id, attempt, head_sha = workflow_identity()
    verify_private_repository(auth)
    if attempt <= 1:
        return {"status": "no_prior_attempt", "restored": False, "authorized_actions": []}
    release = release_by_tag(run_id, auth)
    if release is None:
        return {"status": "no_prior_checkpoint_release", "restored": False, "authorized_actions": []}
    assets = release.get("assets")
    require(isinstance(assets, list), "checkpoint release assets missing")
    candidates: list[tuple[int, str, str, dict[str, Any]]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        match = ASSET_RE.fullmatch(name)
        if not match:
            continue
        asset_run = int(match.group(1))
        asset_attempt = int(match.group(2))
        digest = match.group(3)
        auth_tag = match.group(4)
        if asset_run == run_id and 0 < asset_attempt < attempt:
            candidates.append((asset_attempt, digest, auth_tag, asset))
    if not candidates:
        return {"status": "no_prior_checkpoint_asset", "restored": False, "authorized_actions": []}
    prior_attempt, expected_digest, expected_auth_tag, asset = max(candidates, key=lambda item: item[0])
    raw = download_asset(asset, auth)
    require(hashlib.sha256(raw).hexdigest() == expected_digest, "downloaded checkpoint digest mismatch")
    require(hmac.compare_digest(checkpoint_auth_tag(raw, auth), expected_auth_tag), "downloaded checkpoint authentication mismatch")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("downloaded checkpoint is malformed") from exc
    require(isinstance(value, dict), "downloaded checkpoint must be an object")
    require(value.get("workflow_run_id") == run_id, "restored checkpoint run ID mismatch")
    require(value.get("workflow_run_attempt") == prior_attempt, "restored checkpoint attempt mismatch")
    require(value.get("head_sha") == head_sha, "restored checkpoint head SHA mismatch")
    require(value.get("authorized_actions") == [], "restored checkpoint must remain non-authorizing")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    return {
        "schema_version": "1.0.0",
        "status": "private_checkpoint_restored",
        "restored": True,
        "public_run_id": run_id,
        "current_attempt": attempt,
        "resumed_from_attempt": prior_attempt,
        "public_head_sha": head_sha,
        "checkpoint_sha256": expected_digest,
        "checkpoint_authentication": AUTH_SCHEME,
        "authorized_actions": [],
    }


def cleanup_checkpoint(auth: str) -> dict[str, Any]:
    run_id, _, _ = workflow_identity()
    release = release_by_tag(run_id, auth)
    if release is None:
        return {"status": "checkpoint_release_absent", "deleted": False, "authorized_actions": []}
    release_id = int(release.get("id", 0) or 0)
    require(release_id > 0, "checkpoint release ID invalid")
    api("DELETE", f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/{release_id}", token=auth, allowed=(204,))
    encoded = urllib.parse.quote(release_tag(run_id), safe="")
    api(
        "DELETE",
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/git/refs/tags/{encoded}",
        token=auth,
        allowed=(204, 404, 422),
    )
    return {"status": "private_checkpoint_deleted_after_success", "deleted": True, "authorized_actions": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist and restore authenticated private R11 resumability checkpoints")
    sub = parser.add_subparsers(dest="command", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--output", type=Path, required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--checkpoint", type=Path, required=True)
    publish.add_argument("--receipt", type=Path, required=True)
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    auth = token()
    if args.command == "restore":
        result = restore_checkpoint(args.output, auth)
        print(json.dumps(result, sort_keys=True))
    elif args.command == "publish":
        result = upload_checkpoint(args.checkpoint, auth)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(canonical_json(result))
        print(json.dumps(result, sort_keys=True))
    else:
        result = cleanup_checkpoint(auth)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(canonical_json(result))
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
