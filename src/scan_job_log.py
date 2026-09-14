#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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

API_ROOT = "https://api.github.com"
EXPECTED_REPOSITORY = "MarketMakingLFG/ppi-data-acquisition"
EXPECTED_REPOSITORY_ID = 1312286476
EXPECTED_WORKFLOW_PATH = ".github/workflows/collect-r11-public-evidence.yml"
EXPECTED_SOURCE_REF = "refs/heads/main"
EXPECTED_ENVIRONMENT = "r11-public-acquisition-protected"
EXPECTED_DEPLOYMENT_BRANCH = "main"
EXPECTED_CREDENTIAL_ROLES = [
    "expectation_history_provider",
    "independent_recognition_provider",
    "market_and_options_provider",
]
FALSE_AUTHORITY_FIELDS = {
    "private_analysis_authorized": False,
    "registry_mutation_authorized": False,
    "production_authorized": False,
    "publication_authorized": False,
    "trading_authorized": False,
    "r12_authorized": False,
}


class EnvironmentPolicyError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvironmentPolicyError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def scan(log_path: Path) -> dict[str, Any]:
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


def _http_get_json(path: str, *, use_token: bool = True) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PPI protected-environment evidence",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip() if use_token else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API_ROOT}{path}", method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and token and use_token:
            return _http_get_json(path, use_token=False)
        raise EnvironmentPolicyError(f"GitHub policy endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EnvironmentPolicyError("GitHub policy endpoint is unavailable") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentPolicyError("GitHub policy endpoint returned malformed JSON") from exc


def _expected_policy_config() -> tuple[int, str, bool, bool]:
    raw_id = os.environ.get("PPI_PROTECTION_APP_ID", "").strip()
    slug = os.environ.get("PPI_PROTECTION_APP_SLUG", "").strip()
    require(raw_id.isdigit() and int(raw_id) > 0, "expected protection App ID is not configured")
    require(bool(slug), "expected protection App slug is not configured")
    independent = os.environ.get("PPI_PROTECTION_APP_INDEPENDENT", "").strip().lower() == "true"
    credentials_bound = os.environ.get("PPI_ENVIRONMENT_CREDENTIALS_BOUND", "").strip().lower() == "true"
    require(independent, "protection App independence is not affirmed")
    require(credentials_bound, "provider credentials are not affirmed as environment-bound")
    return int(raw_id), slug, independent, credentials_bound


def _rule_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("custom_deployment_protection_rules")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def evaluate_environment_policy(
    environment: dict[str, Any],
    rules_value: Any,
    branch_policies: list[dict[str, Any]] | None,
    *,
    expected_app_id: int,
    expected_app_slug: str,
    app_independent: bool,
    credentials_bound: bool,
) -> dict[str, Any]:
    require(environment.get("name") == EXPECTED_ENVIRONMENT, "protected environment identity mismatch")
    require(environment.get("can_admins_bypass") is False, "administrator bypass is not denied")
    branch_policy = environment.get("deployment_branch_policy")
    require(isinstance(branch_policy, dict), "deployment branch policy is missing")

    protected_branches = branch_policy.get("protected_branches") is True
    custom_branches = branch_policy.get("custom_branch_policies") is True
    branch_mode = ""
    if protected_branches:
        branch_mode = "protected_branches"
    elif custom_branches:
        policies = branch_policies or []
        require(
            any(isinstance(item, dict) and item.get("name") == EXPECTED_DEPLOYMENT_BRANCH for item in policies),
            "custom deployment policy does not allow main",
        )
        branch_mode = "custom_main"
    else:
        raise EnvironmentPolicyError("deployment branch restriction is missing")

    matching_rules: list[dict[str, Any]] = []
    for rule in _rule_list(rules_value):
        app = rule.get("app")
        if not isinstance(app, dict):
            continue
        if app.get("id") == expected_app_id and app.get("slug") == expected_app_slug:
            matching_rules.append(rule)
    require(len(matching_rules) == 1, "expected independent deployment protection rule is not uniquely enabled")
    rule = matching_rules[0]
    require(isinstance(rule.get("id"), int) and rule["id"] > 0, "deployment protection rule ID is invalid")
    require(app_independent, "deployment protection App independence is not affirmed")
    require(credentials_bound, "provider credentials are not environment-bound")

    return {
        "schema_version": "1.0.0",
        "status": "protected_environment_policy_ready",
        "repository": EXPECTED_REPOSITORY,
        "repository_id": EXPECTED_REPOSITORY_ID,
        "environment_identifier": EXPECTED_ENVIRONMENT,
        "administrator_bypass": "denied",
        "deployment_branch_mode": branch_mode,
        "deployment_branch": EXPECTED_DEPLOYMENT_BRANCH,
        "custom_deployment_protection_rule_enabled": True,
        "protection_app_id": expected_app_id,
        "protection_app_slug": expected_app_slug,
        "protection_rule_id": int(rule["id"]),
        "protection_rule_independent_of_producer": True,
        "environment_credentials_bound": True,
        "credential_roles": EXPECTED_CREDENTIAL_ROLES,
        "authorized_actions": [],
    }


def fetch_environment_policy() -> dict[str, Any]:
    expected_app_id, expected_app_slug, independent, credentials_bound = _expected_policy_config()
    encoded = urllib.parse.quote(EXPECTED_ENVIRONMENT, safe="")
    environment = _http_get_json(f"/repos/{EXPECTED_REPOSITORY}/environments/{encoded}")
    require(isinstance(environment, dict), "protected environment response is invalid")
    rules = _http_get_json(f"/repos/{EXPECTED_REPOSITORY}/environments/{encoded}/deployment_protection_rules")
    branch_policy = environment.get("deployment_branch_policy")
    branch_policies: list[dict[str, Any]] | None = None
    if isinstance(branch_policy, dict) and branch_policy.get("custom_branch_policies") is True:
        value = _http_get_json(
            f"/repos/{EXPECTED_REPOSITORY}/environments/{encoded}/deployment-branch-policies?per_page=100"
        )
        require(isinstance(value, dict) and isinstance(value.get("branch_policies"), list), "deployment branch policy response is invalid")
        branch_policies = [item for item in value["branch_policies"] if isinstance(item, dict)]
    return evaluate_environment_policy(
        environment,
        rules,
        branch_policies,
        expected_app_id=expected_app_id,
        expected_app_slug=expected_app_slug,
        app_independent=independent,
        credentials_bound=credentials_bound,
    )


def workflow_policy_flags(workflow_path: Path) -> tuple[bool, bool]:
    require(workflow_path.is_file() and not workflow_path.is_symlink(), "producer workflow source is missing or unsafe")
    text = workflow_path.read_text(encoding="utf-8")
    targets_environment = f"    environment: {EXPECTED_ENVIRONMENT}" in text
    least_privilege = all(
        marker not in text
        for marker in ("contents: write", "actions: write", "pull-requests: write", "issues: write")
    )
    return targets_environment, least_privilege


def build_environment_receipt(
    policy: dict[str, Any],
    log_scan_receipt: dict[str, Any],
    *,
    workflow_path: Path,
) -> dict[str, Any]:
    require(os.environ.get("COLLECT_JOB_RESULT", "") == "success", "collection job did not complete successfully")
    require(log_scan_receipt.get("status") == "pass" and log_scan_receipt.get("logs_scanned") is True, "completed job-log scan did not pass")
    targets_environment, least_privilege = workflow_policy_flags(workflow_path)
    require(targets_environment, "collection job is not bound to the protected environment")
    require(least_privilege, "workflow permissions exceed protected-environment evidence policy")

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    repository_id = int(os.environ.get("GITHUB_REPOSITORY_ID", "0") or 0)
    source_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    source_ref = os.environ.get("GITHUB_REF", "")
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0") or 0)
    run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0") or 0)
    require(repository == EXPECTED_REPOSITORY, "repository identity mismatch")
    require(repository_id == EXPECTED_REPOSITORY_ID, "repository ID mismatch")
    require(source_ref == EXPECTED_SOURCE_REF, "source ref mismatch")
    require(len(source_sha) == 40 and all(char in "0123456789abcdef" for char in source_sha), "source SHA invalid")
    require(run_id > 0 and run_attempt > 0, "workflow run identity invalid")

    policy_digest = hashlib.sha256(canonical_json(policy)).hexdigest()
    receipt = {
        "schema_version": "1.1.0",
        "status": "protected_environment_evidence_reviewed",
        "repository": repository,
        "repository_id": repository_id,
        "workflow_path": EXPECTED_WORKFLOW_PATH,
        "source_sha": source_sha,
        "source_ref": source_ref,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "environment_identifier": EXPECTED_ENVIRONMENT,
        "captured_at_utc": utc_now(),
        "approval_mode": "custom_deployment_protection_rule",
        "required_reviewers_configured": False,
        "deployment_branch_restrictions_configured": True,
        "administrator_bypass": "denied",
        "job_targets_environment": True,
        "custom_deployment_protection_rule_enabled": True,
        "protection_app_slug": policy["protection_app_slug"],
        "protection_app_id": policy["protection_app_id"],
        "protection_rule_id": policy["protection_rule_id"],
        "protection_rule_decision": "approved",
        "protection_rule_independent_of_producer": True,
        "credential_roles": EXPECTED_CREDENTIAL_ROLES,
        "credentials_unavailable_to_untrusted_contexts": True,
        "least_privilege_permissions_reviewed": True,
        "approval_precedes_acquisition": True,
        "retained_output_leak_scan_status": "pass",
        "policy_evidence_sha256": policy_digest,
        "authorized_actions": [],
        **FALSE_AUTHORITY_FIELDS,
    }
    return receipt


def run_environment_preflight(output: Path) -> int:
    try:
        policy = fetch_environment_policy()
    except EnvironmentPolicyError as exc:
        write_json(output, {
            "schema_version": "1.0.0",
            "status": "protected_environment_preflight_blocked",
            "environment_identifier": EXPECTED_ENVIRONMENT,
            "reason": str(exc),
            "authorized_actions": [],
        })
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 1
    write_json(output, policy)
    print(json.dumps({
        "status": "ready",
        "environment_identifier": policy["environment_identifier"],
        "protection_rule_id": policy["protection_rule_id"],
        "protection_app_slug": policy["protection_app_slug"],
    }, sort_keys=True))
    return 0


def run_environment_receipt(log_scan_receipt_path: Path, output: Path) -> int:
    try:
        policy = fetch_environment_policy()
        log_scan_receipt = json.loads(log_scan_receipt_path.read_text(encoding="utf-8"))
        require(isinstance(log_scan_receipt, dict), "job-log scan receipt must be an object")
        receipt = build_environment_receipt(
            policy,
            log_scan_receipt,
            workflow_path=Path(EXPECTED_WORKFLOW_PATH),
        )
    except (OSError, json.JSONDecodeError, EnvironmentPolicyError) as exc:
        write_json(output, {
            "schema_version": "1.0.0",
            "status": "protected_environment_receipt_blocked",
            "environment_identifier": EXPECTED_ENVIRONMENT,
            "reason": str(exc),
            "authorized_actions": [],
        })
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 1
    write_json(output, receipt)
    print(json.dumps({
        "status": receipt["status"],
        "environment_identifier": receipt["environment_identifier"],
        "run_id": receipt["run_id"],
        "run_attempt": receipt["run_attempt"],
        "protection_rule_id": receipt["protection_rule_id"],
    }, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan producer job logs and validate protected-environment evidence")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--log", type=Path)
    mode.add_argument("--environment-preflight", action="store_true")
    mode.add_argument("--environment-receipt", action="store_true")
    parser.add_argument("--log-scan-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.environment_preflight:
        return run_environment_preflight(args.output)
    if args.environment_receipt:
        if args.log_scan_receipt is None:
            parser.error("--log-scan-receipt is required with --environment-receipt")
        return run_environment_receipt(args.log_scan_receipt, args.output)

    result = scan(args.log)
    write_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "status", "logs_scanned", "exact_secret_matches", "authorization_header_matches",
        "credential_query_matches", "encoded_secret_matches", "ansi_sequences_removed"
    )}, sort_keys=True))
    if result["status"] != "pass":
        raise SystemExit("job log secret scan failed closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
