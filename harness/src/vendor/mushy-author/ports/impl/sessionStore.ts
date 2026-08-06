/**
 * FILE-BACKED SessionStore — the durable conversational transcript, with ZERO new
 * npm dependencies (no native sqlite). It implements the CONCEPTUAL schema of the
 * pinned wire contract section 6 (sessions / turns / events / confirmations /
 * usage) behind the SessionStore port, so a SQLite implementation (sessions.db,
 * WAL) can swap in later behind the SAME port without touching the ConverseLoop.
 *
 * Layout under LEAF_SESSIONS_DIR (default harness/sessions-data/):
 *   sessions.json                 — index of SessionRecord rows (atomic rewrite)
 *   <session_id>/events.jsonl     — append-only wire events, one JSON line each
 *   <session_id>/usage.jsonl      — append-only per-turn usage records
 *   <session_id>/turns.json       — TurnRecord rows (atomic rewrite; small)
 *   <session_id>/confirmations.json — ConfirmationRecord rows (atomic rewrite)
 *
 * Durability discipline:
 *   - Every whole-file write is ATOMIC: write to a temp sibling, then rename over
 *     the destination (Node's rename replaces on Windows too), so a crash never
 *     leaves a half-written index.
 *   - events/usage are APPEND-ONLY JSONL — one write() per line; replay tolerates
 *     a torn final line (ignored) so a crash mid-append loses at most one event.
 *   - seq is per-session MONOTONIC: recovered from the last well-formed line of
 *     events.jsonl on first touch, then tracked in memory.
 *   - All mutations serialize through one in-process queue (the harness is the
 *     store's only writer), so read-modify-write on the JSON files cannot
 *     interleave even under concurrent appendEvent calls.
 */

import { randomUUID } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import type {
  ConfirmationRecord,
  ConverseEventType,
  ConverseTurnUsage,
  SessionRecord,
  SessionStore,
  StoredEvent,
  TurnRecord,
  UsageRecord,
} from "../index.js";

const DEFAULT_DIR = "sessions-data";

/** Default confirmation TTL (wire contract section 7: 300s). */
export const CONFIRMATION_TTL_S = 300;

/** Safety margin over the turn wall-clock budget before an active turns row is
 *  treated as crash-abandoned (the loop always finalizes its turn in-process, so
 *  only a dead process can leave one behind). */
const STALE_TURN_MARGIN_S = 60;

function defaultStaleTurnTimeoutS(): number {
  return (Number(process.env.LEAF_SPINE_TURN_TIMEOUT_S || "") || 120) + STALE_TURN_MARGIN_S;
}

export class TurnLockHeldError extends Error {
  constructor(readonly activeTurnId: string) {
    super(`turn_in_progress: turn ${activeTurnId} is still active`);
    this.name = "TurnLockHeldError";
  }
}

function nowIso(): string {
  return new Date().toISOString();
}

/** Atomic whole-file write: temp sibling + rename (never a half-written file). */
function writeFileAtomic(path: string, content: string): void {
  const tmp = join(dirname(path), `.${randomUUID()}.tmp`);
  writeFileSync(tmp, content, "utf8");
  renameSync(tmp, path);
}

function readJsonFile<T>(path: string, fallback: T): T {
  if (!existsSync(path)) return fallback;
  try {
    return JSON.parse(readFileSync(path, "utf8")) as T;
  } catch {
    return fallback;
  }
}

/** Parse a JSONL file, silently dropping a torn (crash-truncated) final line. */
function readJsonl<T>(path: string): T[] {
  if (!existsSync(path)) return [];
  const out: T[] = [];
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      out.push(JSON.parse(trimmed) as T);
    } catch {
      // torn final line from a crash mid-append: ignore
    }
  }
  return out;
}

export interface FileSessionStoreOptions {
  /** Storage root. Default: LEAF_SESSIONS_DIR env, else ./sessions-data. */
  dir?: string;
  confirmationTtlS?: number;
  /** Age (seconds) past which an active turns row is crash-abandoned. Default:
   *  LEAF_SPINE_TURN_TIMEOUT_S (else 120) + a 60s margin. */
  staleTurnTimeoutS?: number;
}

