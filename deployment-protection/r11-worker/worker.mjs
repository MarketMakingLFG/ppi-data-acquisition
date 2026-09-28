// Fail-closed deployment protection webhook. This implementation NEVER approves.
const EXPECTED_REPO = "MarketMakingLFG/ppi-data-acquisition";
const EXPECTED_ENV = "r11-public-acquisition-protected";
const encoder = new TextEncoder();
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const b64url = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");

async function verifySignature(raw, signature, secret) {
  if (!secret || !/^sha256=[0-9a-f]{64}$/i.test(signature || "")) return false;
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const hex = signature.slice(7);
  const expected = new Uint8Array(hex.match(/../g).map(x => parseInt(x, 16)));
  return crypto.subtle.verify("HMAC", key, expected, raw);
}
async function appJwt(appId, pem) {
  const der = Uint8Array.from(atob(pem.replace(/-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g, "")), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const now = Math.floor(Date.now() / 1000);
  const payload = b64url(encoder.encode(JSON.stringify({ iat: now - 60, exp: now + 540, iss: String(appId) })));
  const header = b64url(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const message = header + "." + payload;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(message));
  return message + "." + b64url(sig);
}
async function github(url, token, method, body) {
  const r = await fetch("https://api.github.com" + url, {
    method, headers: { authorization: "Bearer " + token, accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28", "user-agent": "ppi-r11-independent-protection",
      ...(body ? { "content-type": "application/json" } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {})
  });
  if (!r.ok) throw new Error("GitHub API status " + r.status);
  return r.status === 204 ? null : r.json();
}
export default {
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/github/webhook")
      return json({ error: "not found" }, 404);
    const raw = await request.arrayBuffer();
    if (raw.byteLength > 1024 * 1024) return json({ error: "too large" }, 413);
    if (!await verifySignature(raw, request.headers.get("x-hub-signature-256"), env.GITHUB_WEBHOOK_SECRET))
      return json({ error: "unauthorized" }, 401);
    if (request.headers.get("x-github-event") !== "deployment_protection_rule") return json({ ignored: true });
    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(raw)); } catch { return json({ error: "bad JSON" }, 400); }
    if (payload.action !== "requested") return json({ ignored: true });
    const run = payload.workflow_run;
    const repo = payload.repository;
    const installation = payload.installation;
    const environment = payload.environment;
    // Require complete event identity. Never approve even if these checks pass.
    if (repo?.full_name !== EXPECTED_REPO || repo?.private !== true ||
        run?.head_branch !== "main" || run?.repository?.full_name !== EXPECTED_REPO ||
        environment !== EXPECTED_ENV || !Number.isSafeInteger(run?.id) ||
        !Number.isSafeInteger(installation?.id) || !Number.isSafeInteger(payload.deployment_protection_rule?.id))
      return json({ error: "event identity rejected" }, 403);
    try {
      const jwt = await appJwt(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY);
      const auth = await github("/app/installations/" + installation.id + "/access_tokens", jwt, "POST");
      await github("/repos/" + EXPECTED_REPO + "/actions/runs/" + run.id + "/deployment_protection_rule",
        auth.token, "POST", { environment_name: EXPECTED_ENV, state: "rejected",
          comment: "R11 independent gate remains fail-closed; human-controlled approval not implemented." });
      return json({ handled: true, decision: "rejected" });
    } catch {
      // Never acknowledge a failed review as successful; GitHub's pending protection remains in force.
      return json({ error: "review delivery failed" }, 503);
    }
  }
};
