import { describe, expect, it } from "vitest";
import {
  IntegrationActionSnapshotV1, IntegrationManualWorkflowDefinitionV1,
  IntegrationManualWorkflowRunV1, IntegrationManualWorkflowSnapshotV1,
  IntegrationManualWorkflowRunV2, IntegrationManualWorkflowSnapshotV2,
  IntegrationManualWorkflowCancellationRequestV1, IntegrationManualWorkflowCancellationResultV1,
  emitJsonSchema, emitOpenApi,
} from "../src/index";

const ref = { id: "orders", version: 1, digest: "ab".repeat(32) };
const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const instant = "2026-10-20T12:00:00Z";
const run: IntegrationManualWorkflowRunV2 = {
  schema_version: 2, run_id: uuid, revision: 1, workflow_ref: ref,
  submission_digest: ref.digest, authority_mode: "caller_bound", authority_binding_digest: ref.digest,
  created_at: instant, updated_at: instant, state: "cancel_requested", requested_at: instant,
};
const request: IntegrationManualWorkflowCancellationRequestV1 = {
  schema_version: 1, run_id: uuid, expected_revision: 1,
};
type ChildState = IntegrationActionSnapshotV1["run"]["state"];
type StepState = ChildState | "pending" | "blocked";
function child(state: ChildState, index: number): IntegrationActionSnapshotV1 {
  const effect_outcome = state === "accepted" ? "not_dispatched" : state === "succeeded" ? "applied" :
    state === "failed" ? "not_applied" : "unknown";
  const run_id = `7b92f224-18f2-4939-b368-${index.toString().padStart(12, "0")}`;
  return {
    run: {
      schema_version: 1, run_id, revision: 1, connection_id: "orders-account", connector_ref: ref,
      operation_ref: ref, submission_digest: ref.digest, authority_binding_digest: ref.digest,
      grant_revision: 1, created_at: instant, updated_at: instant, state, effect_outcome,
    },
    receipts: state === "accepted" || state === "running" ? [] : [{
      schema_version: 1, receipt_id: run_id, run_id, sequence: 1, submission_digest: ref.digest,
      operation_ref: ref, observed_at: instant, effect_outcome,
    }],
  };
}
function snapshot(state: IntegrationManualWorkflowRunV2["state"], states: StepState[]): IntegrationManualWorkflowSnapshotV2 {
  const definition: IntegrationManualWorkflowDefinitionV1 = {
    schema_version: 1, id: ref.id, version: 1, title: "Create orders", trigger: "manual", authority_mode: "caller_bound",
    steps: states.map((_, i) => ({
      id: `step-${i}`, kind: "action", connection_id: "orders-account", connector_ref: ref,
      operation_ref: ref, arguments: { amount: 0, enabled: false, note: "", optional: null },
    })),
  };
  const { requested_at, ...ordinary } = run;
  return {
    definition,
    run: state === "cancel_requested" || state === "cancelled" ? { ...run, state } : { ...ordinary, state },
    steps: states.map((value, i) => {
      const identity = { run_id: uuid, step_id: `step-${i}` };
      return value === "pending" ? { ...identity, state: "pending" } : value === "blocked" ?
        { ...identity, state: "blocked", failure_code: "forbidden", effect_outcome: "not_dispatched" } :
        { ...identity, state: "action", action: child(value, i) };
    }),
  };
}

