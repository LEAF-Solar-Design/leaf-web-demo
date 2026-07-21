/**
 * FileSessionStore tests — the durable, zero-dependency transcript store.
 * Proves the section-6 semantics hold ON DISK: idempotent createOrGet, monotonic
 * seq that survives process restart (a fresh store over the same dir), atomic
 * index writes (no temp litter, always-parseable JSON), the store-enforced turn
 * lock, TTL-aware confirmations, and torn-final-line crash tolerance.
 */

import {
  appendFileSync,
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { FileSessionStore, TurnLockHeldError } from "../src/ports/impl/sessionStore.js";
import type { ConverseTurnUsage } from "../src/ports/index.js";

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "leaf-sessions-test-"));
}

const USAGE: ConverseTurnUsage = {
  turns: 1,
  input_tokens: 10,
  output_tokens: 5,
  cache_creation_tokens: 2,
  cache_read_tokens: 100,
  cost_tokens: 17,
};

describe("FileSessionStore — sessions", () => {
  it("createOrGetSession is idempotent per (tenant, drawing) and survives reload", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const a = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const b = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const other = await store.createOrGetSession("demo-tenant", "other");
    expect(b.session_id).toBe(a.session_id);
    expect(other.session_id).not.toBe(a.session_id);

    // A fresh store over the SAME dir (restart) still resolves the same session.
    const reloaded = new FileSessionStore({ dir });
    const c = await reloaded.createOrGetSession("demo-tenant", "rooftop_demo");
    expect(c.session_id).toBe(a.session_id);
    expect(await reloaded.getSession(a.session_id)).toMatchObject({
      tenant_id: "demo-tenant",
      drawing_id: "rooftop_demo",
      status: "idle",
    });
  });

  it("updateSession patches status / sdk_session_id and bumps updated_at", async () => {
    const store = new FileSessionStore({ dir: scratch() });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const updated = await store.updateSession(s.session_id, {
      status: "active",
      sdk_session_id: "sdk-123",
    });
    expect(updated).toMatchObject({ status: "active", sdk_session_id: "sdk-123" });
    await expect(store.updateSession("missing", { status: "idle" })).rejects.toThrow(
      /session_not_found/,
    );
  });
});

describe("FileSessionStore — events (seq + replay + atomicity)", () => {
  it("assigns monotonic seq, replays from afterSeq, and seq survives restart", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    for (let i = 0; i < 3; i++) {
      await store.appendEvent(s.session_id, "turn-1", "text_delta", { text: `t${i}` });
    }

    // Restart: a fresh store recovers the counter from disk and CONTINUES it.
    const store2 = new FileSessionStore({ dir });
    const e4 = await store2.appendEvent(s.session_id, "turn-2", "turn_complete", {
      stop_reason: "end_turn",
    });
    expect(e4.seq).toBe(4);

    const all = await store2.eventsAfter(s.session_id, 0);
    expect(all.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(await store2.eventsAfter(s.session_id, 2)).toHaveLength(2);
    // limit returns the MOST RECENT N, still ascending.
    const limited = await store2.eventsAfter(s.session_id, 0, 2);
    expect(limited.map((e) => e.seq)).toEqual([3, 4]);
  });

  it("concurrent appends serialize into unique contiguous seqs", async () => {
    const store = new FileSessionStore({ dir: scratch() });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const results = await Promise.all(
      Array.from({ length: 25 }, (_, i) =>
        store.appendEvent(s.session_id, "turn-1", "text_delta", { text: `c${i}` }),
      ),
    );
    const seqs = results.map((r) => r.seq).sort((a, b) => a - b);
    expect(seqs).toEqual(Array.from({ length: 25 }, (_, i) => i + 1));
  });

  it("tolerates a torn (crash-truncated) final JSONL line", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    await store.appendEvent(s.session_id, "turn-1", "text_delta", { text: "whole" });
    appendFileSync(join(dir, s.session_id, "events.jsonl"), '{"session_id":"torn', "utf8");

    const store2 = new FileSessionStore({ dir });
    expect(await store2.eventsAfter(s.session_id, 0)).toHaveLength(1);
    const next = await store2.appendEvent(s.session_id, "turn-2", "text_delta", { text: "after" });
    expect(next.seq).toBe(2);
  });

  it("atomic index writes leave no temp litter and always-parseable JSON", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    for (let i = 0; i < 10; i++) {
      await store.createOrGetSession("demo-tenant", `drawing-${i}`);
    }
    expect(readdirSync(dir).filter((f) => f.endsWith(".tmp"))).toEqual([]);
    const index = JSON.parse(readFileSync(join(dir, "sessions.json"), "utf8")) as unknown[];
    expect(index).toHaveLength(10);
  });
});

