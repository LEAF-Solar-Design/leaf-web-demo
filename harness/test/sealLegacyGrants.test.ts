import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { classifyTenant, main, sealTenants } from "../scripts/sealLegacyGrants.js";
import { EnvelopeTenantGrantStore, FileTenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { LocalKeyProvider } from "../src/security/grantEnvelope/keyProvider.js";
import type { KeyProvider } from "../src/security/grantEnvelope/keyProvider.js";
import * as fromEnv from "../src/security/grantEnvelope/fromEnv.js";

const TOKEN = "SYNTHETIC-secret-token-3487";
const LABEL = "private-account-label";
const ACCOUNT = "private-account-id";
const KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc";
const DATE = "2026-01-02T00:00:00.000Z";

describe("sealLegacyGrants", () => {
  let dir: string;
  let keys: LocalKeyProvider;
  let output: string[];
  let errors: string[];
  const path = (tenant: string, suffix = ".grant.json") => join(dir, `${tenant}${suffix}`);
  const write = (tenant: string, value: unknown) => writeFileSync(path(tenant), JSON.stringify(value) + "\n");
  const snapshot = () => Object.fromEntries(readdirSync(dir).sort().map(name => [name, {
    bytes: readFileSync(join(dir, name)).toString("base64"),
    mode: statSync(join(dir, name)).mode,
    mtime: statSync(join(dir, name)).mtimeMs,
  }]));
  const account = (id = ACCOUNT) => ({ id, label: LABEL, kind: "oauth", token: TOKEN, linked_at: DATE });
  const v3Account = (id = ACCOUNT) => ({
    ...account(id), plan: "team", usage_tokens: 123, selection_count: 4,
    last_used_at: DATE, cooldown_until: "2099-01-01T00:00:00.000Z",
    leases: [{ id: "private-lease", expires_at: "2099-01-02T00:00:00.000Z" }],
  });
  const env = () => ({ LEAF_GRANTS_DIR: dir, LEAF_GRANT_STORE: " VAULT ", LEAF_GRANT_KMS_KEY_ARN: KEY_ARN });
  const summary = () => JSON.parse(output[output.length - 1]);
  const sidecars = (tenant: string) => {
    writeFileSync(path(tenant, ".token"), TOKEN);
    writeFileSync(path(tenant, ".kind"), "oauth");
  };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "seal-legacy-grants-"));
    keys = new LocalKeyProvider(Buffer.alloc(32, 7));
    output = [];
    errors = [];
    vi.spyOn(console, "log").mockImplementation(value => { output.push(String(value)); });
    vi.spyOn(console, "error").mockImplementation(value => { errors.push(String(value)); });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    rmSync(dir, { recursive: true, force: true });
  });

  it("classifies record shapes, sidecars and absent tenants without a key provider", () => {
    for (const version of [1, 2, 3]) write(`plain${version}`, { version });
    write("sealed", { v: 1, alg: "A256GCM", iv: "", ct: "", tag: "", wk: "" });
    write("mixed", { v: 1, alg: "A256GCM", iv: "", ct: "", tag: "", wk: "", version: 9 });
    write("null", null);
    write("wrong", { version: 4 });
    writeFileSync(path("broken"), "not json");
    sidecars("only");
    writeFileSync(path("kindonly", ".kind"), "oauth");
    for (const tenant of ["plain1", "plain2", "plain3"]) expect(classifyTenant(dir, tenant).class).toBe("plaintext");
    expect(classifyTenant(dir, "sealed").class).toBe("sealed");
    for (const tenant of ["mixed", "null", "wrong", "broken"]) expect(classifyTenant(dir, tenant).class).toBe("corrupt");
    expect(classifyTenant(dir, "only")).toEqual({ tenant: "only", class: "token_only", legacy_sidecar: true });
    expect(classifyTenant(dir, "kindonly")).toEqual({ tenant: "kindonly", class: "token_only", legacy_sidecar: true });
    expect(classifyTenant(dir, "missing")).toEqual({ tenant: "missing", class: "absent", legacy_sidecar: false });
    expect(() => classifyTenant(dir, "../escape")).toThrow();
  });

  it("dry-run preserves directory bytes and metadata, skips invalid names and never configures KMS", async () => {
    write("plain", { version: 1, kind: "oauth", token: TOKEN });
    sidecars("plain");
    writeFileSync(join(dir, "INVALID.token"), TOKEN);
    const configure = vi.spyOn(fromEnv, "grantKeyProviderFromEnv");
    const before = snapshot();
    expect(await main(["--dry-run"], env())).toBe(0);
    expect(snapshot()).toEqual(before);
    expect(configure).not.toHaveBeenCalled();
    expect(output.map(line => JSON.parse(line))).toEqual([
      { tenant: "plain", class: "plaintext", legacy_sidecar: true },
      { sealed: 0, legacy_remaining: 1, blocked_tenants: 0, outside_scope_legacy: 0 },
    ]);
    expect((await sealTenants(dir, ["missing"], keys)).skipped_names).toBe(1);
    expect(readFileSync(join(dir, "INVALID.token"), "utf8")).toBe(TOKEN);
  });

  it.each([1, 2, 3])("seals v%s via the store while retaining accounts and routing metadata", async version => {
    const accounts = version === 3 ? [v3Account(), v3Account("second-account")] : [account(), account("second-account")];
    write("tenant", version === 1 ? { version, kind: "oauth", token: TOKEN }
      : { version, active_account_id: ACCOUNT, accounts });
    const oldStore = new FileTenantGrantStore({ dir, envFallback: { get: async () => null } });
    const before = await oldStore.status("tenant");
    const grant = await oldStore.get("tenant");
    sidecars("tenant");
    const result = await sealTenants(dir, undefined, keys);
    expect(result.outcomes).toEqual([{ tenant: "tenant", outcome: "sealed", reason: null }]);
    expect(result.summary).toEqual({ sealed: 1, legacy_remaining: 0, blocked_tenants: 0, outside_scope_legacy: 0 });
    const reader = new EnvelopeTenantGrantStore({ dir, keyProvider: keys });
    expect(await reader.get("tenant")).toEqual(grant);
    const after = await reader.status("tenant");
    expect(after.active_account_id).toBe(before.active_account_id);
    expect(after.accounts?.map(row => ({ id: row.id, label: row.label, plan: row.plan ?? null })))
      .toEqual(before.accounts?.map(row => ({ id: row.id, label: row.label, plan: row.plan ?? null })));
    expect(await reader.diagnostic("tenant")).toMatchObject({ backend: "vault", record_format: "v3", linked: true });
    if (version === 3) {
      expect(after.accounts).toEqual(before.accounts);
      // Independently open the sealed payload to cover metadata absent from status.
      const { openGrant } = await import("../src/security/grantEnvelope/envelope.js");
      const bytes = await openGrant(JSON.parse(readFileSync(path("tenant"), "utf8")), {
        tenantId: "tenant", grantId: "tenant.grant.json",
      }, keys);
      expect(JSON.parse(bytes.toString())).toEqual({ version: 3, active_account_id: ACCOUNT, accounts });
    }
    expect(readFileSync(path("tenant"), "utf8")).not.toContain(TOKEN);
    expect(existsSync(path("tenant", ".token"))).toBe(false);
    expect(existsSync(path("tenant", ".kind"))).toBe(false);
    const sealed = snapshot();
    expect((await sealTenants(dir, undefined, keys)).outcomes).toEqual([
      { tenant: "tenant", outcome: "already_sealed", reason: null },
    ]);
    expect(snapshot()).toEqual(sealed);
  });

  it("restores exact original bytes on a read-back failure and keeps sidecars", async () => {
    const original = Buffer.from(` { "version": 1, "kind": "oauth", "token": "${TOKEN}" }\n\n`);
    writeFileSync(path("bad"), original);
    sidecars("bad");
    const broken: KeyProvider = {
      keyId: () => keys.keyId(), wrap: (key, context) => keys.wrap(key, context),
      unwrap: async () => { throw new Error(`${TOKEN} ${LABEL} ${dir}`); },
    };
    const result = await sealTenants(dir, undefined, broken);
    expect(result.outcomes).toEqual([{ tenant: "bad", outcome: "blocked", reason: "verify_failed" }]);
    expect(readFileSync(path("bad"))).toEqual(original);
    expect(readFileSync(path("bad", ".token"), "utf8")).toBe(TOKEN);
    expect(existsSync(path("bad", ".kind"))).toBe(true);
    expect(result.summary).toMatchObject({ sealed: 0, legacy_remaining: 1, blocked_tenants: 1 });
  });

  it("uses the injectable restore writer on separate verifier failure", async () => {
    write("tenant", { version: 1, kind: "oauth", token: TOKEN });
    sidecars("tenant");
    const original = readFileSync(path("tenant"));
    vi.spyOn(EnvelopeTenantGrantStore.prototype, "diagnostic").mockResolvedValue({
      backend: "vault", record_format: "invalid", linked: false,
    } as Awaited<ReturnType<EnvelopeTenantGrantStore["diagnostic"]>>);
    const restore = vi.fn((target: string, bytes: Buffer) => { writeFileSync(target, bytes); });
    const result = await sealTenants(dir, undefined, keys, restore);
    expect(restore).toHaveBeenCalledWith(path("tenant"), original);
    expect(result.outcomes[0].reason).toBe("verify_failed");
    expect(readFileSync(path("tenant"))).toEqual(original);
    expect(existsSync(path("tenant", ".token"))).toBe(true);
    expect(existsSync(path("tenant", ".kind"))).toBe(true);
  });

  it("verifies sealed sidecars before removing them", async () => {
    const store = new EnvelopeTenantGrantStore({ dir, keyProvider: keys });
    await store.put("tenant", TOKEN);
    sidecars("tenant");
    const bytes = readFileSync(path("tenant"));
    const wrongKey = new LocalKeyProvider(Buffer.alloc(32, 8));
    expect((await sealTenants(dir, undefined, wrongKey)).outcomes[0].outcome).toBe("blocked");
    expect(existsSync(path("tenant", ".token"))).toBe(true);
    expect(existsSync(path("tenant", ".kind"))).toBe(true);
    expect((await sealTenants(dir, undefined, keys)).outcomes[0].outcome).toBe("already_sealed");
    expect(readFileSync(path("tenant"))).toEqual(bytes);
    expect(existsSync(path("tenant", ".token"))).toBe(false);
    expect(existsSync(path("tenant", ".kind"))).toBe(false);
  });

  it("keeps plaintext and sidecars on a sealing error and continues to the next tenant", async () => {
    for (const tenant of ["bad", "good"]) write(tenant, { version: 1, kind: "oauth", token: TOKEN });
    sidecars("bad");
    const original = snapshot();
    const provider: KeyProvider = {
      keyId: () => keys.keyId(),
      wrap: async (key, context) => {
        if (context.tenantId === "bad") throw new Error(TOKEN);
        return keys.wrap(key, context);
      },
      unwrap: (wrapped, context) => keys.unwrap(wrapped, context),
    };
    const result = await sealTenants(dir, undefined, provider);
    expect(result.outcomes).toEqual([
      { tenant: "bad", outcome: "blocked", reason: "error" },
      { tenant: "good", outcome: "sealed", reason: null },
    ]);
    const after = snapshot();
    for (const name of ["bad.grant.json", "bad.token", "bad.kind"]) expect(after[name]).toEqual(original[name]);
  });

  it("reports a restore writer failure as blocked and retains both sidecars", async () => {
    write("tenant", { version: 1, kind: "oauth", token: TOKEN });
    sidecars("tenant");
    const broken: KeyProvider = {
      keyId: () => keys.keyId(), wrap: (key, context) => keys.wrap(key, context),
      unwrap: async () => { throw new Error(TOKEN); },
    };
    const restore = vi.fn(() => { throw new Error(dir); });
    const result = await sealTenants(dir, undefined, broken, restore);
    expect(restore).toHaveBeenCalledOnce();
    expect(result.outcomes).toEqual([{ tenant: "tenant", outcome: "blocked", reason: "error" }]);
    expect(result.summary.blocked_tenants).toBe(1);
    expect(existsSync(path("tenant", ".token"))).toBe(true);
    expect(existsSync(path("tenant", ".kind"))).toBe(true);
  });

  it("blocks token-only and corrupt tenants, continues after errors, and does not disclose secrets", async () => {
    sidecars("only");
    write("corrupt", { secret: TOKEN });
    write("invalid", { version: 3 });
    write("valid", { version: 2, active_account_id: ACCOUNT, accounts: [account()] });
    const only = readFileSync(path("only", ".token"));
    const corrupt = readFileSync(path("corrupt"));
    vi.spyOn(fromEnv, "grantKeyProviderFromEnv").mockReturnValue(keys);
    expect(await main(["--seal", "--tenant", "only", "--tenant", "corrupt", "--tenant", "invalid", "--tenant", "valid", "--tenant", "absent"], env())).toBe(1);
    expect(output.slice(0, -1).map(line => JSON.parse(line))).toEqual([
      { tenant: "absent", outcome: "absent", reason: null },
      { tenant: "corrupt", outcome: "blocked", reason: "corrupt_record" },
      { tenant: "invalid", outcome: "blocked", reason: "error" },
      { tenant: "only", outcome: "blocked", reason: "token_only_relink" },
      { tenant: "valid", outcome: "sealed", reason: null },
    ]);
    expect(summary()).toMatchObject({ blocked_tenants: 3, sealed: 1, legacy_remaining: 2 });
    expect(readFileSync(path("only", ".token"))).toEqual(only);
    expect(readFileSync(path("corrupt"))).toEqual(corrupt);
    for (const secret of [TOKEN, LABEL, ACCOUNT, dir]) expect([...output, ...errors].join("\n")).not.toContain(secret);
  });

  it("blocks a kind-only tenant with a non-zero exit and preserves the sidecar", async () => {
    writeFileSync(path("kindonly", ".kind"), "oauth");
    const before = snapshot();
    vi.spyOn(fromEnv, "grantKeyProviderFromEnv").mockReturnValue(keys);
    expect(await main(["--seal"], env())).toBe(1);
    expect(output.map(line => JSON.parse(line))).toEqual([
      { tenant: "kindonly", outcome: "blocked", reason: "token_only_relink" },
      { sealed: 0, legacy_remaining: 1, blocked_tenants: 1, outside_scope_legacy: 0 },
    ]);
    expect(snapshot()).toEqual(before);
  });

  it("limits scope, counts outside legacy and makes the CLI rerun a no-op", async () => {
    write("chosen", { version: 1, kind: "oauth", token: TOKEN });
    write("outside", { version: 1, kind: "oauth", token: TOKEN });
    sidecars("sidecar");
    vi.spyOn(fromEnv, "grantKeyProviderFromEnv").mockReturnValue(keys);
    const outside = readFileSync(path("outside"));
    expect(await main(["--seal", "--tenant", "chosen", "--tenant", "chosen"], env())).toBe(0);
    expect(summary()).toEqual({ sealed: 1, legacy_remaining: 0, blocked_tenants: 0, outside_scope_legacy: 2 });
    expect(readFileSync(path("outside"))).toEqual(outside);
    const before = snapshot();
    output.length = 0;
    expect(await main(["--seal", "--tenant", "chosen"], env())).toBe(0);
    expect(JSON.parse(output[0]).outcome).toBe("already_sealed");
    expect(summary().sealed).toBe(0);
    expect(snapshot()).toEqual(before);
  });

  it.each([{}, { LEAF_GRANT_STORE: "file" }, { LEAF_GRANT_STORE: "vault" },
    { LEAF_GRANT_STORE: "vault", LEAF_GRANT_KMS_KEY_ARN: "invalid-sensitive-arn" }])
  ("refuses invalid seal configuration without writes", async config => {
    write("tenant", { version: 1, kind: "oauth", token: TOKEN });
    const before = snapshot();
    expect(await main(["--seal"], { ...config, LEAF_GRANTS_DIR: dir })).toBe(2);
    expect(snapshot()).toEqual(before);
    expect(errors).toEqual(["Seal requires vault mode and a configured KMS key provider"]);
    expect(output).toHaveLength(1);
    expect(summary().sealed).toBe(0);
  });

  it.each([[], ["--seal", "--seal"], ["--dry-run", "--dry-run"], ["--seal", "--dry-run"],
    ["--unknown"], ["--tenant"], ["--dry-run", "--tenant"], ["--dry-run", "--tenant", "../escape"],
    ["--dry-run", "--tenant", "UPPER"], ["--dry-run", "--tenant", "a".repeat(64)],
    ["--dry-run", "--tenant", ""], ["--dry-run", "positional"]].map(args => [args] as const))
  ("rejects argv %j with fixed usage and no writes", async args => {
    const before = snapshot();
    expect(await main(args, env())).toBe(2);
    expect(snapshot()).toEqual(before);
    expect(errors).toEqual(["Usage: sealLegacyGrants.js (--dry-run | --seal) [--tenant <id>]..."]);
    expect(output).toHaveLength(1);
  });
});
