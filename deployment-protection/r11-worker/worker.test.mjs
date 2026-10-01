import test from "node:test";
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, webcrypto } from "node:crypto";
import worker from "./worker.mjs";

globalThis.crypto ??= webcrypto;

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const pem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
const pemPkcs1 = privateKey.export({ format: "pem", type: "pkcs1" }).toString();
const env = {
  GITHUB_WEBHOOK_SECRET: "unit-test-secret",
  GITHUB_APP_ID: "123456",
  GITHUB_APP_PRIVATE_KEY: pem,
  PPI_MAX_RUN_AGE_SECONDS: "3600"
};

function makeEvent(overrides = {}) {
  const sha = "a".repeat(40);
  return {
    action: "requested",
    environment: "r11-public-acquisition-protected",
    event: "workflow_dispatch",
    sha,
    ref: "main",
    deployment_callback_url:
      "https://api.github.com/repos/MarketMakingLFG/ppi-data-acquisition/actions/runs/123/deployment_protection_rule",
    repository: { id: 1312286476, full_name: "MarketMakingLFG/ppi-data-acquisition", private: false },
    installation: { id: 456 },
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
    method: "POST", headers, body
  });
}

function liveRun(payload, overrides = {}) {
  return {
    id: 123,
    run_attempt: 2,
    head_branch: "main",
    head_sha: payload.sha,
    event: "workflow_dispatch",
    path: ".github/workflows/collect-r11-public-evidence.yml@refs/heads/main",
    conclusion: null,
    created_at: new Date(Date.now() - 30_000).toISOString(),
    repository: { id: 1312286476, full_name: "MarketMakingLFG/ppi-data-acquisition" },
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

function successMock(payload, pending = pendingDeployments(), runOverrides = {}, policyOverrides = {}) {
  const environment = {
    name: "r11-public-acquisition-protected",
    can_admins_bypass: false,
    deployment_branch_policy: { protected_branches: false, custom_branch_policies: true },
    ...(policyOverrides.environment || {})
  };
  const rules = policyOverrides.rules || {
    total_count: 1,
    custom_deployment_protection_rules: [{
      id: 77, enabled: true,
      app: { id: 123456, slug: "ppi-r11-independent-protection" }
    }]
  };
  const branches = policyOverrides.branches || {
    total_count: 1,
    branch_policies: [{ id: 88, name: "main" }]
  };
  return async (url, options = {}) => {
    if (url === "https://api.github.com/app")
      return new Response(JSON.stringify({ id: 123456, slug: "ppi-r11-independent-protection" }), { status: 200 });
    if (url.includes("/app/installations/456/access_tokens"))
      return new Response(JSON.stringify({ token: "test-installation-token-value-0000000000" }), { status: 201 });
    if (url.endsWith("/actions/runs/123"))
      return new Response(JSON.stringify(liveRun(payload, runOverrides)), { status: 200 });
    if (url.endsWith("/actions/runs/123/pending_deployments"))
      return new Response(JSON.stringify(pending), { status: 200 });
    if (url.includes("/deployment_protection_rules"))
      return new Response(JSON.stringify(rules), { status: 200 });
    if (url.includes("/deployment-branch-policies"))
      return new Response(JSON.stringify(branches), { status: 200 });
    if (url.endsWith("/environments/r11-public-acquisition-protected"))
      return new Response(JSON.stringify(environment), { status: 200 });
    if (url.endsWith("/actions/runs/123/deployment_protection_rule"))
      return new Response(null, { status: 204 });
    throw Error("unexpected URL " + url + " " + (options.method || "GET"));
  };
}

test("unsigned webhook cannot call GitHub", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(request(makeEvent(), { signature: false }), env);
    assert.equal(r.status, 401);
  })
);

test("wrong environment is blocked before outbound calls", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(
      request(makeEvent({ environment: "other" }), { delivery: "delivery-wrong-env" }), env
    );
    assert.equal(r.status, 403);
    assert.equal((await r.json()).reason, "wrong_environment");
  })
);

test("wrong callback repository is blocked", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const payload = makeEvent({
      deployment_callback_url:
        "https://api.github.com/repos/Other/repo/actions/runs/123/deployment_protection_rule"
    });
    const r = await worker.fetch(request(payload, { delivery: "delivery-wrong-callback" }), env);
    assert.equal(r.status, 403);
    assert.equal((await r.json()).reason, "callback_path_invalid");
  })
);

test("non-GitHub callback origin is blocked", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const payload = makeEvent({
      deployment_callback_url:
        "https://evil.example/repos/MarketMakingLFG/ppi-data-acquisition/actions/runs/123/deployment_protection_rule"
    });
    const r = await worker.fetch(request(payload, { delivery: "delivery-wrong-origin" }), env);
    assert.equal(r.status, 403);
    assert.equal((await r.json()).reason, "callback_origin_invalid");
  })
);

test("other webhook event is ignored after signature verification", async () =>
  withFetch(() => { throw Error("unexpected outbound request"); }, async () => {
    const r = await worker.fetch(request(makeEvent(), {
      name: "ping", delivery: "delivery-ping-1234"
    }), env);
    assert.equal(r.status, 200);
    assert.equal((await r.json()).ignored, true);
  })
);

