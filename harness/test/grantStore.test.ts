/**
 * FileTenantGrantStore — per-tenant grant persistence (wave 4).
 *
 * Hermetic: temp dir + FAKE tokens only. NEVER touches the real grant at
 * C:/tmp/hosted-oauth-spike/.grant/token — the demo-tenant env fallback is injected as a
 * scripted fake so the real file/env path is never constructed here.
 */

import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createTenantGrantStore, FileTenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
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

    // New writes publish one atomic token+kind record.
    const file = join(dir, "acme.grant.json");
    expect(existsSync(file)).toBe(true);
    const persisted = JSON.parse(readFileSync(file, "utf8"));
    expect(persisted.version).toBe(3);
    expect(persisted.accounts).toHaveLength(1);
    expect(persisted.accounts[0]).toMatchObject({ kind: "oauth", token: FAKE });
    expect(persisted.active_account_id).toBe(persisted.accounts[0].id);
  });

  it("status NEVER returns the token (only linked + linked_at + kind)", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE);
    const status = await store.status("acme");
    // §17: status now also carries `kind` — still NEVER the token.
    expect(Object.keys(status).sort()).toEqual([
      "accounts", "active_account_id", "kind", "linked", "linked_at",
    ]);
    expect(status.kind).toBe("oauth");
    expect(JSON.stringify(status)).not.toContain(FAKE);
  });

  it("unlinked non-demo tenant -> get null, status linked:false", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    expect(await store.get("nobody")).toBeNull();
    expect(await store.status("nobody")).toEqual({ linked: false, linked_at: null });
  });

  it("put adds accounts, activate selects one, and targeted remove preserves the survivor", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    const first = await store.put("acme", FAKE, "oauth", "first@example.com");
    const firstId = first.active_account_id!;
    const second = await store.put("acme", FAKE2, "oauth", "second@example.com");
    const secondId = second.active_account_id!;
    expect(second.accounts).toHaveLength(2);
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE2 });

    await store.activate("acme", firstId);
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE });
    await store.remove("acme", firstId);
    expect((await store.status("acme")).active_account_id).toBe(secondId);
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE2 });
    await store.remove("acme", secondId);
    expect(await store.get("acme")).toBeNull();
    expect((await store.status("acme")).linked).toBe(false);
    await expect(store.remove("acme")).resolves.toBeUndefined(); // idempotent
  });

  it("routes Team and Enterprise mounts by settled token usage without crossing tenants", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE, "oauth", "team-primary", "team");
    await store.put("acme", FAKE2, "oauth", "enterprise-secondary", "enterprise");
    await store.put("other", "FAKE-OAUTH-other-777888999", "oauth", "other-team", "team");

    const first = await store.acquire("acme");
    expect(first.grant).toMatchObject({ kind: "oauth" });
    await store.settle("acme", first.lease_id, {
      usage: { cost_tokens: 12_000 },
      stop_reason: "end_turn",
    });

    const second = await store.acquire("acme");
    expect(second.account_id).not.toBe(first.account_id);
    expect(second.grant).not.toEqual(first.grant);
    const other = await store.acquire("other");
    expect(other.grant).toEqual({ kind: "oauth", oauthToken: "FAKE-OAUTH-other-777888999" });

    const status = await store.status("acme");
    expect(JSON.stringify(status)).not.toContain(FAKE);
    expect(JSON.stringify(status)).not.toContain(FAKE2);
    expect(status.accounts?.every((account) => account.eligible)).toBe(true);
    expect(status.accounts?.find((account) => account.id === first.account_id)?.usage_tokens).toBe(12_000);
  });

  it("keeps unattested OAuth mounts out of automatic routing", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE, "oauth", "legacy subscription");

    await expect(store.acquire("acme")).rejects.toThrow(/Team or Enterprise/i);
    expect((await store.status("acme")).accounts?.[0]).toMatchObject({
      plan: null,
      eligible: false,
    });
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE });
  });

  it("skips a quota-cooled mount and fails closed when every eligible mount is cooling", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE, "oauth", "team-one", "team");
    await store.put("acme", FAKE2, "oauth", "team-two", "team");

    const first = await store.acquire("acme");
    await store.settle("acme", first.lease_id, {
      usage: { cost_tokens: 0 },
      stop_reason: "llm_quota_exhausted",
      retry_after_s: 600,
    });
    const second = await store.acquire("acme");
    expect(second.account_id).not.toBe(first.account_id);
    await store.settle("acme", second.lease_id, {
      usage: { cost_tokens: 0 },
      stop_reason: "llm_quota_exhausted",
      retry_after_s: 600,
    });

    await expect(store.acquire("acme")).rejects.toThrow(/temporarily unavailable/i);
    const status = await store.status("acme");
    expect(status.accounts?.every((account) => typeof account.cooldown_until === "string")).toBe(true);
  });

  it("reserves concurrent acquisitions so simultaneous turns do not choose one mount", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE, "oauth", "team-one", "team");
    await store.put("acme", FAKE2, "oauth", "team-two", "team");

    const [one, two] = await Promise.all([store.acquire("acme"), store.acquire("acme")]);
    expect(one.account_id).not.toBe(two.account_id);
    expect(one.lease_id).not.toBe(two.lease_id);
  });

  it("keeps v1 records readable and migrates them when a second account is added", async () => {
    writeFileSync(join(dir, "acme.grant.json"), JSON.stringify({ version: 1, kind: "oauth", token: FAKE }), "utf8");
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    expect((await store.status("acme")).active_account_id).toBe("legacy");
    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE });

    const status = await store.put("acme", FAKE2, "oauth", "new@example.com");
    expect(status.accounts).toHaveLength(2);
    expect(status.accounts?.map((account) => account.label)).toEqual([
      "Claude subscription", "new@example.com",
    ]);
    expect(JSON.stringify(status)).not.toContain(FAKE);
    expect(JSON.stringify(status)).not.toContain(FAKE2);
  });

  it("rejects selection and removal of an unknown account", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE);
    await expect(store.activate("acme", "not-owned")).rejects.toThrow(/not found/);
    await expect(store.remove("acme", "not-owned")).rejects.toThrow(/not found/);
  });

  it("ignores an interrupted temp record and keeps the last complete grant", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("acme", FAKE, "oauth");
    writeFileSync(
      join(dir, "acme.grant.json.123.interrupted.tmp"),
      '{"version":1,"kind":"api_key","token":"truncated',
      "utf8",
    );

    expect(await store.get("acme")).toEqual({ kind: "oauth", oauthToken: FAKE });
    expect((await store.status("acme")).kind).toBe("oauth");
  });

  it("fails closed on a truncated published record instead of reading stale legacy files", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    writeFileSync(join(dir, "acme.token"), FAKE2, "utf8");
    writeFileSync(join(dir, "acme.kind"), "oauth", "utf8");
    writeFileSync(join(dir, "acme.grant.json"), '{"version":1,"kind":"api_key","token":"cut', "utf8");

    await expect(store.get("acme")).rejects.toThrow(/invalid persisted grant record/);
    await expect(store.status("acme")).rejects.toThrow(/invalid persisted grant record/);
  });

  it("demo-tenant falls back to the env grant when it has no per-tenant file; a file overrides it", async () => {
    const store = new FileTenantGrantStore({
      dir,
      envFallback: new ScriptedEnvFallback({ kind: "oauth", oauthToken: FAKE }),
    });
    // no per-tenant file -> env fallback resolves (linked, but no file mtime)
    expect(await store.get("demo-tenant")).toEqual({ kind: "oauth", oauthToken: FAKE });
    // §17: env-fallback status carries the fallback grant's kind (oauth).
    expect(await store.status("demo-tenant")).toEqual({ linked: true, linked_at: null, kind: "oauth" });

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

  // ------------------------------------------------------------------------- //
  // §17 — BYO API key as a grant kind
  // ------------------------------------------------------------------------- //
  const FAKE_API = "sk-ant-api03-FAKE-not-a-real-key-999888777";
  const FAKE_OAT = "sk-ant-oat01-FAKE-not-a-real-token-111222";

  it("explicit kind:'api_key' -> get returns an api_key grant; status.kind is api_key", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    const status = await store.put("ent", FAKE, "api_key"); // explicit kind wins over prefix
    expect(status).toMatchObject({ linked: true, linked_at: status.linked_at, kind: "api_key" });
    expect(await store.get("ent")).toEqual({ kind: "api_key", apiKey: FAKE });
    expect((await store.status("ent")).kind).toBe("api_key");
    expect(JSON.stringify(await store.status("ent"))).not.toContain(FAKE);
  });

  it("auto-detects api_key from the sk-ant-api prefix when kind is omitted", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("ent2", FAKE_API); // no explicit kind
    expect(await store.get("ent2")).toEqual({ kind: "api_key", apiKey: FAKE_API });
    expect((await store.status("ent2")).kind).toBe("api_key");
  });

  it("auto-detects oauth from the sk-ant-oat prefix; unknown prefix defaults to oauth", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("oat", FAKE_OAT);
    expect(await store.get("oat")).toEqual({ kind: "oauth", oauthToken: FAKE_OAT });
    await store.put("plain", FAKE); // no recognizable prefix -> oauth
    expect(await store.get("plain")).toEqual({ kind: "oauth", oauthToken: FAKE });
  });

  it("remove clears the atomic record (a later put re-detects fresh)", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    await store.put("ent3", FAKE, "api_key");
    await store.remove("ent3");
    expect(await store.get("ent3")).toBeNull();
    // no stale record: a fresh oauth token now resolves as oauth, not api_key.
    await store.put("ent3", FAKE_OAT);
    expect(await store.get("ent3")).toEqual({ kind: "oauth", oauthToken: FAKE_OAT });
  });

  it("legacy token file with NO kind sidecar falls back to prefix detection", async () => {
    // simulate a pre-§17 store: write only the .token file, no .kind sidecar.
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    writeFileSync(join(dir, "legacy.token"), FAKE_API, "utf8");
    expect(await store.get("legacy")).toEqual({ kind: "api_key", apiKey: FAKE_API });
    expect((await store.status("legacy")).kind).toBe("api_key");
  });

  it("legacy token and kind sidecar remain readable", async () => {
    const store = new FileTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
    writeFileSync(join(dir, "legacy-pair.token"), FAKE, "utf8");
    writeFileSync(join(dir, "legacy-pair.kind"), "api_key", "utf8");
    expect(await store.get("legacy-pair")).toEqual({ kind: "api_key", apiKey: FAKE });
    expect((await store.status("legacy-pair")).kind).toBe("api_key");
  });
});

