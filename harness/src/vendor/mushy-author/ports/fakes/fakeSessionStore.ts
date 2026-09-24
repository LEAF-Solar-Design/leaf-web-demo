/**
 * Fake SessionStore - pure in-memory, same section-6 semantics as the
 * file-backed impl (idempotent createOrGet, store-enforced turn lock, monotonic
 * per-session seq, TTL-aware confirmation resolution). For unit tests that want
 * zero filesystem contact; sessionStore.test.ts covers the durable impl.
 */

import { randomUUID } from "node:crypto";
import type {
  ConfirmationActor,
  ConfirmationRecord,
  ConfirmationStatus,
  ConverseEventType,
  ConverseTurnUsage,
  SessionRecord,
  SessionStore,
  StoredEvent,
  TurnRecord,
  UsageRecord,
} from "../index.js";

function nowIso(): string {
  return new Date().toISOString();
}

export class FakeSessionStore implements SessionStore {
  private readonly appSdkSessions = new Map<string, string | null>();

  async getAppSdkSession(tenantId: string, appSessionId: string): Promise<string | null> {
    return this.appSdkSessions.get(JSON.stringify([tenantId, appSessionId])) ?? null;
  }

  async setAppSdkSession(tenantId: string, appSessionId: string, sdkSessionId: string | null): Promise<void> {
    this.appSdkSessions.set(JSON.stringify([tenantId, appSessionId]), sdkSessionId);
  }

  readonly sessions = new Map<string, SessionRecord>();
  readonly turns = new Map<string, TurnRecord[]>();
  readonly events = new Map<string, StoredEvent[]>();
  readonly confirmations = new Map<string, ConfirmationRecord>();
  readonly usage: UsageRecord[] = [];

  async createOrGetSession(tenantId: string, drawingId: string): Promise<SessionRecord> {
    for (const rec of this.sessions.values()) {
      if (rec.tenant_id === tenantId && rec.drawing_id === drawingId && rec.status !== "archived") {
        return rec;
      }
    }
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
    this.sessions.set(rec.session_id, rec);
    return rec;
  }

  async getSession(sessionId: string): Promise<SessionRecord | null> {
    return this.sessions.get(sessionId) ?? null;
  }

  async updateSession(
    sessionId: string,
    patch: Partial<Pick<SessionRecord, "sdk_session_id" | "status" | "summary">>,
  ): Promise<SessionRecord> {
    const rec = this.sessions.get(sessionId);
    if (!rec) throw new Error(`session_not_found: ${sessionId}`);
    const updated = { ...rec, ...patch, updated_at: nowIso() };
    this.sessions.set(sessionId, updated);
    return updated;
  }

  async getActiveTurn(sessionId: string): Promise<TurnRecord | null> {
    return (this.turns.get(sessionId) ?? []).find((t) => t.status === "active") ?? null;
  }

  async beginTurn(sessionId: string, turnId: string): Promise<TurnRecord> {
    const rows = this.turns.get(sessionId) ?? [];
    const active = rows.find((t) => t.status === "active");
    if (active) throw new Error(`turn_in_progress: ${active.turn_id}`);
    const rec: TurnRecord = {
      turn_id: turnId,
      session_id: sessionId,
      seq_start: (this.events.get(sessionId)?.length ?? 0) + 1,
      status: "active",
      stop_reason: null,
      started_at: nowIso(),
      ended_at: null,
    };
    this.turns.set(sessionId, [...rows, rec]);
    return rec;
  }

  async endTurn(
    sessionId: string,
    turnId: string,
    status: "complete" | "failed",
    stopReason: string,
  ): Promise<void> {
    const rows = this.turns.get(sessionId) ?? [];
    const idx = rows.findIndex((t) => t.turn_id === turnId);
    if (idx < 0) return;
    rows[idx] = { ...rows[idx]!, status, stop_reason: stopReason, ended_at: nowIso() };
  }

  async appendEvent(
    sessionId: string,
    turnId: string,
    type: ConverseEventType,
    data: Record<string, unknown>,
  ): Promise<StoredEvent> {
    const rows = this.events.get(sessionId) ?? [];
    const rec: StoredEvent = {
      session_id: sessionId,
      seq: rows.length + 1,
      turn_id: turnId,
      type,
      data,
      ts: nowIso(),
    };
    this.events.set(sessionId, [...rows, rec]);
    return rec;
  }

  async eventsAfter(sessionId: string, afterSeq: number, limit?: number): Promise<StoredEvent[]> {
    const rows = (this.events.get(sessionId) ?? []).filter((e) => e.seq > afterSeq);
    if (limit !== undefined && rows.length > limit) return rows.slice(rows.length - limit);
    return rows;
  }

  async putConfirmation(rec: ConfirmationRecord): Promise<void> {
    this.confirmations.set(rec.confirmation_id, rec);
  }

  async getConfirmation(confirmationId: string): Promise<ConfirmationRecord | null> {
    return this.confirmations.get(confirmationId) ?? null;
  }

  /** Mirrors the real stores: the only status mutation path, so a caller cannot ask
   *  for an illegal transition. Single-threaded here, but the CONTRACT is what the
   *  tests exercise, so it must refuse the same things the durable stores refuse. */
  private transition(
    confirmationId: string,
    from: ConfirmationStatus,
    decide: (rec: ConfirmationRecord, now: number) => ConfirmationRecord | null,
  ): ConfirmationRecord | null {
    const rec = this.confirmations.get(confirmationId);
    if (!rec) return null;
    if (rec.status !== from) return null;
    const updated = decide(rec, Date.now());
    if (!updated) return null;
    this.confirmations.set(confirmationId, updated);
    return updated;
  }

  async decideConfirmation(
    confirmationId: string,
    decision: "approved" | "denied",
    decidedBy: ConfirmationActor,
  ): Promise<ConfirmationRecord | null> {
    const result = this.transition(confirmationId, "pending", (rec, now) => {
      if (Date.parse(rec.expires_at) < now) {
        return { ...rec, status: "expired", decided_at: nowIso(), decided_by: null };
      }
      return {
        ...rec,
        status: decision,
        decided_at: nowIso(),
        decided_by: `${decidedBy.kind}:${decidedBy.id}`,
      };
    });
    return result && result.status === decision ? result : null;
  }

  async consumeApproved(
    confirmationId: string,
    binding: { sessionId: string; action: string; argsJson: string },
    consumedBy: ConfirmationActor,
  ): Promise<ConfirmationRecord | null> {
    return this.transition(confirmationId, "approved", (rec, now) => {
      if (Date.parse(rec.expires_at) < now) return null;
      if (rec.session_id !== binding.sessionId) return null;
      if (rec.action !== binding.action) return null;
      if (rec.args_json !== binding.argsJson) return null;
      return {
        ...rec,
        status: "consumed",
        consumed_at: nowIso(),
        consumed_by: `${consumedBy.kind}:${consumedBy.id}`,
      };
    });
  }

  async appendUsage(sessionId: string, turnId: string, usage: ConverseTurnUsage): Promise<void> {
    this.usage.push({ session_id: sessionId, turn_id: turnId, usage, ts: nowIso() });
  }
}
