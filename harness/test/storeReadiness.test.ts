/**
 * W6-E04: the harness answers readiness for its own session and grant stores at
 * GET /ready (src/storeReadiness.ts), while GET /health stays the constant liveness
 * answer. Every probe is bounded, never creates a directory, and the body carries
 * only kinds, states and required flags.
 */

import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { open, unlink } from "node:fs/promises";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { FileTenantGrantStore, resolveGrantsDir } from "../src/ports/impl/oauthGrantProvider.js";
import type { TenantGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { FileSessionStore, resolveSessionsDir } from "../src/ports/impl/sessionStore.js";
import { createProbedSessionStore } from "../src/ports/impl/sessionStoreFactory.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import {
  createFileStoreProbe,
  createPgStoreProbe,
  createReadinessCheck,
  fileStoreProbe,
  pgStoreProbe,
  storeReadiness,
} from "../src/storeReadiness.js";
import type { PgProbeClient, ProbeFs, ProbeOutcome, StoreProbe, StoreReadiness } from "../src/storeReadiness.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "s3cr3t-harness-readiness-do-not-log-e04";
const HEALTH_BODY = JSON.stringify({ ok: true, service: "leaf-tenant-author-harness" });
const NO_ENV_FALLBACK: TenantGrantStore = { async get() { return null; } };

const readyProbe = (kind: "file" | "postgres" = "file"): StoreProbe<"file" | "postgres"> => ({
  kind,
  probe: async () => "ready",
});

function okClient(calls: string[]): PgProbeClient {
  return {
    async query(sql: string) {
      calls.push(sql);
      return { rows: [{ "?column?": 1 }] };
    },
    release(destroy?: boolean) {
      calls.push(`release:${destroy === true}`);
    },
  };
}

/** A ProbeFs whose creates always succeed and whose unlink fails with each listed code, in order, then succeeds. */
function recordingFs(unlinkFailures: string[]): { fs: ProbeFs; opened: string[]; unlinked: string[] } {
  const opened: string[] = [];
  const unlinked: string[] = [];
  const failures = [...unlinkFailures];
  const fs: ProbeFs = {
    async openExclusive(path: string) {
      opened.push(path);
      return {
        async write() { return undefined; },
        async sync() {},
        async close() {},
      };
    },
    async unlink(path: string) {
      unlinked.push(path);
      const code = failures.shift();
      if (code !== undefined) throw Object.assign(new Error(code), { code });
    },
  };
  return { fs, opened, unlinked };
}

function ports(grantAdmin: FileTenantGrantStore): HarnessPorts {
  return {
    oauth: new FakeOAuthGrantProvider(),
    grantAdmin,
    tenantRepo: new FakeTenantRepoProvider(FIXTURE),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
  };
}

function listen(
  grantAdmin: FileTenantGrantStore,
  readiness: () => Promise<StoreReadiness>,
): { server: Server; baseUrl: string } {
  // The caller-auth gate is ON: /ready, like /health, must answer without the secret.
  const server = createHarness(ports(grantAdmin), {
    auth: { enabled: true, secret: SECRET },
    readiness,
  }).listen(0);
  const addr = server.address() as AddressInfo;
  return { server, baseUrl: `http://127.0.0.1:${addr.port}` };
}

