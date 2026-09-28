# R11 independent deployment-protection Worker (draft)

This Cloudflare Worker is an **intentionally reject-only, fail-closed** starting point for the existing GitHub App `ppi-r11-independent-protection`. It is **not** an operational approval service and must not be treated as an R2 PASS or pilot-ready gate. It cannot approve any deployment.

## Deployment (not performed)

Create a Cloudflare Worker under an account controlled independently from the producer repository. Upload `worker.mjs` as its module entry point. Set encrypted Worker secrets `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_PRIVATE_KEY` (PKCS#8 PEM), and `GITHUB_APP_ID`. Do not put these values in GitHub commits, chat, or logs. Configure the GitHub App webhook URL as `https://<your-worker-host>/github/webhook`, using the matching webhook secret, and subscribe to deployment protection rule events. Install the App on `MarketMakingLFG/ppi-data-acquisition` with the required Actions/Deployments permissions. Enable and save its custom deployment protection rule on environment `r11-public-acquisition-protected`; restrict branches to `main` and disable admin bypass.

The webhook checks the raw-body HMAC-SHA256 signature, repository, event type/action, workflow run branch, installation, and environment before requesting an installation token and sending a **rejection** via GitHub's deployment protection review endpoint. Invalid events cannot trigger a review. API failures return 503; GitHub must retain pending protection rather than treat an HTTP response as an approval.

## Before any real approval path

Implement independent, authenticated human approval with a durable audit trail and strict run/attempt/environment binding, replay protection, expiration, separation from producer-controlled secrets and workflows, and explicit denial behavior. Have an independent reviewer security-review and exercise approval, rejection, invalid-signature, replay, stale-decision, wrong-repo, wrong-branch, and GitHub API failure tests. Confirm actual GitHub environment policy and immutable successful pilot receipts separately. Never replace rejection with unconditional approval.

No deployment, GitHub App configuration, secret creation, pilot, registry write, or merge is performed by this PR.
