import { describe, expect, it } from "vitest";
import {
  IntegrationActionFieldV1, IntegrationActionFormV1, IntegrationActionElementV1,
  IntegrationActionCatalogEntryV1, IntegrationActionCatalogV1,
  IntegrationActionSnapshotV1, IntegrationActionRunV1, IntegrationActionReceiptV1,
  IntegrationActionSubmissionV1, DatasetElementV1, IntegrationElementV1, emitJsonSchema, emitOpenApi,
} from "../src/index";

const ref = { id: "sales", version: 1, digest: "ab".repeat(32) };
const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const otherUuid = "7b92f224-18f2-4939-b368-c199cc1cc291";
const instant = "2026-10-20T12:00:00Z";
const common = { id: "name", label: "Name", required: true, nullable: false };
const fields: IntegrationActionFieldV1[] = [
  { ...common, kind: "text", min_length: 0, max_length: 4096, description: "Display name" },
  { ...common, id: "amount", kind: "number", minimum: 0, maximum: 10, integer: false },
  { ...common, id: "enabled", kind: "boolean", required: false, nullable: true },
  { ...common, id: "status", kind: "choice", choices: [{ label: "Open", value: "open" }] },
];
const form: IntegrationActionFormV1 = { fields, submit_label: "Create", description: "Create a sale" };
const element: IntegrationActionElementV1 = {
  schema_version: 1, id: "create-sale", title: "Create sale", kind: "integration.action",
  connection_id: "sales-account", connector_ref: ref, operation_ref: ref, form,
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
const terminal: IntegrationActionSnapshotV1 = {
  run: { ...run, state: "succeeded", effect_outcome: "applied" },
  receipts: [{ ...receipt, effect_outcome: "applied" }],
};

describe("read-only integration action catalog", () => {
  const entry: IntegrationActionCatalogEntryV1 = {
    connection_id: element.connection_id, connection_title: "Sales account",
    connector_ref: ref, operation_ref: ref, title: element.title, form,
  };
  const catalog: IntegrationActionCatalogV1 = { actions: [entry], next_cursor: null };

  it("accepts bounded descriptors without adding arguments, defaults or executable code", () => {
    expect(IntegrationActionCatalogEntryV1.parse(entry)).toEqual(entry);
    expect(IntegrationActionCatalogV1.parse(catalog)).toEqual(catalog);
    expect(IntegrationActionCatalogV1.parse({ actions: [], next_cursor: null })).toEqual({ actions: [], next_cursor: null });
    expect(IntegrationActionCatalogV1.safeParse({ actions: Array(50).fill(entry), next_cursor: "a".repeat(1024) }).success).toBe(true);
    expect(IntegrationActionCatalogV1.safeParse({ actions: Array(51).fill(entry), next_cursor: null }).success).toBe(false);
    expect(IntegrationActionCatalogV1.safeParse({ actions: [{ ...entry, form: { fields: [], submit_label: "Run" } }], next_cursor: "next_1-2" }).success).toBe(true);
    for (const next_cursor of ["", "x".repeat(1025), "cursor\n", "cursor=", "https://example.test", 1]) {
      expect(IntegrationActionCatalogV1.safeParse({ ...catalog, next_cursor }).success).toBe(false);
    }
    expect(IntegrationActionCatalogV1.safeParse({ actions: [] }).success).toBe(false);
    for (const key of Object.keys(entry)) {
      const sample: Record<string, unknown> = { ...entry };
      delete sample[key];
      expect(IntegrationActionCatalogEntryV1.safeParse(sample).success).toBe(false);
    }
  });

  it("rejects authority, transport metadata, malformed references and invalid forms", () => {
    for (const key of ["scope", "tenant_id", "project_id", "origin", "path", "url", "method", "auth", "headers",
      "credentials", "credential_refs", "grant_revision", "lease", "approval_token", "arguments", "result",
      "idempotency_key", "execute", "default"]) {
      expect(IntegrationActionCatalogV1.safeParse({ ...catalog, [key]: "denied" }).success).toBe(false);
      expect(IntegrationActionCatalogEntryV1.safeParse({ ...entry, [key]: "denied" }).success).toBe(false);
      expect(IntegrationActionCatalogV1.safeParse({ ...catalog, actions: [{ ...entry, [key]: "denied" }] }).success).toBe(false);
    }
    for (const connection_id of ["", "bad slug", "sales\n", "a".repeat(129)]) {
      expect(IntegrationActionCatalogEntryV1.safeParse({ ...entry, connection_id }).success).toBe(false);
    }
    for (const key of ["title", "connection_title"]) for (const value of ["", "a".repeat(121)]) {
      expect(IntegrationActionCatalogEntryV1.safeParse({ ...entry, [key]: value }).success).toBe(false);
    }
    for (const key of ["connector_ref", "operation_ref"]) for (const badRef of [
      { ...ref, version: 0 }, { ...ref, digest: "invalid" }, { ...ref, authority: true },
    ]) expect(IntegrationActionCatalogEntryV1.safeParse({ ...entry, [key]: badRef }).success).toBe(false);
    for (const invalidForm of [
      { ...form, fields: [fields[0], fields[0]] },
      { ...form, fields: [{ ...fields[0], default: "inferred" }] },
      { ...form, fields: [{ ...fields[0], max_length: 4097 }] },
      { ...form, execute: "code" },
    ]) expect(IntegrationActionCatalogV1.safeParse({ ...catalog, actions: [{ ...entry, form: invalidForm }] }).success).toBe(false);
  });

  it("emits only strict bounded descriptor fields and canonical nested references in both formats", () => {
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    for (const [defs, prefix] of [
      [json.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"],
    ] as const) {
      const page = defs.IntegrationActionCatalogV1;
      const descriptor = defs.IntegrationActionCatalogEntryV1;
      expect(page.additionalProperties).toBe(false);
      expect(page.required).toEqual(["actions", "next_cursor"]);
      expect(Object.keys(page.properties)).toEqual(["actions", "next_cursor"]);
      expect(page.properties.actions).toEqual({ type: "array", maxItems: 50,
        items: { $ref: `${prefix}IntegrationActionCatalogEntryV1` } });
      expect(page.properties.next_cursor.anyOf).toEqual([
        { type: "string", minLength: 1, maxLength: 1024, pattern: "^[A-Za-z0-9_-]+(?![\\s\\S])" },
        { type: "null" },
      ]);
      expect(descriptor.additionalProperties).toBe(false);
      expect(Object.keys(descriptor.properties)).toEqual(Object.keys(entry));
      expect(descriptor.required).toEqual(Object.keys(entry));
      expect(descriptor.properties.connection_id).toEqual(defs.IntegrationActionElementV1.properties.connection_id);
      for (const key of ["title", "connection_title"]) {
        expect(descriptor.properties[key]).toEqual({ type: "string", minLength: 1, maxLength: 120 });
      }
      for (const key of ["connector_ref", "operation_ref"]) {
        expect(descriptor.properties[key]).toEqual({ $ref: `${prefix}IntegrationRefV1` });
      }
      expect(descriptor.properties.form).toEqual({ $ref: `${prefix}IntegrationActionFormV1` });
      expect(descriptor.description).toContain("not an execution grant");
      expect(JSON.stringify(descriptor)).not.toContain('"default":');
    }
  });
});

describe("inert integration action forms", () => {
  it("composes table, line and inert action recipes through the shared union", () => {
    const dataset = {
      schema_version: 1, id: "sales-view", title: "Sales", dataset_ref: ref,
      query: { fields: ["day", "amount"], limit: 10 },
    };
    const table = { ...dataset, kind: "dataset.table", props: { columns: ["amount"], row_limit: 10 } };
    const line = { ...dataset, kind: "dataset.line", props: { x_field: "day", y_field: "amount" } };
    expect(IntegrationElementV1.options).toEqual([DatasetElementV1, IntegrationActionElementV1]);
    for (const recipe of [table, line, element]) {
      expect(IntegrationElementV1.parse(JSON.parse(JSON.stringify(recipe)))).toEqual(recipe);
      for (const key of ["arguments", "idempotency_key", "expected_grant_revision", "grant", "authority", "url", "execute"]) {
        expect(IntegrationElementV1.safeParse({ ...recipe, [key]: "denied" }).success).toBe(false);
      }
    }
    for (const recipe of [table, line]) expect(DatasetElementV1.parse(recipe)).toEqual(recipe);
    expect(IntegrationActionElementV1.safeParse(table).success).toBe(false);
    for (const kind of ["provider.action", "integration.submit", "unknown"]) {
      expect(IntegrationElementV1.safeParse({ ...element, kind }).success).toBe(false);
    }
    for (const key of ["connector_ref", "operation_ref"]) {
      expect(IntegrationElementV1.safeParse({ ...element, [key]: { ...ref, digest: "invalid" } }).success).toBe(false);
      expect(IntegrationElementV1.safeParse({ ...element, [key]: { ...ref, grant: true } }).success).toBe(false);
    }
    expect(IntegrationElementV1.safeParse({ ...element, form: { ...form, fields: [fields[0], fields[0]] } }).success).toBe(false);
    expect(IntegrationElementV1.safeParse({ ...element, form: { ...form, fields: [{ ...fields[0], max_length: 4097 }] } }).success).toBe(false);
  });

  it("round trips all scalar field variants, complete forms and zero-field actions", () => {
    for (const field of fields) expect(IntegrationActionFieldV1.parse(field)).toEqual(field);
    expect(IntegrationActionFormV1.parse(form)).toEqual(form);
    expect(IntegrationActionElementV1.parse(element)).toEqual(element);
    const empty = { ...element, form: { fields: [], submit_label: "Run" } };
    expect(IntegrationActionElementV1.parse(empty)).toEqual(empty);
    expect(DatasetElementV1.safeParse(element).success).toBe(false);
    for (const field of [
      { ...common, kind: "text" }, { ...common, kind: "text", min_length: 0, max_length: 0 },
      { ...common, kind: "number", integer: true },
      { ...common, kind: "number", integer: false, minimum: -1.5, maximum: -1.5 },
    ]) expect(IntegrationActionFieldV1.safeParse(field).success).toBe(true);
  });

  it("keeps absent, null, false, zero and empty arguments distinct", () => {
    const submission = {
      schema_version: 1, connection_id: element.connection_id, connector_ref: ref, operation_ref: ref,
      idempotency_key: "request_12345678", expected_grant_revision: 1,
    };
    for (const args of [{}, { value: null }, { value: false }, { value: 0 }, { value: "" }]) {
      expect(IntegrationActionSubmissionV1.parse({ ...submission, arguments: args }).arguments).toEqual(args);
    }
  });

  it("rejects duplicate, reserved and malformed field IDs", () => {
    expect(IntegrationActionFormV1.safeParse({ ...form, fields: [fields[0], fields[0]] }).success).toBe(false);
    for (const id of ["__proto__", "prototype", "constructor", "", "1name", "bad-key", "name\n", "a".repeat(129)]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[0], id }).success).toBe(false);
    }
    for (const id of ["constructorId", "_name", "a".repeat(128)]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[0], id }).success).toBe(true);
    }
  });

  it("requires common flags and the number integer flag without coercion", () => {
    for (const field of fields) {
      for (const key of ["id", "label", "required", "nullable", "kind"]) {
        const sample: Record<string, unknown> = { ...field };
        delete sample[key];
        expect(IntegrationActionFieldV1.safeParse(sample).success).toBe(false);
      }
      for (const key of ["required", "nullable"]) {
        expect(IntegrationActionFieldV1.safeParse({ ...field, [key]: "false" }).success).toBe(false);
      }
    }
    expect(IntegrationActionFieldV1.safeParse({ ...common, kind: "number" }).success).toBe(false);
    expect(IntegrationActionFieldV1.safeParse({ ...fields[1], integer: 1 }).success).toBe(false);
  });

  it("rejects nonfinite, reversed and invalid scalar bounds", () => {
    for (const key of ["min_length", "max_length"]) for (const value of [-1, 4097, 0.5, Infinity, NaN, "1"]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[0], [key]: value }).success).toBe(false);
    }
    expect(IntegrationActionFieldV1.safeParse({ ...fields[0], min_length: 2, max_length: 1 }).success).toBe(false);
    for (const key of ["minimum", "maximum"]) for (const value of [Infinity, -Infinity, NaN, "1"]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[1], [key]: value }).success).toBe(false);
    }
    expect(IntegrationActionFieldV1.safeParse({ ...fields[1], minimum: 2, maximum: 1 }).success).toBe(false);
  });

  it("bounds fields, labels, descriptions and choices, preserving unique choice values", () => {
    const manyFields = Array.from({ length: 100 }, (_, i) => ({ ...fields[0], id: `field_${i}` }));
    expect(IntegrationActionFormV1.safeParse({ ...form, fields: manyFields }).success).toBe(true);
    expect(IntegrationActionFormV1.safeParse({ ...form, fields: [...manyFields, { ...fields[0], id: "extra" }] }).success).toBe(false);
    for (const description of ["", "a".repeat(501)]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[0], description }).success).toBe(false);
    }
    for (const description of ["", "a".repeat(1001)]) {
      expect(IntegrationActionFormV1.safeParse({ ...form, description }).success).toBe(false);
    }
    expect(IntegrationActionFieldV1.safeParse({ ...fields[0], description: "a".repeat(500) }).success).toBe(true);
    expect(IntegrationActionFormV1.safeParse({ ...form, description: "a".repeat(1000) }).success).toBe(true);
    for (const label of ["", "a".repeat(121)]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[0], label }).success).toBe(false);
      expect(IntegrationActionFormV1.safeParse({ ...form, submit_label: label }).success).toBe(false);
    }
    const choices = Array.from({ length: 100 }, (_, i) => ({ label: "Choice", value: `${i}` }));
    expect(IntegrationActionFieldV1.safeParse({ ...fields[3], choices }).success).toBe(true);
    for (const invalid of [[], [...choices, { label: "Extra", value: "extra" }],
      [{ label: "One", value: "same" }, { label: "Two", value: "same" }],
      [{ label: "", value: "one" }], [{ label: "a".repeat(121), value: "one" }],
      [{ label: "One", value: "" }], [{ label: "One", value: "a".repeat(4097) }],
      [{ label: "One", value: "one", authority: true }]]) {
      expect(IntegrationActionFieldV1.safeParse({ ...fields[3], choices: invalid }).success).toBe(false);
    }
    expect(IntegrationActionFieldV1.safeParse({ ...fields[3], choices: [{ label: "One", value: "a".repeat(4096) }] }).success).toBe(true);
  });

  it("rejects authority, credential, transport and executable extensions at each saved boundary", () => {
    for (const key of ["grant_revision", "expected_grant_revision", "actor", "authority", "permission", "secret",
      "credential", "credential_refs", "destination", "url", "method", "headers", "expression", "regex", "html", "default", "unknown"]) {
      expect(IntegrationActionElementV1.safeParse({ ...element, [key]: "denied" }).success).toBe(false);
      expect(IntegrationActionFormV1.safeParse({ ...form, [key]: "denied" }).success).toBe(false);
      for (const field of fields) {
        expect(IntegrationActionFieldV1.safeParse({ ...field, [key]: "denied" }).success).toBe(false);
      }
    }
    for (const field of [
      { ...fields[2], minimum: 0 }, { ...fields[0], integer: true }, { ...fields[1], choices: [] },
    ]) expect(IntegrationActionFieldV1.safeParse(field).success).toBe(false);
    for (const key of Object.keys(element)) {
      const sample: Record<string, unknown> = { ...element };
      delete sample[key];
      expect(IntegrationActionElementV1.safeParse(sample).success).toBe(false);
    }
    for (const key of ["connector_ref", "operation_ref"]) for (const badRef of [
      { ...ref, version: 0 }, { ...ref, digest: "invalid" }, { ...ref, authority: true },
    ]) expect(IntegrationActionElementV1.safeParse({ ...element, [key]: badRef }).success).toBe(false);
  });
});

