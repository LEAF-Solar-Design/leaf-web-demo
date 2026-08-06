/**
 * Invariant v2 spy — the conversational runtime-separation test (supersedes the
 * design-time-only wording for the converse lane; test/designTimeOnly.test.ts
 * still guards the author lane).
 *
 * The invariant: the conversational session PLANS / EXPLAINS / DISPATCHES —
 * registered-tool EXECUTION never touches the Agent SDK. Enforced two ways:
 *
 *   1. STATIC: no converse-lane module (converseLoop, spineSystemPrompt,
 *      sessionStore, appRunClient, the converse fakes) imports the Agent SDK —
 *      not even via the non-literal dynImport escape hatch agentSdkRunner uses.
 *
 *   2. DYNAMIC: across a full read-dispatch + write-propose + confirm-dispatch
 *      scenario, AppRunClient.submitRun is the only execution call surface,
 *      its payload is exactly {tenantId, tool, params, dwg, wait?, waitTimeoutS?}
 *      where `tool` is a registered catalog NAME — never code, never a drawing
 *      delta — and a GateClient check precedes EVERY dispatch.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { ConverseLoop } from "../src/agent/converseLoop.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { GateCheckContext, StoredEvent, SubmitRunRequest } from "../src/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");

/** Every module of the converse lane (the files this invariant governs). */
const CONVERSE_LANE_FILES = [
  "agent/converseLoop.ts",
  "agent/spineSystemPrompt.ts",
  "ports/impl/sessionStore.ts",
  "ports/impl/appRunClient.ts",
  "ports/fakes/fakeConverseRunner.ts",
  "ports/fakes/fakeAppRunClient.ts",
  "ports/fakes/fakeGateClient.ts",
  "ports/fakes/fakeSessionStore.ts",
];

