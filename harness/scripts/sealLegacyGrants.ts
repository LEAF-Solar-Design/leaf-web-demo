// Run sealing in maintenance posture with no concurrent grant writes. These checks
// narrow the race window but do not replace that operational requirement.
// Classification's `sealed` means envelope-shaped only; only --seal opens it.
import { randomUUID } from "node:crypto";
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { EnvelopeTenantGrantStore, resolveGrantsDir } from "../src/ports/impl/oauthGrantProvider.js";
import type { GrantStatus } from "../src/ports/index.js";
import { grantKeyProviderFromEnv } from "../src/security/grantEnvelope/fromEnv.js";
import { sealGrant } from "../src/security/grantEnvelope/envelope.js";
import type { KeyProvider } from "../src/security/grantEnvelope/keyProvider.js";

const TENANT = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const USAGE = "Usage: sealLegacyGrants.js (--dry-run | --seal) [--tenant <id>]...";
const REFUSAL = "Seal requires vault mode and a configured KMS key provider";
type Classification = "sealed" | "plaintext" | "token_only" | "corrupt" | "absent";
type Outcome = { tenant: string; outcome: "sealed" | "already_sealed" | "blocked" | "absent"; reason: string | null };
type Summary = { sealed: number; legacy_remaining: number; blocked_tenants: number; outside_scope_legacy: number; skipped_names: number };
export type RestoreWriter = (path: string, bytes: Buffer) => void | Promise<void>;

function plaintext(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && "version" in value
    && [1, 2, 3].includes((value as Record<string, unknown>).version as number);
}

export function classifyTenant(dir: string, tenant: string): { tenant: string; class: Classification; legacy_sidecar: boolean } {
  if (!TENANT.test(tenant)) throw new Error("Invalid tenant");
  const token = existsSync(join(dir, `${tenant}.token`));
  const legacy_sidecar = token || existsSync(join(dir, `${tenant}.kind`));
  const path = join(dir, `${tenant}.grant.json`);
  let classification: Classification = legacy_sidecar ? "token_only" : "absent";
  if (existsSync(path)) {
    classification = "corrupt";
    try {
      const value: unknown = JSON.parse(readFileSync(path, "utf8"));
      if (plaintext(value)) classification = "plaintext";
      else if (typeof value === "object" && value !== null) {
        const record = value as Record<string, unknown>;
        if (!("version" in record) && record.v === 1 && record.alg === "A256GCM"
          && ["iv", "ct", "tag", "wk"].every(key => typeof record[key] === "string")) classification = "sealed";
      }
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
    }
  }
  return { tenant, class: classification, legacy_sidecar };
}

function inventory(dir: string): { tenants: string[]; skipped_names: number } {
  const tenants = new Set<string>();
  let skipped_names = 0;
  for (const name of existsSync(dir) ? readdirSync(dir) : []) {
    const suffix = [".grant.json", ".token", ".kind"].find(ending => name.endsWith(ending));
    if (!suffix) continue;
    const tenant = name.slice(0, -suffix.length);
    if (TENANT.test(tenant)) tenants.add(tenant);
    else skipped_names++;
  }
  return { tenants: [...tenants].sort(), skipped_names };
}

class GrantChangedDuringMigration extends Error {
  constructor() { super("grant changed during migration"); }
}

