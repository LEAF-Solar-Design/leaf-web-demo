import { describe, expect, it, vi } from "vitest";
import type { Pool } from "pg";
import type { AgentRunInput, AgentRunResult, AgentRunner } from "../src/ports/index.js";
import {
  MeteredAgentRunner, e2bCostEventKey, e2bCostUsd, meteringConfigFromEnv, withMeteringIdentity,
} from "../src/ports/impl/meteredAgentRunner.js";
import type { CostEvent, MeteredAgentRunnerOptions } from "../src/ports/impl/meteredAgentRunner.js";
import { CostEventConflict, PgCostEventSink, COST_EVENT_STATEMENT_TIMEOUT_MS } from "../src/ports/impl/pgCostEventSink.js";

const ORG = "00000000-0000-4000-8000-000000000001";
const ENDED = "2026-10-22T12:00:00.000Z";
const INPUT: AgentRunInput = {
  description: "count", systemPrompt: "system", repoDir: "/fake",
  grant: { kind: "oauth", oauthToken: "fake" },
  toolset: {
    fsTenantRepo: { root: "/fake", readFile: () => "", exists: () => false, listDir: () => [] },
    submitTool: () => { throw new Error("unused"); },
    apsTestRun: async () => { throw new Error("unused"); },
  },
};
const RESULT: AgentRunResult = {
  tool: {
    name: "count", version: "1", description: "count", kind: "script", engine_op: "count",
    params: {}, returns: {}, capabilities: [], provenance: { author: "agent", created: ENDED },
  },
  code: "source", preview: "counted", files: ["tool.py"],
};

function setup(inner: AgentRunner = { run: async () => RESULT }, overrides: Partial<MeteredAgentRunnerOptions> = {}) {
  const events: CostEvent[] = [];
  const log = vi.fn();
  let tick = 0n;
  const runner = new MeteredAgentRunner(inner, {
    rate: "0.000014", sink: { record: async event => { events.push(event); } },
    identityFor: () => ({ orgId: ORG, runRef: "run-1" }),
    monotonicNow: () => { const value = tick; tick += 2500000000n; return value; },
    endTime: () => new Date(ENDED), log, ...overrides,
  });
  return { runner, events, log };
}

describe("E2B cost metering", () => {
  it("delegates the original input and returns the exact result with one complete event", async () => {
    const run = vi.fn(async () => RESULT);
    const { runner, events } = setup({ run });
    expect(await runner.run(INPUT)).toBe(RESULT);
    expect(run).toHaveBeenCalledExactlyOnceWith(INPUT);
    expect(events).toEqual([{
      org_id: ORG, addon: "e2b_exec", mode: "managed", vendor: "e2b", unit: "sandbox_seconds",
      quantity: "2.500", unit_cost_usd: "0.000014", cost_usd: "0.000035", cost_basis: "estimated",
      event_key: "93d6941c0d1da035f8fafeb675cd2f575fc8bc5d1b5894c3d04f136c55e301f6",
      occurred_at: ENDED, provenance: { source: "harness-e2b", run_ref: "run-1" },
    }]);
  });

  it.each([false, true])("records failures and preserves the original error (synchronous=%s)", async synchronous => {
    const error = new Error("original runner failure");
    const { runner, events } = setup({ run: () => {
      if (synchronous) throw error;
      return Promise.reject(error);
    } });
    await expect(runner.run(INPUT)).rejects.toBe(error);
    expect(events).toHaveLength(1);
    expect(events[0].quantity).toBe("2.500");
  });

  it("isolates sink errors and logs each error class once", async () => {
    const record = vi.fn().mockRejectedValue(new TypeError("secret error text"));
    const { runner, log } = setup(undefined, { sink: { record } });
    expect(await runner.run(INPUT)).toBe(RESULT);
    expect(await runner.run(INPUT)).toBe(RESULT);
    expect(record).toHaveBeenCalledTimes(2);
    expect(log).toHaveBeenCalledExactlyOnceWith("[cost-metering] sink_failure:TypeError");
    record.mockRejectedValue(new RangeError("another secret"));
    await runner.run(INPUT);
    expect(log).toHaveBeenCalledTimes(2);
  });

  it("preserves a runner failure when both the sink and logger fail", async () => {
    const original = new Error("runner");
    const { runner } = setup({ run: async () => { throw original; } }, {
      sink: { record: async () => { throw new Error("sink"); } },
      log: () => { throw new Error("logger"); },
    });
    await expect(runner.run(INPUT)).rejects.toBe(original);
  });

  it("computes decimal micro-dollars exactly with half-up ties", () => {
    expect(e2bCostUsd(2500n, "0.000014")).toBe("0.000035");
    expect(e2bCostUsd(250n, "0.000014")).toBe("0.000004");
    expect(e2bCostUsd(249n, "0.000014")).toBe("0.000003");
    expect(e2bCostUsd(0n, "0.000014")).toBe("0.000000");
    expect(e2bCostUsd(1001n, "0.1234567890")).toBe("0.123580");
  });

  it("matches the Python cost_event_key vector and normalizes org case", () => {
    // Literal computed by platform.tenant_cost_store.cost_event_key on origin/main.
    expect(e2bCostEventKey(ORG, "run-1")).toBe("93d6941c0d1da035f8fafeb675cd2f575fc8bc5d1b5894c3d04f136c55e301f6");
    const org = "abcdefab-0000-4000-8000-000000000001";
    expect(e2bCostEventKey(org.toUpperCase(), "run-1")).toBe(e2bCostEventKey(org, "run-1"));
    expect(() => e2bCostEventKey(ORG, "non-ascii-\u00e9")).toThrow("ASCII");
  });

  it.each([undefined, "demo-tenant", "", "not-a-uuid"])("skips unresolved org %s and counts every skip", async orgId => {
    const { runner, events, log } = setup(undefined, { identityFor: () => ({ orgId }) });
    await runner.run(INPUT);
    await runner.run(INPUT);
    expect(events).toEqual([]);
    expect(runner.counters.skipped_unresolved).toBe(2);
    expect(log).toHaveBeenCalledExactlyOnceWith("[cost-metering] skipped_unresolved");
  });

  it("keeps concurrent authenticated request identities separate and gives runs ASCII ids", async () => {
    const { runner, events } = setup(undefined, { identityFor: undefined });
    const other = "00000000-0000-4000-8000-000000000002";
    await Promise.all([
      withMeteringIdentity({ orgId: ORG, runRef: "request-1" }, () => runner.run(INPUT)),
      withMeteringIdentity({ orgId: other }, () => runner.run(INPUT)),
    ]);
    expect(events.map(event => event.org_id)).toEqual([ORG, other]);
    expect(events[0].provenance.run_ref).toBe("request-1");
    expect(events[1].provenance.run_ref).toMatch(/^[0-9a-f-]{36}$/);
    await runner.run(INPUT);
    expect(runner.counters.skipped_unresolved).toBe(1);
  });

  it.each([
    {}, { LEAF_COST_METERING: "true", LEAF_E2B_USD_PER_SECOND: "0.000014" },
    ...[undefined, "", "0", "0.000", "-1", "1e-5", " 0.1", "NaN", "1.2.3", "0.00000000001"].map(rate => ({
      LEAF_COST_METERING: "1", LEAF_E2B_USD_PER_SECOND: rate,
    })),
  ])("does not opt in with invalid configuration %j", env => {
    expect(meteringConfigFromEnv(env).enabled).toBe(false);
  });

  it("opts in only with the explicit flag and configured rate", () => {
    expect(meteringConfigFromEnv({ LEAF_COST_METERING: "1", LEAF_E2B_USD_PER_SECOND: "0.000014" }))
      .toEqual({ enabled: true, rate: "0.000014" });
  });
});

