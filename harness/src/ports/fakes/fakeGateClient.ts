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
 * The pending records model the APP's authoritative store (wire contract sections
 * 6 + 7): each is bound to the minting tenant + session + action + args hash and
 * is consumed EXACTLY ONCE, so a drifted, replayed, or cross-session resume is
 * denied here exactly as the real gate denies it.
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
  "request_publication",
  "register_tool",
  "request_confirmation",
  "customize_platform",
]);

export interface RecordedGateCheck {
  action: string;
  args: Record<string, unknown>;
  ctx: GateCheckContext;
  decision: GateCheckResult["decision"];
}

/** The app's pending-approval record (the gating truth the harness mirrors). */
interface PendingApproval {
  tenantId: string;
  sessionId: string;
  action: string;
  argsHash: string;
  approved: boolean;
  consumed: boolean;
}

/** Stable hash of the approved args — confirmation_id itself is never bound. */
function hashArgs(args: Record<string, unknown>): string {
  const stable = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(stable);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>)
          .filter(([k]) => k !== "confirmation_id")
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([k, val]) => [k, stable(val)]),
      );
    }
    return v;
  };
  return JSON.stringify(stable(args));
}

export class FakeGateClient implements GateClient {
  readonly checks: RecordedGateCheck[] = [];
  /** action name or run-tool target -> deny reason. */
  private readonly denials = new Map<string, string>();
  /** confirmation_id -> the app-side pending record it was minted with. */
  private readonly pending = new Map<string, PendingApproval>();
  killSwitch = false;
  /** When set, the next awaiting_approval uses this id (lets tests pin ids). */
  nextConfirmationId: string | null = null;

  deny(actionOrTarget: string, reason: string): void {
    this.denials.set(actionOrTarget, reason);
  }

  /** Simulate the app approving a pending confirmation (section 7 step 3a). */
  grant(confirmationId: string): void {
    const rec = this.pending.get(confirmationId);
    if (!rec) {
      // The app can only approve what its gate minted; granting anything else
      // would let a test assert a path the real system cannot reach.
      throw new Error(`no pending approval to grant: ${confirmationId}`);
    }
    rec.approved = true;
  }

  async check(
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): Promise<GateCheckResult> {
    const result = this.decide(action, args, ctx);
    this.checks.push({ action, args, ctx, decision: result.decision });
    return result;
  }

  private decide(
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): GateCheckResult {
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
    if (
      action === "run_read_tool" ||
      action === "run_write_tool" ||
      action === "submit_live_solve" ||
      action === "author_tool" ||
      action === "customize_platform"
    ) {
      const target = String(args.tool ?? "");
      const targetDenial = this.denials.get(target);
      if (targetDenial) {
        return { decision: "deny", reason: targetDenial, policy: "always_deny", rung: "R2" };
      }
      if (action !== "run_read_tool") {
        const rung =
          action === "submit_live_solve"
            ? "R4"
            : action === "author_tool"
              ? "R5"
              : action === "customize_platform"
                ? "R7"
                : "R3";
        const cid = typeof args.confirmation_id === "string" ? args.confirmation_id : null;
        if (cid) {
          const denial = this.consume(cid, action, args, ctx);
          return denial ?? { decision: "allow", policy: "confirm_once", rung };
        }
        const confirmationId = this.nextConfirmationId ?? randomUUID();
        this.nextConfirmationId = null;
        this.pending.set(confirmationId, {
          tenantId: ctx.tenantId,
          sessionId: ctx.sessionId,
          action,
          argsHash: hashArgs(args),
          approved: false,
          consumed: false,
        });
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

  /**
   * An approval is valid ONLY for the tenant + session + action + args it was
   * minted against, and only once. Returns the deny verdict when any of those
   * fail; on success the record is consumed and null is returned.
   */
  private consume(
    confirmationId: string,
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): GateCheckResult | null {
    const rung =
      action === "submit_live_solve" ? "R4" : action === "author_tool" ? "R5" : "R3";
    const deny = (reason: string): GateCheckResult => ({
      decision: "deny",
      reason,
      policy: "confirm_once",
      rung,
    });
    const rec = this.pending.get(confirmationId);
    if (!rec) return deny("unknown_confirmation");
    if (rec.tenantId !== ctx.tenantId || rec.sessionId !== ctx.sessionId) {
      // Same answer either way: a foreign approval is simply not an approval.
      return deny("confirmation_not_bound_to_session");
    }
    if (rec.action !== action) return deny("action_mismatch");
    if (!rec.approved) return deny("confirmation_not_approved");
    if (rec.consumed) return deny("confirmation_already_consumed");
    if (rec.argsHash !== hashArgs(args)) return deny("args_mismatch");
    rec.consumed = true;
    return null;
  }
}
