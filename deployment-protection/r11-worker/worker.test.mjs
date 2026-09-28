import test from "node:test";
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, webcrypto } from "node:crypto";
import worker from "./worker.mjs";

globalThis.crypto ??= webcrypto;
const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const pem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
const env = { GITHUB_WEBHOOK_SECRET: "unit-test-secret", GITHUB_APP_ID: "123456", GITHUB_APP_PRIVATE_KEY: pem };
const event = {
  action: "requested",
  repository: { full_name: "MarketMakingLFG/ppi-data-acquisition", private: true },
  workflow_run: { id: 123, head_branch: "main", repository: { full_name: "MarketMakingLFG/ppi-data-acquisition" } },
  installation: { id: 456 },
  deployment_protection_rule: { id: 789 },
  environment: "r11-public-acquisition-protected"
};
const request = (payload, { signature = true, name = "deployment_protection_rule" } = {}) => {
  const body = JSON.stringify(payload);
  const headers = { "x-github-event": name };
  if (signature) headers["x-hub-signature-256"] = "sha256=" + createHmac("sha256", env.GITHUB_WEBHOOK_SECRET).update(body).digest("hex");
  return new Request("https://example.workers.dev/github/webhook", { method: "POST", headers, body });
};
async function withFetch(mock, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try { return await fn(); } finally { globalThis.fetch = original; }
}
test("unsigned webhook cannot call GitHub", async () => withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
  const r = await worker.fetch(request(event, { signature: false }), env);
  assert.equal(r.status, 401);
}));
test("wrong branch fails closed before outbound calls", async () => withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
  const r = await worker.fetch(request({ ...event, workflow_run: { ...event.workflow_run, head_branch: "feature" } }), env);
  assert.equal(r.status, 403);
}));
test("wrong environment fails closed", async () => withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
  const r = await worker.fetch(request({ ...event, environment: "other" }), env);
  assert.equal(r.status, 403);
}));
test("other event is ignored", async () => withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
  const r = await worker.fetch(request(event, { name: "ping" }), env);
  assert.equal(r.status, 200);
}));
test("matching event issues rejection only", async () => {
  const calls = [];
  await withFetch(async (url, options) => {
    calls.push({ url, options });
    if (url.includes("/access_tokens")) return new Response(JSON.stringify({ token: "unit-test-installation-token" }), { status: 201 });
    return new Response("", { status: 200 });
  }, async () => {
    const r = await worker.fetch(request(event), env);
    assert.equal(r.status, 200);
    assert.equal((await r.json()).decision, "rejected");
  });
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[1].options.body).state, "rejected");
  assert.equal(JSON.parse(calls[1].options.body).environment_name, "r11-public-acquisition-protected");
});
test("GitHub API errors fail closed", async () => withFetch(async () => new Response("no", { status: 403 }), async () => {
  const r = await worker.fetch(request(event), env);
  assert.equal(r.status, 503);
}));