export class FileSessionStore implements SessionStore {
  private readonly dir: string;
  private readonly ttlS: number;
  private readonly staleTurnMs: number;
  /** In-memory per-session seq counters (recovered lazily from events.jsonl). */
  private readonly seqBySession = new Map<string, number>();
  /** All mutations serialize through this chain (single-writer discipline). */
  private queue: Promise<unknown> = Promise.resolve();

  constructor(opts: FileSessionStoreOptions = {}) {
    this.dir = resolve(opts.dir ?? process.env.LEAF_SESSIONS_DIR ?? DEFAULT_DIR);
    this.ttlS = opts.confirmationTtlS ?? CONFIRMATION_TTL_S;
    this.staleTurnMs = (opts.staleTurnTimeoutS ?? defaultStaleTurnTimeoutS()) * 1000;
    mkdirSync(this.dir, { recursive: true });
  }

  /** Serialize a mutation; reads that only touch append-only files skip the queue. */
  private locked<T>(fn: () => T): Promise<T> {
    const next = this.queue.then(fn);
    // Keep the chain alive even when fn throws (the caller still sees the rejection).
    this.queue = next.catch(() => undefined);
    return next;
  }

  // ---- paths ---------------------------------------------------------------- //
  private indexPath(): string {
    return join(this.dir, "sessions.json");
  }
  private sessionDir(sessionId: string): string {
    return join(this.dir, sessionId);
  }
  private eventsPath(sessionId: string): string {
    return join(this.sessionDir(sessionId), "events.jsonl");
  }
  private usagePath(sessionId: string): string {
    return join(this.sessionDir(sessionId), "usage.jsonl");
  }
  private turnsPath(sessionId: string): string {
    return join(this.sessionDir(sessionId), "turns.json");
  }
  private confirmationsPath(sessionId: string): string {
    return join(this.sessionDir(sessionId), "confirmations.json");
  }

  // ---- sessions --------------------------------------------------------------- //
  private readIndex(): SessionRecord[] {
    return readJsonFile<SessionRecord[]>(this.indexPath(), []);
  }
  private writeIndex(rows: SessionRecord[]): void {
    writeFileAtomic(this.indexPath(), JSON.stringify(rows, null, 2) + "\n");
  }

  async createOrGetSession(tenantId: string, drawingId: string): Promise<SessionRecord> {
    return this.locked(() => {
      const rows = this.readIndex();
      // UNIQUE(tenant_id, drawing_id): idempotent create-or-get.
      const existing = rows.find(
        (r) => r.tenant_id === tenantId && r.drawing_id === drawingId && r.status !== "archived",
      );
      if (existing) return existing;
      const now = nowIso();
      const rec: SessionRecord = {
        session_id: randomUUID(),
        tenant_id: tenantId,
        drawing_id: drawingId,
        sdk_session_id: null,
        status: "idle",
        summary: null,
        created_at: now,
        updated_at: now,
      };
      mkdirSync(this.sessionDir(rec.session_id), { recursive: true });
      this.writeIndex([...rows, rec]);
      return rec;
    });
  }

  async getSession(sessionId: string): Promise<SessionRecord | null> {
    return this.readIndex().find((r) => r.session_id === sessionId) ?? null;
  }

  async updateSession(
    sessionId: string,
    patch: Partial<Pick<SessionRecord, "sdk_session_id" | "status" | "summary">>,
  ): Promise<SessionRecord> {
    return this.locked(() => {
      const rows = this.readIndex();
      const idx = rows.findIndex((r) => r.session_id === sessionId);
      if (idx < 0) throw new Error(`session_not_found: ${sessionId}`);
      const updated: SessionRecord = { ...rows[idx]!, ...patch, updated_at: nowIso() };
      rows[idx] = updated;
      this.writeIndex(rows);
      return updated;
    });
  }

