import { describe, expect, expectTypeOf, it } from "vitest";
import { z } from "zod";
import { DatasetDefinitionV1, DatasetQueryV1, type DatasetFieldV1 } from "../src/integration";
import {
  DatasetDerivationBoundRequestV1, DatasetDerivationCapabilitiesV1, DatasetDerivationRequestV1,
  DatasetDerivationResultV1, DatasetDerivationLineageV1, DerivedDatasetDefinitionV1, ProjectMaterialDatasetDefinitionV1,
} from "../src/dataset-derivation";

const history = { id: "history", version: 1, digest: "a".repeat(64) };
const observations = { id: "observations", version: 2, digest: "b".repeat(64) };
const domain = { id: "scope", version: 1, digest: "c".repeat(64) };
const material = { kind: "project_material" as const, material_id: "00000000-0000-4000-8000-000000000001",
  sha256: "d".repeat(64), byte_length: 1024 };
function field(id: string, kind: DatasetFieldV1["type"]["kind"], nullable = false): DatasetFieldV1 {
  if (!["identifier", "text", "count", "instant", "date"].includes(kind)) throw new Error("Fixture kind not supported");
  return { id, label: id, type: { kind } as DatasetFieldV1["type"], nullable, sensitivity: "internal" };
}
function definition(ref: Pick<typeof history, "id" | "version">, fields: DatasetFieldV1[], key = "id") {
  return { schema_version: 1 as const, id: ref.id, version: ref.version, title: ref.id, fields,
    key_fields: [key], reporting_time_zone: "UTC", lineage: material, retention_days: 30 };
}
const historyDefinition = definition(history, [field("id", "identifier"), field("repo", "identifier"),
  field("occurred", "instant", true), field("state", "text")]);
const anchorDefinition = definition(observations, [field("id", "identifier"), field("repo", "text"),
  field("at", "instant"), field("open_count", "count", true), field("provenance", "text")]);
const scopeDefinition = definition(domain, [field("repo", "identifier"), field("order", "count")], "repo");
function groupRequest() {
  return { schema_version: 1, output: { id: "grouped", version: 1, title: "Activity" }, inputs: { primary: history },
    plan: { kind: "group_count", filters: [{ field: "state", op: "eq", value: "landed" }],
      group_by: [{ field: "repo", as: "repository", bucket: "value" }],
      count: { as: "landed", label: "Landed work" }, windows: [{ id: "all", filters: [] }] } };
}
function windowRequest() {
  return { schema_version: 1, output: { id: "observed-rate", version: 1, title: "Observed rate" },
    inputs: { primary: observations, events: history },
    plan: { kind: "window_count", anchor_fields: ["at", "open_count", "provenance"], at_field: "at",
      event_time_field: "occurred", event_filters: [{ field: "state", op: "eq", value: "landed" }],
      partition_by: [{ anchor_field: "repo", event_field: "repo" }], lookback_seconds: 604800,
      lower_inclusive: true, upper_inclusive: true, count: { as: "landed", label: "Landed work" } },
    computed: [{ as: "estimate", label: "Observed estimate", reason_as: "estimate_reason", reason_label: "Estimate status",
      numerator: "open_count", denominator: "landed", multiplier: 7, unit: "days" }] };
}
function boundGroup(request: unknown = groupRequest()) {
  return { request, bindings: [{ alias: "primary", dataset_ref: history, resource_revision: 3, definition: historyDefinition }] };
}
function boundWindow(request: unknown = windowRequest()) {
  return { request, bindings: [
    { alias: "primary", dataset_ref: observations, resource_revision: 4, definition: anchorDefinition },
    { alias: "events", dataset_ref: history, resource_revision: 3, definition: historyDefinition },
  ] };
}
function derivedDefinition() {
  const request = groupRequest();
  return { ...definition(request.output, [field("aggregate_key", "identifier"), field("repository", "identifier"),
    field("landed", "count"), field("window_id", "identifier")], "aggregate_key"),
    lineage: { kind: "derived", contract_version: 1,
      inputs: [{ alias: "primary", dataset_ref: history, resource_revision: 3, original_binding: material }],
      plan: request.plan, engine_revision: "synthetic-engine-v1", derivation_digest: "e".repeat(64) } };
}

