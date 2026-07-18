/**
 * FileTenantGrantStore — per-tenant grant persistence (wave 4).
 *
 * Hermetic: temp dir + FAKE tokens only. NEVER touches the real grant at
 * C:/tmp/hosted-oauth-spike/.grant/token — the demo-tenant env fallback is injected as a
 * scripted fake so the real file/env path is never constructed here.
 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FileTenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import type { AgentGrant } from "../src/ports/index.js";
import type { TenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";

const FAKE = "FAKE-OAUTH-not-a-real-token-000111222";
const FAKE2 = "FAKE-OAUTH-second-333444555";

/** A scripted env fallback so the demo-tenant back-compat path never reads a real file. */
class ScriptedEnvFallback implements TenantGrantStore {
  constructor(private readonly grant: AgentGrant | null) {}
  async get(): Promise<AgentGrant | null> {
    return this.grant;
  }
}

describe("FileTenantGrantStore", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "leaf-grants-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("put -> get returns the oauth grant; the token is persisted to its own file", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    const status = await store.put("acme", FAKE);
    expect(status.linked).toBe(true);
    expect(typeof status.linked_at).toBe("string");

    const grant = await store.get("acme");
    expect(grant).toEqual({ kind: "oauth", oauthToken: FAKE });

    // persisted to <dir>/acme.token (the ONLY place the token lives)
    const file = join(dir, "acme.token");
    expect(existsSync(file)).toBe(true);
    expect(readFileSync(file, "utf8")).toBe(FAKE);
  });

  it("status NEVER returns the token (only linked + linked_at)", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE);
    const status = await store.status("acme");
    expect(Object.keys(status).sort()).toEqual(["linked", "linked_at"]);
    expect(JSON.stringify(status)).not.toContain(FAKE);
  });

  it("unlinked non-demo tenant -> get null, status linked:false", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    expect(await store.get("nobody")).toBeNull();
    expect(await store.status("nobody")).toEqual({ linked: false, linked_at: null });
  });

  it("put replaces, remove unlinks (idempotent)", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE);
    await store.put("acme", FAKE2);
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE2 });

    await store.remove("acme");
    expect(await store.get("acme")).toBeNull();
    expect((await store.status("acme")).linked).toBe(false);
    await expect(store.remove("acme")).resolves.toBeUndefined(); // idempotent
  });

  it("demo-tenant falls back to the env grant when it has no per-tenant file; a file overrides it", async () => {
    const store = new FileTenantGrantStore({
      dir,
      envFallback: new ScriptedEnvFallback({ kind: "oauth", oauthToken: FAKE }),
    });
    // no per-tenant file -> env fallback resolves (linked, but no file mtime)
    expect(await store.get("demo-tenant")).toEqual({ kind: "oauth", oauthToken: FAKE });
    expect(await store.status("demo-tenant")).toEqual({ linked: true, linked_at: null });

    // a per-tenant file WINS over the env fallback
    await store.put("demo-tenant", FAKE2);
    expect(await store.get("demo-tenant")).toEqual({ kind: "oauth", oauthToken: FAKE2 });
    expect((await store.status("demo-tenant")).linked_at).toBeTypeOf("string");
  });

  it("rejects traversal / separator tenant ids (cannot escape the grants dir)", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await expect(store.put("../evil", FAKE)).rejects.toThrow(/invalid tenant id/);
    await expect(store.get("a/b")).rejects.toThrow(/invalid tenant id/);
    await expect(store.status("..")).rejects.toThrow(/invalid tenant id/);
  });
});
