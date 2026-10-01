const EXPECTED_REPO = "MarketMakingLFG/ppi-data-acquisition";
const EXPECTED_REPO_ID = 1312286476;
const EXPECTED_ENV = "r11-public-acquisition-protected";
const EXPECTED_WORKFLOW = ".github/workflows/collect-r11-public-evidence.yml";
const EXPECTED_REF = "refs/heads/main";
const DEFAULT_MAX_RUN_AGE_SECONDS = 3600;
const encoder = new TextEncoder();
const inFlight = new Set();
const completedDeliveries = new Map();

class PolicyError extends Error {
  constructor(code) { super(code); this.code = code; }
}

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { "content-type": "application/json" }
});
const b64url = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
const requirePolicy = (condition, code) => { if (!condition) throw new PolicyError(code); };

async function verifySignature(raw, signature, secret) {
  if (!secret || !/^sha256=[0-9a-f]{64}$/i.test(signature || "")) return false;
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]
  );
  const hex = signature.slice(7);
  const expected = new Uint8Array(hex.match(/../g).map(x => parseInt(x, 16)));
  return crypto.subtle.verify("HMAC", key, expected, raw);
}

async function appJwt(appId, pem) {
  requirePolicy(/^\d+$/.test(String(appId || "")) && Number(appId) > 0, "app_id_missing");
  requirePolicy(typeof pem === "string" && pem.includes("BEGIN PRIVATE KEY"), "app_key_missing");
  const der = Uint8Array.from(
    atob(pem.replace(/-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g, "")),
    c => c.charCodeAt(0)
  );
  const key = await crypto.subtle.importKey(
    "pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]
  );
  const now = Math.floor(Date.now() / 1000);
  const payload = b64url(encoder.encode(JSON.stringify({
    iat: now - 60,
    exp: now + 540,
    iss: String(appId)
  })));
  const header = b64url(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const message = header + "." + payload;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(message));
  return message + "." + b64url(sig);
}

async function github(url, token, method = "GET", body) {
  const response = await fetch("https://api.github.com" + url, {
    method,
    headers: {
      authorization: "Bearer " + token,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "ppi-r11-independent-protection",
      ...(body ? { "content-type": "application/json" } : {})
    },
    ...(body ? { body: JSON.stringify(body) } : {})
  });
  if (!response.ok) throw new Error("GitHub API status " + response.status);
  if (response.status === 204) return null;
  return response.json();
}

function workflowPathMatches(path) {
  return path === EXPECTED_WORKFLOW || (
    typeof path === "string" && path.startsWith(EXPECTED_WORKFLOW + "@")
  );
}

function routingIdentity(payload) {
  const run = payload?.workflow_run;
  requirePolicy(payload?.repository?.full_name === EXPECTED_REPO, "wrong_repository");
  requirePolicy(payload?.repository?.private === true, "repository_not_private");
  requirePolicy(payload?.environment === EXPECTED_ENV, "wrong_environment");
  requirePolicy(Number.isSafeInteger(run?.id) && run.id > 0, "run_id_invalid");
  requirePolicy(Number.isSafeInteger(payload?.installation?.id) && payload.installation.id > 0, "installation_id_invalid");
  requirePolicy(
    Number.isSafeInteger(payload?.deployment_protection_rule?.id) && payload.deployment_protection_rule.id > 0,
    "protection_rule_id_invalid"
  );
}

function payloadPolicy(payload, deliveryId, env) {
  const run = payload.workflow_run;
  requirePolicy(typeof deliveryId === "string" && deliveryId.length >= 8 && deliveryId.length <= 200, "delivery_id_invalid");
  requirePolicy(payload.action === "requested", "action_invalid");
  requirePolicy(payload.event === "workflow_dispatch", "trigger_event_invalid");
  requirePolicy(payload.ref === EXPECTED_REF, "ref_invalid");
  requirePolicy(/^[0-9a-f]{40}$/i.test(payload.sha || ""), "sha_invalid");
  requirePolicy(run.head_branch === "main", "branch_invalid");
  requirePolicy(run.repository?.full_name === EXPECTED_REPO, "run_repository_invalid");
  requirePolicy(run.event === "workflow_dispatch", "run_event_invalid");
  requirePolicy(Number.isSafeInteger(run.run_attempt) && run.run_attempt > 0, "run_attempt_invalid");
  requirePolicy((run.head_sha || "").toLowerCase() === payload.sha.toLowerCase(), "payload_sha_mismatch");
  if (run.path != null) requirePolicy(workflowPathMatches(run.path), "payload_workflow_invalid");
  const configuredAppId = Number(env.GITHUB_APP_ID);
  requirePolicy(Number.isSafeInteger(configuredAppId) && configuredAppId > 0, "app_id_missing");
  const app = payload.deployment_protection_rule?.app;
  if (app?.id != null) requirePolicy(app.id === configuredAppId, "protection_app_mismatch");
}

