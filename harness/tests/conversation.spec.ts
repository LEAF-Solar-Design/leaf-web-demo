/**
 * Card B-C6: conversation harness suite.
 *
 * Oracle (frozen, End-to-end clause): create project -> converse ->
 * disconnect -> recover -> identical transcript; runs in the disposable-task
 * shape (rolled-back transaction).
 *
 * Exercises PgSessionStore (harness/src/ports/impl/pgSessionStore.ts) --
 * the harness's own durable conversation transcript store (sessions / turns
 * / events), matching pgSessionStore.contract.test.ts's own PostgreSQL
 * convention: an explicit *_TEST_URL, skip cleanly otherwise, never the
 * application's implicit DATABASE_URL.
 *
 * "Disposable-task shape (rolled-back transaction)": DisposableTransactionPool
 * below is a duck-typed `pg.Pool` substitute backed by exactly ONE real,
 * already-`BEGIN`-ed client. It absorbs every BEGIN/COMMIT/ROLLBACK the store
 * issues internally (PgSessionStore commits its own transactions per call --
 * see the private `transaction()` helper) so the whole scenario, schema DDL
 * included, stays inside the single outer transaction this file opens and
 * always rolls back in `finally`, even on assertion failure. The scenario
 * runs against a real, live database and leaves zero residue -- verified
 * directly, from a second, independent connection, at the end of the test.
 */
import { randomUUID } from "node:crypto";
import { Client, type Pool } from "pg";
import { afterAll, describe, expect, it } from "vitest";

import { PgSessionStore } from "../src/ports/impl/pgSessionStore.js";
import type { ConverseEventType, StoredEvent } from "../src/ports/index.js";

const testUrl = process.env.PG_SESSION_STORE_TEST_URL ?? process.env.DATABASE_URL;
const describeWithPostgres = testUrl ? describe : describe.skip;

/** No-op success shape for the intercepted BEGIN/COMMIT/ROLLBACK calls below. */
const ABSORBED = "SELECT 1";

class DisposableTransactionPool {
  constructor(private readonly client: Client) {}

  async query(text: string, params?: unknown[]): Promise<unknown> {
    return this.client.query(this.intercept(text), params as never[] | undefined);
  }

  async connect(): Promise<{
    query: (text: string, params?: unknown[]) => Promise<unknown>;
    release: () => void;
  }> {
    return {
      query: (text: string, params?: unknown[]) =>
        this.client.query(this.intercept(text), params as never[] | undefined),
      release: () => {},
    };
  }

  /** BEGIN/COMMIT/ROLLBACK from the store's own transaction helper must never
   * end this file's single outer transaction -- absorb them silently. */
  private intercept(text: string): string {
    const bare = text.trim().toUpperCase();
    return bare === "BEGIN" || bare === "COMMIT" || bare === "ROLLBACK" ? ABSORBED : text;
  }
}

describeWithPostgres("conversation harness suite (disposable-task, rolled back)", () => {
  const pgClient = new Client({ connectionString: testUrl });

  afterAll(async () => {
    await pgClient.end();
  });

  it("create project -> converse -> disconnect -> recover reproduces an identical transcript", async () => {
    await pgClient.connect();
    await pgClient.query("BEGIN");
    const disposablePool = new DisposableTransactionPool(pgClient) as unknown as Pool;
    const tablePrefix = `conv_e2e_${randomUUID().replaceAll("-", "").slice(0, 16)}`;

    try {
      const tenantId = `tenant-${randomUUID()}`;
      const drawingId = `drawing-${randomUUID()}`; // the "project" this conversation belongs to

      // --- create project ---------------------------------------------- //
      const author = new PgSessionStore({
        pool: disposablePool,
        tablePrefix,
        staleTurnTimeoutS: 180,
      });
      await author.initializeSchema();
      const session = await author.createOrGetSession(tenantId, drawingId);
      expect(session.tenant_id).toBe(tenantId);
      expect(session.drawing_id).toBe(drawingId);

      // --- converse: two turns, several events each ---------------------- //
      const scripted: Array<[string, Array<[ConverseEventType, Record<string, unknown>]>]> = [
        [
          "turn-1",
          [
            ["turn_started", {}],
            ["text_delta", { text: "hello" }],
            ["text_delta", { text: " world" }],
            ["turn_complete", { stop_reason: "end_turn" }],
          ],
        ],
        [
          "turn-2",
          [
            ["turn_started", {}],
            ["tool_call", { tool: "add-panel" }],
            ["tool_result", { ok: true }],
            ["turn_complete", { stop_reason: "end_turn" }],
          ],
        ],
      ];

      const written: StoredEvent[] = [];
      for (const [turnId, events] of scripted) {
        await author.beginTurn(session.session_id, turnId);
        for (const [type, data] of events) {
          written.push(await author.appendEvent(session.session_id, turnId, type, data));
        }
        await author.endTurn(session.session_id, turnId, "complete", "end_turn");
      }
      expect(written).toHaveLength(8);

      // --- disconnect ----------------------------------------------------- //
      // The authoring instance (and any state it cached in memory) is
      // discarded. A brand-new PgSessionStore instance -- sharing only the
      // still-open transaction's underlying connection, never the old
      // instance's memory -- stands in for the reconnect.
      const recovered = new PgSessionStore({
        pool: disposablePool,
        tablePrefix,
        staleTurnTimeoutS: 180,
      });

      // --- recover -> identical transcript -------------------------------- //
      // Recovery is side-effect-free and idempotent: running it twice must
      // reproduce the exact same ordered transcript both times.
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const transcript = await recovered.eventsAfter(session.session_id, 0, 100);
        expect(transcript.map((e) => e.seq)).toEqual(written.map((e) => e.seq));
        expect(transcript.map((e) => e.turn_id)).toEqual(written.map((e) => e.turn_id));
        expect(transcript.map((e) => ({ type: e.type, data: e.data }))).toEqual(
          written.map((e) => ({ type: e.type, data: e.data })),
        );
      }
    } finally {
      await pgClient.query("ROLLBACK");
    }

    // --- disposable-task shape: zero residue, checked from a fresh, wholly
    // independent connection that never saw the rolled-back transaction. --- //
    const verifyClient = new Client({ connectionString: testUrl });
    await verifyClient.connect();
    try {
      const result = await verifyClient.query<{ relation: string | null }>(
        "SELECT to_regclass($1) AS relation",
        [`"${tablePrefix}_sessions"`],
      );
      expect(result.rows[0]?.relation).toBeNull();
    } finally {
      await verifyClient.end();
    }
  });
});
