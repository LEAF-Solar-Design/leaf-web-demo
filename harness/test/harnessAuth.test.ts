/**
 * F5 — harness caller-auth gate (shared secret on X-Harness-Secret).
 *
 * The harness HTTP surface is NOT public: /author, /run-registered, and the
 * PUT/GET/DELETE /grants/{tenantId} grant-admin routes must all require the shared
 * secret when the gate is enabled; /health stays open. Proves, hermetically (fakes,
 * fake tokens, temp grants dir, NO process.env mutation — the gate is pinned via the
 * explicit `opts.auth` on createHarness):
 *
 *   - gate ON, no/wrong secret  -> 401 (author, run, grants GET/PUT/DELETE); the
 *     grant store is NOT mutated by an unauthed PUT/DELETE;
 *   - gate ON, correct secret   -> routes work (author 200; grants round-trip);
 *   - /health is exempt (200 without a secret) in every mode;
 *   - the fail-closed rule: gate ON with an EMPTY configured secret authenticates
 *     nothing (an empty provided secret must not match an empty expected secret);
 *   - gate OFF (default) -> every route behaves exactly as before (no secret needed);
 *   - the secret is never echoed in any response body;
 *   - resolveHarnessAuth() parses the env flag/secret the live serve path relies on.
 */

import { mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createHarness, resolveHarnessAuth } from "../src/server.js";
import type { HarnessAuthConfig } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { FileTenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import type { TenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "s3cr3t-harness-shared-do-not-log-abc123";
const FAKE_TOKEN = "FAKE-OAUTH-not-a-real-token-xyz789";

/** No demo env fallback: keeps the real grant file untouched in every test here. */
const NO_ENV_FALLBACK: TenantGrantStore = { async get() { return null; } };

function listen(
  ports: HarnessPorts,
  auth: HarnessAuthConfig,
): { server: Server; baseUrl: string } {
  const server = createHarness(ports, { auth }).listen(0);
  const addr = server.address() as AddressInfo;
  return { server, baseUrl: `http://127.0.0.1:${addr.port}` };
}

// --------------------------------------------------------------------------- //
// Gate ENABLED — correct secret required on every non-health route.
// --------------------------------------------------------------------------- //

describe("harness auth gate (enabled)", () => {
  let dir: string;
  let server: Server;
  let baseUrl: string;
  let store: FileTenantGrantStore;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "leaf-harness-auth-"));
    store = new FileTenantGrantStore({ dir, envFallback: NO_ENV_FALLBACK });
    ({ server, baseUrl } = listen(
      {
        // FakeOAuthGrantProvider always resolves a grant, so a correctly-authed
        // /author reaches 200 and we are testing the CALLER gate, not the grant gate.
        oauth: new FakeOAuthGrantProvider(),
        grantAdmin: store,
        tenantRepo: new FakeTenantRepoProvider(FIXTURE),
        broker: new FakeBrokerApsClient(),
        agentRunner: new FakeAgentRunner(),
      },
      { enabled: true, secret: SECRET },
    ));
  });
  afterEach(() => {
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  it("GET /health stays open (no secret)", async () => {
    const r = await fetch(`${baseUrl}/health`);
    expect(r.status).toBe(200);
    expect((await r.json()) as { ok: boolean }).toMatchObject({ ok: true });
  });

  it("POST /author with NO secret -> 401 (auth, not grant_required)", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(401);
    const text = await r.text();
    expect(text).not.toContain(SECRET); // secret never echoed
    const body = JSON.parse(text) as { error?: { code?: string }; grant_required?: boolean };
    expect(body.error?.code).toBe("harness_auth_required");
    // This is a CALLER-auth failure, not a missing Claude grant.
    expect(body.grant_required).toBeUndefined();
  });

  it("POST /author with WRONG secret -> 401", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": "wrong-secret" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(401);
    expect(await r.text()).not.toContain(SECRET);
  });

  it("POST /author with CORRECT secret -> 200 author body", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(200);
    const body = (await r.json()) as { tool: unknown; code: unknown; preview: unknown };
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "tool"]);
  });

  it("POST /run-registered with NO secret -> 401", async () => {
    const r = await fetch(`${baseUrl}/run-registered`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tool: "count-by-layer" }),
    });
    expect(r.status).toBe(401);
    expect((JSON.parse(await r.text()) as { error?: { code?: string } }).error?.code).toBe(
      "harness_auth_required",
    );
  });

  it("GET /grants/{tid} with NO secret -> 401", async () => {
    const r = await fetch(`${baseUrl}/grants/acme`);
    expect(r.status).toBe(401);
  });

  it("PUT /grants/{tid} with NO secret -> 401 and the store is NOT mutated", async () => {
    const r = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: FAKE_TOKEN }),
    });
    expect(r.status).toBe(401);
    expect(await r.text()).not.toContain(FAKE_TOKEN); // token never echoed
    // Fail-closed BEFORE the store: the tenant must still be unlinked.
    expect((await store.status("acme")).linked).toBe(false);
  });

  it("DELETE /grants/{tid} with NO secret -> 401 and the grant survives", async () => {
    await store.put("acme", FAKE_TOKEN);
    expect((await store.status("acme")).linked).toBe(true);
    const r = await fetch(`${baseUrl}/grants/acme`, { method: "DELETE" });
    expect(r.status).toBe(401);
    // The unauthed DELETE must NOT have removed the grant.
    expect((await store.status("acme")).linked).toBe(true);
  });

  it("PUT then GET /grants/{tid} with CORRECT secret round-trips (token never echoed)", async () => {
    const put = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ token: FAKE_TOKEN }),
    });
    expect(put.status).toBe(200);
    const putText = await put.text();
    expect(putText).not.toContain(FAKE_TOKEN);
    expect(putText).not.toContain(SECRET);
    expect((JSON.parse(putText) as { linked: boolean }).linked).toBe(true);

    const get = await fetch(`${baseUrl}/grants/acme`, {
      headers: { "x-harness-secret": SECRET },
    });
    expect(get.status).toBe(200);
    expect((await get.json()) as { linked: boolean }).toMatchObject({ linked: true });
  });
});

