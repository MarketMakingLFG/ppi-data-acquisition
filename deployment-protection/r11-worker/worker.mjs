import { createPrivateKey, sign as nodeSign } from "node:crypto";

const EXPECTED_REPO = "MarketMakingLFG/ppi-data-acquisition";
const EXPECTED_OWNER = "MarketMakingLFG";
const EXPECTED_REPO_ID = 1312286476;
const EXPECTED_ENV = "r11-public-acquisition-protected";
const EXPECTED_APP_SLUG = "ppi-r11-independent-protection";
const EXPECTED_WORKFLOW = ".github/workflows/collect-r11-public-evidence.yml";
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
  requirePolicy(
    typeof pem === "string" &&
      (pem.includes("BEGIN PRIVATE KEY") || pem.includes("BEGIN RSA PRIVATE KEY")),
    "app_key_missing"
  );
  let key;
  try { key = createPrivateKey(pem); }
  catch { throw new PolicyError("app_key_invalid"); }
  const now = Math.floor(Date.now() / 1000);
  const payload = b64url(encoder.encode(JSON.stringify({
    iat: now - 60, exp: now + 540, iss: String(appId)
  })));
  const header = b64url(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const message = header + "." + payload;
  const signature = nodeSign("RSA-SHA256", Buffer.from(message), key);
  return message + "." + b64url(signature);
}

