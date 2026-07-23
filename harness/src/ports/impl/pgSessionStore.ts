/**
 * PostgreSQL SessionStore implementation.
 *
 * The adapter does not create schema implicitly. Production must apply the
 * matching migration before constructing it. initializeSchema() is an explicit
 * helper for contract tests and disposable environments.
 */

import { randomUUID } from "node:crypto";
import { Pool } from "pg";
import type { PoolClient, PoolConfig, QueryResultRow } from "pg";

import type {
  ConfirmationRecord,
  ConfirmationStatus,
  ConverseEventType,
  ConverseTurnUsage,
  SessionRecord,
  SessionStatus,
  SessionStore,
  StoredEvent,
  TurnRecord,
} from "../index.js";
import { TurnLockHeldError } from "./sessionStore.js";

const STALE_TURN_MARGIN_S = 60;
const DEFAULT_PREFIX = "harness";

function defaultStaleTurnTimeoutS(): number {
  return (Number(process.env.LEAF_SPINE_TURN_TIMEOUT_S || "") || 120) + STALE_TURN_MARGIN_S;
}

function iso(value: Date | string): string {
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}

function identifier(value: string): string {
  if (!/^[a-z][a-z0-9_]*$/.test(value)) {
    throw new Error(`invalid PostgreSQL table prefix: ${value}`);
  }
  return `"${value}"`;
}

interface SessionRow extends QueryResultRow {
  session_id: string;
  tenant_id: string;
  drawing_id: string;
  sdk_session_id: string | null;
  status: SessionStatus;
  summary: string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

interface TurnRow extends QueryResultRow {
  turn_id: string;
  session_id: string;
  seq_start: number | string;
  status: "active" | "complete" | "failed";
  stop_reason: string | null;
  started_at: Date | string;
  ended_at: Date | string | null;
}

interface EventRow extends QueryResultRow {
  session_id: string;
  seq: number | string;
  turn_id: string;
  type: ConverseEventType;
  data: Record<string, unknown>;
  ts: Date | string;
}

interface ConfirmationRow extends QueryResultRow {
  confirmation_id: string;
  session_id: string;
  turn_id: string;
  action: string;
  args_json: string;
  kind: string;
  status: ConfirmationStatus;
  created_at: Date | string;
  expires_at: Date | string;
  decided_at: Date | string | null;
  decided_by: string | null;
}

interface ExactMatchRow extends QueryResultRow {
  exact: boolean;
}

function sessionRecord(row: SessionRow): SessionRecord {
  return {
    ...row,
    created_at: iso(row.created_at),
    updated_at: iso(row.updated_at),
  };
}

function turnRecord(row: TurnRow): TurnRecord {
  return {
    ...row,
    seq_start: Number(row.seq_start),
    started_at: iso(row.started_at),
    ended_at: row.ended_at === null ? null : iso(row.ended_at),
  };
}

function storedEvent(row: EventRow): StoredEvent {
  return {
    ...row,
    seq: Number(row.seq),
    ts: iso(row.ts),
  };
}

function confirmationRecord(row: ConfirmationRow): ConfirmationRecord {
  return {
    ...row,
    created_at: iso(row.created_at),
    expires_at: iso(row.expires_at),
    decided_at: row.decided_at === null ? null : iso(row.decided_at),
  };
}

export interface PgSessionStoreOptions {
  /** An existing pool takes precedence and remains owned by the caller. */
  pool?: Pool;
  /** Explicit pg Pool configuration. No connection URL is read implicitly. */
  poolConfig?: PoolConfig;
  /** Prefix for the five tables. Default: harness. */
  tablePrefix?: string;
  staleTurnTimeoutS?: number;
}

export class InactiveTurnError extends Error {
  constructor(
    readonly turnId: string,
    readonly activeTurnId: string | null,
  ) {
    super(
      activeTurnId === null
        ? `turn_not_active: turn ${turnId} has no active session turn`
        : `turn_not_active: turn ${turnId} is fenced by active turn ${activeTurnId}`,
    );
    this.name = "InactiveTurnError";
  }
}

/**
 * PostgreSQL-backed durable transcript store.
 *
 * Concurrency invariants:
 * - a partial unique index permits at most one active turn per session;
 * - transaction-scoped advisory locks serialize turn starts and event sequence;
 * - event (session_id, seq) and confirmation_id constraints are final guards;
 * - confirmation resolution is one conditional UPDATE, so only one caller can
 *   move a pending row to a terminal state.
 */
export class PgSessionStore implements SessionStore {
  private readonly pool: Pool;
  private readonly ownsPool: boolean;
  private readonly staleTurnMs: number;
  private readonly tables: {
    sessions: string;
    turns: string;
    events: string;
    confirmations: string;
    usage: string;
    activeTurnIndex: string;
    activeSessionIndex: string;
  };