// --------------------------------------------------------------------------- //
// Gate ENABLED but secret UNSET — fail-closed: authenticate nothing.
// --------------------------------------------------------------------------- //

describe("harness auth gate (fail-closed: enabled, empty secret)", () => {
  let server: Server;
  let baseUrl: string;

  beforeEach(() => {
    ({ server, baseUrl } = listen(
      {
        oauth: new FakeOAuthGrantProvider(),
        tenantRepo: new FakeTenantRepoProvider(FIXTURE),
        broker: new FakeBrokerApsClient(),
        agentRunner: new FakeAgentRunner(),
      },
      { enabled: true, secret: "" },
    ));
  });
  afterEach(() => server.close());

  it("POST /author with NO secret -> 401", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(401);
  });

  it("POST /author with an EMPTY X-Harness-Secret -> 401 (empty must not match empty)", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": "" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(401);
  });

  it("POST /author with a GUESSED secret -> 401", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": "anything" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(401);
  });

  it("GET /health still open even when fail-closed", async () => {
    expect((await fetch(`${baseUrl}/health`)).status).toBe(200);
  });
});

// --------------------------------------------------------------------------- //
// Gate DISABLED (default) — every route behaves exactly as before.
// --------------------------------------------------------------------------- //

describe("harness auth gate (disabled / default)", () => {
  let dir: string;
  let server: Server;
  let baseUrl: string;
  let store: FileTenantGrantStore;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "leaf-harness-auth-off-"));
    store = new FileTenantGrantStore({ dir, envFallback: NO_ENV_FALLBACK });
    ({ server, baseUrl } = listen(
      {
        oauth: new FakeOAuthGrantProvider(),
        grantAdmin: store,
        tenantRepo: new FakeTenantRepoProvider(FIXTURE),
        broker: new FakeBrokerApsClient(),
        agentRunner: new FakeAgentRunner(),
      },
      { enabled: false, secret: "" },
    ));
  });
  afterEach(() => {
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  it("POST /author works with NO secret", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(r.status).toBe(200);
    const body = (await r.json()) as { tool: unknown; code: unknown; preview: unknown };
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "tool"]);
  });

  it("PUT /grants/{tid} works with NO secret", async () => {
    const r = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: FAKE_TOKEN }),
    });
    expect(r.status).toBe(200);
    expect((JSON.parse(await r.text()) as { linked: boolean }).linked).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// resolveHarnessAuth() — the env parse the live serve path relies on.
// --------------------------------------------------------------------------- //

describe("resolveHarnessAuth", () => {
  it("empty env -> disabled, empty secret", () => {
    expect(resolveHarnessAuth({})).toEqual({ enabled: false, secret: "" });
  });

  it('LEAF_HARNESS_AUTH="1" + secret -> enabled', () => {
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "1", LEAF_HARNESS_SECRET: "abc" })).toEqual({
      enabled: true,
      secret: "abc",
    });
  });

  it('accepts "true"/"yes"/"on" (case-insensitive) and rejects "0"/"false"', () => {
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "true" }).enabled).toBe(true);
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "YES" }).enabled).toBe(true);
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "On" }).enabled).toBe(true);
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "0" }).enabled).toBe(false);
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "false" }).enabled).toBe(false);
  });

  it("trims a surrounding-whitespace secret", () => {
    expect(resolveHarnessAuth({ LEAF_HARNESS_AUTH: "1", LEAF_HARNESS_SECRET: "  abc  " }).secret).toBe(
      "abc",
    );
  });
});
