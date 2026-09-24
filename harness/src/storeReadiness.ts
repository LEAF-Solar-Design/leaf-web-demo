/**
 * Harness store readiness (GET /ready): can the harness's OWN session store and
 * grant store serve right now? The app process cannot mount these stores, so the
 * harness answers for them and the app's harness dependency consumes the answer.
 *
 * Contract:
 * - Bounded: every probe of one check runs concurrently under ONE total deadline
 *   (default 1500 ms, clamped 100..3000 ms); a probe still running then is `timeout`.
 * - A probe never throws; any failure is `unavailable`.
 * - A file probe never creates the store directory and touches no file but its own
 *   uniquely named sentinel, which it removes whenever it created one. It proves the
 *   directory accepts a create, write, sync and unlink; it does NOT prove that every
 *   store file name inside it is writable. A postgres probe runs `SELECT 1` on one
 *   pooled client under a transaction-local statement timeout, races it against its
 *   own timer, and releases the client exactly once (destroyed on any failure).
 * - The result carries ONLY kinds, states and required flags: never a path, a
 *   connection string, an error message, a tenant id or a grant.
 * - createReadinessCheck coalesces concurrent calls into one in-flight check, caches
 *   the last result briefly, and joins a store probe that is still running instead
 *   of starting another, so a burst of polls cannot fan out into store operations.
 */

import { randomUUID } from "node:crypto";
import { open, unlink } from "node:fs/promises";
import { join } from "node:path";

export type StoreState = "ready" | "unavailable" | "timeout" | "not_configured";
/**
 * What a single probe can report on its own. A probe may report `timeout` from its
 * own timer (the postgres probe does); the check also assigns `timeout` at its
 * deadline, and `not_configured` is assigned only by the check.
 */
export type ProbeOutcome = "ready" | "unavailable" | "timeout";
export type SessionStoreKind = "file" | "postgres" | "none";
export type GrantStoreKind = "file" | "vault";

export interface StoreProbe<K extends string> {
  readonly kind: K;
  probe(): Promise<ProbeOutcome>;
}

export interface StoreStatus<K extends string> {
  kind: K;
  state: StoreState;
  required: boolean;
}

export interface StoreReadiness {
  ready: boolean;
  stores: {
    session: StoreStatus<SessionStoreKind>;
    grants: StoreStatus<GrantStoreKind>;
  };
}

export interface StoreReadinessInput {
  /** null when the harness does not construct a session store (no app URL or dispatch secret). */
  session: StoreProbe<"file" | "postgres"> | null;
  /** The grant store is always required. */
  grants: StoreProbe<GrantStoreKind>;
  /** Total deadline for the whole check. Default 1500, clamped 100..3000. */
  deadlineMs?: number;
}

export const DEFAULT_READINESS_DEADLINE_MS = 1500;
const MIN_READINESS_DEADLINE_MS = 100;
const MAX_READINESS_DEADLINE_MS = 3000;
export const DEFAULT_READINESS_CACHE_MS = 1000;
export const PG_PROBE_STATEMENT_TIMEOUT_MS = 1000;

/** Clamp a requested deadline into 100..3000 ms; a missing or non-finite value is the default. */
export function clampReadinessDeadlineMs(value: number | undefined): number {
  if (value === undefined || !Number.isFinite(value)) return DEFAULT_READINESS_DEADLINE_MS;
  return Math.min(Math.max(Math.trunc(value), MIN_READINESS_DEADLINE_MS), MAX_READINESS_DEADLINE_MS);
}

// ---------------------------------------------------------------------------
// File store probe
// ---------------------------------------------------------------------------

export interface ProbeFileHandle {
  write(data: Uint8Array): Promise<unknown>;
  sync(): Promise<void>;
  close(): Promise<void>;
}

/** The two filesystem operations the file probe uses; injectable for tests. */
export interface ProbeFs {
  /** Exclusive create (`wx`): fails if the path exists or its directory does not. */
  openExclusive(path: string): Promise<ProbeFileHandle>;
  unlink(path: string): Promise<void>;
}