describe("integration action read snapshots", () => {
  it("accepts current journal topology and requires terminal evidence matching the outcome", () => {
    for (const [state, effect_outcome] of [
      ["accepted", "not_dispatched"], ["running", "unknown"], ["succeeded", "applied"],
      ["failed", "not_dispatched"], ["failed", "not_applied"], ["uncertain", "unknown"],
    ] as const) {
      const sample = { run: { ...run, state, effect_outcome }, receipts: [] as IntegrationActionReceiptV1[] };
      const nonterminal = state === "accepted" || state === "running";
      expect(IntegrationActionSnapshotV1.safeParse(sample).success).toBe(nonterminal);
      sample.receipts = [{ ...receipt, effect_outcome }];
      expect(IntegrationActionSnapshotV1.safeParse(sample).success).toBe(!nonterminal);
    }
    expect(IntegrationActionSnapshotV1.parse(terminal)).toEqual(terminal);
    expect(IntegrationActionSnapshotV1.safeParse({ ...terminal, receipts: [receipt] }).success).toBe(false);
  });

  it("allows bounded append-only reconciliation history with earlier unknown evidence", () => {
    const reconciled = {
      ...terminal,
      receipts: [receipt, { ...terminal.receipts[0], receipt_id: otherUuid, sequence: 3,
        reconciliation: { operation_ref: ref, observation_receipt_id: uuid } }],
    };
    expect(IntegrationActionSnapshotV1.parse(reconciled)).toEqual(reconciled);
    const receipts = Array.from({ length: 100 }, (_, i) => ({
      ...receipt, receipt_id: `7b92f224-18f2-4939-b368-${i.toString().padStart(12, "0")}`,
      sequence: i + 2, effect_outcome: i === 99 ? "applied" as const : "unknown" as const,
    }));
    expect(IntegrationActionSnapshotV1.safeParse({ ...terminal, receipts }).success).toBe(true);
    expect(IntegrationActionSnapshotV1.safeParse({ ...terminal, receipts: [...receipts, {
      ...terminal.receipts[0], receipt_id: otherUuid, sequence: 102,
    }] }).success).toBe(false);
  });

  it("rejects cross-run, cross-submission and every exact operation reference mismatch", () => {
    for (const patch of [
      { run_id: otherUuid }, { submission_digest: "cd".repeat(32) },
      { operation_ref: { ...ref, id: "other" } }, { operation_ref: { ...ref, version: 2 } },
      { operation_ref: { ...ref, digest: "cd".repeat(32) } },
    ]) expect(IntegrationActionSnapshotV1.safeParse({ ...terminal, receipts: [{ ...terminal.receipts[0], ...patch }] }).success).toBe(false);
  });

  it("rejects reordered or repeated sequences and duplicate receipt IDs", () => {
    for (const receipts of [
      [{ ...receipt, sequence: 2 }, { ...terminal.receipts[0], receipt_id: otherUuid, sequence: 1 }],
      [receipt, { ...terminal.receipts[0], receipt_id: otherUuid, sequence: 1 }],
      [receipt, { ...terminal.receipts[0], sequence: 2 }],
    ]) expect(IntegrationActionSnapshotV1.safeParse({ ...terminal, receipts }).success).toBe(false);
  });

  it("bounds observations by the run lifetime without discarding fractional precision", () => {
    const sample = { ...terminal, run: { ...terminal.run,
      created_at: "2026-10-20T12:00:00.000002Z", updated_at: "2026-10-20T12:00:00.000004Z",
    } };
    for (const [observed_at, valid] of [
      ["2026-10-20T12:00:00.000001Z", false], ["2026-10-20T12:00:00.000002Z", true],
      ["2026-10-20T12:00:00.000003Z", true], ["2026-10-20T12:00:00.0000040Z", true],
      ["2026-10-20T12:00:00.000005Z", false], [instant, false],
    ] as const) expect(IntegrationActionSnapshotV1.safeParse({ ...sample,
      receipts: [{ ...terminal.receipts[0], observed_at }],
    }).success).toBe(valid);
  });

  it("keeps the envelope strict and requires both run and receipts", () => {
    for (const sample of [{ run }, { receipts: [] }, { ...terminal, authority: true },
      { ...terminal, run: { ...terminal.run, extra: true } },
      { ...terminal, receipts: [{ ...terminal.receipts[0], extra: true }] }]) {
      expect(IntegrationActionSnapshotV1.safeParse(sample).success).toBe(false);
    }
  });
});

