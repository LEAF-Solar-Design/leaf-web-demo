import { describe, expect, it } from "vitest";
import {
  IntegrationManualWorkflowStepV1, IntegrationManualWorkflowDefinitionV1,
  IntegrationManualWorkflowSubmissionV1, IntegrationManualWorkflowStepSnapshotV1,
  IntegrationManualWorkflowRunV1, IntegrationManualWorkflowSnapshotV1,
  IntegrationManualWorkflowSubmissionResultV1, IntegrationActionSnapshotV1,
  emitJsonSchema, emitOpenApi,
} from "../src/index";

const ref = { id: "orders", version: 1, digest: "ab".repeat(32) };
const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const instant = "2026-10-20T12:00:00Z";
const step: IntegrationManualWorkflowStepV1 = {
  id: "create", kind: "action", connection_id: "orders-account",
  connector_ref: ref, operation_ref: ref, arguments: { amount: 0, enabled: false, note: "", optional: null },
};
const definition: IntegrationManualWorkflowDefinitionV1 = {
  schema_version: 1, id: ref.id, version: ref.version, title: "Create orders",
  trigger: "manual", authority_mode: "caller_bound", steps: [step],
};
const submission: IntegrationManualWorkflowSubmissionV1 = {
  schema_version: 1, workflow_ref: ref, idempotency_key: "request_12345678",
  step_grants: [{ step_id: step.id, expected_grant_revision: 1 }],
};
const run: IntegrationManualWorkflowRunV1 = {
  schema_version: 1, run_id: uuid, revision: 1, workflow_ref: ref,
  submission_digest: ref.digest, authority_mode: "caller_bound", authority_binding_digest: ref.digest,
  created_at: instant, updated_at: instant, state: "accepted",
};
const pending: IntegrationManualWorkflowStepSnapshotV1 = { run_id: uuid, step_id: step.id, state: "pending" };
const blocked: IntegrationManualWorkflowStepSnapshotV1 = {
  run_id: uuid, step_id: step.id, state: "blocked", failure_code: "forbidden", effect_outcome: "not_dispatched",
};
type ChildState = IntegrationActionSnapshotV1["run"]["state"];
function child(state: ChildState, index: number): IntegrationActionSnapshotV1 {
  const effect_outcome = state === "accepted" ? "not_dispatched" : state === "succeeded" ? "applied" :
    state === "failed" ? "not_applied" : "unknown";
  const run_id = `7b92f224-18f2-4939-b368-${index.toString().padStart(12, "0")}`;
  return {
    run: {
      schema_version: 1, run_id, revision: 1, connection_id: step.connection_id, connector_ref: ref,
      operation_ref: ref, submission_digest: ref.digest, authority_binding_digest: ref.digest,
      grant_revision: 1, created_at: instant, updated_at: instant, state, effect_outcome,
    },
    receipts: state === "accepted" || state === "running" ? [] : [{
      schema_version: 1, receipt_id: run_id, run_id, sequence: 1, submission_digest: ref.digest,
      operation_ref: ref, observed_at: instant, effect_outcome,
    }],
  };
}
type StepState = ChildState | "pending" | "blocked";
function snapshot(state: IntegrationManualWorkflowRunV1["state"], states: StepState[]): IntegrationManualWorkflowSnapshotV1 {
  return {
    definition: { ...definition, steps: states.map((_, i) => ({ ...step, id: `step-${i}` })) },
    run: { ...run, state },
    steps: states.map((value, i) => {
      const identity = { run_id: uuid, step_id: `step-${i}` };
      return value === "pending" ? { ...identity, state: "pending" } : value === "blocked" ?
        { ...blocked, ...identity } : { ...identity, state: "action", action: child(value, i) };
    }),
  };
}