describe("invariant v2 — static: the converse lane never imports the Agent SDK", () => {
  for (const rel of CONVERSE_LANE_FILES) {
    it(`${rel} has no SDK import (literal or dynamic)`, () => {
      const source = readFileSync(join(SRC, rel), "utf8");
      expect(source).not.toMatch(/@anthropic-ai/);
      expect(source).not.toMatch(/claude-agent-sdk/);
      // The non-literal dynamic-import escape hatch (agentSdkRunner's dynImport
      // pattern) is banned here too: no dynamic import() of ANY kind.
      expect(source).not.toMatch(/\bimport\s*\(/);
      // Nor may the loop reach the SDK sideways through the author runner.
      expect(source).not.toMatch(/agentSdkRunner/);
    });
  }

  it("converseLoop imports only the spine prompt, the ports boundary, and the redactor", () => {
    const source = readFileSync(join(SRC, "agent/converseLoop.ts"), "utf8");
    const specifiers = [...source.matchAll(/from\s+"([^"]+)"/g)].map((m) => m[1]!);
    for (const spec of specifiers) {
      expect(
        spec === "node:crypto" ||
          spec === "./spineSystemPrompt.js" ||
          spec === "../ports/index.js" ||
          // The loop writes an arbitrary caught error into a DURABLE transcript
          // event, so it must be able to scrub a token-shaped string first
          // (sol-critic PR #117, blocker 2). Admitted only because redact.ts is
          // leaf-pure — the assertion below pins that, so this exception can
          // never become a back door to the SDK.
          spec === "../redact.js",
      ).toBe(true);
    }
  });

  it("the redactor stays leaf-pure, so admitting it into the lane imports nothing else", () => {
    const source = readFileSync(join(SRC, "vendor", "mushy-author", "redact.ts"), "utf8");
    expect([...source.matchAll(/from\s+"([^"]+)"/g)]).toHaveLength(0);
    expect(source).not.toMatch(/\bimport\b/);
  });
});

describe("invariant v2 — dynamic: submitRun is the only side-effecting surface", () => {
  it("read + write + confirm scenario: gated, name-only, delta-free dispatch", async () => {
    const runner = new FakeConverseRunner();
    const appRun = new FakeAppRunClient();
    const gate = new FakeGateClient();
    const store = new FakeSessionStore();
    const loop = new ConverseLoop({ runner, appRun, gate, store });

    // One shared ordered trace across the gate and the dispatch surface, so the
    // "gate BEFORE every dispatch" claim is provable, not inferred.
    const trace: string[] = [];
    const origCheck = gate.check.bind(gate);
    gate.check = async (action: string, args: Record<string, unknown>, ctx: GateCheckContext) => {
      trace.push(`gate:${action}`);
      return origCheck(action, args, ctx);
    };
    const origSubmit = appRun.submitRun.bind(appRun);
    appRun.submitRun = async (req: SubmitRunRequest) => {
      trace.push(`submit:${req.tool}`);
      return origSubmit(req);
    };

    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const send = async (text?: string, confirm?: { confirmationId: string; approved: boolean }) => {
      const { done } = await loop.handleMessage({
        sessionId: session.session_id,
        tenantId: "demo-tenant",
        ...(text !== undefined ? { text } : {}),
        ...(confirm !== undefined ? { confirm } : {}),
        contextPacket: { drawing: { id: "rooftop_demo" } },
        authoritySessionId: "app-session-runtime-separation",
        authorityTurnId: "app-turn-runtime-separation",
      });
      await done;
    };

    // (a) read auto-dispatch, (b) write -> proposal, (c) approve -> dispatch.
    await send("RUN:count-by-layer");
    await send('RUN:add-panel PARAMS:{"row":2}');
    const events: StoredEvent[] = await store.eventsAfter(session.session_id, 0);
    const proposed = events.find((e) => e.type === "proposed_run")!;
    const cid = String(proposed.data.confirmation_id);
    gate.grant(cid);
    await send(undefined, { confirmationId: cid, approved: true });

    // --- The dispatch payload contract ------------------------------------- //
    expect(appRun.submitCalls.length).toBe(2); // the read + the confirmed write
    const ALLOWED_KEYS = new Set([
      "tenantId",
      "authoritySessionId",
      "authorityTurnId",
      "tool",
      "params",
      "dwg",
      "catalogDigest",
      "drawingVersion",
      "expectedDrawingHead",
      "catalogCommit",
      "effectiveCatalogDigest",
      "toolManifestSha256",
      "wait",
      "waitTimeoutS",
    ]);
    for (const call of appRun.submitCalls) {
      for (const key of Object.keys(call)) expect(ALLOWED_KEYS.has(key)).toBe(true);
      // A registered catalog NAME — never code.
      expect(typeof call.tool).toBe("string");
      expect(appRun.catalog.some((c) => c.name === call.tool)).toBe(true);
      expect(typeof call.dwg).toBe("string");
      expect(call.authoritySessionId).toBe("app-session-runtime-separation");
      expect(call.authorityTurnId).toBe("app-turn-runtime-separation");
      expect(call).not.toHaveProperty("subject");
      expect(call).not.toHaveProperty("tier");
      expect(call.catalogDigest).toMatch(/^sha256:[0-9a-f]{64}$/);
      // Params are plain data and carry no authored code / drawing deltas.
      const paramsJson = JSON.stringify(call.params);
      for (const banned of ["code", "script", "entry", "source", "entities", "delta", "def run("]) {
        expect(paramsJson).not.toContain(banned);
      }
    }

    // --- The only side-effecting surface ------------------------------------ //
    // Every other AppRunClient method touched in the scenario is a read.
    const touched = new Set(appRun.methodLog);
    for (const method of touched) {
      expect(["submitRun", "getCapabilities", "getDrawingState", "getJob"]).toContain(method);
    }
    // The confirmed write dispatched a job — it did NOT return a drawing mutation.
    const jobLinked = (await store.eventsAfter(session.session_id, 0)).filter(
      (e) => e.type === "job_linked",
    );
    expect(jobLinked.length).toBe(2);
    for (const ev of jobLinked) expect(typeof ev.data.job_id).toBe("string");

    // --- Gate precedes EVERY dispatch ---------------------------------------- //
    // The consult speaks the app policy catalog's rung vocabulary (the real gate
    // denies raw spine tool names as unknown_action).
    for (const [i, entry] of trace.entries()) {
      if (!entry.startsWith("submit:")) continue;
      const before = trace.slice(0, i);
      const lastGate = before.reverse().find((e) => e.startsWith("gate:"));
      expect(lastGate).toBe(
        entry === "submit:count-by-layer" ? "gate:run_read_tool" : "gate:run_write_tool",
      );
    }
    // ...and every tool execution (3 here) was gate-checked exactly once.
    expect(gate.checks.map((c) => c.action)).toEqual([
      "run_read_tool",
      "run_write_tool",
      "run_write_tool",
    ]);
  });
});
