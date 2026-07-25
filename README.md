# PPI Data Acquisition

This public repository is the long-running acquisition execution plane for PPI evidence.

## Operating rule

All provider retrieval, pagination, batching, retries, rate-limit handling, objective validation, sanitization, hashing, packaging, artifact upload, retention, attestation, and failure recovery run here on public GitHub Actions.

The private repository receives one completed immutable artifact and performs only final semantic curation, analytical normalization, private calculations, scoring, independent countability, final reporting, and a separate short append-only registry-governance review.

## Stable identity

- Repository: `spoudel2010-ux/ppi-data-acquisition`
- Repository ID: `1312286476`
- Required visibility: public
- Required default branch: `main`

Trust checks bind both the repository name and numeric ID.

## Three-repository boundary

### `poudlesuman32-star/ai-market-news`

Public source intelligence and reusable code:

- provider and source definitions;
- endpoint, field, ticker-alias, and category mappings;
- schemas, adapters, fixtures, sanitization policy, and licensing classifications;
- source-health and compatibility checks;
- versioned public collector releases and contracts.

### `spoudel2010-ux/ppi-data-acquisition`

Public provider execution:

- provider requests, pagination, batching, bounded retries, backoff, and rate-limit handling;
- complete frozen-cohort retrieval;
- licensing-safe payload handling;
- deterministic serialization and canonical timestamps;
- category-specific objective validation;
- response, bundle, manifest, package, and receipt hashes;
- separate success and failure artifacts;
- attestations, retention, cleanup, and all recollection.

### `musksuman3/ai-signal-engine`

Private final analysis only:

1. Verify one exact public artifact and its provenance.
2. Safely extract and revalidate every file hash.
3. Perform final semantic curation.
4. Perform analytical normalization.
5. Calculate private derived features.
6. Score the cumulative cohort.
7. Determine independent countability.
8. Produce one final private report.
9. Use a separate short governance review for an append-only registry proposal.

The private repository must never call providers, retry retrieval, recollect stale evidence, rebuild public artifacts, or start broad workflow fan-out.

## Frozen contract lineage

Use only these Version 1 identities:

- Private analytical contract: `PPI-R11-BATCH-EVIDENCE-003-R1`
- Public acquisition contract: `PPI-R11-PUBLIC-ACQUISITION-003-R1`
- Public collector release: `PPI-PUBLIC-COLLECTOR-003-R1`

Do not mix these identities with unversioned contract names. Any schema, provider operation, ticker scope, category, or collector change requires a new revision.

## Batch-3 scope

Cumulative tickers:

`AAPL, MU, NVDA, AMD, AVGO, INTC, TSM, ARM, QCOM, MRVL, GFS, TXN`

New candidates:

`QCOM, MRVL, GFS, TXN`

Required categories:

- expectation history;
- independent recognition;
- market time series; and
- specialized contract data.

All twelve tickers require fresh current-batch evidence. Historical accepted evidence remains immutable but does not substitute for current cumulative scoring input.

## Exact success package

A successful batch-3 artifact contains exactly 50 retained paths:

- 48 evidence bundles;
- one cumulative twelve-ticker manifest; and
- one immutable collection receipt.

Optional files are not permitted in the success package. Request correlation belongs inside the receipt.

The receipt binds repository and workflow identity, run ID and attempt, approved head SHA, artifact ID and digest, package hash, collector release, contract hashes, queue receipt, manifest hash, collection timestamps, provider request/retry/failure counts, licensing dispositions, exact path count, and `authorized_actions: []`.

## Provider licensing and storage

Every provider operation requires one frozen disposition before collection:

- `raw_public_allowed`
- `sanitized_public_allowed`
- `hash_and_metadata_only`
- `encrypted_private_handoff`
- `public_storage_prohibited`

A payload must not be uploaded publicly merely because collection succeeded. Unsupported licensing or transfer terms fail closed or require a reviewed contract revision.

## Objective validation

Public checks are deterministic and do not decide semantic approval, scoring, or countability.

Required controls include:

- exact ticker and provider-operation match;
- category-specific required fields;
- per-request start, response, and provider-event timestamps;
- future-date rejection;
- numeric and structural validation;
- complete twelve-ticker/four-category coverage;
- valid-empty status separated from provider failure;
- deterministic duplicate identity;
- exact file, manifest, and package hashes;
- credential-leak scanning.

## Retry and failure policy

Preferred execution uses three four-ticker shards and one public aggregator.

Retries are limited to transient network errors, HTTP 429, and reviewed provider 5xx responses. They honor `Retry-After`, use bounded exponential backoff with jitter, and never substitute providers.

A failed or partial run produces a separately named failure-diagnostics artifact. It must never use the success artifact identity and can never enter private analysis.

## Workflow security

Provider-bearing jobs must:

- run only from protected `main` or an approved immutable tag;
- check out the exact approved ref with persisted credentials disabled;
- use least-privilege permissions;
- use a protected GitHub environment for provider credentials when available;
- expose no provider secrets to pull-request jobs;
- pin third-party actions to full commit SHAs;
- scan logs and retained files for secrets and authorization data;
- generate a provenance attestation for the final package when supported.

The temporary cross-repository bootstrap token must be rotated or removed after repository setup is complete.

## Private handoff requirements

Private analysis may begin only when the public run is completed/successful and the exact success artifact passes repository, workflow, run, attempt, branch, SHA, artifact, digest, package, contract, queue, manifest, freshness, size, path-count, and attestation checks.

Private extraction must reject absolute paths, traversal, links, duplicate entries, unexpected paths, excessive expansion, and oversized files.

The final private analysis stage must have no provider credentials, no GitHub token, and no external network.

## Atomic credit

Batch-3 registry credit is atomic for `QCOM, MRVL, GFS, TXN` under revision R1.

- Missing or invalid bundle: no private run.
- One new ticker fails curation or countability: no batch-3 credit.
- Three of four new tickers pass: no partial credit.
- A prior approved ticker fails the current cumulative score: batch 3 does not count, while historical approval remains unchanged.
- Duplicate artifact plus analysis identity: deterministic no-op.

## Scheduling policy

The initial collector is manual-only. Public completion does not automatically start private analysis. Private schedules remain paused during migration.

Reviewed public schedules may be added only after licensing, retry, sharding, success/failure separation, retention, attestation, and private trust-gate controls are proven.

## Prohibited authority

This repository may not perform final semantic approval, analytical normalization, private feature calculations, scoring, countability, ticker approval, registry mutation, production activation, publication, broker access, orders, trading, MMM/raw-data writes, or R12 authorization.

## Merge gate for bootstrap PR #1

Do not merge the bootstrap PR until:

- the hardened README and `-R1` contract lineage are present;
- exact 50-path success output is enforced;
- provider licensing dispositions are frozen;
- success and failure artifact identities are separated;
- category-specific checks and per-request timestamps exist;
- protected-ref secret handling is implemented;
- third-party actions are SHA-pinned;
- package size, retention, cleanup, and attestation rules are implemented.

## Canonical architecture document

The complete validated plan is maintained at:

`poudlesuman32-star/ai-market-news/docs/architecture/PPI_THREE_REPOSITORY_ARCHITECTURE_AND_MIGRATION_PLAN.md`