describe("finite caller-bound manual workflow contracts", () => {
  it("round trips all seven schemas and each strict variant", () => {
    for (const [schema, sample] of [
      [IntegrationManualWorkflowStepV1, step], [IntegrationManualWorkflowDefinitionV1, definition],
      [IntegrationManualWorkflowSubmissionV1, submission], [IntegrationManualWorkflowRunV1, run],
      [IntegrationManualWorkflowStepSnapshotV1, pending], [IntegrationManualWorkflowStepSnapshotV1, blocked],
      [IntegrationManualWorkflowStepSnapshotV1, { ...pending, state: "action", action: child("accepted", 0) }],
      [IntegrationManualWorkflowSnapshotV1, snapshot("accepted", ["pending"])],
      [IntegrationManualWorkflowSubmissionResultV1, { status: "admitted", run, replayed: false }],
      [IntegrationManualWorkflowSubmissionResultV1, { status: "admitted", run, replayed: true }],
      [IntegrationManualWorkflowSubmissionResultV1, { status: "refused", code: "idempotency_conflict" }],
    ] as const) expect(schema.parse(JSON.parse(JSON.stringify(sample)))).toEqual(sample);
  });

  it("bounds ordered steps and grants to 1 through 8 with unique identities", () => {
    for (const count of [0, 1, 8, 9]) {
      const steps = Array.from({ length: count }, (_, i) => ({ ...step, id: `step-${i}` }));
      const step_grants = steps.map((entry) => ({ step_id: entry.id, expected_grant_revision: 1 }));
      const valid = count === 1 || count === 8;
      expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, steps }).success).toBe(valid);
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, step_grants }).success).toBe(valid);
      expect(IntegrationManualWorkflowSnapshotV1.safeParse(snapshot("accepted", steps.map(() => "pending"))).success).toBe(valid);
    }
    expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, steps: [step, step] }).success).toBe(false);
    expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, step_grants: [submission.step_grants[0], submission.step_grants[0]] }).success).toBe(false);
    // Grant order against the resolved definition is a host check, not a submission-only refinement.
    expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, step_grants: [
      { step_id: "second", expected_grant_revision: 1 }, { step_id: "first", expected_grant_revision: 1 },
    ] }).success).toBe(true);
  });

  it("reuses bounded literal scalar arguments without interpreting business names", () => {
    for (const args of [{}, step.arguments, { authority: true, credential: "business data" },
      { value: "a".repeat(4096) }, Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`field_${i}`, i]))]) {
      expect(IntegrationManualWorkflowStepV1.parse({ ...step, arguments: args }).arguments).toEqual(args);
    }
    for (const args of [null, [], { value: {} }, { value: [] }, { value: undefined }, { value: Infinity },
      { value: NaN }, { value: "a".repeat(4097) }, { "bad-key": true }, { "name\n": true },
      Object.fromEntries(Array.from({ length: 101 }, (_, i) => [`field_${i}`, i])),
      ...["__proto__", "prototype", "constructor"].map((key) => JSON.parse(`{"${key}":1}`))]) {
      expect(IntegrationManualWorkflowStepV1.safeParse({ ...step, arguments: args }).success).toBe(false);
    }
  });

  it("rejects authority injection, overrides, transport and extra fields at every boundary", () => {
    for (const [schema, sample] of [
      [IntegrationManualWorkflowStepV1, step], [IntegrationManualWorkflowDefinitionV1, definition],
      [IntegrationManualWorkflowSubmissionV1, submission], [IntegrationManualWorkflowRunV1, run],
      [IntegrationManualWorkflowStepSnapshotV1, pending], [IntegrationManualWorkflowStepSnapshotV1, blocked],
      [IntegrationManualWorkflowStepSnapshotV1, { ...pending, state: "action", action: child("accepted", 0) }],
      [IntegrationManualWorkflowSnapshotV1, snapshot("accepted", ["pending"])],
    ] as const) {
      for (const key of ["actor", "tenant", "role", "approval", "authority", "credentials", "headers", "url",
        "expression", "output_ref", "credential_binding_mode", "child_idempotency_key", "raw_result", "extra"]) {
        expect(schema.safeParse({ ...sample, [key]: true }).success).toBe(false);
      }
      for (const key of Object.keys(sample)) {
        const incomplete: Record<string, unknown> = { ...sample };
        delete incomplete[key];
        expect(schema.safeParse(incomplete).success).toBe(false);
      }
    }
    for (const patch of [{ arguments: {} }, { authority_binding_digest: ref.digest }, { idempotency_keys: [] }]) {
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, ...patch }).success).toBe(false);
    }
    expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, step_grants: [
      { ...submission.step_grants[0], arguments: {} },
    ] }).success).toBe(false);
    for (const patch of [{ trigger: "schedule" }, { authority_mode: "standing" }, { digest: ref.digest }]) {
      expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, ...patch }).success).toBe(false);
    }
    expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, effect_outcome: "applied" }).success).toBe(false);
  });

  it("reuses key, UUID, digest, slug, safe revision and fractional time conventions", () => {
    for (const idempotency_key of ["a".repeat(16), "a".repeat(128)]) {
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, idempotency_key }).success).toBe(true);
    }
    for (const idempotency_key of ["a".repeat(15), "a".repeat(129), "request_12345678\n", "request/12345678"]) {
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, idempotency_key }).success).toBe(false);
    }
    for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, "1"]) {
      expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, revision: value }).success).toBe(false);
      expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, version: value }).success).toBe(false);
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, step_grants: [{ step_id: step.id, expected_grant_revision: value }] }).success).toBe(false);
    }
    expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, revision: Number.MAX_SAFE_INTEGER }).success).toBe(true);
    for (const value of ["bad", `${uuid}\n`]) {
      expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, run_id: value }).success).toBe(false);
      expect(IntegrationManualWorkflowStepSnapshotV1.safeParse({ ...pending, run_id: value }).success).toBe(false);
    }
    for (const key of ["submission_digest", "authority_binding_digest"]) for (const value of ["a".repeat(63), "AB".repeat(32), `${ref.digest}\n`]) {
      expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, [key]: value }).success).toBe(false);
    }
    for (const badRef of [{ ...ref, id: "orders\n" }, { ...ref, version: 0 }, { ...ref, digest: "invalid" }, { ...ref, authority: true }]) {
      expect(IntegrationManualWorkflowSubmissionV1.safeParse({ ...submission, workflow_ref: badRef }).success).toBe(false);
      expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, workflow_ref: badRef }).success).toBe(false);
      expect(IntegrationManualWorkflowStepV1.safeParse({ ...step, connector_ref: badRef }).success).toBe(false);
      expect(IntegrationManualWorkflowStepV1.safeParse({ ...step, operation_ref: badRef }).success).toBe(false);
    }
    for (const value of ["", "Orders", "orders\n"]) {
      expect(IntegrationManualWorkflowStepV1.safeParse({ ...step, id: value }).success).toBe(false);
      expect(IntegrationManualWorkflowStepV1.safeParse({ ...step, connection_id: value }).success).toBe(false);
    }
    for (const value of ["2026-02-29T12:00:00Z", "2026-04-31T12:00:00Z", "2026-10-20T24:00:00Z", `${instant}\n`]) {
      for (const key of ["created_at", "updated_at"]) expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, [key]: value }).success).toBe(false);
    }
    for (const [created_at, updated_at, valid] of [
      [instant, instant, true], [instant, "2026-10-20T11:59:59Z", false],
      ["2026-10-20T12:00:00.000002Z", "2026-10-20T12:00:00.000001Z", false],
      ["2026-10-20T12:00:00.1Z", "2026-10-20T12:00:00.1000Z", true],
    ] as const) expect(IntegrationManualWorkflowRunV1.safeParse({ ...run, created_at, updated_at }).success).toBe(valid);
  });

  it("keeps pending, blocked and child evidence distinct", () => {
    for (const failure_code of ["invalid_input", "forbidden", "revoked", "conflict", "quota", "timeout", "unavailable", "invalid_response"]) {
      expect(IntegrationManualWorkflowStepSnapshotV1.safeParse({ ...blocked, failure_code }).success).toBe(true);
    }
    for (const sample of [
      { ...blocked, failure_code: "outcome_unknown" }, { ...blocked, effect_outcome: "unknown" },
      { ...pending, effect_outcome: "not_dispatched" }, { ...pending, action: child("accepted", 0) },
      { ...blocked, action: child("accepted", 0) }, { ...pending, state: "action" },
      { ...pending, state: "action", action: { ...child("succeeded", 0), receipts: [] } },
    ]) expect(IntegrationManualWorkflowStepSnapshotV1.safeParse(sample).success).toBe(false);
  });

  it("checks every parent state against the prefix and frontier, including transient settlement", () => {
    const cases: Array<[StepState[], string[]]> = [
      [["pending", "pending"], ["accepted", "running"]],
      [["succeeded", "pending"], ["running"]],
      [["succeeded", "accepted", "pending"], ["running"]],
      [["succeeded", "running", "pending"], ["running"]],
      [["succeeded", "failed", "pending"], ["running", "failed"]],
      [["succeeded", "uncertain", "pending"], ["running", "uncertain"]],
      [["succeeded", "blocked", "pending"], ["running", "failed"]],
      [["succeeded", "succeeded"], ["running", "succeeded"]],
      [["blocked", "succeeded"], []], [["failed", "accepted"], []],
      [["uncertain", "running"], []], [["pending", "succeeded"], []],
      [["running", "blocked"], []], [["blocked", "blocked"], []],
    ];
    for (const [states, allowed] of cases) for (const state of ["accepted", "running", "succeeded", "failed", "uncertain"] as const) {
      expect(IntegrationManualWorkflowSnapshotV1.safeParse(snapshot(state, states)).success, `${state}: ${states.join(",")}`).toBe(allowed.includes(state));
    }
  });

  it("matches exact definition order, parent identity, unique children and child references", () => {
    const sample = snapshot("running", ["succeeded", "accepted"]);
    for (const steps of [sample.steps.slice(1), [...sample.steps].reverse(), [sample.steps[0], sample.steps[0]],
      [{ ...sample.steps[0], step_id: "other" }, sample.steps[1]],
      [{ ...sample.steps[0], run_id: "7b92f224-18f2-4939-b368-c199cc1cc291" }, sample.steps[1]]]) {
      expect(IntegrationManualWorkflowSnapshotV1.safeParse({ ...sample, steps }).success).toBe(false);
    }
    for (const patch of [{ id: "other" }, { version: 2 }]) {
      expect(IntegrationManualWorkflowSnapshotV1.safeParse({ ...sample, definition: { ...sample.definition, ...patch } }).success).toBe(false);
    }
    const action = child("accepted", 1);
    for (const patch of [
      { connection_id: "other" }, { run_id: child("succeeded", 0).run.run_id },
      ...["connector_ref", "operation_ref"].flatMap((key) => [
        { [key]: { ...ref, id: "other" } }, { [key]: { ...ref, version: 2 } }, { [key]: { ...ref, digest: "cd".repeat(32) } },
      ]),
    ]) {
      const steps = [sample.steps[0], { ...pending, step_id: "step-1", state: "action", action: { ...action, run: { ...action.run, ...patch } } }];
      expect(IntegrationManualWorkflowSnapshotV1.safeParse({ ...sample, steps }).success).toBe(false);
    }
  });

  it("keeps conflict responses minimal and nondisclosing", () => {
    const conflict = { status: "refused", code: "idempotency_conflict" };
    for (const extra of [{ run }, { replayed: true }, { workflow_ref: ref }, { submission }, { actor: uuid }, { submission_digest: ref.digest }]) {
      expect(IntegrationManualWorkflowSubmissionResultV1.safeParse({ ...conflict, ...extra }).success).toBe(false);
    }
    for (const value of [{ ...conflict, code: "forbidden" }, { status: "admitted", run },
      { status: "admitted", run, replayed: "true" }, { status: "admitted", run, replayed: false, code: "idempotency_conflict" }]) {
      expect(IntegrationManualWorkflowSubmissionResultV1.safeParse(value).success).toBe(false);
    }
  });

  it("emits seven canonical definitions, bounds and references in both existing producers", () => {
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    for (const [defs, prefix] of [[json.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"]] as const) {
      for (const name of ["IntegrationManualWorkflowStepV1", "IntegrationManualWorkflowDefinitionV1", "IntegrationManualWorkflowSubmissionV1", "IntegrationManualWorkflowRunV1", "IntegrationManualWorkflowSnapshotV1"]) {
        expect(defs[name].additionalProperties).toBe(false);
      }
      expect(defs.IntegrationManualWorkflowDefinitionV1.properties.steps).toMatchObject({ minItems: 1, maxItems: 8, items: { $ref: `${prefix}IntegrationManualWorkflowStepV1` } });
      expect(defs.IntegrationManualWorkflowSubmissionV1.properties.step_grants).toMatchObject({ minItems: 1, maxItems: 8, items: { additionalProperties: false } });
      expect(defs.IntegrationManualWorkflowSnapshotV1.properties.steps).toMatchObject({ minItems: 1, maxItems: 8, items: { $ref: `${prefix}IntegrationManualWorkflowStepSnapshotV1` } });
      for (const [name, key, target] of [
        ["IntegrationManualWorkflowSnapshotV1", "definition", "IntegrationManualWorkflowDefinitionV1"],
        ["IntegrationManualWorkflowSnapshotV1", "run", "IntegrationManualWorkflowRunV1"],
        ["IntegrationManualWorkflowSubmissionV1", "workflow_ref", "IntegrationRefV1"],
        ["IntegrationManualWorkflowRunV1", "workflow_ref", "IntegrationRefV1"],
        ["IntegrationManualWorkflowStepV1", "connector_ref", "IntegrationRefV1"],
        ["IntegrationManualWorkflowStepV1", "operation_ref", "IntegrationRefV1"],
      ]) expect(defs[name].properties[key]).toEqual({ $ref: `${prefix}${target}` });
      expect(defs.IntegrationManualWorkflowStepV1.properties.arguments).toEqual(defs.IntegrationActionSubmissionV1.properties.arguments);
      const branches = defs.IntegrationManualWorkflowStepSnapshotV1.anyOf;
      expect(branches).toHaveLength(3);
      for (const branch of branches) expect(branch.additionalProperties).toBe(false);
      expect(Object.keys(branches[0].properties).sort()).toEqual(["run_id", "state", "step_id"]);
      expect(branches[1].properties.failure_code.enum).not.toContain("outcome_unknown");
      expect(branches[2].properties.action).toEqual({ $ref: `${prefix}IntegrationActionSnapshotV1` });
      const results = defs.IntegrationManualWorkflowSubmissionResultV1.anyOf;
      expect(results).toHaveLength(2);
      expect(results[0].properties.run).toEqual({ $ref: `${prefix}IntegrationManualWorkflowRunV1` });
      for (const branch of results) expect(branch.additionalProperties).toBe(false);
      expect(Object.keys(results[1].properties).sort()).toEqual(["code", "status"]);
      // Cross-object order, state, identity and hash obligations are not JSON Schema proofs.
      expect(defs.IntegrationManualWorkflowSnapshotV1.description).toContain("runtime refinements");
      expect(defs.IntegrationManualWorkflowSnapshotV1.description).toContain("host verifies");
      expect(defs.IntegrationManualWorkflowSubmissionV1.description).toContain("rechecks membership");
      expect(defs.IntegrationManualWorkflowDefinitionV1.description).toContain("never authorizes execution");
    }
    expect(Object.keys(openapi.paths).sort()).toEqual([
      "/cards/{card_id}/approve", "/proposals/{proposal_id}", "/sessions/{session_id}/events",
      "/sessions/{session_id}/thread", "/sessions/{session_id}/turns", "/worker/jobs/{job_id}/events",
    ]);
  });
});