function livePolicy(payload, currentRun, pendingDeployments, env) {
  const run = payload.workflow_run;
  requirePolicy(currentRun?.id === run.id, "live_run_id_mismatch");
  requirePolicy(currentRun?.repository?.full_name === EXPECTED_REPO, "live_repository_mismatch");
  requirePolicy(currentRun?.repository?.id === EXPECTED_REPO_ID, "live_repository_id_mismatch");
  requirePolicy(currentRun?.head_branch === "main", "live_branch_invalid");
  requirePolicy(currentRun?.event === "workflow_dispatch", "live_event_invalid");
  requirePolicy((currentRun?.head_sha || "").toLowerCase() === payload.sha.toLowerCase(), "live_sha_mismatch");
  requirePolicy(currentRun?.run_attempt === run.run_attempt, "live_attempt_mismatch");
  requirePolicy(workflowPathMatches(currentRun?.path), "live_workflow_invalid");
  requirePolicy(currentRun?.conclusion == null, "run_already_concluded");
  const createdMs = Date.parse(currentRun?.created_at || "");
  requirePolicy(Number.isFinite(createdMs), "run_created_at_invalid");
  const maxAge = Number(env.PPI_MAX_RUN_AGE_SECONDS || DEFAULT_MAX_RUN_AGE_SECONDS);
  requirePolicy(Number.isFinite(maxAge) && maxAge >= 60 && maxAge <= 86400, "max_age_invalid");
  const ageSeconds = (Date.now() - createdMs) / 1000;
  requirePolicy(ageSeconds >= -300 && ageSeconds <= maxAge, "run_stale");
  requirePolicy(Array.isArray(pendingDeployments), "pending_deployments_invalid");
  const pending = pendingDeployments.find(item => item?.environment?.name === EXPECTED_ENV);
  requirePolicy(Boolean(pending), "environment_not_pending");
  return { ageSeconds: Math.floor(ageSeconds) };
}

async function review(token, runId, state, comment) {
  return github(
    "/repos/" + EXPECTED_REPO + "/actions/runs/" + runId + "/deployment_protection_rule",
    token,
    "POST",
    { environment_name: EXPECTED_ENV, state, comment }
  );
}

function audit(decision, payload, deliveryId, extra = {}) {
  console.log(JSON.stringify({
    type: "r11_deployment_protection_decision",
    decision,
    delivery_id: deliveryId,
    repository: EXPECTED_REPO,
    environment: EXPECTED_ENV,
    run_id: payload?.workflow_run?.id || null,
    run_attempt: payload?.workflow_run?.run_attempt || null,
    head_sha: payload?.sha || null,
    ...extra
  }));
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/github/webhook")
      return json({ error: "not found" }, 404);

    const raw = await request.arrayBuffer();
    if (raw.byteLength > 1024 * 1024) return json({ error: "too large" }, 413);
    if (!await verifySignature(raw, request.headers.get("x-hub-signature-256"), env.GITHUB_WEBHOOK_SECRET))
      return json({ error: "unauthorized" }, 401);
    if (request.headers.get("x-github-event") !== "deployment_protection_rule")
      return json({ ignored: true });

    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(raw)); }
    catch { return json({ error: "bad JSON" }, 400); }

    if (payload.action !== "requested") return json({ ignored: true });

    const deliveryId = request.headers.get("x-github-delivery") || "";
    try {
      routingIdentity(payload);
      payloadPolicy(payload, deliveryId, env);
    } catch (error) {
      const reason = error instanceof PolicyError ? error.code : "payload_policy_error";
      audit("blocked", payload, deliveryId, { reason });
      return json({ error: "policy blocked", reason }, 403);
    }

    if (completedDeliveries.has(deliveryId))
      return json({ handled: true, ...completedDeliveries.get(deliveryId), replay: true });
    if (inFlight.has(deliveryId))
      return json({ handled: true, decision: "processing", replay: true }, 202);

    inFlight.add(deliveryId);
    try {
      const jwt = await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY);
      const auth = await github(
        "/app/installations/" + payload.installation.id + "/access_tokens",
        jwt,
        "POST"
      );
      requirePolicy(typeof auth?.token === "string" && auth.token.length > 20, "installation_token_invalid");

      const runId = payload.workflow_run.id;
      const currentRun = await github("/repos/" + EXPECTED_REPO + "/actions/runs/" + runId, auth.token);
      const pending = await github(
        "/repos/" + EXPECTED_REPO + "/actions/runs/" + runId + "/pending_deployments",
        auth.token
      );

      let live;
      try {
        live = livePolicy(payload, currentRun, pending, env);
      } catch (error) {
        const reason = error instanceof PolicyError ? error.code : "live_policy_error";
        if (reason === "environment_not_pending") {
          const result = { decision: "ignored", reason };
          completedDeliveries.set(deliveryId, result);
          audit("ignored", payload, deliveryId, { reason });
          return json({ handled: true, ...result });
        }
        await review(
          auth.token,
          runId,
          "rejected",
          "R11 autonomous policy rejected this request: " + reason + "."
        );
        const result = { decision: "rejected", reason };
        completedDeliveries.set(deliveryId, result);
        audit("rejected", payload, deliveryId, { reason });
        return json({ handled: true, ...result });
      }

      await review(
        auth.token,
        runId,
        "approved",
        "R11 autonomous policy approved: signed webhook, exact repo/environment/main/workflow identity, current run/attempt/SHA, freshness, and pending deployment verified. delivery=" + deliveryId
      );
      const result = { decision: "approved", policy: "r11-autonomous-v1" };
      completedDeliveries.set(deliveryId, result);
      audit("approved", payload, deliveryId, { policy: result.policy, run_age_seconds: live.ageSeconds });
      return json({ handled: true, ...result });
    } catch (error) {
      const reason = error instanceof PolicyError ? error.code : "github_api_or_auth_failure";
      audit("pending", payload, deliveryId, { reason });
      return json({ error: "review delivery failed", reason }, 503);
    } finally {
      inFlight.delete(deliveryId);
    }
  }
};