describe("action presentation and snapshot emission", () => {
  it("emits strict definitions, typed variants and structural bounds in both formats", () => {
    const json = JSON.parse(JSON.stringify(emitJsonSchema()));
    const openapi = JSON.parse(JSON.stringify(emitOpenApi()));
    for (const [defs, prefix] of [
      [json.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"],
    ] as const) {
      expect(defs.IntegrationElementV1.anyOf).toEqual([
        { $ref: `${prefix}DatasetElementV1` },
        { $ref: `${prefix}IntegrationActionElementV1` },
      ]);
      expect(defs.DatasetElementV1.anyOf.map((branch: { properties: { kind: { const: string } } }) => branch.properties.kind.const))
        .toEqual(["dataset.table", "dataset.line"]);
      for (const name of ["IntegrationActionFormV1", "IntegrationActionElementV1", "IntegrationActionSnapshotV1"]) {
        expect(defs[name].type).toBe("object");
        expect(defs[name].additionalProperties).toBe(false);
      }
      const branches = defs.IntegrationActionFieldV1.anyOf;
      expect(branches).toHaveLength(4);
      expect(branches.map((branch: { properties: { kind: { const: string } } }) => branch.properties.kind.const))
        .toEqual(["text", "number", "boolean", "choice"]);
      for (const branch of branches) {
        expect(branch.additionalProperties).toBe(false);
        expect(branch.required).toEqual(expect.arrayContaining(["id", "label", "required", "nullable", "kind"]));
        expect(branch.properties.id).toMatchObject({ type: "string", minLength: 1, maxLength: 128 });
        for (const id of ["__proto__", "prototype", "constructor"]) expect(new RegExp(branch.properties.id.pattern).test(id)).toBe(false);
        expect(branch.properties.label).toMatchObject({ type: "string", minLength: 1, maxLength: 120 });
        expect(branch.properties.description).toMatchObject({ type: "string", minLength: 1, maxLength: 500 });
        expect(branch.properties.required.type).toBe("boolean");
        expect(branch.properties.nullable.type).toBe("boolean");
      }
      for (const key of ["min_length", "max_length"]) {
        expect(branches[0].properties[key]).toMatchObject({ type: "integer", minimum: 0, maximum: 4096 });
      }
      for (const key of ["minimum", "maximum"]) expect(branches[1].properties[key].type).toBe("number");
      expect(branches[1].properties.integer.type).toBe("boolean");
      expect(branches[1].required).toContain("integer");
      expect(branches[3].properties.choices).toMatchObject({ type: "array", minItems: 1, maxItems: 100 });
      expect(branches[3].properties.choices.items.additionalProperties).toBe(false);
      expect(branches[3].properties.choices.items.properties.value).toMatchObject({ type: "string", minLength: 1, maxLength: 4096 });
      expect(defs.IntegrationActionFormV1.properties.fields).toMatchObject({
        type: "array", minItems: 0, maxItems: 100, items: { $ref: `${prefix}IntegrationActionFieldV1` },
      });
      expect(defs.IntegrationActionFormV1.description).toContain("not business semantics or authority");
      expect(defs.IntegrationActionFormV1.properties.description).toMatchObject({ minLength: 1, maxLength: 1000 });
      expect(defs.IntegrationActionElementV1.properties.kind.const).toBe("integration.action");
      expect(defs.IntegrationActionElementV1.properties.form).toEqual({ $ref: `${prefix}IntegrationActionFormV1` });
      for (const key of ["connector_ref", "operation_ref"]) {
        expect(defs.IntegrationActionElementV1.properties[key]).toEqual({ $ref: `${prefix}IntegrationRefV1` });
      }
      expect(defs.IntegrationActionSnapshotV1.properties.run).toEqual({ $ref: `${prefix}IntegrationActionRunV1` });
      expect(defs.IntegrationActionSnapshotV1.properties.receipts).toMatchObject({
        type: "array", maxItems: 100, items: { $ref: `${prefix}IntegrationActionReceiptV1` },
      });
    }
  });
});