  constructor(opts: PgSessionStoreOptions) {
    if (!opts.pool && !opts.poolConfig) {
      throw new Error("PgSessionStore requires an explicit pool or poolConfig");
    }
    const prefix = opts.tablePrefix ?? DEFAULT_PREFIX;
    identifier(prefix);
    this.tables = {
      sessions: identifier(`${prefix}_sessions`),
      turns: identifier(`${prefix}_turns`),
      events: identifier(`${prefix}_events`),
      confirmations: identifier(`${prefix}_confirmations`),
      usage: identifier(`${prefix}_usage`),
      activeTurnIndex: identifier(`${prefix}_one_active_turn`),
      activeSessionIndex: identifier(`${prefix}_one_active_session`),
    };
    const staleTurnTimeoutS = opts.staleTurnTimeoutS ?? defaultStaleTurnTimeoutS();
    if (!Number.isFinite(staleTurnTimeoutS) || staleTurnTimeoutS <= 0) {
      throw new Error("staleTurnTimeoutS must be finite and positive");
    }
    this.pool = opts.pool ?? new Pool(opts.poolConfig);
    this.ownsPool = opts.pool === undefined;
    this.staleTurnMs = staleTurnTimeoutS * 1000;
  }

  /** Close only a pool created by this adapter. */
  async close(): Promise<void> {
    if (this.ownsPool) await this.pool.end();
  }

  /** Explicit setup for tests and disposable environments. */
  async initializeSchema(): Promise<void> {
    const t = this.tables;
    await this.pool.query(`
      CREATE TABLE IF NOT EXISTS ${t.sessions} (
        session_id uuid PRIMARY KEY,
        tenant_id text NOT NULL,
        drawing_id text NOT NULL,
        sdk_session_id text,
        status text NOT NULL CHECK (status IN ('idle', 'active', 'dormant', 'archived')),
        summary text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS ${t.activeSessionIndex}
        ON ${t.sessions} (tenant_id, drawing_id) WHERE status <> 'archived';

      CREATE TABLE IF NOT EXISTS ${t.turns} (
        turn_id text NOT NULL,
        session_id uuid NOT NULL REFERENCES ${t.sessions}(session_id) ON DELETE CASCADE,
        seq_start bigint NOT NULL,
        status text NOT NULL CHECK (status IN ('active', 'complete', 'failed')),
        stop_reason text,
        started_at timestamptz NOT NULL,
        ended_at timestamptz,
        PRIMARY KEY (session_id, turn_id)
      );
      CREATE UNIQUE INDEX IF NOT EXISTS ${t.activeTurnIndex}
        ON ${t.turns} (session_id) WHERE status = 'active';

      CREATE TABLE IF NOT EXISTS ${t.events} (
        session_id uuid NOT NULL REFERENCES ${t.sessions}(session_id) ON DELETE CASCADE,
        seq bigint NOT NULL,
        turn_id text NOT NULL,
        type text NOT NULL,
        data jsonb NOT NULL,
        ts timestamptz NOT NULL,
        PRIMARY KEY (session_id, seq)
      );

      CREATE TABLE IF NOT EXISTS ${t.confirmations} (
        confirmation_id text PRIMARY KEY,
        session_id uuid NOT NULL REFERENCES ${t.sessions}(session_id) ON DELETE CASCADE,
        turn_id text NOT NULL,
        action text NOT NULL,
        args_json text NOT NULL,
        kind text NOT NULL,
        status text NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
        created_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        decided_at timestamptz,
        decided_by text
      );

      CREATE TABLE IF NOT EXISTS ${t.usage} (
        usage_id bigserial PRIMARY KEY,
        session_id uuid NOT NULL REFERENCES ${t.sessions}(session_id) ON DELETE CASCADE,
        turn_id text NOT NULL,
        usage jsonb NOT NULL,
        ts timestamptz NOT NULL
      );
    `);
  }

  private async transaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  private async lockSession(client: PoolClient, sessionId: string): Promise<void> {
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [sessionId]);
  }

  private async reapAbandoned(client: PoolClient, sessionId: string): Promise<void> {
    await client.query(
      `UPDATE ${this.tables.turns}
          SET status = 'failed', stop_reason = 'abandoned', ended_at = NOW()
        WHERE session_id = $1
          AND status = 'active'
          AND started_at < NOW() - ($2 * INTERVAL '1 millisecond')`,
      [sessionId, this.staleTurnMs],
    );
  }