test("matching real payload shape is approved after app and live run checks", async () => {
  const payload = makeEvent();
  const calls = [];
  await withFetch(async (url, options = {}) => {
    calls.push({ url, options });
    return successMock(payload)(url, options);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-approve-0002" }), env);
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.decision, "approved");
    assert.equal(body.policy, "r11-autonomous-v3");
  });
  assert.equal(calls.length, 5);
  const reviewBody = JSON.parse(calls[4].options.body);
  assert.equal(reviewBody.state, "approved");
  assert.equal(reviewBody.environment_name, "r11-public-acquisition-protected");
});

test("stale live run is actively rejected", async () => {
  const payload = makeEvent();
  const calls = [];
  await withFetch(async (url, options = {}) => {
    calls.push({ url, options });
    return successMock(payload, pendingDeployments(), {
      created_at: new Date(Date.now() - 7200_000).toISOString()
    })(url, options);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-stale-0002" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "run_stale");
  });
  assert.equal(JSON.parse(calls.find(call => call.url.endsWith("/deployment_protection_rule")).options.body).state, "rejected");
});

test("wrong live workflow is actively rejected", async () => {
  const payload = makeEvent();
  await withFetch(successMock(payload, pendingDeployments(), {
    path: ".github/workflows/other.yml@refs/heads/main"
  }), async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-workflow-0002" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "live_workflow_invalid");
  });
});

test("SHA mismatch is actively rejected", async () => {
  const payload = makeEvent();
  await withFetch(successMock(payload, pendingDeployments(), {
    head_sha: "b".repeat(40)
  }), async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-sha-0002" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "live_sha_mismatch");
  });
});

test("environment no longer pending is idempotently ignored", async () => {
  const payload = makeEvent();
  let reviewCalls = 0;
  await withFetch(async (url, options = {}) => {
    if (url.endsWith("/deployment_protection_rule") && options.method === "POST") reviewCalls++;
    return successMock(payload, [])(url, options);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-not-pending-2" }), env);
    const body = await r.json();
    assert.equal(body.decision, "ignored");
    assert.equal(body.reason, "environment_not_pending");
  });
  assert.equal(reviewCalls, 0);
});

test("duplicate delivery is replay-safe after completed decision", async () => {
  const payload = makeEvent();
  let outbound = 0;
  const delivery = "delivery-replay-0002";
  await withFetch(async (url, options = {}) => {
    outbound++;
    return successMock(payload)(url, options);
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

test("GitHub App identity mismatch fails closed", async () => {
  const payload = makeEvent();
  await withFetch(async (url) => {
    if (url === "https://api.github.com/app")
      return new Response(JSON.stringify({ id: 999999 }), { status: 200 });
    throw Error("unexpected URL " + url);
  }, async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-app-id-0002" }), env);
    assert.equal(r.status, 503);
    assert.equal((await r.json()).reason, "app_identity_mismatch");
  });
});

test("GitHub App PKCS#1 RSA PEM is accepted", async () => {
  const payload = makeEvent();
  const pkcs1Env = { ...env, GITHUB_APP_PRIVATE_KEY: pemPkcs1 };
  await withFetch(successMock(payload), async () => {
    const r = await worker.fetch(
      request(payload, { delivery: "delivery-pkcs1-0002" }),
      pkcs1Env
    );
    assert.equal(r.status, 200);
    assert.equal((await r.json()).decision, "approved");
  });
});

test("GitHub API errors fail closed and leave protection pending", async () =>
  withFetch(async () => new Response("no", { status: 403 }), async () => {
    const r = await worker.fetch(
      request(makeEvent(), { delivery: "delivery-api-fail-0002" }), env
    );
    assert.equal(r.status, 503);
    assert.equal((await r.json()).reason, "github_api_or_auth_failure");
  })
);


test("admin bypass drift is actively rejected", async () => {
  const payload = makeEvent();
  await withFetch(successMock(payload, pendingDeployments(), {}, {
    environment: { can_admins_bypass: true }
  }), async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-admin-bypass-v3" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "admin_bypass_not_disabled");
  });
});

test("custom deployment rule must be exactly this App", async () => {
  const payload = makeEvent();
  await withFetch(successMock(payload, pendingDeployments(), {}, {
    rules: {
      total_count: 1,
      custom_deployment_protection_rules: [{
        id: 77, enabled: true, app: { id: 999999, slug: "other-app" }
      }]
    }
  }), async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-rule-drift-v3" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "custom_rule_app_id_mismatch");
  });
});

test("deployment branch policy must be main only", async () => {
  const payload = makeEvent();
  await withFetch(successMock(payload, pendingDeployments(), {}, {
    branches: {
      total_count: 2,
      branch_policies: [{ id: 88, name: "main" }, { id: 89, name: "release/*" }]
    }
  }), async () => {
    const r = await worker.fetch(request(payload, { delivery: "delivery-branch-drift-v3" }), env);
    const body = await r.json();
    assert.equal(body.decision, "rejected");
    assert.equal(body.reason, "branch_policy_count_invalid");
  });
});