describe("Postgres cost event sink with a fake client", () => {
  async function fixture(inserted: boolean, identical: boolean) {
    const { runner, events } = setup();
    await runner.run(INPUT);
    const query = vi.fn(async (sql: string, _values?: unknown[]) => ({
      rows: sql.includes("INSERT INTO") ? (inserted ? [{ id: 1 }] : [])
        : sql.includes("SELECT id FROM") ? (identical ? [{ id: 1 }] : []) : [],
    }));
    const release = vi.fn();
    const pool = { connect: async () => ({ query, release }) } as unknown as Pick<Pool, "connect">;
    return { sink: new PgCostEventSink(pool), event: events[0], query, release };
  }

  it("inserts parameterized facts with a bounded transaction-local timeout", async () => {
    const { sink, event, query, release } = await fixture(true, false);
    await sink.record(event);
    expect(query.mock.calls[1]).toEqual([
      "SELECT set_config('statement_timeout', $1, true)", [String(COST_EVENT_STATEMENT_TIMEOUT_MS)],
    ]);
    expect(query.mock.calls[2][0]).toContain("ON CONFLICT (event_key) DO NOTHING");
    expect(query.mock.calls[2][1]).toEqual([
      ORG, "e2b_exec", "managed", "e2b", "sandbox_seconds", "2.500", "0.000014",
      "0.000035", "estimated", event.event_key, ENDED, JSON.stringify(event.provenance),
    ]);
    expect(query).toHaveBeenLastCalledWith("COMMIT");
    expect(release).toHaveBeenCalledOnce();
  });

  it("accepts identical replays using a separate snapshot statement", async () => {
    const { sink, event, query } = await fixture(false, true);
    await sink.record(event);
    expect(query.mock.calls[3][0]).toContain("provenance IS NOT DISTINCT FROM $12::jsonb");
    expect(query).toHaveBeenLastCalledWith("COMMIT");
  });

  it("rejects differing replays and releases the connection after rollback", async () => {
    const { sink, event, query, release } = await fixture(false, false);
    await expect(sink.record(event)).rejects.toBeInstanceOf(CostEventConflict);
    expect(query).toHaveBeenLastCalledWith("ROLLBACK");
    expect(release).toHaveBeenCalledOnce();
  });
});