  // ---- turns ------------------------------------------------------------------ //
  private readTurns(sessionId: string): TurnRecord[] {
    return readJsonFile<TurnRecord[]>(this.turnsPath(sessionId), []);
  }
  private writeTurns(sessionId: string, rows: TurnRecord[]): void {
    mkdirSync(this.sessionDir(sessionId), { recursive: true });
    writeFileAtomic(this.turnsPath(sessionId), JSON.stringify(rows, null, 2) + "\n");
  }

  /** Active turns persist across restart BY DESIGN (the lock is durable), so a
   *  process crash mid-turn would otherwise brick the session with eternal 409s.
   *  A row older than the turn wall-clock budget (+margin) can only be such a
   *  leftover — the loop always finalizes live turns in-process. */
  private isAbandoned(t: TurnRecord): boolean {
    return Date.parse(t.started_at) + this.staleTurnMs < Date.now();
  }

  private reapAbandonedTurns(sessionId: string): void {
    const rows = this.readTurns(sessionId);
    let changed = false;
    for (let i = 0; i < rows.length; i++) {
      const t = rows[i]!;
      if (t.status === "active" && this.isAbandoned(t)) {
        rows[i] = { ...t, status: "failed", stop_reason: "abandoned", ended_at: nowIso() };
        changed = true;
      }
    }
    if (changed) this.writeTurns(sessionId, rows);
  }

  async getActiveTurn(sessionId: string): Promise<TurnRecord | null> {
    const active = this.readTurns(sessionId).find((t) => t.status === "active") ?? null;
    if (active && this.isAbandoned(active)) {
      await this.locked(() => this.reapAbandonedTurns(sessionId));
      return null;
    }
    return active;
  }

  async beginTurn(sessionId: string, turnId: string): Promise<TurnRecord> {
    return this.locked(() => {
      this.reapAbandonedTurns(sessionId);
      const rows = this.readTurns(sessionId);
      const active = rows.find((t) => t.status === "active");
      if (active) throw new TurnLockHeldError(active.turn_id);
      const rec: TurnRecord = {
        turn_id: turnId,
        session_id: sessionId,
        seq_start: this.currentSeq(sessionId) + 1,
        status: "active",
        stop_reason: null,
        started_at: nowIso(),
        ended_at: null,
      };
      this.writeTurns(sessionId, [...rows, rec]);
      return rec;
    });
  }

  async endTurn(
    sessionId: string,
    turnId: string,
    status: "complete" | "failed",
    stopReason: string,
  ): Promise<void> {
    await this.locked(() => {
      const rows = this.readTurns(sessionId);
      const idx = rows.findIndex((t) => t.turn_id === turnId);
      if (idx < 0) return;
      rows[idx] = { ...rows[idx]!, status, stop_reason: stopReason, ended_at: nowIso() };
      this.writeTurns(sessionId, rows);
    });
  }

  // ---- events ------------------------------------------------------------------ //
  /** Current seq for a session (0 when no events yet), recovered from disk once. */
  private currentSeq(sessionId: string): number {
    const cached = this.seqBySession.get(sessionId);
    if (cached !== undefined) return cached;
    const events = readJsonl<StoredEvent>(this.eventsPath(sessionId));
    const last = events.length > 0 ? events[events.length - 1]!.seq : 0;
    this.seqBySession.set(sessionId, last);
    return last;
  }

  async appendEvent(
    sessionId: string,
    turnId: string,
    type: ConverseEventType,
    data: Record<string, unknown>,
  ): Promise<StoredEvent> {
    return this.locked(() => {
      const seq = this.currentSeq(sessionId) + 1;
      const rec: StoredEvent = {
        session_id: sessionId,
        seq,
        turn_id: turnId,
        type,
        data,
        ts: nowIso(),
      };
      mkdirSync(this.sessionDir(sessionId), { recursive: true });
      appendFileSync(this.eventsPath(sessionId), JSON.stringify(rec) + "\n", "utf8");
      this.seqBySession.set(sessionId, seq);
      return rec;
    });
  }

