import { describe, expect, it } from "vitest";
import {
  IntegrationActionSubmissionV1, IntegrationActionRunV1,
  IntegrationActionReceiptV1, IntegrationActionSubmissionResultV1,
  ConnectorDefinitionV1, OperationV1,
  emitJsonSchema, emitOpenApi,
} from "../src/index";

const ref = { id: "sales", version: 1, digest: "ab".repeat(32) };
const operation: OperationV1 = {
  schema_version: 1, id: "create-sale", title: "Create sale", method: "POST", path: "/v1/sales",
  effect: "write", pagination: { kind: "none", records_pointer: "/data" },
  limits: { timeout_ms: 1000, max_response_bytes: 1024, max_records: 100, max_pages: 1 },
  idempotency: { kind: "provider_key", header: "Idempotency-Key" },
};
const connector: ConnectorDefinitionV1 = {
  schema_version: 1, id: "sales", version: 1, title: "Sales", state: "published", protocol: "https-rest",
  origins: ["https://api.example.com"], auth: { kind: "bearer" }, operations: [operation],
};
const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const instant = "2026-10-20T12:00:00Z";
const submission: IntegrationActionSubmissionV1 = {
  schema_version: 1, connection_id: "sales-account", connector_ref: ref, operation_ref: ref,
  idempotency_key: "request_12345678", expected_grant_revision: 1,
  arguments: { name: "Sale", amount: 1.25, enabled: true, optional: null },
};
const run: IntegrationActionRunV1 = {
  schema_version: 1, run_id: uuid, revision: 1, connection_id: "sales-account",
  connector_ref: ref, operation_ref: ref, submission_digest: ref.digest,
  authority_binding_digest: ref.digest, grant_revision: 1, created_at: instant,
  updated_at: instant, state: "accepted", effect_outcome: "not_dispatched",
};
const receipt: IntegrationActionReceiptV1 = {
  schema_version: 1, receipt_id: uuid, run_id: uuid, sequence: 1,
  submission_digest: ref.digest, operation_ref: ref, observed_at: instant, effect_outcome: "unknown",
};
const reconciliation = { operation_ref: ref, observation_receipt_id: uuid };
const outcomes = ["not_dispatched", "applied", "not_applied", "unknown"] as const;
const allowed: Record<IntegrationActionRunV1["state"], readonly string[]> = {
  accepted: ["not_dispatched"], running: ["unknown"], succeeded: ["applied"],
  failed: ["not_dispatched", "not_applied"], uncertain: ["unknown"],
};

