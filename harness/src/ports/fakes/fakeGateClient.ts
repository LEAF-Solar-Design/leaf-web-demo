/**
 * Fake GateClient - an in-memory stand-in for POST /internal/agent/gate that
 * mirrors the app gate's decision semantics AND its action vocabulary: the app
 * policy catalog (server/agent_policy.json) defines RUNG actions
 * (read_platform_state, run_read_tool, run_write_tool, submit_live_solve,
 * undo_drawing_version, author_tool, register_tool), and the real gate denies
 * anything else as unknown_action — so this fake does too, keeping the harness
 * tests honest about what the integrated path accepts. Phase-1 policy:
 *   - kill switch -> deny everything;
 *   - configured denials (by action or by run-tool target) -> deny;
 *   - unknown action -> deny "unknown_action" (exactly like agent_gate.gate());
 *   - run_write_tool / submit_live_solve -> awaiting_approval with a
 *     confirmation_id, unless the call carries an already-GRANTED
 *     confirmation_id (args-bound approval, wire contract section 7) -> allow;
 *   - everything else known -> allow.
 * request_confirmation is UI-only and carries no catalog entry; the app side is
 * expected to auto-allow it at rung 0, which this fake encodes.
 * Records every check so tests can prove the gate ran before EVERY execution.
 */

import { randomUUID } from "node:crypto";
import type {
  GateCheckContext,
  GateCheckResult,
  GateClient,
} from "../index.js";

/** The app policy catalog's action names + the rung-0 UI confirmation request. */
const KNOWN_ACTIONS = new Set([
  "read_platform_state",
  "run_read_tool",
  "run_write_tool",
  "submit_live_solve",
  "undo_drawing_version",
  "author_tool",
  "register_tool",
  "request_confirmation",
]);

export interface RecordedGateCheck {
  action: string;
  args: Record<string, unknown>;
  ctx: GateCheckContext;
  decision: GateCheckResult["decision"];
}

export class FakeGateClient implements GateClient {
  readonly checks: RecordedGateCheck[] = [];
  /** action name or run-tool target -> deny reason. */
  private readonly denials = new Map<string, string>();
  /** confirmation_ids the app-side pending store has APPROVED. */
  private readonly granted = new Set<string>();
  killSwitch = false;
  /** When set, the next awaiting_approval uses this id (lets tests pin ids). */
  nextConfirmationId: string | null = null;

  deny(actionOrTarget: string, reason: string): void {
    this.denials.set(actionOrTarget, reason);
  }

  /** Simulate the app approving a pending confirmation (section 7 step 3a). */
  grant(confirmationId: string): void {
    this.granted.add(confirmationId);
  }

  async check(
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): Promise<GateCheckResult> {
    const result = this.decide(action, args);
    this.checks.push({ action, args, ctx, decision: result.decision });
    return result;
  }

  private decide(action: string, args: Record<string, unknown>): GateCheckResult {
    if (this.killSwitch) {
      return { decision: "deny", reason: "agent kill switch active", policy: "kill_switch", rung: "R0" };
    }
    if (!KNOWN_ACTIONS.has(action)) {
      return { decision: "deny", reason: "unknown_action", policy: "catalog", rung: "R0" };
    }
    const actionDenial = this.denials.get(action);
    if (actionDenial) {
      return { decision: "deny", reason: actionDenial, policy: "always_deny", rung: "R0" };
    }
    if (action === "run_read_tool" || action === "run_write_tool" || action === "submit_live_solve") {
      const target = String(args.tool ?? "");
      const targetDenial = this.denials.get(target);
      if (targetDenial) {
        return { decision: "deny", reason: targetDenial, policy: "always_deny", rung: "R2" };
      }
      if (action !== "run_read_tool") {
        const rung = action === "submit_live_solve" ? "R4" : "R3";
        const cid = typeof args.confirmation_id === "string" ? args.confirmation_id : null;
        if (cid && this.granted.has(cid)) {
          return { decision: "allow", policy: "confirm_once", rung };
        }
        const confirmationId = this.nextConfirmationId ?? randomUUID();
        this.nextConfirmationId = null;
        return {
          decision: "awaiting_approval",
          confirmation_id: confirmationId,
          reason: "write capability requires approval",
          policy: "always_confirm",
          rung,
        };
      }
      return { decision: "allow", policy: "auto", rung: "R2" };
    }
    return { decision: "allow", policy: "auto", rung: "R0" };
  }
}
