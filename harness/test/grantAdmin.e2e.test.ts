/**
 * Harness grant-admin endpoints + per-request tenant grant resolution (wave 4).
 *
 * Hermetic: FAKE tokens, temp grants dir, fakes for the other ports. Proves:
 *   - PUT/GET/DELETE /grants/{tenantId} round-trip; the token is NEVER echoed;
 *   - 501 when no grantAdmin store is wired;
 *   - POST /author resolves the BODY tenant's grant: a tenant with no linked grant ->
 *     401 { grant_required: true }; a tenant that just linked one -> 200 author body.
 */

import { mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import {
  FileTenantGrantStore,
  OAuthGrantProviderImpl,
} from "../src/ports/impl/oauthGrantProvider.js";
import type { TenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const FAKE = "FAKE-OAUTH-not-a-real-token-abc123def456";

/** No demo env fallback: keeps the real grant file untouched in every test here. */
const NO_ENV_FALLBACK: TenantGrantStore = { async get() { return null; } };

function listen(ports: HarnessPorts): { server: Server; baseUrl: string } {
  const server = createHarness(ports).listen(0);
  const addr = server.address() as AddressInfo;
  return { server, baseUrl: `http://127.0.0.1:${addr.port}` };
}

describe("harness /grants admin endpoints", () => {
  let dir: string;
  let server: Server;
  let baseUrl: string;
  let store: FileTenantGrantStore;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "leaf-grants-"));
    store = new FileTenantGrantStore({ dir, envFallback: NO_ENV_FALLBACK });
    ({ server, baseUrl } = listen({
      oauth: new OAuthGrantProviderImpl({ store }),
      grantAdmin: store,
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    }));
  });
  afterEach(() => {
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  it("PUT links, GET reports status, DELETE unlinks — token never echoed", async () => {
    const put = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: FAKE }),
    });
    expect(put.status).toBe(200);
    const putText = await put.text();
    expect(putText).not.toContain(FAKE); // never echoed
    const putBody = JSON.parse(putText) as { linked: boolean; linked_at: string | null };
    expect(putBody.linked).toBe(true);
    expect(typeof putBody.linked_at).toBe("string");

    const get = await fetch(`${baseUrl}/grants/acme`);
    const getText = await get.text();
    expect(getText).not.toContain(FAKE);
    expect((JSON.parse(getText) as { linked: boolean }).linked).toBe(true);

    const del = await fetch(`${baseUrl}/grants/acme`, { method: "DELETE" });
    expect(del.status).toBe(200);
    expect((await del.json()) as unknown).toEqual({ linked: false, linked_at: null });

    const after = await fetch(`${baseUrl}/grants/acme`);
    expect((await after.json() as { linked: boolean }).linked).toBe(false);
  });

  it("PUT with no token -> 400", async () => {
    const r = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    });
    expect(r.status).toBe(400);
  });

  it("adds two labeled accounts, selects the first, and removes only that account", async () => {
    const first = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: FAKE, label: "first@example.com" }),
    }).then((response) => response.json()) as { active_account_id: string };
    const secondToken = "FAKE-OAUTH-second-not-real";
    const second = await fetch(`${baseUrl}/grants/acme`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: secondToken, label: "second@example.com" }),
    }).then((response) => response.json()) as { active_account_id: string; accounts: unknown[] };
    expect(second.accounts).toHaveLength(2);

    const select = await fetch(`${baseUrl}/grants/acme`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ account_id: first.active_account_id }),
    });
    expect(select.status).toBe(200);
    expect((await select.json() as { active_account_id: string }).active_account_id).toBe(first.active_account_id);

    const remove = await fetch(`${baseUrl}/grants/acme?account_id=${encodeURIComponent(first.active_account_id)}`, {
      method: "DELETE",
    });
    const bodyText = await remove.text();
    expect(bodyText).not.toContain(FAKE);
    expect(bodyText).not.toContain(secondToken);
    const body = JSON.parse(bodyText) as { active_account_id: string; accounts: unknown[] };
    expect(body.active_account_id).toBe(second.active_account_id);
    expect(body.accounts).toHaveLength(1);
  });

  it("GET diagnostic returns only token-free v2 storage facts", async () => {
    await store.put("acme", FAKE);
    const r = await fetch(`${baseUrl}/grants/acme/diagnostic`);
    expect(r.status).toBe(200);
    const text = await r.text();
    expect(text).not.toContain(FAKE);
    expect(text.toLowerCase()).not.toContain("filename");
    expect(text.toLowerCase()).not.toContain("path\":");
    const body = JSON.parse(text) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual([
      "backend", "degraded", "kind", "legacy_fallback_present", "linked",
      "linked_at", "owner", "path_class", "persistence", "record_format", "schema",
    ].sort());
    expect(body).toMatchObject({
      schema: "leaf.grant-diagnostic.v1",
      linked: true,
      kind: "oauth",
      backend: "file",
      path_class: "local_file",
      record_format: "v2",
      legacy_fallback_present: false,
      degraded: false,
      owner: { mode: process.platform === "win32" ? "0666" : "0600" },
      persistence: { atomic_publish: true, file_fsync: true },
    });
  });
});

describe("harness /grants without a grantAdmin store", () => {
  it("returns 501", async () => {
    const { server, baseUrl } = listen({
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    });
    try {
      const r = await fetch(`${baseUrl}/grants/acme`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: FAKE }),
      });
      expect(r.status).toBe(501);
    } finally {
      server.close();
    }
  });
});

describe("POST /author resolves the body tenant's grant", () => {
  let dir: string;
  let server: Server;
  let baseUrl: string;
  let store: FileTenantGrantStore;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "leaf-grants-"));
    store = new FileTenantGrantStore({ dir, envFallback: NO_ENV_FALLBACK });
    ({ server, baseUrl } = listen({
      oauth: new OAuthGrantProviderImpl({ store }),
      grantAdmin: store,
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    }));
  });
  afterEach(() => {
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  it("a tenant with NO linked grant -> 401 grant_required (no token)", async () => {
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer", tenant_id: "no-grant" }),
    });
    expect(r.status).toBe(401);
    const text = await r.text();
    expect(text).not.toContain(FAKE);
    const body = JSON.parse(text) as { grant_required?: boolean; error?: { message: string } };
    expect(body.grant_required).toBe(true);
    expect(typeof body.error?.message).toBe("string");
  });

  it("after that tenant links a grant -> 200 author body", async () => {
    await store.put("has-grant", FAKE);
    const r = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer", tenant_id: "has-grant" }),
    });
    expect(r.status).toBe(200);
    const body = (await r.json()) as { tool: unknown; code: unknown; preview: unknown };
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "tool"]);
  });
});