describe("opt-in manual workflow cancellation contracts", () => {
  it("round trips the four exports with inferred types and keeps V1 closed", () => {
    const sample = snapshot("cancel_requested", ["accepted", "pending"]);
    const result: IntegrationManualWorkflowCancellationResultV1 = { status: "recorded", snapshot: sample };
    expect(IntegrationManualWorkflowRunV2.parse(run)).toEqual(run);
    expect(IntegrationManualWorkflowSnapshotV2.parse(sample)).toEqual(sample);
    expect(IntegrationManualWorkflowCancellationRequestV1.parse(request)).toEqual(request);
    expect(IntegrationManualWorkflowCancellationResultV1.parse(result)).toEqual(result);
    for (const state of ["accepted", "running", "succeeded", "failed", "uncertain"] as const) {
      const states: StepState[] = state === "accepted" ? ["pending"] : state === "succeeded" ? ["succeeded"] :
        state === "failed" ? ["failed"] : state === "uncertain" ? ["uncertain"] : ["running"];
      const v2 = snapshot(state, states);
      const v1 = { ...v2, run: { ...v2.run, schema_version: 1 } };
      expect(IntegrationManualWorkflowSnapshotV2.parse(v2)).toEqual(v2);
      expect(IntegrationManualWorkflowSnapshotV1.parse(v1)).toEqual(v1);
      expect(IntegrationManualWorkflowRunV1.safeParse(v2.run).success).toBe(false);
      expect(IntegrationManualWorkflowSnapshotV1.safeParse(v2).success).toBe(false);
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...v2.run, requested_at: instant }).success).toBe(false);
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...v2.run, requested_at: undefined }).success).toBe(false);
    }
    for (const state of ["cancel_requested", "cancelled"] as const) {
      const sample = snapshot(state, ["pending"]);
      expect(IntegrationManualWorkflowRunV1.safeParse({ ...sample.run, schema_version: 1 }).success).toBe(false);
      expect(IntegrationManualWorkflowSnapshotV1.safeParse({ ...sample, run: { ...sample.run, schema_version: 1 } }).success).toBe(false);
      const { requested_at, ...missing } = sample.run;
      expect(IntegrationManualWorkflowRunV2.safeParse(missing).success).toBe(false);
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...missing, requested_at: undefined }).success).toBe(false);
    }
  });

  it("checks creation, request and update ordering without losing fractional precision", () => {
    for (const [created_at, requested_at, updated_at, valid] of [
      [instant, instant, instant, true],
      [instant, "2026-10-20T12:00:01Z", "2026-10-20T12:00:02Z", true],
      [instant, "2026-10-20T11:59:59Z", instant, false],
      [instant, "2026-10-20T12:00:01Z", instant, false],
      [instant, instant, "2026-10-20T11:59:59Z", false],
      ["2026-10-20T12:00:00.000002Z", "2026-10-20T12:00:00.000001Z", instant, false],
      [instant, "2026-10-20T12:00:00.000002Z", "2026-10-20T12:00:00.000001Z", false],
      [instant, "2026-10-20T12:00:00.1Z", "2026-10-20T12:00:00.1000Z", true],
    ] as const) expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, created_at, requested_at, updated_at }).success).toBe(valid);
    for (const requested_at of [null, "", "2026-02-29T12:00:00Z", "2026-04-31T12:00:00Z", "2026-10-20T24:00:00Z", `${instant}\n`]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, requested_at }).success).toBe(false);
    }
  });

  it("retains strict boundaries and scalar constraints without adding capabilities", () => {
    const result = { status: "recorded", snapshot: snapshot("cancelled", ["pending"]) };
    for (const [schema, sample] of [
      [IntegrationManualWorkflowRunV2, run], [IntegrationManualWorkflowSnapshotV2, result.snapshot],
      [IntegrationManualWorkflowCancellationRequestV1, request], [IntegrationManualWorkflowCancellationResultV1, result],
    ] as const) {
      for (const key of ["child_id", "idempotency_key", "grant", "effect_outcome", "reason", "arguments", "token", "caller", "extra"]) {
        expect(schema.safeParse({ ...sample, [key]: true }).success).toBe(false);
      }
      for (const key of Object.keys(sample)) {
        const incomplete: Record<string, unknown> = { ...sample };
        delete incomplete[key];
        expect(schema.safeParse(incomplete).success).toBe(false);
      }
    }
    for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, "1"]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, revision: value }).success).toBe(false);
      expect(IntegrationManualWorkflowCancellationRequestV1.safeParse({ ...request, expected_revision: value }).success).toBe(false);
    }
    for (const value of [1, Number.MAX_SAFE_INTEGER]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, revision: value }).success).toBe(true);
      expect(IntegrationManualWorkflowCancellationRequestV1.safeParse({ ...request, expected_revision: value }).success).toBe(true);
    }
    for (const run_id of ["bad", `${uuid}\n`]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, run_id }).success).toBe(false);
      expect(IntegrationManualWorkflowCancellationRequestV1.safeParse({ ...request, run_id }).success).toBe(false);
    }
    for (const key of ["submission_digest", "authority_binding_digest"]) for (const value of ["ab", "AB".repeat(32), `${ref.digest}\n`]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, [key]: value }).success).toBe(false);
    }
    for (const workflow_ref of [{ ...ref, id: "Orders" }, { ...ref, version: 0 }, { ...ref, digest: "bad" }]) {
      expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, workflow_ref }).success).toBe(false);
    }
    expect(IntegrationManualWorkflowRunV2.safeParse({ ...run, authority_mode: "standing" }).success).toBe(false);
    expect(IntegrationManualWorkflowCancellationRequestV1.safeParse({ ...request, schema_version: 2 }).success).toBe(false);
  });

  it("allows running structures while pending and only known settled effects when cancelled", () => {
    for (const states of [
      ["pending"], ["accepted", "pending"], ["succeeded", "pending"], ["succeeded", "accepted", "pending"],
      ["succeeded", "failed", "pending"], ["succeeded", "blocked", "pending"], ["succeeded", "succeeded"],
    ] satisfies StepState[][]) for (const state of ["cancel_requested", "cancelled"] as const) {
      expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot(state, states)).success).toBe(true);
    }
    for (const frontier of ["running", "uncertain"] as const) {
      expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot("cancel_requested", ["succeeded", frontier, "pending"])).success).toBe(true);
      expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot("cancelled", ["succeeded", frontier, "pending"])).success).toBe(false);
    }
    const undispatched = snapshot("cancelled", ["failed", "pending"]);
    const failed = undispatched.steps[0];
    if (failed.state !== "action") throw new Error("Expected action fixture");
    failed.action.run.effect_outcome = "not_dispatched";
    failed.action.receipts[0].effect_outcome = "not_dispatched";
    expect(IntegrationManualWorkflowSnapshotV2.parse(undispatched)).toEqual(undispatched);
    for (const states of [["pending", "succeeded"], ["failed", "accepted"], ["running", "blocked"], ["blocked", "blocked"]] satisfies StepState[][]) {
      for (const state of ["cancel_requested", "cancelled"] as const) {
        expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot(state, states)).success).toBe(false);
      }
    }
    for (const count of [0, 1, 8, 9]) {
      expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot("cancelled", Array.from({ length: count }, () => "pending"))).success).toBe(count === 1 || count === 8);
    }
    for (const [state, states] of [["accepted", ["accepted"]], ["succeeded", ["pending"]], ["failed", ["uncertain"]], ["uncertain", ["failed"]]] as const) {
      expect(IntegrationManualWorkflowSnapshotV2.safeParse(snapshot(state, [...states])).success).toBe(false);
    }
  });

  it("preserves exact definition, order, parent, child uniqueness and reference checks", () => {
    for (const state of ["cancel_requested", "cancelled"] as const) {
      const sample = snapshot(state, ["succeeded", "accepted"]);
      for (const steps of [sample.steps.slice(1), [...sample.steps].reverse(), [sample.steps[0], sample.steps[0]],
        [{ ...sample.steps[0], step_id: "other" }, sample.steps[1]],
        [{ ...sample.steps[0], run_id: child("accepted", 9).run.run_id }, sample.steps[1]]]) {
        expect(IntegrationManualWorkflowSnapshotV2.safeParse({ ...sample, steps }).success).toBe(false);
      }
      for (const patch of [{ id: "other" }, { version: 2 }]) {
        expect(IntegrationManualWorkflowSnapshotV2.safeParse({ ...sample, definition: { ...sample.definition, ...patch } }).success).toBe(false);
      }
      const action = child("accepted", 1);
      for (const patch of [
        { connection_id: "other" }, { run_id: child("succeeded", 0).run.run_id.toUpperCase() },
        ...["connector_ref", "operation_ref"].flatMap((key) => [
          { [key]: { ...ref, id: "other" } }, { [key]: { ...ref, version: 2 } }, { [key]: { ...ref, digest: "cd".repeat(32) } },
        ]),
      ]) {
        const steps = [sample.steps[0], { ...sample.steps[1], state: "action", action: { ...action, run: { ...action.run, ...patch } } }];
        expect(IntegrationManualWorkflowSnapshotV2.safeParse({ ...sample, steps }).success).toBe(false);
      }
    }
  });

  it("binds result statuses to snapshot states without inventing terminal cancellation evidence", () => {
    for (const status of ["recorded", "already_recorded", "already_terminal"] as const) {
      for (const state of ["accepted", "running", "succeeded", "failed", "uncertain", "cancel_requested", "cancelled"] as const) {
        const states: StepState[] = state === "succeeded" ? ["succeeded"] : state === "failed" ? ["failed"] :
          state === "uncertain" ? ["uncertain"] : ["pending"];
        const sample = { status, snapshot: snapshot(state, states) };
        const allowed = status === "already_terminal" ? state === "succeeded" || state === "failed" :
          state === "cancel_requested" || state === "cancelled";
        expect(IntegrationManualWorkflowCancellationResultV1.safeParse(sample).success).toBe(allowed);
        if (allowed) expect(IntegrationManualWorkflowCancellationResultV1.parse(sample)).toEqual(sample);
      }
    }
    const terminal = snapshot("succeeded", ["succeeded"]);
    expect(IntegrationManualWorkflowCancellationResultV1.safeParse({
      status: "already_terminal", snapshot: { ...terminal, run: { ...terminal.run, requested_at: instant } },
    }).success).toBe(false);
    expect(IntegrationManualWorkflowCancellationResultV1.safeParse({ status: "error", snapshot: terminal }).success).toBe(false);
  });

  it("retains original reconciliation receipts and never mutates the input", () => {
    const sample = snapshot("cancelled", ["succeeded", "failed", "pending"]);
    const frontier = sample.steps[1];
    if (frontier.state !== "action") throw new Error("Expected action fixture");
    frontier.action.receipts.unshift({
      ...frontier.action.receipts[0], receipt_id: uuid, sequence: 1, effect_outcome: "unknown", failure_code: "outcome_unknown",
    });
    frontier.action.receipts[1] = { ...frontier.action.receipts[1], sequence: 2 };
    const before = JSON.parse(JSON.stringify(sample));
    expect(IntegrationManualWorkflowSnapshotV2.parse(sample)).toEqual(before);
    expect(sample).toEqual(before);
    const pending = snapshot("cancel_requested", ["running"]);
    const pendingBefore = JSON.parse(JSON.stringify(pending));
    expect(IntegrationManualWorkflowSnapshotV2.parse(pending)).toEqual(pendingBefore);
    expect(pending).toEqual(pendingBefore);
    pending.run.state = "cancelled";
    const invalidBefore = JSON.parse(JSON.stringify(pending));
    expect(IntegrationManualWorkflowSnapshotV2.safeParse(pending).success).toBe(false);
    expect(pending).toEqual(invalidBefore);
  });

  it("emits four additive named definitions and resolvable references without HTTP routes", () => {
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    const names = ["IntegrationManualWorkflowRunV2", "IntegrationManualWorkflowSnapshotV2",
      "IntegrationManualWorkflowCancellationRequestV1", "IntegrationManualWorkflowCancellationResultV1"];
    for (const [defs, prefix] of [[json.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"]] as const) {
      expect(Object.keys(defs).slice(-4)).toEqual(names);
      for (const name of names) expect(defs[name].additionalProperties).toBe(false);
      expect(defs.IntegrationManualWorkflowRunV2.properties.schema_version.const).toBe(2);
      expect(defs.IntegrationManualWorkflowRunV2.properties.state.enum).toContain("cancelled");
      expect(defs.IntegrationManualWorkflowSnapshotV2.properties.steps).toMatchObject({
        minItems: 1, maxItems: 8, items: { $ref: `${prefix}IntegrationManualWorkflowStepSnapshotV1` },
      });
      for (const [name, key, target] of [
        ["IntegrationManualWorkflowSnapshotV2", "definition", "IntegrationManualWorkflowDefinitionV1"],
        ["IntegrationManualWorkflowSnapshotV2", "run", "IntegrationManualWorkflowRunV2"],
        ["IntegrationManualWorkflowRunV2", "workflow_ref", "IntegrationRefV1"],
        ["IntegrationManualWorkflowCancellationResultV1", "snapshot", "IntegrationManualWorkflowSnapshotV2"],
      ]) expect(defs[name].properties[key]).toEqual({ $ref: `${prefix}${target}` });
      const visit = (value: unknown): void => {
        if (value === null || typeof value !== "object") return;
        for (const [key, nested] of Object.entries(value)) {
          if (key === "$ref") {
            expect(typeof nested).toBe("string");
            expect(String(nested).startsWith(prefix)).toBe(true);
            expect(defs[String(nested).slice(prefix.length)]).toBeDefined();
          } else visit(nested);
        }
      };
      for (const name of names) visit(defs[name]);
    }
    expect(Object.keys(openapi.paths).sort()).toEqual([
      "/cards/{card_id}/approve", "/proposals/{proposal_id}", "/sessions/{session_id}/events",
      "/sessions/{session_id}/thread", "/sessions/{session_id}/turns", "/worker/jobs/{job_id}/events",
    ]);
  });
});
