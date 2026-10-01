# R11 independent deployment-protection service (draft)

This service is an **intentionally reject-only, fail-closed** implementation for the GitHub App `ppi-r11-independent-protection`. It is **not** an operational approval service and must not be treated as an R2 PASS or pilot-ready gate. It cannot approve any deployment.

## Current hosting and live evidence

The draft is hosted independently on Render as `ppi-r11-independent-protection` at `https://ppi-r11-independent-protection.onrender.com`. The webhook endpoint is `/github/webhook`. Runtime configuration is supplied only through Render environment secrets: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_WEBHOOK_SECRET`; secret values must never be committed or logged.

On 2026-10-01 UTC, the Render build for commit `fa068f02e5152d1d07334598b9544db761857bf0` completed successfully with **6/6 unit tests passing**. A GitHub App `ping` redelivery then reached the live Render endpoint and returned HTTP **200** with body `{"ignored":true}`. Because signature verification occurs before event dispatch, this demonstrates that the configured GitHub webhook secret matched the Render secret for that signed delivery and that GitHub-to-Render HTTPS delivery reached the application.

This evidence does **not** prove the deployment-protection review path. No live `deployment_protection_rule` event has yet demonstrated App JWT creation, installation-token exchange, or a GitHub rejection API call.

## Behavior

The webhook verifies the raw-body HMAC-SHA256 signature before parsing or acting. It checks event type/action, repository, private-repository identity, `main` workflow branch, installation ID, deployment-protection rule ID, run ID, and environment `r11-public-acquisition-protected`. For a matching deployment-protection request it obtains a GitHub App installation token and sends **rejection only** through GitHub's deployment-protection review endpoint.

Invalid events cannot trigger a review. GitHub API failures return 503. A transport-level HTTP success is never treated as deployment approval.

## Render deployment

The repository root `package.json` runs `npm test` for build validation and `npm start` for the Node HTTP adapter in `server.mjs`. The server binds to Render's `PORT`, exposes `/healthz`, and forwards webhook requests to `worker.mjs`.

The GitHub App webhook URL is:

`https://ppi-r11-independent-protection.onrender.com/github/webhook`

The App is intended to be installed only on `MarketMakingLFG/ppi-data-acquisition` with the required Actions/Deployments permissions. The protected environment remains `r11-public-acquisition-protected`, restricted to `main`, with admin bypass disabled and the custom deployment-protection rule enabled.

## Before any real approval path

Do not add unconditional or unattended approval. Before an approval-capable path is enabled, implement an independent authenticated approval authority, durable audit trail, strict run/attempt/environment binding, replay protection/idempotency, expiration and stale-decision handling, separation from producer-controlled credentials/workflows, and explicit denial behavior. Security tests must cover approval authorization, rejection, invalid signatures, replay, stale decisions, wrong repository/branch/environment, and GitHub API failures.

A live `deployment_protection_rule` receipt and immutable pilot evidence are still required before claiming the protection gate closed. Producer pilot execution, registry mutation, protection bypass, and merge remain outside this draft's evidence.

## Local unit checks

From the repository root:

`node --test deployment-protection/r11-worker/worker.test.mjs`

Use Node.js 20 or later. The tests mock GitHub and exercise invalid signatures, identity mismatches, reject-only decisions, and API failures. Unit tests are not a substitute for live GitHub App integration evidence.
