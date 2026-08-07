/**
 * Plan-first, harness half (chip: "In plan-first, execution starts only after
 * the approval lifecycle completes").
 *
 * The mechanism is ONE knob: under the x-leaf-approval-policy sidecar the
 * runner's allowedTools is EMPTY, because the SDK auto-approves allowlisted
 * names BEFORE canUseTool runs — emptying the list is the only way the APS
 * tool reaches the confirmation lifecycle. The safety of that emptying rests
 * on the tenant-denial exemption being derived from CONSTANTS, not from the
 * allowedTools array: the APS tool must CONFIRM, never be DENIED.
 */
import { IncomingMessage } from "node:http";
import { describe, expect, it } from "vitest";

import {
  buildTurnOptions,
  requiresToolConfirmation,
  tenantMcpToolDenial,
} from "../src/ports/impl/agentSdkTurnRunner.js";
import { planFirstOption } from "../src/server.js";

const APS = "mcp__converse__aps_test_run";

function options(planFirst: boolean | undefined) {
  return buildTurnOptions({
    childEnv: {},
    model: undefined,
    maxTurns: 24,
    abortController: new AbortController(),
    server: { fake: true },

    canUseTool: (async () => ({ behavior: "deny", message: "x" })) as never,
    ...(planFirst === undefined ? {} : { planFirst }),
  });
}

describe("plan-first: the allowlist knob", () => {
  it("empties allowedTools under plan_first and ONLY under plan_first", () => {
    expect(options(undefined).allowedTools).toEqual([APS]);
    expect(options(false).allowedTools).toEqual([APS]);
    expect(options(true).allowedTools).toEqual([]);
  });

  it("the APS tool CONFIRMS under plan_first — never denied", () => {
    // The tenant-denial exemption comes from the constants, so an empty
    // allowedTools cannot turn the APS tool into a denied tenant tool.
    expect(tenantMcpToolDenial(APS)).toBeNull();
    expect(requiresToolConfirmation(APS, new Set(["aps_test_run"]))).toBe(true);
    // ...while a tenant server's tool stays denied exactly as before.
    expect(tenantMcpToolDenial("mcp__tenantsrv__anything")).not.toBeNull();
  });
});

describe("plan-first: the sidecar header", () => {
  function req(headers: Record<string, string>): IncomingMessage {
    return { headers } as unknown as IncomingMessage;
  }

  it("recognizes exactly the documented value", () => {
    expect(planFirstOption(req({ "x-leaf-approval-policy": "plan_first" })))
      .toEqual({ planFirst: true });
  });

  it("anything else is today's behavior — the header can only NARROW", () => {
    expect(planFirstOption(req({}))).toEqual({});
    expect(planFirstOption(req({ "x-leaf-approval-policy": "PLAN_FIRST" }))).toEqual({});
    expect(planFirstOption(req({ "x-leaf-approval-policy": "auto_approve_reads" }))).toEqual({});
    expect(planFirstOption(req({ "x-leaf-approval-policy": "" }))).toEqual({});
  });
});