describe("integration action execution evidence", () => {
  it("keeps operations without adapter references compatible", () => {
    expect(OperationV1.parse(operation)).toEqual(operation);
    expect(ConnectorDefinitionV1.parse(connector)).toEqual(connector);
  });

  it("preserves an immutable adapter reference inside a connector", () => {
    const adapter_ref = { id: "sales-write-adapter", version: 2, digest: "cd".repeat(32) };
    const sample = { ...connector, operations: [{ ...operation, adapter_ref }] };
    expect(ConnectorDefinitionV1.parse(sample)).toEqual(sample);
  });

  it("rejects malformed or extended adapter references", () => {
    for (const adapter_ref of [null, { ...ref, version: 0 }, { ...ref, version: -1 },
      { ...ref, version: 1.5 }, { ...ref, version: "1" }, { ...ref, digest: "a".repeat(63) },
      { ...ref, digest: "a".repeat(65) }, { ...ref, digest: "AB".repeat(32) },
      { ...ref, digest: `${ref.digest}\n` }, { ...ref, authority: true }]) {
      const sample = { ...operation, adapter_ref };
      expect(OperationV1.safeParse(sample).success).toBe(false);
      expect(ConnectorDefinitionV1.safeParse({ ...connector, operations: [sample] }).success).toBe(false);
    }
  });

  it("keeps submission operation references independent of adapter references", () => {
    expect(IntegrationActionSubmissionV1.parse(submission).operation_ref).toEqual(ref);
    expect(IntegrationActionSubmissionV1.safeParse({ ...submission, adapter_ref: ref }).success).toBe(false);
    expect(IntegrationActionSubmissionV1.safeParse({
      ...submission, operation_ref: { ...ref, adapter_ref: ref },
    }).success).toBe(false);
  });

  it("emits optional adapter references using the shared immutable definition", () => {
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    for (const [defs, prefix] of [
      [json.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"],
    ] as const) {
      expect(defs.OperationV1.properties.adapter_ref).toEqual({ $ref: `${prefix}IntegrationRefV1` });
      expect(defs.OperationV1.required).not.toContain("adapter_ref");
      expect(defs.IntegrationActionSubmissionV1.properties.operation_ref).toEqual({ $ref: `${prefix}IntegrationRefV1` });
      expect(defs.IntegrationActionSubmissionV1.properties).not.toHaveProperty("adapter_ref");
    }
  });

  it("round trips submissions, runs, receipts and both submission results", () => {
    for (const [schema, sample] of [
      [IntegrationActionSubmissionV1, submission], [IntegrationActionRunV1, run],
      [IntegrationActionReceiptV1, receipt],
      [IntegrationActionSubmissionResultV1, { status: "admitted", run, replayed: false }],
      [IntegrationActionSubmissionResultV1, { status: "admitted", run, replayed: true }],
      [IntegrationActionSubmissionResultV1, { status: "refused", code: "idempotency_conflict" }],
    ] as const) expect(schema.parse(JSON.parse(JSON.stringify(sample)))).toEqual(sample);
  });

  it("checks every compatible and incompatible state/outcome pair at runtime", () => {
    for (const state of Object.keys(allowed) as Array<keyof typeof allowed>) {
      for (const effect_outcome of outcomes) {
        const sample = { ...run, state, effect_outcome };
        expect(IntegrationActionRunV1.safeParse(sample).success).toBe(allowed[state].includes(effect_outcome));
        expect(IntegrationActionSubmissionResultV1.safeParse({ status: "admitted", run: sample, replayed: false }).success)
          .toBe(allowed[state].includes(effect_outcome));
      }
    }
    expect(IntegrationActionRunV1.safeParse({ ...run, state: "queued" }).success).toBe(false);
    expect(IntegrationActionRunV1.safeParse({ ...run, effect_outcome: "write" }).success).toBe(false);
  });

  it("bounds idempotency keys with an absolute ASCII end", () => {
    for (const idempotency_key of ["a".repeat(16), "a".repeat(128), "Ab09_-".repeat(3)]) {
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, idempotency_key }).success).toBe(true);
    }
    for (const idempotency_key of ["", "a".repeat(15), "a".repeat(129), `${submission.idempotency_key}\n`,
      "abcdefghijklmnop/", "abcdefghijklmnop ", "abcdefghijklmnopé"]) {
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, idempotency_key }).success).toBe(false);
    }
  });

  it("accepts bounded flat scalar arguments, including application names that convey no authority", () => {
    for (const args of [{}, { value: "a".repeat(4096) },
      Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`field_${i}`, i])),
      { url: "https://example.com", authority: true, credential: "application data" }]) {
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, arguments: args }).success).toBe(true);
    }
    for (const args of [null, [], "text", { value: "a".repeat(4097) }, { value: {} }, { value: [] },
      { value: undefined }, { value: Infinity }, { value: -Infinity }, { value: NaN },
      { "bad-key": 1 }, { "field\n": 1 }, { "1field": 1 }, { ["a".repeat(129)]: 1 },
      Object.fromEntries(Array.from({ length: 101 }, (_, i) => [`field_${i}`, i]))]) {
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, arguments: args }).success).toBe(false);
    }
    for (const key of ["__proto__", "prototype", "constructor"]) {
      const args = JSON.parse(`{"${key}":"denied"}`);
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, arguments: args }).success).toBe(false);
    }
  });

  it("requires positive safe revisions and receipt sequences without coercion", () => {
    for (const [schema, sample, keys] of [
      [IntegrationActionSubmissionV1, submission, ["expected_grant_revision"]],
      [IntegrationActionRunV1, run, ["revision", "grant_revision"]],
      [IntegrationActionReceiptV1, receipt, ["sequence"]],
    ] as const) {
      for (const key of keys) {
        expect(schema.safeParse({ ...sample, [key]: Number.MAX_SAFE_INTEGER }).success).toBe(true);
        for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN, "1"]) {
          expect(schema.safeParse({ ...sample, [key]: value }).success).toBe(false);
        }
      }
    }
  });

  it("requires real UTC dates and preserves fractional timestamp ordering", () => {
    for (const value of ["2026-02-29T12:00:00Z", "2026-04-31T12:00:00Z", "2026-10-20T24:00:00Z",
      "2026-10-20T12:00:00+00:00", `${instant}\n`, "2026-10-20"]) {
      expect(IntegrationActionRunV1.safeParse({ ...run, created_at: value }).success).toBe(false);
      expect(IntegrationActionRunV1.safeParse({ ...run, updated_at: value }).success).toBe(false);
      expect(IntegrationActionReceiptV1.safeParse({ ...receipt, observed_at: value }).success).toBe(false);
    }
    for (const [created_at, updated_at, valid] of [
      [instant, instant, true], [instant, "2026-10-21T00:00:00Z", true],
      [instant, "2026-10-20T11:59:59Z", false],
      ["2024-02-29T12:00:00Z", instant, true],
      ["2026-10-20T12:00:00.000002Z", "2026-10-20T12:00:00.000001Z", false],
      ["2026-10-20T12:00:00.1Z", "2026-10-20T12:00:00.1000Z", true],
      ["2026-10-20T12:00:00.000001Z", instant, false],
      [instant, "2026-10-20T12:00:00.000001Z", true],
    ] as const) expect(IntegrationActionRunV1.safeParse({ ...run, created_at, updated_at }).success).toBe(valid);
  });

  it("validates UUIDs, slugs, digests and pinned references at every boundary", () => {
    for (const [schema, sample, uuidKeys, refKeys, digestKeys] of [
      [IntegrationActionSubmissionV1, submission, [], ["connector_ref", "operation_ref"], []],
      [IntegrationActionRunV1, run, ["run_id"], ["connector_ref", "operation_ref"], ["submission_digest", "authority_binding_digest"]],
      [IntegrationActionReceiptV1, receipt, ["receipt_id", "run_id"], ["operation_ref"], ["submission_digest", "result_digest"]],
    ] as const) {
      for (const key of uuidKeys) for (const value of ["invalid", `${uuid}\n`]) {
        expect(schema.safeParse({ ...sample, [key]: value }).success).toBe(false);
      }
      for (const key of refKeys) for (const value of [{ ...ref, id: "sales\n" }, { ...ref, version: 0 },
        { ...ref, digest: "AB".repeat(32) }, { ...ref, authority: true }]) {
        expect(schema.safeParse({ ...sample, [key]: value }).success).toBe(false);
      }
      for (const key of digestKeys) for (const value of ["a".repeat(63), "a".repeat(65), "AB".repeat(32), `${ref.digest}\n`]) {
        expect(schema.safeParse({ ...sample, [key]: value }).success).toBe(false);
      }
      expect(schema.safeParse({ ...sample, schema_version: 2 }).success).toBe(false);
    }
    for (const connection_id of ["", "Sales", "sales\n"]) {
      expect(IntegrationActionSubmissionV1.safeParse({ ...submission, connection_id }).success).toBe(false);
      expect(IntegrationActionRunV1.safeParse({ ...run, connection_id }).success).toBe(false);
    }
    for (const patch of [{ operation_ref: { ...ref, version: 0 } }, { observation_receipt_id: "invalid" }, { extra: true }]) {
      expect(IntegrationActionReceiptV1.safeParse({ ...receipt, reconciliation: { ...reconciliation, ...patch } }).success).toBe(false);
    }
  });

  it("rejects unknown envelope fields, raw payloads and authority extensions", () => {
    for (const [schema, sample] of [
      [IntegrationActionSubmissionV1, submission], [IntegrationActionRunV1, run], [IntegrationActionReceiptV1, receipt],
    ] as const) {
      for (const key of ["url", "headers", "credentials", "bearer", "authority", "approval", "claim_token",
        "provider_response", "input_data", "result", "raw_result", "unknown"]) {
        expect(schema.safeParse({ ...sample, [key]: "denied" }).success).toBe(false);
      }
      for (const key of Object.keys(sample)) {
        const incomplete: Record<string, unknown> = { ...sample };
        delete incomplete[key];
        expect(schema.safeParse(incomplete).success).toBe(false);
      }
    }
  });

  it("checks receipt failure and reconciliation consistency at runtime", () => {
    for (const effect_outcome of outcomes) {
      expect(IntegrationActionReceiptV1.safeParse({ ...receipt, effect_outcome }).success).toBe(true);
      expect(IntegrationActionReceiptV1.safeParse({ ...receipt, effect_outcome, reconciliation }).success)
        .toBe(effect_outcome !== "not_dispatched");
      for (const failure_code of ["invalid_input", "forbidden", "revoked", "conflict", "quota", "timeout",
        "unavailable", "invalid_response", "outcome_unknown"]) {
        expect(IntegrationActionReceiptV1.safeParse({ ...receipt, effect_outcome, failure_code }).success)
          .toBe(effect_outcome !== "applied" && (failure_code !== "outcome_unknown" || effect_outcome === "unknown"));
      }
    }
    expect(IntegrationActionReceiptV1.safeParse({ ...receipt, result_digest: ref.digest }).success).toBe(true);
    expect(IntegrationActionReceiptV1.safeParse({ ...receipt, failure_code: "other" }).success).toBe(false);
    expect(IntegrationActionReceiptV1.safeParse({ ...receipt, effect_outcome: "succeeded" }).success).toBe(false);
  });

  it("keeps conflict responses minimal and refuses disclosure or mixed branches", () => {
    const conflict = { status: "refused", code: "idempotency_conflict" };
    for (const extra of [{ run }, { replayed: true }, { arguments: submission.arguments }, { submission },
      { submission_digest: ref.digest }, { other_input: submission }, { message: "other input" }]) {
      expect(IntegrationActionSubmissionResultV1.safeParse({ ...conflict, ...extra }).success).toBe(false);
    }
    for (const value of [{ status: "refused", code: "forbidden" }, { status: "admitted", run },
      { status: "admitted", run, replayed: "true" }, { status: "admitted", run, replayed: false, code: "idempotency_conflict" }]) {
      expect(IntegrationActionSubmissionResultV1.safeParse(value).success).toBe(false);
    }
  });

  it("emits all four strict definitions in both bundles without advertising action paths", () => {
    // Runtime checks above cover refinements that JSON Schema cannot express.
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    for (const defs of [json.$defs, openapi.components.schemas]) {
      for (const name of ["IntegrationActionSubmissionV1", "IntegrationActionRunV1", "IntegrationActionReceiptV1"]) {
        expect(defs[name].type).toBe("object");
        expect(defs[name].additionalProperties).toBe(false);
        expect(defs[name].required).toContain("schema_version");
      }
      const branches = defs.IntegrationActionSubmissionResultV1.anyOf;
      expect(branches).toHaveLength(2);
      for (const branch of branches) expect(branch.additionalProperties).toBe(false);
      expect(Object.keys(branches[1].properties).sort()).toEqual(["code", "status"]);
      expect(defs.IntegrationActionReceiptV1.properties.reconciliation.additionalProperties).toBe(false);
      const args = defs.IntegrationActionSubmissionV1.properties.arguments;
      expect(args.type).toBe("object");
      expect(args.description).toContain("at most 100");
      expect(args.maxProperties).toBe(100);
      const namePattern = new RegExp(args.propertyNames.pattern);
      for (const name of ["__proto__", "prototype", "constructor"]) expect(namePattern.test(name)).toBe(false);
      for (const name of ["constructorId", "amount", "_id"]) expect(namePattern.test(name)).toBe(true);
      expect(args.additionalProperties.anyOf).toHaveLength(4);
      expect(args.additionalProperties.anyOf.find((branch: { type: string }) => branch.type === "string").maxLength).toBe(4096);
    }
    expect(Object.keys(openapi.paths).sort()).toEqual([
      "/cards/{card_id}/approve", "/proposals/{proposal_id}", "/sessions/{session_id}/events",
      "/sessions/{session_id}/thread", "/sessions/{session_id}/turns", "/worker/jobs/{job_id}/events",
    ]);
  });
});