const nodeProbeFs: ProbeFs = {
  async openExclusive(path: string): Promise<ProbeFileHandle> {
    const handle = await open(path, "wx", 0o600);
    return {
      write: (data) => handle.write(data),
      sync: () => handle.sync(),
      close: () => handle.close(),
    };
  },
  unlink: (path: string) => unlink(path),
};

const SENTINEL_BYTES = new TextEncoder().encode("leaf-ready\n");

/** True when an unlink failed only because the file is already gone. */
function isAlreadyGone(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: unknown }).code === "ENOENT";
}

/** One probe's outcome, plus the sentinel it created and could not remove (else null). */
interface FileProbeRun {
  outcome: ProbeOutcome;
  leaked: string | null;
}

/**
 * The one file probe both entry points share: create, write, fsync, close and
 * remove one uniquely named sentinel in `dir`. Never rejects.
 */
async function runFileProbe(dir: string, fs: ProbeFs): Promise<FileProbeRun> {
  if (typeof dir !== "string" || dir.length === 0) return { outcome: "unavailable", leaked: null };
  const sentinel = join(dir, `.leaf-ready-${randomUUID()}.probe`);
  let handle: ProbeFileHandle;
  try {
    handle = await fs.openExclusive(sentinel);
  } catch {
    return { outcome: "unavailable", leaked: null };
  }
  // Past a successful create the sentinel is ours: whatever fails next, the finally
  // still closes and removes it when it can, and the result stays `unavailable`.
  let ok = false;
  let leaked: string | null = null;
  try {
    await handle.write(SENTINEL_BYTES);
    await handle.sync();
    ok = true;
  } catch {
    ok = false;
  } finally {
    try {
      await handle.close();
    } catch {
      ok = false;
    }
    try {
      await fs.unlink(sentinel);
    } catch (err) {
      ok = false;
      if (!isAlreadyGone(err)) leaked = sentinel;
    }
  }
  return { outcome: ok ? "ready" : "unavailable", leaked };
}

/**
 * Create, write, fsync, close and remove one uniquely named sentinel in `dir`.
 * Never creates `dir` (a missing directory is `unavailable`) and never touches
 * any other file. Any failure along the way is `unavailable`. Stateless: every
 * call names a new sentinel; a caller that polls uses createFileStoreProbe.
 */
export async function fileStoreProbe(dir: string, fs: ProbeFs = nodeProbeFs): Promise<ProbeOutcome> {
  return (await runFileProbe(dir, fs)).outcome;
}

/**
 * fileStoreProbe that holds at most one sentinel it could not remove: a later call
 * first retries removing that same file and creates no new sentinel until it is
 * gone (removed now, or ENOENT), so a store that refuses deletes accumulates at
 * most one probe file however often it is polled. Concurrent calls join the one
 * running call. Never rejects; touches no file but its own sentinels.
 */
export function createFileStoreProbe(dir: string, fs: ProbeFs = nodeProbeFs): () => Promise<ProbeOutcome> {
  let leaked: string | null = null;
  let running: Promise<ProbeOutcome> | null = null;
  const probeOnce = async (): Promise<ProbeOutcome> => {
    if (leaked !== null) {
      try {
        await fs.unlink(leaked);
      } catch (err) {
        if (!isAlreadyGone(err)) return "unavailable";
      }
      leaked = null;
    }
    const run = await runFileProbe(dir, fs);
    leaked = run.leaked;
    return run.outcome;
  };
  return () => {
    if (running === null) {
      running = probeOnce()
        .catch((): ProbeOutcome => "unavailable")
        .finally(() => {
          running = null;
        });
    }
    return running;
  };
}

// ---------------------------------------------------------------------------
// PostgreSQL store probe
// ---------------------------------------------------------------------------

/** The slice of a pooled pg client the probe needs. */
export interface PgProbeClient {
  query(sql: string): Promise<unknown>;
  /** `true` destroys the client instead of returning it to the pool. */
  release(destroy?: boolean): void;
}

