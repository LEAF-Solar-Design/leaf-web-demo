/**
 * Capability approval: mint a pending approval on denial, and redeem it exactly
 * once before any action runs.
 *
 * Mandate gates 4 and 7:
 *   4. A denied capability performs no action and creates an approval request.
 *   7. An expired or stale approval performs no action.
 *
 * The hazard this closes is that an approval is only as good as the record behind
 * it. A caller that hands back `{ approved: true, proposal }` is asserting its own
 * authority; without consulting the stored `ConfirmationRecord` an expired, denied,
 * replayed, foreign or argument-swapped approval would execute. So redemption is a
 * STORE operation, never a parameter.
 *
 * DECIDING and SPENDING are separate operations here, and that separation is the whole
 * design. An earlier version fused them: one call both supplied the answer and consumed
 * the record, which left only two possible wirings and both were broken. If the executing
 * request carried the answer, it approved itself and the denial was a speed bump. If an
 * operator route answered first, every later attempt found a non-pending record and no
 * approved action could ever run.
 *
 * Two rules make the binding real:
 *   - Spending is one-shot, enforced by the STORE in a single atomic transition, not by
 *     a read followed by a write here. A read-then-write cannot be made safe at this
 *     layer: two callers can both pass the read before either writes.
 *   - The record is bound to its exact arguments. An approval for "delete a.txt" must
 *     not authorise "delete b.txt", so the submitted args are compared against the
 *     args the approval was minted for, canonicalised so key order cannot smuggle a
 *     mismatch past a string compare.
 */

import { randomUUID } from "node:crypto";
import type { ConfirmationActor, ConfirmationRecord, SessionStore } from "../index.js";

/** Default approval lifetime. An approval that never expires is a standing grant. */
export const DEFAULT_APPROVAL_TTL_S = 900;

/**
 * Stable JSON for the args-exact binding: object keys sorted at every depth, so
 * `{a,b}` and `{b,a}` produce one string. Without this the binding rejects a
 * semantically identical re-invocation, which trains callers to bypass it.
 * Arrays keep their order, because order is meaning there.
 */
export function canonicalArgsJson(value: unknown): string {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (node && typeof node === "object") {
      const source = node as Record<string, unknown>;
      const out: Record<string, unknown> = {};
      for (const key of Object.keys(source).sort()) out[key] = walk(source[key]);
      return out;
    }
    return node;
  };
  return JSON.stringify(walk(value) ?? null);
}

export interface MintApprovalInput {
  sessionId: string;
  turnId: string;
  /** The gated action, e.g. a spine tool or capability name. */
  action: string;
  /** The EXACT args this approval will cover. Nothing else is authorised. */
  args: Record<string, unknown>;
  kind: string;
  ttlS?: number;
  /** Injected for tests; production passes nothing and uses the wall clock. */
  now?: () => number;
  /** Injected for tests; production passes nothing and uses randomUUID. */
  newId?: () => string;
}

/**
 * Gate 4: record the approval request that a denial creates.
 *
 * The caller must have performed NO action before calling this. Minting is what
 * makes a denial answerable instead of a dead end.
 */
export async function mintApprovalRequest(
  store: SessionStore,
  input: MintApprovalInput,
): Promise<ConfirmationRecord> {
  const now = (input.now ?? Date.now)();
  const ttlS = input.ttlS ?? DEFAULT_APPROVAL_TTL_S;
  const record: ConfirmationRecord = {
    confirmation_id: (input.newId ?? randomUUID)(),
    session_id: input.sessionId,
    turn_id: input.turnId,
    action: input.action,
    args_json: canonicalArgsJson(input.args),
    kind: input.kind,
    status: "pending",
    created_at: new Date(now).toISOString(),
    expires_at: new Date(now + ttlS * 1000).toISOString(),
    decided_at: null,
    decided_by: null,
  };
  await store.putConfirmation(record);
  return record;
}

/**
 * THE DECISION CHANNEL. An operator (or whatever the application trusts to speak for
 * one) answers a pending approval. This performs no action and authorises nothing by
 * itself; it only records the answer.
 *
 * Returns the decided record when THIS call decided it, and null otherwise: unknown id,
 * already answered, or expired. Null is not an error, it means somebody or something
 * else got there first, which is exactly what a caller must not paper over.
 *
 * Deliberately separate from spending. Fusing the two is what let a request assert its
 * own approval: if the executor supplies the decision, the denial is theatre.
 */
export async function decideApproval(
  store: SessionStore,
  input: {
    confirmationId: string;
    decision: "approved" | "denied";
    decidedBy: ConfirmationActor;
  },
): Promise<ConfirmationRecord | null> {
  return store.decideConfirmation(input.confirmationId, input.decision, input.decidedBy);
}

/** Why spending was refused. Every value means NO ACTION MAY BE TAKEN. */
export type SpendRefusal = "not_spendable";

export type ApprovalSpend =
  | { ok: true; record: ConfirmationRecord }
  | { ok: false; reason: SpendRefusal };

export interface SpendApprovalInput {
  confirmationId: string;
  /** The session presenting the approval. */
  sessionId: string;
  /** The action about to run. */
  action: string;
  /** The args about to run; canonicalised before comparison. */
  args: Record<string, unknown>;
  /** Recorded as the component that spent it. */
  consumedBy: ConfirmationActor;
}

/**
 * SPEND an approval, exactly once, immediately before performing the action.
 *
 * The store does the deciding, in one atomic step that checks state, expiry, session,
 * action and arguments together. This function only canonicalises the arguments and
 * translates the outcome, because any check performed HERE would be a read the store
 * could not enforce.
 *
 * One refusal reason on purpose. A caller cannot act differently on "expired" than on
 * "wrong arguments" (both mean stop), while a detailed reason handed back over the wire
 * tells a prober which part of a forged presentation to fix next.
 */
export async function spendApproval(
  store: SessionStore,
  input: SpendApprovalInput,
): Promise<ApprovalSpend> {
  const record = await store.consumeApproved(
    input.confirmationId,
    {
      sessionId: input.sessionId,
      action: input.action,
      argsJson: canonicalArgsJson(input.args),
    },
    input.consumedBy,
  );
  return record ? { ok: true, record } : { ok: false, reason: "not_spendable" };
}