describe("versioned dataset derivation contracts", () => {
  it("keeps the final inferred lineage precise and preserves base refinements", () => {
    expectTypeOf<DerivedDatasetDefinitionV1["lineage"]>().toEqualTypeOf<z.infer<typeof DatasetDerivationLineageV1>>();
    expectTypeOf<DerivedDatasetDefinitionV1["lineage"]["kind"]>().toEqualTypeOf<"derived">();
    const valid = derivedDefinition();
    expect(DerivedDatasetDefinitionV1.safeParse(valid).success).toBe(true);
    for (const invalid of [
      { ...valid, key_fields: ["missing"] },
      { ...valid, fields: valid.fields.map(item => item.id === "aggregate_key" ? { ...item, nullable: true } : item) },
      { ...valid, fields: [...valid.fields, valid.fields[0]] },
      { ...valid, time_field: "landed" },
      { ...valid, lineage: material },
      { ...valid, lineage: { ...valid.lineage, unexpected: true } },
    ]) expect(DerivedDatasetDefinitionV1.safeParse(invalid).success).toBe(false);
  });

  it("round trips both bounded operations and the producer's actual material binding", () => {
    for (const request of [groupRequest(), windowRequest()]) expect(DatasetDerivationRequestV1.parse(request)).toEqual(request);
    expect(ProjectMaterialDatasetDefinitionV1.parse(historyDefinition)).toEqual(historyDefinition);
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup()).success).toBe(true);
    expect(DatasetDerivationBoundRequestV1.safeParse(boundWindow()).success).toBe(true);
    expect(DerivedDatasetDefinitionV1.parse(derivedDefinition())).toEqual(derivedDefinition());
  });

  it("keeps raw query and ordinary definition V1 strict", () => {
    expect(DatasetQueryV1.safeParse({ fields: ["repo"], limit: 20, aggregate: groupRequest().plan }).success).toBe(false);
    expect(DatasetDefinitionV1.safeParse(derivedDefinition()).success).toBe(false);
  });

  it("admits a global group and explicit ordered domain without inventing a third input", () => {
    const global = groupRequest(); global.plan.group_by = [];
    expect(DatasetDerivationRequestV1.safeParse(global).success).toBe(true);
    const scoped = { ...groupRequest(), inputs: { primary: history, domain },
      plan: { ...groupRequest().plan, domain: { keys: [{ group_field: "repository", domain_field: "repo" }], order_field: "order" } } };
    expect(DatasetDerivationBoundRequestV1.safeParse({ request: scoped, bindings: [...boundGroup().bindings,
      { alias: "domain", dataset_ref: domain, resource_revision: 2, definition: scopeDefinition }] }).success).toBe(true);
    expect(DatasetDerivationRequestV1.safeParse({ ...scoped, inputs: { ...scoped.inputs, events: history } }).success).toBe(false);
  });

  it.each(["tenant_id", "principal", "token", "url", "sql", "role"])("rejects caller-supplied authority or execution field %s", key => {
    expect(DatasetDerivationRequestV1.safeParse({ ...groupRequest(), [key]: "caller assertion" }).success).toBe(false);
  });

  it("rejects unknown nested fields, missing event inputs and unused input roles", () => {
    expect(DatasetDerivationRequestV1.safeParse({ ...groupRequest(), plan: { ...groupRequest().plan, execute: "code" } }).success).toBe(false);
    const window = windowRequest();
    expect(DatasetDerivationRequestV1.safeParse({ ...window, inputs: { primary: observations } }).success).toBe(false);
    expect(DatasetDerivationRequestV1.safeParse({ ...window, inputs: { ...window.inputs, domain } }).success).toBe(false);
    expect(DatasetDerivationRequestV1.safeParse({ ...groupRequest(), inputs: { primary: history, domain } }).success).toBe(false);
  });

  it.each(["aggregate_key", "window_id", "constructor", "__proto__"])("rejects reserved or unsafe generated alias %s", as => {
    const request = groupRequest(); request.plan.count.as = as;
    expect(DatasetDerivationRequestV1.safeParse(request).success).toBe(false);
  });

  it("rejects collisions between grouping, count, quotient and reason outputs", () => {
    const request = groupRequest(); request.plan.count.as = "repository";
    expect(DatasetDerivationRequestV1.safeParse(request).success).toBe(false);
    const window = windowRequest(); window.computed[0].reason_as = "landed";
    expect(DatasetDerivationRequestV1.safeParse(window).success).toBe(false);
  });

  it("requires unique window identities and a complete domain key mapping", () => {
    const request = groupRequest(); request.plan.windows.push({ id: "all", filters: [] });
    expect(DatasetDerivationRequestV1.safeParse(request).success).toBe(false);
    expect(DatasetDerivationRequestV1.safeParse({ ...groupRequest(), inputs: { primary: history, domain },
      plan: { ...groupRequest().plan, domain: { keys: [] } } }).success).toBe(false);
  });

  it("enforces window/filter/calculation budgets and UTF-8 plan bytes", () => {
    const request = groupRequest(); request.plan.windows = Array.from({ length: 5 }, (_, i) => ({ id: `window-${i}`, filters: [] }));
    expect(DatasetDerivationRequestV1.safeParse(request).success).toBe(false);
    const manyFilters = groupRequest(); manyFilters.plan.filters = Array.from({ length: 21 }, () => ({ field: "state", op: "eq", value: "landed" }));
    expect(DatasetDerivationRequestV1.safeParse(manyFilters).success).toBe(false);
    const large = groupRequest(); large.plan.filters[0].value = "é".repeat(34000);
    const result = DatasetDerivationRequestV1.safeParse(large);
    expect(result.success).toBe(false);
    if (!result.success) expect(result.error.issues.some(issue => issue.path[0] === "plan")).toBe(true);
    const window = windowRequest(); window.computed = Array.from({ length: 5 }, (_, i) => ({ ...window.computed[0], as: `v${i}`, reason_as: `r${i}` }));
    expect(DatasetDerivationRequestV1.safeParse(window).success).toBe(false);
  });

  it("requires explicit boolean bounds and bounded positive lookback", () => {
    const request = windowRequest();
    expect(DatasetDerivationRequestV1.safeParse({ ...request, plan: { ...request.plan, lower_inclusive: "true" } }).success).toBe(false);
    for (const seconds of [0, 315360001]) expect(DatasetDerivationRequestV1.safeParse({ ...request,
      plan: { ...request.plan, lookback_seconds: seconds } }).success).toBe(false);
    expect(DatasetDerivationRequestV1.safeParse({ ...request, computed: [{ ...request.computed[0], multiplier: Infinity }] }).success).toBe(false);
  });

  it("rejects accessors and cycles before parsing or serializing caller objects", () => {
    let reads = 0;
    const accessor = { ...groupRequest() };
    Object.defineProperty(accessor, "output", { enumerable: true, get() { reads++; return groupRequest().output; } });
    expect(DatasetDerivationRequestV1.safeParse(accessor).success).toBe(false);
    expect(reads).toBe(0);
    const cyclic: Record<string, unknown> = { ...groupRequest() }; cyclic.loop = cyclic;
    expect(DatasetDerivationRequestV1.safeParse(cyclic).success).toBe(false);
  });

  it("matches host-loaded id/version/digest and unique input roles", () => {
    const mismatch = boundGroup(); mismatch.bindings[0] = { ...mismatch.bindings[0], dataset_ref: { ...history, digest: "f".repeat(64) } };
    expect(DatasetDerivationBoundRequestV1.safeParse(mismatch).success).toBe(false);
    const version = boundGroup(); version.bindings[0] = { ...version.bindings[0], definition: { ...historyDefinition, version: 2 } };
    expect(DatasetDerivationBoundRequestV1.safeParse(version).success).toBe(false);
    expect(DatasetDerivationBoundRequestV1.safeParse({ ...boundGroup(), bindings: [...boundGroup().bindings, ...boundGroup().bindings] }).success).toBe(false);
  });

  it("checks filter values, unknown source fields and non-null distinct identity", () => {
    const request = groupRequest(); request.plan.filters[0].value = 3 as unknown as string;
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup(request)).success).toBe(false);
    const unknown = groupRequest(); unknown.plan.group_by[0].field = "missing";
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup(unknown)).success).toBe(false);
    const nullable = { ...groupRequest(), plan: { ...groupRequest().plan, count: { ...groupRequest().plan.count, distinct_by: ["occurred"] } } };
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup(nullable)).success).toBe(false);
  });

  it("requires timezone conversion only for instant day buckets", () => {
    const request = groupRequest(); request.plan.group_by = [{ field: "occurred", as: "day", bucket: "day" }];
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup(request)).success).toBe(false);
    const zoned = { ...request, plan: { ...request.plan, group_by: [{ ...request.plan.group_by[0], time_zone: "UTC" }] } };
    expect(DatasetDerivationBoundRequestV1.safeParse(boundGroup(zoned)).success).toBe(true);
    expect(DatasetDerivationRequestV1.safeParse({ ...zoned, plan: { ...zoned.plan,
      group_by: [{ ...zoned.plan.group_by[0], time_zone: "Not/A_Zone" }] } }).success).toBe(false);
  });

  it("checks partition compatibility, anchor time and numeric quotient operands", () => {
    const request = windowRequest(); request.plan.partition_by[0].event_field = "occurred";
    expect(DatasetDerivationBoundRequestV1.safeParse(boundWindow(request)).success).toBe(false);
    const nullableAt = boundWindow(); nullableAt.bindings[0].definition = { ...anchorDefinition,
      fields: anchorDefinition.fields.map(item => item.id === "at" ? { ...item, nullable: true } : item) };
    expect(DatasetDerivationBoundRequestV1.safeParse(nullableAt).success).toBe(false);
    const text = windowRequest(); text.computed[0].numerator = "provenance";
    expect(DatasetDerivationBoundRequestV1.safeParse(boundWindow(text)).success).toBe(false);
    const chained = windowRequest(); chained.computed.push({ ...chained.computed[0], as: "next", reason_as: "next_reason", numerator: "estimate" });
    expect(DatasetDerivationBoundRequestV1.safeParse(boundWindow(chained)).success).toBe(false);
  });

  it("validates generated metadata and preserves immutable parsed inputs", () => {
    const invalid = derivedDefinition(); invalid.fields = invalid.fields.filter(item => item.id !== "landed");
    expect(DerivedDatasetDefinitionV1.safeParse(invalid).success).toBe(false);
    const source = windowRequest(), before = JSON.stringify(source);
    const parsed = DatasetDerivationRequestV1.parse(source);
    parsed.output.title = "Changed";
    expect(JSON.stringify(source)).toBe(before);
    expect(DatasetDerivationRequestV1.parse(source)).not.toBe(source);
  });

  it("validates the advertised bounded capability and complete publication result", () => {
    const capability = { schema_version: 1, contract: "dataset-derivation-v1", operations: ["group_count", "window_count"],
      max_inputs: 2, max_records_per_input: 100000, max_windows: 4 };
    expect(DatasetDerivationCapabilitiesV1.parse(capability)).toEqual(capability);
    expect(DatasetDerivationCapabilitiesV1.safeParse({ ...capability, authority: true }).success).toBe(false);
    const result = { reference: history, revision: 1, rowCount: 0, status: "complete" };
    expect(DatasetDerivationResultV1.parse(result)).toEqual(result);
    expect(DatasetDerivationResultV1.safeParse({ ...result, rowCount: 100001 }).success).toBe(false);
    expect(DatasetDerivationResultV1.safeParse({ ...result, status: "partial" }).success).toBe(false);
  });

  it("emits the strict wire schema for ordinary tool-schema consumers", () => {
    const schema = z.toJSONSchema(DatasetDerivationRequestV1);
    expect(schema.type).toBe("object");
    expect(schema.additionalProperties).toBe(false);
    expect(schema.properties).toHaveProperty("plan");
  });
});