  async eventsAfter(sessionId: string, afterSeq: number, limit?: number): Promise<StoredEvent[]> {
    const events = readJsonl<StoredEvent>(this.eventsPath(sessionId)).filter(
      (e) => e.seq > afterSeq,
    );
    if (limit !== undefined && events.length > limit) {
      return events.slice(events.length - limit); // most recent N, still ascending
    }
    return events;
  }

  // ---- confirmations ------------------------------------------------------------ //
  private readConfirmations(sessionId: string): ConfirmationRecord[] {
    return readJsonFile<ConfirmationRecord[]>(this.confirmationsPath(sessionId), []);
  }
  private writeConfirmations(sessionId: string, rows: ConfirmationRecord[]): void {
    mkdirSync(this.sessionDir(sessionId), { recursive: true });
    writeFileAtomic(
      this.confirmationsPath(sessionId),
      JSON.stringify(rows, null, 2) + "\n",
    );
  }

  /** Where a confirmation lives is derivable from its session; keep a tiny pointer
   *  index so getConfirmation(confirmationId) needs no full scan. */
  private confirmationIndexPath(): string {
    return join(this.dir, "confirmations-index.json");
  }
  private readConfirmationIndex(): Record<string, string> {
    return readJsonFile<Record<string, string>>(this.confirmationIndexPath(), {});
  }

  /** New pending confirmation record, TTL-stamped (section 7: 300s default). */
  newConfirmation(
    sessionId: string,
    turnId: string,
    action: string,
    args: Record<string, unknown>,
    kind: string,
    confirmationId?: string,
  ): ConfirmationRecord {
    const now = Date.now();
    return {
      confirmation_id: confirmationId ?? randomUUID(),
      session_id: sessionId,
      turn_id: turnId,
      action,
      args_json: JSON.stringify(args),
      kind,
      status: "pending",
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + this.ttlS * 1000).toISOString(),
      decided_at: null,
      decided_by: null,
    };
  }

  async putConfirmation(rec: ConfirmationRecord): Promise<void> {
    await this.locked(() => {
      const rows = this.readConfirmations(rec.session_id).filter(
        (r) => r.confirmation_id !== rec.confirmation_id,
      );
      this.writeConfirmations(rec.session_id, [...rows, rec]);
      const index = this.readConfirmationIndex();
      index[rec.confirmation_id] = rec.session_id;
      writeFileAtomic(this.confirmationIndexPath(), JSON.stringify(index, null, 2) + "\n");
    });
  }

  async getConfirmation(confirmationId: string): Promise<ConfirmationRecord | null> {
    const sessionId = this.readConfirmationIndex()[confirmationId];
    if (!sessionId) return null;
    return (
      this.readConfirmations(sessionId).find((r) => r.confirmation_id === confirmationId) ?? null
    );
  }

  async resolveConfirmation(
    confirmationId: string,
    approved: boolean,
    decidedBy: string,
  ): Promise<ConfirmationRecord | null> {
    return this.locked(() => {
      const sessionId = this.readConfirmationIndex()[confirmationId];
      if (!sessionId) return null;
      const rows = this.readConfirmations(sessionId);
      const idx = rows.findIndex((r) => r.confirmation_id === confirmationId);
      if (idx < 0) return null;
      const rec = rows[idx]!;
      if (rec.status !== "pending") return rec; // already decided/expired: no-op
      const expired = Date.parse(rec.expires_at) < Date.now();
      const updated: ConfirmationRecord = {
        ...rec,
        status: expired ? "expired" : approved ? "approved" : "denied",
        decided_at: nowIso(),
        decided_by: expired ? null : decidedBy,
      };
      rows[idx] = updated;
      this.writeConfirmations(sessionId, rows);
      return updated;
    });
  }

  // ---- usage ---------------------------------------------------------------------- //
  async appendUsage(sessionId: string, turnId: string, usage: ConverseTurnUsage): Promise<void> {
    await this.locked(() => {
      const rec: UsageRecord = { session_id: sessionId, turn_id: turnId, usage, ts: nowIso() };
      mkdirSync(this.sessionDir(sessionId), { recursive: true });
      appendFileSync(this.usagePath(sessionId), JSON.stringify(rec) + "\n", "utf8");
    });
  }
}
