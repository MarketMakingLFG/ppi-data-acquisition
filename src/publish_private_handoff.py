#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

API_ROOT = "https://api.github.com"
UPLOAD_ROOT = "https://uploads.github.com"
PRIVATE_REPOSITORY = "musksuman3/ai-signal-engine"
PRIVATE_REPOSITORY_ID = 1290626648
MAX_ARCHIVE_BYTES = 150_000_000
EXPECTED_FILE_COUNT = 50


class HandoffError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HandoffError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def api(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    body: bytes | None = None,
    content_type: str = "application/json",
    allowed: tuple[int, ...] = (200, 201, 204),
) -> tuple[int, Any]:
    require(payload is None or body is None, "payload and body are mutually exclusive")
    data = body
    if payload is not None:
        data = canonical_json(payload)
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PPI public private-release handoff",
            **({"Content-Type": content_type} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", errors="replace")
    if status not in allowed:
        message = parsed.get("message") if isinstance(parsed, dict) else str(parsed)
        raise HandoffError(f"GitHub API {method} {url} returned {status}: {message}")
    return status, parsed


def expected_paths(package_root: Path) -> list[str]:
    result = sorted(path.relative_to(package_root).as_posix() for path in package_root.rglob("*") if path.is_file())
    require(len(result) == EXPECTED_FILE_COUNT, f"private package must contain exactly {EXPECTED_FILE_COUNT} files")
    require("cumulative-manifest.json" in result and "collection-receipt.json" in result, "manifest or receipt is missing")
    require(sum(path.startswith("bundles/") and path.endswith(".json") for path in result) == 48, "bundle count mismatch")
    return result


def deterministic_zip(package_root: Path, output: Path) -> tuple[str, int]:
    paths = expected_paths(package_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in paths:
            source = package_root / relative
            require(source.is_file() and not source.is_symlink(), f"unsafe package path: {relative}")
            member = PurePosixPath(relative)
            require(not member.is_absolute() and ".." not in member.parts, f"unsafe package member: {relative}")
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, source.read_bytes())
    size = output.stat().st_size
    require(0 < size <= MAX_ARCHIVE_BYTES, "private handoff archive exceeds size limit")
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def release_by_tag(tag: str, token: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(tag, safe="")
    status, value = api(
        "GET",
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/tags/{encoded}",
        token=token,
        allowed=(200, 404),
    )
    if status == 404:
        return None
    require(isinstance(value, dict), "unexpected release response")
    return value


def ensure_release(tag: str, token: str, public_run_id: int, attempt: int, head_sha: str) -> dict[str, Any]:
    existing = release_by_tag(tag, token)
    if existing is not None:
        require(existing.get("draft") is False, "existing private handoff release is draft")
        return existing
    _, value = api(
        "POST",
        f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}/releases",
        token=token,
        payload={
            "tag_name": tag,
            "target_commitish": "main",
            "name": f"PPI R11 public handoff {public_run_id} attempt {attempt}",
            "body": (
                "Immutable private handoff from "
                f"spoudel2010-ux/ppi-data-acquisition run {public_run_id} attempt {attempt}, "
                f"head {head_sha}. No production, publication, broker, order, trading, MMM/raw-data, or R12 authority."
            ),
            "draft": False,
            "prerelease": True,
        },
    )
    require(isinstance(value, dict), "unexpected release creation response")
    return value


def upload_asset(release: dict[str, Any], archive: Path, asset_name: str, token: str) -> dict[str, Any]:
    assets = release.get("assets")
    require(isinstance(assets, list), "private release assets are missing")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == asset_name]
    if matches:
        require(len(matches) == 1, "duplicate private handoff release assets")
        require(int(matches[0].get("size", 0) or 0) == archive.stat().st_size, "existing handoff asset size mismatch")
        return matches[0]
    release_id = int(release.get("id", 0) or 0)
    require(release_id > 0, "private release ID is invalid")
    query = urllib.parse.urlencode({"name": asset_name})
    _, value = api(
        "POST",
        f"{UPLOAD_ROOT}/repos/{PRIVATE_REPOSITORY}/releases/{release_id}/assets?{query}",
        token=token,
        body=archive.read_bytes(),
        content_type="application/zip",
        allowed=(201,),
    )
    require(isinstance(value, dict), "unexpected asset upload response")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Package exactly 50 paths and publish a private release handoff")
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    token = os.environ.get("PPI_PRIVATE_HANDOFF_TOKEN", "").strip()
    require(token, "PPI_PRIVATE_HANDOFF_TOKEN is missing")
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    head_sha = os.environ.get("GITHUB_SHA", "").lower()
    require(run_id > 0 and attempt > 0, "workflow identity is invalid")
    require(len(head_sha) == 40 and all(char in "0123456789abcdef" for char in head_sha), "workflow head SHA is invalid")

    _, repository = api("GET", f"{API_ROOT}/repos/{PRIVATE_REPOSITORY}", token=token)
    require(isinstance(repository, dict), "unexpected private repository response")
    require(int(repository.get("id", 0)) == PRIVATE_REPOSITORY_ID, "private repository ID mismatch")
    require(repository.get("private") is True, "private handoff repository must remain private")

    archive_sha, archive_size = deterministic_zip(args.package_root, args.archive)
    tag = f"ppi-r11-public-{run_id}-{attempt}"
    asset_name = f"ppi-r11-public-package-{run_id}-{attempt}.zip"
    release = ensure_release(tag, token, run_id, attempt, head_sha)
    asset = upload_asset(release, args.archive, asset_name, token)

    summary = {
        "schema_version": "1.0.0",
        "status": "private_release_handoff_complete",
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
        "public_raw_artifact_uploaded": False,
        "private_repository_dispatched": False,
        "registry_mutation_authorized": False,
        "production_authorized": False,
        "publication_authorized": False,
        "trading_authorized": False,
        "r12_authorized": False,
        "authorized_actions": [],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_bytes(canonical_json(summary))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as exc:
        raise SystemExit(str(exc)) from exc
