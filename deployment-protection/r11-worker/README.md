# R11 independent deployment-protection service

This service implements the independent GitHub App deployment-protection rule for `r11-public-acquisition-protected`.

Normal operation is designed for **zero routine human intervention**. The service does not approve unconditionally: it automatically approves only when the signed GitHub webhook and current GitHub run state satisfy the fixed R11 policy. Any missing, stale, mismatched, or unverifiable evidence fails closed.

## Autonomous policy

Policy identifier: `r11-autonomous-v1`.

A request can be approved only when all of the following are true:

- GitHub webhook HMAC-SHA256 verification succeeds before JSON parsing.
- Event is `deployment_protection_rule` with action `requested`.
- Repository is exactly `MarketMakingLFG/ppi-data-acquisition` and is private.
- Environment is exactly `r11-public-acquisition-protected`.
- Trigger is exactly `workflow_dispatch`.
- Ref is exactly `refs/heads/main`.
- Payload SHA is a valid 40-character commit SHA and matches the workflow run.
- Workflow run repository, branch, event, attempt, and SHA match the webhook payload.
- Workflow path is exactly `.github/workflows/collect-r11-public-evidence.yml` (GitHub's `@ref` suffix is accepted).
- GitHub App ID matches the configured App when the webhook payload supplies App identity.
- The service successfully creates a GitHub App JWT and installation access token.
- A fresh live copy of the workflow run is fetched from GitHub and still matches the payload.
- The run is not concluded and is not older than the configured freshness window (default 3600 seconds).
- GitHub's pending-deployments API confirms that `r11-public-acquisition-protected` is still waiting for protection.

Only after all of those checks pass does the service send `state: approved` to GitHub.

Policy mismatches discovered after authentication are actively rejected. GitHub authentication/API failures return HTTP 503 and leave the protection pending rather than turning uncertainty into approval.

## Replay and stale-request handling

The service uses the GitHub delivery ID for in-process duplicate suppression. More importantly, each decision re-reads the live workflow run and GitHub pending-deployment state. A redelivery after the environment is no longer pending cannot create a new approval. Current run attempt, SHA, workflow path, branch, event, conclusion, and age must all remain valid.

The GitHub deployment-protection review itself is the durable decision record. Render also emits a structured decision log containing only non-secret identifiers and policy result metadata.

## Hosting

Render service:

- Name: `ppi-r11-independent-protection`
- Webhook endpoint: `https://ppi-r11-independent-protection.onrender.com/github/webhook`
- Health endpoint: `/healthz`
- Runtime: Node.js
- Build validation: `npm test`
- Start command: `npm start`

Required Render secrets:

- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_WEBHOOK_SECRET`

Optional policy setting:

- `PPI_MAX_RUN_AGE_SECONDS` — default `3600`, allowed range 60–86400 seconds.

Secret values must never be committed, printed, or copied into documentation.

## Verified evidence so far

Before autonomous-policy implementation, the Render service successfully built with 6/6 reject-only tests, and a signed GitHub App `ping` redelivery reached the live Render endpoint and returned HTTP 200 with `{"ignored":true}`. Because signature verification happens before event dispatch, that demonstrated the GitHub-to-Render webhook secret/signature path.

The autonomous-policy implementation adds live GitHub run verification, pending-deployment verification, automatic approval, active rejection, stale-run checks, replay handling, and structured audit logging. Unit and deployment evidence for the exact autonomous-policy commit must pass before a provider pilot is considered valid.

A provider pilot is the required live integration test for the remaining path: `deployment_protection_rule` webhook → App JWT → installation token → live policy reads → deployment-protection approval → protected job start.

## Safety invariants

- No protection bypass is used.
- No approval is issued from an unsigned, stale, mismatched, or unverifiable request.
- No producer provider secret is sent to or stored by this service.
- GitHub API/authentication uncertainty remains pending/fail-closed.
- Registry mutation occurs only after the corresponding evidence gate passes.