function sidecarBytes(path: string): Buffer | undefined {
  try { return readFileSync(path); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

function requireSidecarBytes(path: string, expected: Buffer | undefined): void {
  const current = sidecarBytes(path);
  if (expected ? !current?.equals(expected) : current !== undefined) throw new GrantChangedDuringMigration();
}

function requireCurrentBytes(path: string, expected: readonly Buffer[]): void {
  let current: Buffer;
  try { current = readFileSync(path); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") throw new GrantChangedDuringMigration();
    throw error;
  }
  if (!expected.some(bytes => current.equals(bytes))) throw new GrantChangedDuringMigration();
}

class MigratingEnvelopeStore extends EnvelopeTenantGrantStore {
  private readonly snapshots = new Map<string, Buffer>();
  private readonly published = new Map<string, Buffer>();
  private readonly migrationKeys: KeyProvider;

  constructor(opts: { dir: string; keyProvider: KeyProvider }) {
    super(opts);
    this.migrationKeys = opts.keyProvider;
  }

  protected override async readRecordPayload(tenantId: string, path: string): Promise<unknown> {
    if (existsSync(path)) {
      const bytes = readFileSync(path);
      const value: unknown = JSON.parse(bytes.toString("utf8"));
      if (plaintext(value)) {
        this.snapshots.set(path, bytes);
        return value;
      }
    }
    return super.readRecordPayload(tenantId, path);
  }

  protected override async writeRecord(tenantId: string, record: unknown): Promise<void> {
    const path = this.recordFile(tenantId);
    const snapshot = this.snapshots.get(path);
    if (!snapshot) throw new GrantChangedDuringMigration();
    const sealed = await sealGrant(JSON.stringify(record), {
      tenantId, grantId: `${tenantId}.grant.json`,
    }, this.migrationKeys);
    const bytes = Buffer.from(JSON.stringify(sealed) + "\n");
    atomicRestore(path, bytes, () => requireCurrentBytes(path, [snapshot]),
      () => { this.published.set(path, bytes); });
  }

  requireRestorable(path: string, original: Buffer): void {
    const sealed = this.published.get(path);
    requireCurrentBytes(path, sealed ? [original, sealed] : [original]);
  }
}

function atomicRestore(path: string, bytes: Buffer, beforePublish?: () => void, onPublished?: () => void): void {
  const temp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  let fd: number | undefined;
  try {
    fd = openSync(temp, "wx", 0o600);
    writeFileSync(fd, bytes);
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    // Keep the optimistic check and rename synchronous, with no await between them.
    beforePublish?.();
    renameSync(temp, path);
    onPublished?.();
    // Directory fsync is unavailable on Windows. Other failures must surface.
    if (process.platform !== "win32") {
      try {
        const directory = openSync(dirname(path), "r");
        try { fsyncSync(directory); } finally { closeSync(directory); }
      } catch (error) {
        if (!["EINVAL", "ENOTSUP", "EOPNOTSUPP"].includes((error as NodeJS.ErrnoException).code ?? "")) throw error;
      }
    }
  } finally {
    if (fd !== undefined) closeSync(fd);
    rmSync(temp, { force: true });
  }
}

async function verified(store: EnvelopeTenantGrantStore, tenant: string, before: GrantStatus): Promise<boolean> {
  const diagnostic = await store.diagnostic(tenant);
  const after = await store.status(tenant);
  const ids = (status: GrantStatus) => (status.accounts ?? []).map(account => account.id).sort();
  return diagnostic.backend === "vault" && diagnostic.record_format === "v3" && diagnostic.linked
    && before.linked && after.linked && !!before.active_account_id
    && after.active_account_id === before.active_account_id
    && ids(before).length > 0 && JSON.stringify(ids(before)) === JSON.stringify(ids(after));
}

function summarize(dir: string, scope: string[], all: string[], sealed: number, blocked_tenants: number, skipped_names: number): Summary {
  const legacy = (tenant: string) => {
    try {
      const record = classifyTenant(dir, tenant);
      return record.class === "plaintext" || record.legacy_sidecar;
    } catch {
      // An unreadable record cannot be certified free of legacy credentials.
      return true;
    }
  };
  return {
    sealed, legacy_remaining: scope.filter(legacy).length, blocked_tenants, skipped_names,
    outside_scope_legacy: all.filter(tenant => !scope.includes(tenant) && legacy(tenant)).length,
  };
}

export async function sealTenants(dir: string, tenantScope: readonly string[] | undefined, keyProvider: KeyProvider, restore: RestoreWriter = atomicRestore) {
  if (tenantScope?.some(tenant => !TENANT.test(tenant))) throw new Error("Invalid tenant");
  const lock = join(dir, ".sealLegacyGrants.lock");
  let fd: number;
  try { fd = openSync(lock, "wx", 0o600); } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    const found = inventory(dir);
    const scope = [...new Set(tenantScope?.length ? tenantScope : found.tenants)].sort();
    return { outcomes: [] as Outcome[], skipped_names: found.skipped_names,
      summary: summarize(dir, scope, found.tenants, 0, 1, found.skipped_names) };
  }
  try {
    return await sealTenantsLocked(dir, tenantScope, keyProvider, restore);
  } finally {
    try { closeSync(fd); } finally { rmSync(lock, { force: true }); }
  }
}

async function sealTenantsLocked(dir: string, tenantScope: readonly string[] | undefined, keyProvider: KeyProvider, restore: RestoreWriter) {
  const found = inventory(dir);
  const scope = [...new Set(tenantScope?.length ? tenantScope : found.tenants)].sort();
  const migrating = new MigratingEnvelopeStore({ dir, keyProvider });
  const reader = new EnvelopeTenantGrantStore({ dir, keyProvider });
  const outcomes: Outcome[] = [];
  for (const tenant of scope) {
    let outcome: Outcome = { tenant, outcome: "blocked", reason: "error" };
    try {
      const record = classifyTenant(dir, tenant);
      const sidecars = [".token", ".kind"].map(suffix => {
        const path = join(dir, `${tenant}${suffix}`);
        return { path, bytes: sidecarBytes(path) };
      });
      if (record.class === "token_only" || record.class === "corrupt") {
        outcome.reason = record.class === "token_only" ? "token_only_relink" : "corrupt_record";
      } else if (record.class === "absent") {
        outcome = { tenant, outcome: "absent", reason: null };
      } else if (record.class === "sealed" && !record.legacy_sidecar) {
        outcome.reason = "sealed_unreadable";
        const status = await reader.status(tenant);
        const diagnostic = await reader.diagnostic(tenant);
        if (diagnostic.backend === "vault" && diagnostic.record_format === "v3" && diagnostic.linked && status.linked) {
          outcome = { tenant, outcome: "already_sealed", reason: null };
        }
      } else {
        const path = join(dir, `${tenant}.grant.json`);
        const original = record.class === "plaintext" ? readFileSync(path) : undefined;
        const before = await (original ? migrating : reader).status(tenant);
        let pass = false;
        try {
          if (original) {
            if (!before.active_account_id) throw new Error("Missing active account");
            await migrating.activate(tenant, before.active_account_id);
          }
          pass = await verified(reader, tenant, before);
        } catch (error) {
          if (error instanceof GrantChangedDuringMigration) throw error;
          // activate itself reads back after publishing; include that read in verification.
          let unchanged = false;
          try { unchanged = !!original && readFileSync(path).equals(original); } catch { /* The guarded restore checks absence below. */ }
          if (unchanged) throw new Error("Conversion failed");
        }
        if (!pass) {
          if (original) {
            const guard = () => migrating.requireRestorable(path, original);
            if (restore === atomicRestore) atomicRestore(path, original, guard);
            else {
              guard();
              await restore(path, original);
            }
          }
          outcome.reason = "verify_failed";
        } else {
          // Check both before deleting either, then recheck immediately before each removal.
          for (const sidecar of sidecars) requireSidecarBytes(sidecar.path, sidecar.bytes);
          for (const sidecar of sidecars) {
            requireSidecarBytes(sidecar.path, sidecar.bytes);
            if (sidecar.bytes) rmSync(sidecar.path, { force: true });
          }
          outcome = { tenant, outcome: original ? "sealed" : "already_sealed", reason: null };
        }
      }
    } catch (error) {
      if (error instanceof GrantChangedDuringMigration) outcome.reason = "changed_during_migration";
      // Never expose store errors or tenant credential metadata.
    }
    outcomes.push(outcome);
  }
  return {
    outcomes, skipped_names: found.skipped_names,
    summary: summarize(dir, scope, found.tenants, outcomes.filter(row => row.outcome === "sealed").length,
      outcomes.filter(row => row.outcome === "blocked").length + (tenantScope?.length ? 0 : found.skipped_names), found.skipped_names),
  };
}

export async function main(argv: string[], env: NodeJS.ProcessEnv = process.env): Promise<number> {
  let summary: Summary = { sealed: 0, legacy_remaining: 0, blocked_tenants: 0, outside_scope_legacy: 0, skipped_names: 0 };
  const print = (value: unknown) => console.log(JSON.stringify(value));
  try {
    let mode: string | undefined;
    const tenants: string[] = [];
    for (let index = 0; index < argv.length; index++) {
      const arg = argv[index];
      if ((arg === "--dry-run" || arg === "--seal") && !mode) mode = arg;
      else if (arg === "--tenant" && TENANT.test(argv[index + 1] ?? "")) tenants.push(argv[++index]);
      else { console.error(USAGE); return 2; }
    }
    if (!mode) { console.error(USAGE); return 2; }
    let provider: KeyProvider | undefined;
    if (mode === "--seal") {
      try {
        if ((env.LEAF_GRANT_STORE ?? "").trim().toLowerCase() === "vault") provider = grantKeyProviderFromEnv(env);
      } catch { /* Configuration details must not enter output. */ }
      if (!provider) { console.error(REFUSAL); return 2; }
    }
    const dir = resolveGrantsDir(env);
    if (provider) {
      const result = await sealTenants(dir, tenants, provider);
      summary = result.summary;
      result.outcomes.forEach(print);
    } else {
      const found = inventory(dir);
      const scope = [...new Set(tenants.length ? tenants : found.tenants)].sort();
      scope.forEach(tenant => print(classifyTenant(dir, tenant)));
      summary = summarize(dir, scope, found.tenants, 0, 0, found.skipped_names);
    }
    return summary.blocked_tenants || (!tenants.length && summary.skipped_names)
      || (mode === "--seal" && summary.legacy_remaining) ? 1 : 0;
  } catch {
    console.error("Grant sealing failed");
    summary.blocked_tenants++;
    return 1;
  } finally { print(summary); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main(process.argv.slice(2), process.env);
}