async function github(path, token, method = "GET", body) {
  const response = await fetch("https://api.github.com" + path, {
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
  if (!response.ok) {
    throw new Error("GitHub API status " + response.status + " for " + path.split("?")[0]);
  }
  if (response.status === 204) return null;
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

function parseCallback(payload) {
  requirePolicy(typeof payload?.deployment_callback_url === "string", "callback_missing");
  let url;
  try { url = new URL(payload.deployment_callback_url); }
  catch { throw new PolicyError("callback_invalid"); }
  requirePolicy(url.protocol === "https:" && url.hostname === "api.github.com", "callback_origin_invalid");
  requirePolicy(url.search === "" && url.hash === "", "callback_query_invalid");
  const expected = /^\/repos\/MarketMakingLFG\/ppi-data-acquisition\/actions\/runs\/(\d+)\/deployment_protection_rule$/;
  const match = url.pathname.match(expected);
  requirePolicy(Boolean(match), "callback_path_invalid");
  const runId = Number(match[1]);
  requirePolicy(Number.isSafeInteger(runId) && runId > 0, "run_id_invalid");
  return { runId, callbackPath: url.pathname };
}

function routingPolicy(payload, deliveryId, env) {
  requirePolicy(typeof deliveryId === "string" && deliveryId.length >= 8 && deliveryId.length <= 200, "delivery_id_invalid");
  requirePolicy(payload?.action === "requested", "action_invalid");
  requirePolicy(payload?.repository?.full_name === EXPECTED_REPO, "wrong_repository");
  requirePolicy(payload?.repository?.id === EXPECTED_REPO_ID, "wrong_repository_id");
  requirePolicy(payload?.environment === EXPECTED_ENV, "wrong_environment");
  requirePolicy(payload?.event === "workflow_dispatch", "trigger_event_invalid");
  requirePolicy(payload?.ref === "main" || payload?.ref === "refs/heads/main", "ref_invalid");
  requirePolicy(/^[0-9a-f]{40}$/i.test(payload?.sha || ""), "sha_invalid");
  requirePolicy(Number.isSafeInteger(payload?.installation?.id) && payload.installation.id > 0, "installation_id_invalid");
  const configuredAppId = Number(env.GITHUB_APP_ID);
  requirePolicy(Number.isSafeInteger(configuredAppId) && configuredAppId > 0, "app_id_missing");
  return parseCallback(payload);
}

function workflowPathMatches(path) {
  return path === EXPECTED_WORKFLOW || (
    typeof path === "string" && path.startsWith(EXPECTED_WORKFLOW + "@")
  );
}

async function installationToken(jwt, installationId) {
  const auth = await github(
    "/app/installations/" + installationId + "/access_tokens",
    jwt,
    "POST",
    {
      repository_ids: [EXPECTED_REPO_ID],
      permissions: { actions: "read", deployments: "write" }
    }
  );
  requirePolicy(typeof auth?.token === "string" && auth.token.length > 20, "installation_token_invalid");
  return auth.token;
}

async function controlPlaneSnapshot(token) {
  const encodedEnv = encodeURIComponent(EXPECTED_ENV);
  const [environment, customRules, branchPolicies] = await Promise.all([
    github("/repos/" + EXPECTED_REPO + "/environments/" + encodedEnv, token),
    github("/repos/" + EXPECTED_REPO + "/environments/" + encodedEnv + "/deployment_protection_rules?per_page=100", token),
    github("/repos/" + EXPECTED_REPO + "/environments/" + encodedEnv + "/deployment-branch-policies?per_page=100", token)
  ]);
  return { environment, customRules, branchPolicies };
}

function verifyControlPlane(snapshot, env) {
  const { environment, customRules, branchPolicies } = snapshot;
  requirePolicy(environment?.name === EXPECTED_ENV, "environment_identity_mismatch");
  requirePolicy(environment?.can_admins_bypass === false, "admin_bypass_not_disabled");
  requirePolicy(environment?.deployment_branch_policy?.protected_branches === false, "protected_branches_mode_invalid");
  requirePolicy(environment?.deployment_branch_policy?.custom_branch_policies === true, "custom_branch_policy_not_enabled");

  const rules = customRules?.custom_deployment_protection_rules;
  requirePolicy(Array.isArray(rules), "custom_rules_invalid");
  requirePolicy(customRules?.total_count === 1 && rules.length === 1, "custom_rule_count_invalid");
  const rule = rules[0];
  requirePolicy(rule?.enabled === true, "custom_rule_not_enabled");
  requirePolicy(rule?.app?.id === Number(env.GITHUB_APP_ID), "custom_rule_app_id_mismatch");
  requirePolicy(rule?.app?.slug === EXPECTED_APP_SLUG, "custom_rule_app_slug_mismatch");

  const policies = branchPolicies?.branch_policies;
  requirePolicy(Array.isArray(policies), "branch_policies_invalid");
  requirePolicy(branchPolicies?.total_count === 1 && policies.length === 1, "branch_policy_count_invalid");
  requirePolicy(policies[0]?.name === "main", "branch_policy_not_main_only");

  return {
    protectionRuleId: rule.id,
    branchPolicyId: policies[0].id
  };
}

function livePolicy(payload, runId, currentRun, pendingDeployments, env) {
  requirePolicy(currentRun?.id === runId, "live_run_id_mismatch");
  requirePolicy(currentRun?.repository?.full_name === EXPECTED_REPO, "live_repository_mismatch");
  requirePolicy(currentRun?.repository?.id === EXPECTED_REPO_ID, "live_repository_id_mismatch");
  requirePolicy(currentRun?.head_branch === "main", "live_branch_invalid");
  requirePolicy(currentRun?.event === "workflow_dispatch", "live_event_invalid");
  requirePolicy((currentRun?.head_sha || "").toLowerCase() === payload.sha.toLowerCase(), "live_sha_mismatch");
  requirePolicy(Number.isSafeInteger(currentRun?.run_attempt) && currentRun.run_attempt > 0, "live_attempt_invalid");
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

  return { ageSeconds: Math.floor(ageSeconds), runAttempt: currentRun.run_attempt };
}

async function review(token, callbackPath, state, comment) {
  return github(callbackPath, token, "POST", {
    environment_name: EXPECTED_ENV,
    state,
    comment
  });
}

function audit(decision, payload, deliveryId, extra = {}) {
  console.log(JSON.stringify({
    type: "r11_deployment_protection_decision",
    policy: "r11-autonomous-v3",
    decision,
    delivery_id: deliveryId,
    repository: EXPECTED_REPO,
    environment: EXPECTED_ENV,
    head_sha: payload?.sha || null,
    ...extra
  }));
}

export async function selfTest(env) {
  const jwt = await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY);
  const app = await github("/app", jwt);
  requirePolicy(app?.id === Number(env.GITHUB_APP_ID), "app_identity_mismatch");
  requirePolicy(app?.slug === EXPECTED_APP_SLUG, "app_slug_mismatch");

  const installations = await github("/app/installations?per_page=100", jwt);
  requirePolicy(Array.isArray(installations), "installations_invalid");
  const installation = installations.find(item => item?.account?.login === EXPECTED_OWNER);
  requirePolicy(Number.isSafeInteger(installation?.id) && installation.id > 0, "expected_installation_missing");

  const token = await installationToken(jwt, installation.id);
  const repo = await github("/repos/" + EXPECTED_REPO, token);
  requirePolicy(repo?.id === EXPECTED_REPO_ID && repo?.full_name === EXPECTED_REPO, "repository_identity_mismatch");

  const control = await controlPlaneSnapshot(token);
  const verified = verifyControlPlane(control, env);

  return {
    ok: true,
    policy: "r11-autonomous-v3",
    app_id: app.id,
    app_slug: app.slug,
    installation_id: installation.id,
    repository_id: repo.id,
    environment: EXPECTED_ENV,
    protection_rule_id: verified.protectionRuleId,
    branch_policy_id: verified.branchPolicyId
  };
}

export async function redeliverLatestFailedProtection(env) {
  const jwt = await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY);
  const app = await github("/app", jwt);
  requirePolicy(app?.id === Number(env.GITHUB_APP_ID), "app_identity_mismatch");
  requirePolicy(app?.slug === EXPECTED_APP_SLUG, "app_slug_mismatch");
  const deliveries = await github("/app/hook/deliveries?per_page=30&status=failure", jwt);
  requirePolicy(Array.isArray(deliveries), "delivery_list_invalid");
  const cutoff = Date.now() - 2 * 60 * 60 * 1000;
  const candidate = deliveries
    .filter(d =>
      d?.event === "deployment_protection_rule" &&
      d?.action === "requested" &&
      d?.repository_id === EXPECTED_REPO_ID &&
      Number(d?.status_code) >= 400 &&
      Number.isFinite(Date.parse(d?.delivered_at || "")) &&
      Date.parse(d.delivered_at) >= cutoff
    )
    .sort((a, b) => Date.parse(b.delivered_at) - Date.parse(a.delivered_at))[0];
  if (!candidate) return { redelivered: false, reason: "no_recent_failed_protection_delivery" };
  await github("/app/hook/deliveries/" + candidate.id + "/attempts", jwt, "POST");
  return {
    redelivered: true,
    delivery_id: candidate.id,
    delivery_guid: candidate.guid || null,
    original_status_code: candidate.status_code
  };
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
    let route;
    try {
      route = routingPolicy(payload, deliveryId, env);
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
      const app = await github("/app", jwt);
      requirePolicy(app?.id === Number(env.GITHUB_APP_ID), "app_identity_mismatch");
      requirePolicy(app?.slug === EXPECTED_APP_SLUG, "app_slug_mismatch");

      const token = await installationToken(jwt, payload.installation.id);
      const [currentRun, pending, control] = await Promise.all([
        github("/repos/" + EXPECTED_REPO + "/actions/runs/" + route.runId, token),
        github("/repos/" + EXPECTED_REPO + "/actions/runs/" + route.runId + "/pending_deployments", token),
        controlPlaneSnapshot(token)
      ]);

      let live;
      let verifiedControl;
      try {
        live = livePolicy(payload, route.runId, currentRun, pending, env);
        verifiedControl = verifyControlPlane(control, env);
      } catch (error) {
        const reason = error instanceof PolicyError ? error.code : "live_policy_error";
        if (reason === "environment_not_pending") {
          const result = { decision: "ignored", reason };
          completedDeliveries.set(deliveryId, result);
          audit("ignored", payload, deliveryId, { reason, run_id: route.runId });
          return json({ handled: true, ...result });
        }
        await review(
          token,
          route.callbackPath,
          "rejected",
          "R11 autonomous policy v3 rejected this request: " + reason + ". delivery=" + deliveryId
        );
        const result = { decision: "rejected", reason, policy: "r11-autonomous-v3" };
        completedDeliveries.set(deliveryId, result);
        audit("rejected", payload, deliveryId, { reason, run_id: route.runId });
        return json({ handled: true, ...result });
      }

      await review(
        token,
        route.callbackPath,
        "approved",
        "R11 autonomous policy v3 approved: signed GitHub delivery, exact repository/environment/main/workflow/SHA, fresh run attempt, pending deployment, admin bypass disabled, exactly one matching custom rule, and main-only deployment branch policy verified. delivery=" + deliveryId
      );
      const result = { decision: "approved", policy: "r11-autonomous-v3" };
      completedDeliveries.set(deliveryId, result);
      audit("approved", payload, deliveryId, {
        run_id: route.runId,
        run_attempt: live.runAttempt,
        run_age_seconds: live.ageSeconds,
        protection_rule_id: verifiedControl.protectionRuleId,
        branch_policy_id: verifiedControl.branchPolicyId
      });
      return json({ handled: true, ...result });
    } catch (error) {
      const reason = error instanceof PolicyError ? error.code : "github_api_or_auth_failure";
      audit("pending", payload, deliveryId, { reason, run_id: route.runId });
      return json({ error: "review delivery failed", reason }, 503);
    } finally {
      inFlight.delete(deliveryId);
    }
  }
};