/**
 * F18 grant-store seam (security-audit 2026-07-18): the live serve path selects the
 * backend via createTenantGrantStore(). `vault` requested-but-unwired must throw —
 * never silently fall back to on-disk token files.
 */
describe("createTenantGrantStore (F18 seam)", () => {
  const saved = process.env.LEAF_GRANT_STORE;
  afterEach(() => {
    if (saved === undefined) delete process.env.LEAF_GRANT_STORE;
    else process.env.LEAF_GRANT_STORE = saved;
  });

  it("default / file backend yields a working FileTenantGrantStore", async () => {
    delete process.env.LEAF_GRANT_STORE;
    const dir = mkdtempSync(join(tmpdir(), "leaf-grants-seam-"));
    try {
      const store = createTenantGrantStore({ dir, envFallback: new ScriptedEnvFallback(null) });
      expect(store).toBeInstanceOf(FileTenantGrantStore);
      await store.put("seam", FAKE);
      expect(await store.get("seam")).toEqual({ kind: "oauth", oauthToken: FAKE });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("LEAF_GRANT_STORE=vault fails LOUDLY (unwired seam, no silent disk fallback)", () => {
    process.env.LEAF_GRANT_STORE = "vault";
    expect(() => createTenantGrantStore()).toThrowError(/vault/);
  });
});
