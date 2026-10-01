import test from "node:test";
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, webcrypto } from "node:crypto";
import worker from "./worker.mjs";

globalThis.crypto ??= webcrypto;

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const pem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
const env = {
  GITHUB_WEBHOOK_SECRET: "unit-test-secret",
  GITHUB_APP_ID: "123456",
  GITHUB_APP_PRIVATE_KEY: pem,
  PPI_MAX_RUN_AGE_SECONDS: "3600"
};

function makeEvent(overrides = {}) {
  const sha = "a".repeat(40);
  const run = {
    id: 123,
    run_attempt: 2,
    head_branch: "main",
    head_sha: sha,
    event: "workflow_dispatch",
    path: ".github/workflows/collect-r11-public-evidence.yml@refs/heads/main",
    repository: { full_name: "MarketMakingLFG/ppi-data-acquisition" }
  };
  return {
    action: "requested",
    event: "workflow_dispatch",
    ref: "refs/heads/main",
    sha,
    repository: { id: 1312286476, full_name: "MarketMakingLFG/ppi-data-acquisition", private: false },
    workflow_run: run,
    installation: { id: 456 },
    deployment_protection_rule: { id: 789, app: { id: 123456 } },
    environment: "r11-public-acquisition-protected",
    ...overrides
  };
}

function request(payload, {
  signature = true,
  name = "deployment_protection_rule",
  delivery = "delivery-12345678"
} = {}) {
  const body = JSON.stringify(payload);
  const headers = { "x-github-event": name, "x-github-delivery": delivery };
  if (signature) {
    headers["x-hub-signature-256"] = "sha256=" +
      createHmac("sha256", env.GITHUB_WEBHOOK_SECRET).update(body).digest("hex");
  }
  return new Request("https://example.onrender.com/github/webhook", {
    method: "POST",
    headers,
    body
  });
}

function liveRun(payload, overrides = {}) {
  return {
    id: payload.workflow_run.id,
    run_attempt: payload.workflow_run.run_attempt,
    head_branch: "main",
    head_sha: payload.sha,
    event: "workflow_dispatch",
    path: ".github/workflows/collect-r11-public-evidence.yml@refs/heads/main",
    conclusion: null,
    created_at: new Date(Date.now() - 30_000).toISOString(),
    repository: { full_name: "MarketMakingLFG/ppi-data-acquisition" },
    ...overrides
  };
}

function pendingDeployments() {
  return [{ environment: { name: "r11-public-acquisition-protected" } }];
}

async function withFetch(mock, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try { return await fn(); }
  finally { globalThis.fetch = original; }
}

test("unsigned webhook cannot call GitHub", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(request(makeEvent(), { signature: false }), env);
    assert.equal(r.status, 401);
  })
);

test("wrong branch is blocked before outbound calls", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const payload = makeEvent({
      ref: "refs/heads/feature",
      workflow_run: { ...makeEvent().workflow_run, head_branch: "feature" }
    });
    const r = await worker.fetch(request(payload, { delivery: "delivery-wrong-branch" }), env);
    assert.equal(r.status, 403);
    assert.equal((await r.json()).reason, "ref_invalid");
  })
);

test("wrong environment is blocked", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(
      request(makeEvent({ environment: "other" }), { delivery: "delivery-wrong-env" }),
      env
    );
    assert.equal(r.status, 403);
  })
);

test("other event is ignored after signature verification", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(request(makeEvent(), {
      name: "ping",
      delivery: "delivery-ping-1234"
    }), env);
    assert.equal(r.status, 200);
    assert.equal((await r.json()).ignored, true);
  })
);

test("matching event is approved only after live run and pending environment checks", async () => {
  const payload = makeEvent();
  const calls = [];
  await withFetch(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.includes("/access_tokens"))
      return new Response(JSON.stringify({ token: "x".repeat(40) }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload)), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify(pendingDeployments()), { status: 200 });
    if (url.endsWith("/deployment_protection_rule"))
      return new Response(null, { status: 204 });
    throw Error("unexpected URL " + url);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-approve-0001" }), env);
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.decision, "approved");
    assert.equal(body.policy, "r11-autonomous-v1");
  });
  assert.equal(calls.length, 4);
  const reviewBody = JSON.parse(calls[3].options.body);
  assert.equal(reviewBody.state, "approved");
  assert.equal(reviewBody.environment_name, "r11-public-acquisition-protected");
});

test("stale run is actively rejected", async () => {
  const payload = makeEvent();
  const calls = [];
  await withFetch(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.includes("/access_tokens"))
      return new Response(JSON.stringify({ token: "x".repeat(40) }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload, {
        created_at: new Date(Date.now() - 7200_000).toISOString()
      })), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify(pendingDeployments()), { status: 200 });
    if (url.endsWith("/deployment_protection_rule"))
      return new Response(null, { status: 204 });
    throw Error("unexpected URL " + url);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-stale-0001" }), env);
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "run_stale");
  });
  assert.equal(JSON.parse(calls[3].options.body).state, "rejected");
});

test("wrong live workflow is actively rejected", async () => {
  const payload = makeEvent();
  await withFetch(async (url) => {
    if (url.includes("/access_tokens"))
      return new Response(JSON.stringify({ token: "x".repeat(40) }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload, {
        path: ".github/workflows/other.yml@refs/heads/main"
      })), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify(pendingDeployments()), { status: 200 });
    if (url.endsWith("/deployment_protection_rule"))
      return new Response(null, { status: 204 });
    throw Error("unexpected URL " + url);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-workflow-0001" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "live_workflow_invalid");
  });
});

test("environment no longer pending is idempotently ignored", async () => {
  const payload = makeEvent();
  let reviewCalls = 0;
  await withFetch(async (url) => {
    if (url.includes("/access_tokens"))
      return new Response(JSON.stringify({ token: "x".repeat(40) }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload)), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify([]), { status: 200 });
    if (url.endsWith("/deployment_protection_rule")) {
      reviewCalls++;
      return new Response(null, { status: 204 });
    }
    throw Error("unexpected URL " + url);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-not-pending" }), env);
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.decision, "ignored");
    assert.equal(body.reason, "environment_not_pending");
  });
  assert.equal(reviewCalls, 0);
});

test("duplicate delivery is replay-safe after a completed decision", async () => {
  const payload = makeEvent();
  let outbound = 0;
  const delivery = "delivery-replay-0001";
  await withFetch(async (url) => {
    outbound++;
    if (url.includes("/access_tokens"))
      return new Response(JSON.stringify({ token: "x".repeat(40) }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload)), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify(pendingDeployments()), { status: 200 });
    if (url.endsWith("/deployment_protection_rule"))
      return new Response(null, { status: 204 });
    throw Error("unexpected URL " + url);
  }, async () => {
    const first = await worker.fetch(request(payload, { delivery }), env);
    assert.equal((await first.json()).decision, "approved");
    const countAfterFirst = outbound;
    const second = await worker.fetch(request(payload, { delivery }), env);
    const body = await second.json();
    assert.equal(body.decision, "approved");
    assert.equal(body.replay, true);
    assert.equal(outbound, countAfterFirst);
  });
});

test("GitHub API errors fail closed and leave protection pending", async () =>
  withFetch(async () => new Response("no", { status: 403 }), async () => {
    const r = await worker.fetch(
      request(makeEvent(), { delivery: "delivery-api-fail-0001" }),
      env
    );
    assert.equal(r.status, 503);
    assert.equal((await r.json()).reason, "github_api_or_auth_failure");
  })
);