describe("W6-E04 harness store readiness", () => {
  let root: string;
  let server: Server | null = null;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-e04-ready-"));
  });
  afterEach(() => {
    server?.close();
    server = null;
    vi.unstubAllEnvs();
    rmSync(root, { recursive: true, force: true });
  });

  it("E04 row1 a writable file store directory is ready and leaves no file behind", async () => {
    // The store owns its directory; the probe only uses it.
    const dir = join(root, "sessions");
    mkdirSync(dir);
    expect(readdirSync(dir)).toEqual([]);
    await expect(fileStoreProbe(dir)).resolves.toBe("ready");
    expect(readdirSync(dir)).toEqual([]);
  });

  it("E04 row2 a missing directory is unavailable and is not created", async () => {
    const missing = join(root, "never-created", "grants");
    await expect(fileStoreProbe(missing)).resolves.toBe("unavailable");
    expect(existsSync(missing)).toBe(false);
    expect(existsSync(join(root, "never-created"))).toBe(false);
  });

  it("E04 row3 a directory that refuses writes is unavailable", async () => {
    let unlinkCalls = 0;
    const refusesCreate: ProbeFs = {
      async openExclusive() {
        throw Object.assign(new Error("EACCES: permission denied"), { code: "EACCES" });
      },
      async unlink() {
        unlinkCalls += 1;
        throw new Error("unlink must not run when nothing was created");
      },
    };
    await expect(fileStoreProbe(root, refusesCreate)).resolves.toBe("unavailable");
    expect(unlinkCalls).toBe(0);
    expect(readdirSync(root)).toEqual([]);
  });

  it("E04 row11 a write or sync that fails after the create removes the sentinel and is unavailable", async () => {
    // The create is real, so the sentinel really exists on disk until the probe removes it.
    for (const failing of ["write", "sync"] as const) {
      const dir = join(root, `store-${failing}`);
      mkdirSync(dir);
      const created: string[] = [];
      const unlinked: string[] = [];
      const failsAfterCreate: ProbeFs = {
        async openExclusive(path: string) {
          const handle = await open(path, "wx", 0o600);
          created.push(path);
          expect(readdirSync(dir)).toHaveLength(1);
          return {
            async write(data: Uint8Array) {
              if (failing === "write") throw Object.assign(new Error("ENOSPC"), { code: "ENOSPC" });
              return handle.write(data);
            },
            async sync() {
              if (failing === "sync") throw Object.assign(new Error("EIO"), { code: "EIO" });
              return handle.sync();
            },
            close: () => handle.close(),
          };
        },
        async unlink(path: string) {
          unlinked.push(path);
          await unlink(path);
        },
      };
      await expect(fileStoreProbe(dir, failsAfterCreate)).resolves.toBe("unavailable");
      expect(created).toHaveLength(1);
      expect(unlinked).toEqual(created);
      expect(dirname(created[0]!)).toBe(dir);
      expect(readdirSync(dir)).toEqual([]);
    }
  });

  it("E04 row12 a postgres query that never settles is destroyed once within the deadline and stays timeout", async () => {
    const releases: boolean[] = [];
    let queried = false;
    let settleQuery: (value: unknown) => void = () => {};
    const check = createReadinessCheck(
      {
        session: {
          kind: "postgres",
          probe: () =>
            pgStoreProbe(async () => ({
              query: () => {
                queried = true;
                return new Promise((settle) => { settleQuery = settle; });
              },
              release: (destroy?: boolean) => { releases.push(destroy === true); },
            }), 50),
        },
        grants: { kind: "file", probe: async () => "ready" },
        deadlineMs: 400,
      },
      { cacheMs: 60_000 },
    );
    const first = await check();
    expect(queried).toBe(true);
    expect(first.stores.session).toEqual({ kind: "postgres", state: "timeout", required: true });
    expect(first.ready).toBe(false);
    // Destroyed, not pooled, and already by the time the check answered.
    expect(releases).toEqual([true]);

    settleQuery({ rows: [{ "?column?": 1 }] });
    await new Promise((r) => setTimeout(r, 20));
    expect(releases).toEqual([true]);
    const again = await check();
    expect(again).toBe(first);
    expect(again.stores.session.state).toBe("timeout");
  });

  it("E04 row13 a postgres connect still pending after a timeout is joined, never repeated", async () => {
    let connectCalls = 0;
    const pending: Array<(client: PgProbeClient) => void> = [];
    const probe = createPgStoreProbe(() => {
      connectCalls += 1;
      return new Promise<PgProbeClient>((resolveConnect) => { pending.push(resolveConnect); });
    }, 5);

    for (let i = 0; i < 3; i += 1) {
      await expect(probe()).resolves.toBe("timeout");
      expect(connectCalls).toBe(1);
    }

    // The late connect lands: destroyed on arrival, never queried.
    let queries = 0;
    const releases: boolean[] = [];
    pending[0]!({
      async query() { queries += 1; return { rows: [] }; },
      release(destroy?: boolean) { releases.push(destroy === true); },
    });
    await new Promise((r) => setTimeout(r, 10));
    expect(releases).toEqual([true]);
    expect(queries).toBe(0);

    // Only once that attempt has settled does the next probe connect again.
    await expect(probe()).resolves.toBe("timeout");
    expect(connectCalls).toBe(2);
  });

  it("E04 row14 a store probe that outlives the deadline is not started again after the cache expires", async () => {
    let clock = 5_000_000;
    let sessionStarts = 0;
    let grantStarts = 0;
    const releasers: Array<() => void> = [];
    const held = (count: () => void): (() => Promise<ProbeOutcome>) => () => {
      count();
      return new Promise<ProbeOutcome>((settle) => { releasers.push(() => settle("ready")); });
    };
    const check = createReadinessCheck(
      {
        session: { kind: "postgres", probe: held(() => { sessionStarts += 1; }) },
        grants: { kind: "file", probe: held(() => { grantStarts += 1; }) },
        deadlineMs: 20,
      },
      { now: () => clock },
    );

    const first = await check();
    expect(first.stores.session.state).toBe("timeout");
    expect(first.stores.grants.state).toBe("timeout");
    expect(first.ready).toBe(false);

    clock += 1_001;
    const second = await check();
    expect(second).not.toBe(first);
    expect(second.stores.session.state).toBe("timeout");
    expect(second.stores.grants.state).toBe("timeout");
    expect(sessionStarts).toBe(1);
    expect(grantStarts).toBe(1);

    for (const release of releasers.splice(0)) release();
    await new Promise((r) => setTimeout(r, 10));
    clock += 1_001;
    const third = await check();
    expect(sessionStarts).toBe(2);
    expect(grantStarts).toBe(2);
    // The fresh probes are held too, so the third check times out again.
    expect(third.stores.session.state).toBe("timeout");
    for (const release of releasers.splice(0)) release();
  });

  it("E04 row15 a sentinel the store will not delete is retried, never multiplied", async () => {
    const { fs, opened, unlinked } = recordingFs(["EPERM", "EPERM", "EPERM"]);
    const probe = createFileStoreProbe(root, fs);

    await expect(probe()).resolves.toBe("unavailable");
    expect(opened).toHaveLength(1);
    const a = opened[0]!;
    expect(unlinked).toEqual([a]);

    await expect(probe()).resolves.toBe("unavailable");
    expect(opened).toHaveLength(1);
    expect(unlinked).toEqual([a, a]);

    await expect(probe()).resolves.toBe("unavailable");
    expect(opened).toHaveLength(1);
    expect(unlinked).toEqual([a, a, a]);

    // The retry of A succeeds, then a fresh probe creates B and removes it.
    await expect(probe()).resolves.toBe("ready");
    expect(opened).toHaveLength(2);
    const b = opened[1]!;
    expect(b).not.toBe(a);
    expect(unlinked).toEqual([a, a, a, a, b]);
  });

  it("E04 row16 an already-removed leaked sentinel counts as removed", async () => {
    const { fs, opened, unlinked } = recordingFs(["EPERM", "ENOENT"]);
    const probe = createFileStoreProbe(root, fs);

    await expect(probe()).resolves.toBe("unavailable");
    expect(opened).toHaveLength(1);
    const a = opened[0]!;

    await expect(probe()).resolves.toBe("ready");
    expect(opened).toHaveLength(2);
    const b = opened[1]!;
    expect(b).not.toBe(a);
    expect(unlinked).toEqual([a, a, b]);
  });

  it("E04 row17 fileStoreProbe keeps no state between calls", async () => {
    const { fs, opened } = recordingFs(["EPERM", "EPERM"]);
    await expect(fileStoreProbe(root, fs)).resolves.toBe("unavailable");
    await expect(fileStoreProbe(root, fs)).resolves.toBe("unavailable");
    expect(opened).toHaveLength(2);
    expect(new Set(opened).size).toBe(2);
  });

  it("E04 row4 a postgres connect that rejects is unavailable and a hanging one is timeout at the deadline", async () => {
    await expect(
      pgStoreProbe(async () => { throw new Error("connect ECONNREFUSED"); }),
    ).resolves.toBe("unavailable");

    const started = Date.now();
    const result = await storeReadiness({
      session: { kind: "postgres", probe: () => pgStoreProbe(() => new Promise<PgProbeClient>(() => {})) },
      grants: { kind: "file", probe: async () => "ready" },
      deadlineMs: 100,
    });
    const elapsed = Date.now() - started;
    expect(result.stores.session).toEqual({ kind: "postgres", state: "timeout", required: true });
    expect(result.stores.grants.state).toBe("ready");
    expect(result.ready).toBe(false);
    expect(elapsed).toBeGreaterThanOrEqual(90);
    expect(elapsed).toBeLessThan(1500);
  });

  it("E04 row5 a postgres probe that succeeds after a failure reads ready again (recovery)", async () => {
    const calls: string[] = [];
    let attempt = 0;
    const connect = async (): Promise<PgProbeClient> => {
      attempt += 1;
      if (attempt === 1) throw new Error("the database is restarting");
      return okClient(calls);
    };
    const check = createReadinessCheck(
      {
        session: { kind: "postgres", probe: () => pgStoreProbe(connect) },
        grants: { kind: "file", probe: async () => "ready" },
      },
      { cacheMs: 0 },
    );
    const first = await check();
    expect(first.stores.session.state).toBe("unavailable");
    expect(first.ready).toBe(false);
    const second = await check();
    expect(second.stores.session.state).toBe("ready");
    expect(second.ready).toBe(true);
    // SELECT 1 under a transaction-local statement timeout; the healthy client goes back to the pool.
    expect(calls[0]).toBe("BEGIN; SET LOCAL statement_timeout = 1000; SELECT 1; COMMIT");
    expect(calls[1]).toBe("release:false");

    // A client whose query fails is destroyed, never returned to the pool.
    const failing: string[] = [];
    await expect(pgStoreProbe(async () => ({
      async query() { throw new Error("canceling statement due to statement timeout"); },
      release(destroy?: boolean) { failing.push(`release:${destroy === true}`); },
    }))).resolves.toBe("unavailable");
    expect(failing).toEqual(["release:true"]);
  });

  it("E04 row6 an unconfigured session store is not_configured, not required, and the result is ready", async () => {
    const result = await storeReadiness({ session: null, grants: { kind: "file", probe: async () => "ready" } });
    expect(result).toEqual({
      ready: true,
      stores: {
        session: { kind: "none", state: "not_configured", required: false },
        grants: { kind: "file", state: "ready", required: true },
      },
    });
  });

  it("E04 row7 the result body contains no path, no connection string and no error text", async () => {
    const secretDir = join(root, "tenant-acme-grants-SENSITIVE");
    const connectionString = "postgresql://leaf:hunter2@db.internal:5432/leaf";
    const errorText = `connect ECONNREFUSED ${connectionString}`;
    const result = await storeReadiness({
      session: {
        kind: "postgres",
        probe: () => pgStoreProbe(async () => { throw new Error(errorText); }),
      },
      grants: { kind: "file", probe: () => fileStoreProbe(secretDir) },
    });
    const encoded = JSON.stringify(result);
    for (const forbidden of [
      secretDir, root, "SENSITIVE", "tenant-acme", connectionString, "postgresql://",
      "hunter2", "db.internal", "ECONNREFUSED", "Error", "ENOENT",
    ]) {
      expect(encoded).not.toContain(forbidden);
    }
    expect(result.stores.session.state).toBe("unavailable");
    expect(result.stores.grants.state).toBe("unavailable");
    expect(Object.keys(result.stores.session).sort()).toEqual(["kind", "required", "state"]);
    expect(Object.keys(result.stores.grants).sort()).toEqual(["kind", "required", "state"]);
  });

  it("E04 row8 GET /ready answers 503 when the grant store is unavailable and GET /health still answers the constant 200", async () => {
    const missingGrants = join(root, "grants-missing");
    const grantAdmin = new FileTenantGrantStore({ dir: missingGrants, envFallback: NO_ENV_FALLBACK });
    const readiness = createReadinessCheck({
      session: null,
      grants: { kind: "file", probe: () => fileStoreProbe(missingGrants) },
    });
    let baseUrl: string;
    ({ server, baseUrl } = listen(grantAdmin, readiness));

    const [ready, health] = await Promise.all([fetch(`${baseUrl}/ready`), fetch(`${baseUrl}/health`)]);
    expect(ready.status).toBe(503);
    expect(ready.headers.get("cache-control")).toBe("no-store");
    const readyText = await ready.text();
    expect(JSON.parse(readyText)).toEqual({
      ok: false,
      ready: false,
      service: "leaf-tenant-author-harness",
      stores: {
        session: { kind: "none", state: "not_configured", required: false },
        grants: { kind: "file", state: "unavailable", required: true },
      },
    });
    expect(readyText).not.toContain(missingGrants);
    expect(readyText).not.toContain(SECRET);

    expect(health.status).toBe(200);
    expect(await health.text()).toBe(HEALTH_BODY);
    expect(existsSync(missingGrants)).toBe(false);
  });

  it("E04 row9 ten concurrent GET /ready calls run the store probes once", async () => {
    const grantsDir = join(root, "grants");
    mkdirSync(grantsDir);
    let sessionRuns = 0;
    let grantRuns = 0;
    const slow = (count: () => void): (() => Promise<ProbeOutcome>) => async () => {
      count();
      await new Promise((r) => setTimeout(r, 60));
      return "ready";
    };
    let clock = 1_000_000;
    const readiness = createReadinessCheck(
      {
        session: { kind: "file", probe: slow(() => { sessionRuns += 1; }) },
        grants: { kind: "file", probe: slow(() => { grantRuns += 1; }) },
      },
      { now: () => clock },
    );
    let baseUrl: string;
    ({ server, baseUrl } = listen(new FileTenantGrantStore({ dir: grantsDir, envFallback: NO_ENV_FALLBACK }), readiness));

    const responses = await Promise.all(Array.from({ length: 10 }, () => fetch(`${baseUrl}/ready`)));
    expect(responses.map((r) => r.status)).toEqual(Array(10).fill(200));
    for (const r of responses) {
      expect(await r.json()).toMatchObject({ ok: true, ready: true });
    }
    expect(sessionRuns).toBe(1);
    expect(grantRuns).toBe(1);

    // The last result is reused inside the 1000 ms window, then probed afresh.
    clock += 999;
    expect((await fetch(`${baseUrl}/ready`)).status).toBe(200);
    expect(sessionRuns).toBe(1);
    clock += 2;
    expect((await fetch(`${baseUrl}/ready`)).status).toBe(200);
    expect(sessionRuns).toBe(2);
    expect(grantRuns).toBe(2);
  });

  it("E04 row10 a tenant with no grant file does not make /ready unready", async () => {
    const grantsDir = join(root, "grants");
    mkdirSync(grantsDir);
    const grantAdmin = new FileTenantGrantStore({ dir: grantsDir, envFallback: NO_ENV_FALLBACK });
    await expect(grantAdmin.get("tenant-without-grant")).resolves.toBeNull();
    const readiness = createReadinessCheck({
      session: readyProbe(),
      grants: { kind: "file", probe: () => fileStoreProbe(grantsDir) },
    });
    let baseUrl: string;
    ({ server, baseUrl } = listen(grantAdmin, readiness));

    const r = await fetch(`${baseUrl}/ready`, { headers: { "x-tenant-id": "tenant-without-grant" } });
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({
      ok: true,
      ready: true,
      service: "leaf-tenant-author-harness",
      stores: {
        session: { kind: "file", state: "ready", required: true },
        grants: { kind: "file", state: "ready", required: true },
      },
    });
    expect(readdirSync(grantsDir)).toEqual([]);
  });

  it("the resolvers name the directories the store constructors use", () => {
    const sessionsDir = join(root, "sessions-env");
    const grantsDir = join(root, "grants-env");
    vi.stubEnv("LEAF_SESSIONS_DIR", sessionsDir);
    vi.stubEnv("LEAF_GRANTS_DIR", grantsDir);
    const sessionStore = new FileSessionStore();
    const grantStore = new FileTenantGrantStore({ envFallback: NO_ENV_FALLBACK });
    expect(resolveSessionsDir()).toBe((sessionStore as unknown as { dir: string }).dir);
    expect(resolveGrantsDir()).toBe((grantStore as unknown as { dir: string }).dir);
    expect(resolveSessionsDir({ LEAF_SESSIONS_DIR: "rel-sessions" })).toBe(resolve("rel-sessions"));
    expect(resolveGrantsDir({})).toBe("C:/tmp/leaf-grants");
  });

  it("the probed session store handle probes the directory its file store was built with", async () => {
    const sessionsDir = join(root, "probed-sessions");
    vi.stubEnv("LEAF_SESSIONS_DIR", sessionsDir);
    const handle = createProbedSessionStore({});
    expect(handle.kind).toBe("file");
    expect(handle.store).toBeInstanceOf(FileSessionStore);
    expect(handle.readiness.kind).toBe("file");
    await expect(handle.readiness.probe()).resolves.toBe("ready");
    expect(readdirSync(sessionsDir)).toEqual([]);
    await handle.close();
    expect(() => createProbedSessionStore({ LEAF_HARNESS_SESSION_STORE: "redis" })).toThrow(/use file or postgres/);
    expect(() => createProbedSessionStore({ LEAF_HARNESS_SESSION_STORE: "postgres" })).toThrow(
      /requires LEAF_HARNESS_DATABASE_URL or DATABASE_URL/,
    );
  });
});