/** Clamp a probe timeout into 1..3000 ms; a non-finite value is the default. */
function clampPgProbeTimeoutMs(statementTimeoutMs: number): number {
  return Number.isFinite(statementTimeoutMs)
    ? Math.min(Math.max(Math.trunc(statementTimeoutMs), 1), MAX_READINESS_DEADLINE_MS)
    : PG_PROBE_STATEMENT_TIMEOUT_MS;
}

/** One connect-and-query attempt: `work` never rejects; `expire` is idempotent. */
interface PgProbeRun {
  readonly work: Promise<ProbeOutcome>;
  expire(): void;
}

/**
 * Start one attempt: connect, then `SELECT 1` under a transaction-local statement
 * timeout. `expire` marks the attempt timed out and destroys a client already held;
 * a connect that lands after it is destroyed on arrival with no query, and a query
 * that settles after it reads `timeout`. The client is released exactly once.
 */
function startPgProbeRun(connect: () => Promise<PgProbeClient>, timeoutMs: number): PgProbeRun {
  let client: PgProbeClient | null = null;
  let released = false;
  let timedOut = false;
  /** Release at most once; false when the release itself throws. */
  const releaseOnce = (destroy: boolean): boolean => {
    if (released || client === null) return true;
    released = true;
    try {
      client.release(destroy);
      return true;
    } catch {
      return false;
    }
  };

  const work = (async (): Promise<ProbeOutcome> => {
    try {
      client = await connect();
    } catch {
      return "unavailable";
    }
    if (timedOut) {
      releaseOnce(true);
      return "timeout";
    }
    try {
      await client.query(`BEGIN; SET LOCAL statement_timeout = ${timeoutMs}; SELECT 1; COMMIT`);
    } catch {
      releaseOnce(true);
      return "unavailable";
    }
    // Destroyed at the timer already: a late answer from that client is not readiness.
    if (timedOut) return "timeout";
    return releaseOnce(false) ? "ready" : "unavailable";
  })();

  return {
    work,
    expire: () => {
      timedOut = true;
      releaseOnce(true);
    },
  };
}