  async createOrGetSession(tenantId: string, drawingId: string): Promise<SessionRecord> {
    const sessionId = randomUUID();
    const result = await this.pool.query<SessionRow>(
      `INSERT INTO ${this.tables.sessions}
         (session_id, tenant_id, drawing_id, sdk_session_id, status, summary, created_at, updated_at)
       VALUES ($1, $2, $3, NULL, 'idle', NULL, NOW(), NOW())
       ON CONFLICT (tenant_id, drawing_id) WHERE status <> 'archived'
       DO UPDATE SET tenant_id = EXCLUDED.tenant_id
       RETURNING *`,
      [sessionId, tenantId, drawingId],
    );
    return sessionRecord(result.rows[0]!);
  }

  async getSession(sessionId: string): Promise<SessionRecord | null> {
    const result = await this.pool.query<SessionRow>(
      `SELECT * FROM ${this.tables.sessions} WHERE session_id = $1`,
      [sessionId],
    );
    return result.rows[0] ? sessionRecord(result.rows[0]) : null;
  }

  async updateSession(
    sessionId: string,
    patch: Partial<Pick<SessionRecord, "sdk_session_id" | "status" | "summary">>,
  ): Promise<SessionRecord> {
    const result = await this.pool.query<SessionRow>(
      `UPDATE ${this.tables.sessions}
          SET sdk_session_id = CASE WHEN $2::boolean THEN $3 ELSE sdk_session_id END,
              status = COALESCE($4, status),
              summary = CASE WHEN $5::boolean THEN $6 ELSE summary END,
              updated_at = NOW()
        WHERE session_id = $1
        RETURNING *`,
      [
        sessionId,
        Object.hasOwn(patch, "sdk_session_id"),
        patch.sdk_session_id,
        patch.status,
        Object.hasOwn(patch, "summary"),
        patch.summary,
      ],
    );
    if (!result.rows[0]) throw new Error(`session_not_found: ${sessionId}`);
    return sessionRecord(result.rows[0]);
  }

  async getActiveTurn(sessionId: string): Promise<TurnRecord | null> {
    return this.transaction(async (client) => {
      await this.lockSession(client, sessionId);
      await this.reapAbandoned(client, sessionId);
      const result = await client.query<TurnRow>(
        `SELECT * FROM ${this.tables.turns}
          WHERE session_id = $1 AND status = 'active'`,
        [sessionId],
      );
      return result.rows[0] ? turnRecord(result.rows[0]) : null;
    });
  }

  async beginTurn(sessionId: string, turnId: string): Promise<TurnRecord> {
    return this.transaction(async (client) => {
      await this.lockSession(client, sessionId);
      await this.reapAbandoned(client, sessionId);
      const active = await client.query<TurnRow>(
        `SELECT * FROM ${this.tables.turns}
          WHERE session_id = $1 AND status = 'active'`,
        [sessionId],
      );
      if (active.rows[0]) throw new TurnLockHeldError(active.rows[0].turn_id);

      const result = await client.query<TurnRow>(
        `INSERT INTO ${this.tables.turns}
           (turn_id, session_id, seq_start, status, stop_reason, started_at, ended_at)
         VALUES (
           $1, $2,
           COALESCE((SELECT MAX(seq) + 1 FROM ${this.tables.events} WHERE session_id = $2), 1),
           'active', NULL, NOW(), NULL
         )
         RETURNING *`,
        [turnId, sessionId],
      );
      return turnRecord(result.rows[0]!);
    });
  }

  async endTurn(
    sessionId: string,
    turnId: string,
    status: "complete" | "failed",
    stopReason: string,
  ): Promise<void> {
    await this.transaction(async (client) => {
      await this.lockSession(client, sessionId);
      await this.reapAbandoned(client, sessionId);
      await client.query(
        `UPDATE ${this.tables.turns}
            SET status = $3, stop_reason = $4, ended_at = NOW()
          WHERE session_id = $1 AND turn_id = $2 AND status = 'active'`,
        [sessionId, turnId, status, stopReason],
      );
    });
  }

  async appendEvent(
    sessionId: string,
    turnId: string,
    type: ConverseEventType,
    data: Record<string, unknown>,
  ): Promise<StoredEvent> {
    return this.transaction(async (client) => {
      await this.lockSession(client, sessionId);
      await this.reapAbandoned(client, sessionId);
      const active = await client.query<TurnRow>(
        `SELECT * FROM ${this.tables.turns}
          WHERE session_id = $1 AND status = 'active'`,
        [sessionId],
      );
      const activeTurnId = active.rows[0]?.turn_id ?? null;
      if (activeTurnId !== turnId) {
        throw new InactiveTurnError(turnId, activeTurnId);
      }
      const result = await client.query<EventRow>(
        `INSERT INTO ${this.tables.events} (session_id, seq, turn_id, type, data, ts)
         VALUES (
           $1,
           COALESCE((SELECT MAX(seq) + 1 FROM ${this.tables.events} WHERE session_id = $1), 1),
           $2, $3, $4::jsonb, NOW()
         )
         RETURNING *`,
        [sessionId, turnId, type, JSON.stringify(data)],
      );
      return storedEvent(result.rows[0]!);
    });
  }