describe("FileSessionStore — turn lock", () => {
  it("beginTurn enforces ONE active turn; endTurn releases", async () => {
    const store = new FileSessionStore({ dir: scratch() });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");

    const t1 = await store.beginTurn(s.session_id, "turn-1");
    expect(t1).toMatchObject({ turn_id: "turn-1", status: "active", seq_start: 1 });
    expect((await store.getActiveTurn(s.session_id))?.turn_id).toBe("turn-1");

    await expect(store.beginTurn(s.session_id, "turn-2")).rejects.toBeInstanceOf(TurnLockHeldError);
    await expect(store.beginTurn(s.session_id, "turn-2")).rejects.toMatchObject({
      activeTurnId: "turn-1",
    });

    await store.endTurn(s.session_id, "turn-1", "complete", "end_turn");
    expect(await store.getActiveTurn(s.session_id)).toBeNull();
    await store.beginTurn(s.session_id, "turn-2");
    expect((await store.getActiveTurn(s.session_id))?.turn_id).toBe("turn-2");
  });

  it("a RECENT active turn survives restart (the lock is durable, not reaped)", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    await store.beginTurn(s.session_id, "turn-1");

    const store2 = new FileSessionStore({ dir });
    expect((await store2.getActiveTurn(s.session_id))?.turn_id).toBe("turn-1");
    await expect(store2.beginTurn(s.session_id, "turn-2")).rejects.toBeInstanceOf(TurnLockHeldError);
  });

  it("a crash-abandoned active turn (older than the wall-clock budget) is reaped, not an eternal lock", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    await store.beginTurn(s.session_id, "turn-1");

    // Simulate a harness crash mid-turn: backdate the persisted active row far
    // past the budget (default LEAF_SPINE_TURN_TIMEOUT_S 120s + 60s margin).
    const turnsPath = join(dir, s.session_id, "turns.json");
    const rows = JSON.parse(readFileSync(turnsPath, "utf8")) as Record<string, unknown>[];
    rows[0]!.started_at = new Date(Date.now() - 3_600_000).toISOString();
    writeFileSync(turnsPath, JSON.stringify(rows, null, 2) + "\n", "utf8");

    // A fresh store over the same dir (restart) no longer reports the lock...
    const store2 = new FileSessionStore({ dir });
    expect(await store2.getActiveTurn(s.session_id)).toBeNull();
    // ...the next turn begins, and the leftover row was closed as failed/abandoned.
    await store2.beginTurn(s.session_id, "turn-2");
    const after = JSON.parse(readFileSync(turnsPath, "utf8")) as Record<string, unknown>[];
    expect(after[0]).toMatchObject({
      turn_id: "turn-1",
      status: "failed",
      stop_reason: "abandoned",
    });
    expect(after[0]!.ended_at).not.toBeNull();
    expect(after[1]).toMatchObject({ turn_id: "turn-2", status: "active" });
  });
});

describe("FileSessionStore — confirmations", () => {
  it("put / get / resolve, with duplicate resolution a no-op", async () => {
    const store = new FileSessionStore({ dir: scratch() });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const rec = store.newConfirmation(s.session_id, "turn-1", "run_capability", {
      tool: "add-panel",
      params: { row: 2 },
    }, "run_capability");
    await store.putConfirmation(rec);

    expect(await store.getConfirmation(rec.confirmation_id)).toMatchObject({
      status: "pending",
      action: "run_capability",
    });

    const resolved = await store.resolveConfirmation(rec.confirmation_id, true, "demo-tenant");
    expect(resolved).toMatchObject({ status: "approved", decided_by: "demo-tenant" });
    expect(resolved!.decided_at).not.toBeNull();

    // Second resolution does not flip the decision.
    const again = await store.resolveConfirmation(rec.confirmation_id, false, "demo-tenant");
    expect(again!.status).toBe("approved");
    expect(await store.resolveConfirmation("missing", true, "x")).toBeNull();
  });

  it("resolving past the TTL marks the record expired (section 7: TTL 300s)", async () => {
    const store = new FileSessionStore({ dir: scratch(), confirmationTtlS: -1 });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    const rec = store.newConfirmation(s.session_id, "turn-1", "run_capability", {}, "run_capability");
    await store.putConfirmation(rec);
    const resolved = await store.resolveConfirmation(rec.confirmation_id, true, "demo-tenant");
    expect(resolved).toMatchObject({ status: "expired", decided_by: null });
  });
});

describe("FileSessionStore — usage", () => {
  it("appends one JSONL usage record per turn", async () => {
    const dir = scratch();
    const store = new FileSessionStore({ dir });
    const s = await store.createOrGetSession("demo-tenant", "rooftop_demo");
    await store.appendUsage(s.session_id, "turn-1", USAGE);
    const path = join(dir, s.session_id, "usage.jsonl");
    expect(existsSync(path)).toBe(true);
    const lines = readFileSync(path, "utf8").trim().split("\n");
    expect(lines).toHaveLength(1);
    expect(JSON.parse(lines[0]!)).toMatchObject({
      session_id: s.session_id,
      turn_id: "turn-1",
      usage: { cost_tokens: 17 },
    });
  });
});