/** Race an attempt against a fresh timer; at the timer the attempt is expired. Never rejects. */
async function racePgProbeRun(run: PgProbeRun, timeoutMs: number): Promise<ProbeOutcome> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<ProbeOutcome>((resolveExpired) => {
    timer = setTimeout(() => {
      run.expire();
      resolveExpired("timeout");
    }, timeoutMs);
    timer.unref?.();
  });
  try {
    return await Promise.race([run.work, expired]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/**
 * Run `SELECT 1` on one client from the store's own pool. The statement timeout is
 * SET LOCAL inside the probe's transaction, so a healthy client returns to the pool
 * with its session settings unchanged; a failed client is destroyed, never reused.
 * The probe also races connect and query against its own timer of the same length:
 * when it fires, a client stuck in its query is destroyed (release(true)) at once
 * and the probe reports `timeout`; a connect that lands later is destroyed on
 * arrival. The client is released exactly once, whatever settles afterwards.
 * Every call starts a new attempt; a caller that polls uses createPgStoreProbe.
 */
export function pgStoreProbe(
  connect: () => Promise<PgProbeClient>,
  statementTimeoutMs: number = PG_PROBE_STATEMENT_TIMEOUT_MS,
): Promise<ProbeOutcome> {
  const timeoutMs = clampPgProbeTimeoutMs(statementTimeoutMs);
  return racePgProbeRun(startPgProbeRun(connect, timeoutMs), timeoutMs);
}

/**
 * pgStoreProbe with at most one outstanding attempt: a call while the last attempt's
 * connect or query is still unsettled (even after an earlier call reported `timeout`)
 * never calls `connect` again; it races that attempt against its own timer. The
 * attempt is forgotten only when its own work settles, never at a timer, so polling
 * cannot queue acquisitions on a pool whose connects are hanging. Never rejects.
 */
export function createPgStoreProbe(
  connect: () => Promise<PgProbeClient>,
  statementTimeoutMs: number = PG_PROBE_STATEMENT_TIMEOUT_MS,
): () => Promise<ProbeOutcome> {
  const timeoutMs = clampPgProbeTimeoutMs(statementTimeoutMs);
  let outstanding: PgProbeRun | null = null;
  return () => {
    if (outstanding === null) {
      const run = startPgProbeRun(connect, timeoutMs);
      outstanding = run;
      const forget = (): void => {
        if (outstanding === run) outstanding = null;
      };
      void run.work.then(forget, forget);
    }
    return racePgProbeRun(outstanding, timeoutMs);
  };
}

// ---------------------------------------------------------------------------
// The check
// ---------------------------------------------------------------------------

function settleProbe(store: StoreProbe<string>): Promise<ProbeOutcome> {
  try {
    return store.probe().then(
      (outcome): ProbeOutcome => (outcome === "ready" || outcome === "timeout" ? outcome : "unavailable"),
      (): ProbeOutcome => "unavailable",
    );
  } catch {
    return Promise.resolve("unavailable");
  }
}

/** Run both store probes concurrently under one total deadline. Never rejects. */
export async function storeReadiness(input: StoreReadinessInput): Promise<StoreReadiness> {
  const deadlineMs = clampReadinessDeadlineMs(input.deadlineMs);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<"timeout">((resolveDeadline) => {
    timer = setTimeout(() => resolveDeadline("timeout"), deadlineMs);
    timer.unref?.();
  });
  const bounded = (store: StoreProbe<string>): Promise<StoreState> =>
    Promise.race([settleProbe(store), deadline]);
  try {
    const [sessionState, grantState] = await Promise.all([
      input.session ? bounded(input.session) : Promise.resolve<StoreState>("not_configured"),
      bounded(input.grants),
    ]);
    const sessionRequired = input.session !== null;
    const session: StoreStatus<SessionStoreKind> = {
      kind: input.session ? input.session.kind : "none",
      state: sessionState,
      required: sessionRequired,
    };
    const grants: StoreStatus<GrantStoreKind> = {
      kind: input.grants.kind,
      state: grantState,
      required: true,
    };
    const ready = grantState === "ready" && (!sessionRequired || sessionState === "ready");
    return { ready, stores: { session, grants } };
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** Join a probe that is still running (for example a hung connect) instead of starting another. */
function singleFlight<K extends string>(store: StoreProbe<K>): StoreProbe<K> {
  let running: Promise<ProbeOutcome> | null = null;
  return {
    kind: store.kind,
    probe(): Promise<ProbeOutcome> {
      if (!running) {
        running = settleProbe(store).finally(() => {
          running = null;
        });
      }
      return running;
    },
  };
}

export interface ReadinessCheckOptions {
  /** How long the last result is reused. Default 1000 ms. */
  cacheMs?: number;
  /** Clock, injectable for tests. */
  now?: () => number;
}

/**
 * The /ready dependency: one in-flight check shared by concurrent callers, the last
 * result reused for `cacheMs`, and at most one running probe per store.
 */
export function createReadinessCheck(
  input: StoreReadinessInput,
  opts: ReadinessCheckOptions = {},
): () => Promise<StoreReadiness> {
  const cacheMs = opts.cacheMs ?? DEFAULT_READINESS_CACHE_MS;
  const now = opts.now ?? Date.now;
  const guarded: StoreReadinessInput = {
    session: input.session ? singleFlight(input.session) : null,
    grants: singleFlight(input.grants),
    ...(input.deadlineMs !== undefined ? { deadlineMs: input.deadlineMs } : {}),
  };
  let inFlight: Promise<StoreReadiness> | null = null;
  let cached: { at: number; value: StoreReadiness } | null = null;
  return () => {
    if (cached && now() - cached.at < cacheMs) return Promise.resolve(cached.value);
    if (inFlight) return inFlight;
    const check = storeReadiness(guarded)
      .then((value) => {
        cached = { at: now(), value };
        return value;
      })
      .finally(() => {
        inFlight = null;
      });
    inFlight = check;
    return check;
  };
}