  async eventsAfter(sessionId: string, afterSeq: number, limit?: number): Promise<StoredEvent[]> {
    const params: unknown[] = [sessionId, afterSeq];
    const sql =
      limit === undefined
        ? `SELECT * FROM ${this.tables.events}
            WHERE session_id = $1 AND seq > $2
            ORDER BY seq ASC`
        : `SELECT * FROM (
             SELECT * FROM ${this.tables.events}
              WHERE session_id = $1 AND seq > $2
              ORDER BY seq DESC
              LIMIT $3
           ) recent
           ORDER BY seq ASC`;
    if (limit !== undefined) params.push(limit);
    const result = await this.pool.query<EventRow>(sql, params);
    return result.rows.map(storedEvent);
  }

  async putConfirmation(rec: ConfirmationRecord): Promise<void> {
    await this.transaction(async (client) => {
      const values = [
        rec.confirmation_id,
        rec.session_id,
        rec.turn_id,
        rec.action,
        rec.args_json,
        rec.kind,
        rec.status,
        rec.created_at,
        rec.expires_at,
        rec.decided_at,
        rec.decided_by,
      ];
      const inserted = await client.query(
        `INSERT INTO ${this.tables.confirmations}
           (confirmation_id, session_id, turn_id, action, args_json, kind, status,
            created_at, expires_at, decided_at, decided_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
         ON CONFLICT (confirmation_id) DO NOTHING
         RETURNING confirmation_id`,
        values,
      );
      if (inserted.rowCount === 1) return;

      const existing = await client.query<ExactMatchRow>(
        `SELECT (
            session_id = $2::uuid
            AND turn_id = $3
            AND action = $4
            AND args_json = $5
            AND kind = $6
            AND status = $7
            AND created_at = $8::timestamptz
            AND expires_at = $9::timestamptz
            AND decided_at IS NOT DISTINCT FROM $10::timestamptz
            AND decided_by IS NOT DISTINCT FROM $11
          ) AS exact
          FROM ${this.tables.confirmations}
         WHERE confirmation_id = $1`,
        values,
      );
      if (existing.rows[0]?.exact !== true) {
        throw new Error(`confirmation_conflict: ${rec.confirmation_id}`);
      }
    });
  }

  async getConfirmation(confirmationId: string): Promise<ConfirmationRecord | null> {
    const result = await this.pool.query<ConfirmationRow>(
      `SELECT * FROM ${this.tables.confirmations} WHERE confirmation_id = $1`,
      [confirmationId],
    );
    return result.rows[0] ? confirmationRecord(result.rows[0]) : null;
  }

  async resolveConfirmation(
    confirmationId: string,
    approved: boolean,
    decidedBy: string,
  ): Promise<ConfirmationRecord | null> {
    return this.transaction(async (client) => {
      const updated = await client.query<ConfirmationRow>(
        `UPDATE ${this.tables.confirmations}
            SET status = CASE
                  WHEN expires_at < NOW() THEN 'expired'
                  WHEN $2 THEN 'approved'
                  ELSE 'denied'
                END,
                decided_at = NOW(),
                decided_by = CASE WHEN expires_at < NOW() THEN NULL ELSE $3 END
          WHERE confirmation_id = $1 AND status = 'pending'
          RETURNING *`,
        [confirmationId, approved, decidedBy],
      );
      if (updated.rows[0]) return confirmationRecord(updated.rows[0]);

      const existing = await client.query<ConfirmationRow>(
        `SELECT * FROM ${this.tables.confirmations} WHERE confirmation_id = $1`,
        [confirmationId],
      );
      return existing.rows[0] ? confirmationRecord(existing.rows[0]) : null;
    });
  }

  async appendUsage(
    sessionId: string,
    turnId: string,
    usage: ConverseTurnUsage,
  ): Promise<void> {
    await this.pool.query(
      `INSERT INTO ${this.tables.usage} (session_id, turn_id, usage, ts)
       VALUES ($1, $2, $3::jsonb, NOW())`,
      [sessionId, turnId, JSON.stringify(usage)],
    );
  }
}
