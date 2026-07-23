/**
 * PostgreSQL SessionStore contract checks.
 *
 * These tests never use the application's DATABASE_URL implicitly. Supply a
 * disposable PostgreSQL URL through PG_SESSION_STORE_TEST_URL to run them.
 */

import { randomUUID } from "node:crypto";
import { Pool } from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  InactiveTurnError,
  PgSessionStore,
} from "../src/ports/impl/pgSessionStore.js";
import { TurnLockHeldError } from "../src/ports/impl/sessionStore.js";
import type { ConfirmationRecord } from "../src/ports/index.js";

const testUrl = process.env.PG_SESSION_STORE_TEST_URL;
const describeWithPostgres = testUrl ? describe : describe.skip;

describe("PgSessionStore configuration", () => {
  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    "rejects invalid staleTurnTimeoutS %s",
    (staleTurnTimeoutS) => {
      expect(
        () =>
          new PgSessionStore({
            poolConfig: { connectionString: "postgresql://invalid.invalid/test" },
            staleTurnTimeoutS,
          }),
      ).toThrow(/finite and positive/);
    },
  );
});

describeWithPostgres("PgSessionStore contract", () => {
  const prefix = `harness_test_${randomUUID().replaceAll("-", "")}`;
  const pool = new Pool({ connectionString: testUrl, max: 8 });
  const first = new PgSessionStore({
    pool,
    tablePrefix: prefix,
    staleTurnTimeoutS: 180,
  });
  const second = new PgSessionStore({
    pool,
    tablePrefix: prefix,
    staleTurnTimeoutS: 180,
  });

  beforeAll(async () => {
    await first.initializeSchema();
  });

  afterAll(async () => {
    await pool.query(`
      DROP TABLE IF EXISTS
        "${prefix}_usage",
        "${prefix}_confirmations",
        "${prefix}_events",
        "${prefix}_turns",
        "${prefix}_sessions"
      CASCADE
    `);
    await pool.end();
  });

  it("creates one active session per tenant and drawing", async () => {
    const [a, b] = await Promise.all([
      first.createOrGetSession("tenant-a", "drawing-a"),
      second.createOrGetSession("tenant-a", "drawing-a"),
    ]);
    expect(b.session_id).toBe(a.session_id);

    const updated = await first.updateSession(a.session_id, {
      sdk_session_id: "sdk-a",
      status: "active",
      summary: "ready",
    });
    expect(updated).toMatchObject({
      sdk_session_id: "sdk-a",
      status: "active",
      summary: "ready",
    });
    await expect(first.updateSession(randomUUID(), { status: "idle" })).rejects.toThrow(
      /session_not_found/,
    );
  });

  it("assigns unique contiguous event sequence across two writers", async () => {
    const session = await first.createOrGetSession("tenant-events", "drawing-events");
    await first.beginTurn(session.session_id, "turn-events");
    await expect(
      second.appendEvent(session.session_id, "stale-turn", "text_delta", { text: "stale" }),
    ).rejects.toBeInstanceOf(InactiveTurnError);

    const writes = Array.from({ length: 20 }, (_, i) =>
      (i % 2 === 0 ? first : second).appendEvent(
        session.session_id,
        "turn-events",
        "text_delta",
        { text: `${i}` },
      ),
    );
    const events = await Promise.all(writes);
    expect(events.map((event) => event.seq).sort((a, b) => a - b)).toEqual(
      Array.from({ length: 20 }, (_, i) => i + 1),
    );
    expect((await first.eventsAfter(session.session_id, 0, 3)).map((event) => event.seq)).toEqual([
      18, 19, 20,
    ]);
    await first.endTurn(session.session_id, "turn-events", "complete", "end_turn");
    await expect(
      second.appendEvent(session.session_id, "turn-events", "text_delta", { text: "late" }),
    ).rejects.toMatchObject({ activeTurnId: null });
  });

  it("allows exactly one active turn across two writers", async () => {
    const session = await first.createOrGetSession("tenant-turn", "drawing-turn");
    const attempts = await Promise.allSettled([
      first.beginTurn(session.session_id, "turn-a"),
      second.beginTurn(session.session_id, "turn-b"),
    ]);
    const successful = attempts.filter((attempt) => attempt.status === "fulfilled");
    const rejected = attempts.filter((attempt) => attempt.status === "rejected");
    expect(successful).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason).toBeInstanceOf(TurnLockHeldError);

    const active = await first.getActiveTurn(session.session_id);
    expect(["turn-a", "turn-b"]).toContain(active?.turn_id);
    await first.endTurn(session.session_id, active!.turn_id, "complete", "end_turn");
    expect(await second.getActiveTurn(session.session_id)).toBeNull();
  });

  it("does not overwrite a terminal or reaped turn", async () => {
    const terminalSession = await first.createOrGetSession(
      "tenant-terminal",
      "drawing-terminal",
    );
    await first.beginTurn(terminalSession.session_id, "turn-terminal");
    await first.endTurn(terminalSession.session_id, "turn-terminal", "complete", "end_turn");
    await second.endTurn(terminalSession.session_id, "turn-terminal", "failed", "late_worker");
    const terminal = await pool.query<{ status: string; stop_reason: string }>(
      `SELECT status, stop_reason FROM "${prefix}_turns"
        WHERE session_id = $1 AND turn_id = $2`,
      [terminalSession.session_id, "turn-terminal"],
    );
    expect(terminal.rows[0]).toMatchObject({ status: "complete", stop_reason: "end_turn" });

    const staleStore = new PgSessionStore({
      pool,
      tablePrefix: prefix,
      staleTurnTimeoutS: 0.001,
    });
    const staleSession = await first.createOrGetSession("tenant-stale", "drawing-stale");
    await staleStore.beginTurn(staleSession.session_id, "turn-stale");
    await new Promise((resolve) => setTimeout(resolve, 20));
    await staleStore.endTurn(staleSession.session_id, "turn-stale", "complete", "late_complete");
    const reaped = await pool.query<{ status: string; stop_reason: string }>(
      `SELECT status, stop_reason FROM "${prefix}_turns"
        WHERE session_id = $1 AND turn_id = $2`,
      [staleSession.session_id, "turn-stale"],
    );
    expect(reaped.rows[0]).toMatchObject({ status: "failed", stop_reason: "abandoned" });
  });

  it("resolves a pending confirmation once across two writers", async () => {
    const session = await first.createOrGetSession(
      "tenant-confirmation",
      "drawing-confirmation",
    );
    const now = Date.now();
    const confirmation: ConfirmationRecord = {
      confirmation_id: randomUUID(),
      session_id: session.session_id,
      turn_id: "turn-confirmation",
      action: "run_capability",
      args_json: JSON.stringify({ tool: "add-panel" }),
      kind: "run_capability",
      status: "pending",
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + 300_000).toISOString(),
      decided_at: null,
      decided_by: null,
    };
    await first.putConfirmation(confirmation);
    await second.putConfirmation(confirmation);
    await expect(
      second.putConfirmation({
        ...confirmation,
        args_json: JSON.stringify({ tool: "different-tool" }),
      }),
    ).rejects.toThrow(/confirmation_conflict/);

    const [approved, denied] = await Promise.all([
      first.resolveConfirmation(confirmation.confirmation_id, true, "operator-a"),
      second.resolveConfirmation(confirmation.confirmation_id, false, "operator-b"),
    ]);
    expect(approved?.status).toBe(denied?.status);
    expect(approved?.decided_by).toBe(denied?.decided_by);
    expect(["approved", "denied"]).toContain(approved?.status);

    const replay = await first.resolveConfirmation(
      confirmation.confirmation_id,
      approved?.status !== "approved",
      "operator-c",
    );
    expect(replay).toMatchObject({
      status: approved?.status,
      decided_by: approved?.decided_by,
    });
  });
});
