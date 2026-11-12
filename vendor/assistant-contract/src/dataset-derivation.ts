import { z } from "zod";
import { DatasetDefinitionV1, DatasetFieldV1, DatasetQueryV1, DatasetReadV1, IntegrationRefV1 } from "./integration";

// Separate entry: this contract does not expand V1 raw queries or grant authority.
const unsafe = new Set(["__proto__", "prototype", "constructor"]);
const reserved = new Set(["aggregate_key", "window_id"]);
const field = DatasetFieldV1.shape.id.refine(id => !unsafe.has(id), "Unsafe field identity");
const outputField = field.refine(id => !reserved.has(id), "Reserved output identity");
const title = DatasetFieldV1.shape.label;
const positive = z.number().int().min(1).max(2_147_483_647);
const filters = DatasetQueryV1.shape.filters.unwrap();
const alias = z.enum(["primary", "events", "domain"]);
const numeric = new Set(["number", "count", "ratio", "money", "duration"]);

function unique(values: string[], ctx: z.RefinementCtx, path: (string | number)[]) {
  const seen = new Set<string>();
  values.forEach((value, index) => {
    if (seen.has(value)) ctx.addIssue({ code: "custom", path: [...path, index], message: "Duplicate identity" });
    seen.add(value);
  });
}
const distinctFields = z.array(field).min(1).max(8).superRefine((ids, ctx) => unique(ids, ctx, []));
const count = z.strictObject({ as: outputField, label: title, distinct_by: distinctFields.optional() });
const groupKey = z.strictObject({
  field, as: outputField, bucket: z.enum(["value", "day"]),
  time_zone: DatasetDefinitionV1.shape.reporting_time_zone.optional(),
}).superRefine((key, ctx) => {
  if (key.bucket === "value" && key.time_zone !== undefined)
    ctx.addIssue({ code: "custom", path: ["time_zone"], message: "Value grouping has no timezone conversion" });
});

export const GroupCountV1 = z.strictObject({
  kind: z.literal("group_count"), filters: filters.optional(),
  group_by: z.array(groupKey).max(8), count,
  windows: z.array(z.strictObject({ id: IntegrationRefV1.shape.id, filters })).min(1).max(4),
  domain: z.strictObject({
    keys: z.array(z.strictObject({ group_field: outputField, domain_field: field })).max(8),
    order_field: field.optional(),
  }).optional(),
}).superRefine((plan, ctx) => {
  unique([...plan.group_by.map(key => key.as), plan.count.as], ctx, ["output_fields"]);
  unique(plan.windows.map(window => window.id), ctx, ["windows"]);
  if (plan.domain) {
    const expected = plan.group_by.map(key => key.as);
    const keys = plan.domain.keys.map(key => key.group_field);
    unique(keys, ctx, ["domain", "keys"]);
    if (keys.length !== expected.length || expected.some(id => !keys.includes(id)))
      ctx.addIssue({ code: "custom", path: ["domain", "keys"], message: "Domain must cover every output group key exactly once" });
  }
});
export type GroupCountV1 = z.infer<typeof GroupCountV1>;

export const WindowCountV1 = z.strictObject({
  kind: z.literal("window_count"), anchor_fields: z.array(outputField).max(8),
  anchor_filters: filters.optional(), at_field: field, event_time_field: field,
  event_filters: filters.optional(),
  partition_by: z.array(z.strictObject({ anchor_field: field, event_field: field })).max(8),
  lookback_seconds: z.number().int().min(1).max(315_360_000),
  lower_inclusive: z.boolean(), upper_inclusive: z.boolean(), count,
}).superRefine((plan, ctx) => {
  unique(plan.anchor_fields, ctx, ["anchor_fields"]);
  unique([...plan.anchor_fields, plan.count.as], ctx, ["output_fields"]);
  unique(plan.partition_by.map(pair => pair.anchor_field), ctx, ["partition_by", "anchor_field"]);
  unique(plan.partition_by.map(pair => pair.event_field), ctx, ["partition_by", "event_field"]);
});
export type WindowCountV1 = z.infer<typeof WindowCountV1>;

export const DatasetDerivationPlanV1 = z.union([GroupCountV1, WindowCountV1]);
export const DatasetQuotientV1 = z.strictObject({
  as: outputField, label: title, reason_as: outputField, reason_label: title,
  numerator: field, denominator: field, multiplier: z.number().finite().nonnegative(),
  unit: z.string().min(1).max(40).regex(/^[^\u0000-\u001f\u007f]+(?![\s\S])/).optional(),
});
const computed = z.array(DatasetQuotientV1).max(4);
type Plan = z.infer<typeof DatasetDerivationPlanV1>;
type Quotient = z.infer<typeof DatasetQuotientV1>;

function outputNames(plan: Plan, calculations: Quotient[], ctx: z.RefinementCtx) {
  const names = plan.kind === "group_count" ? plan.group_by.map(key => key.as) : plan.anchor_fields;
  unique([...names, plan.count.as, ...calculations.flatMap(item => [item.as, item.reason_as])], ctx, ["output_fields"]);
}
function inputRoles(plan: Plan, roles: string[], ctx: z.RefinementCtx) {
  const expected = ["primary", ...(plan.kind === "window_count" ? ["events"] : plan.domain ? ["domain"] : [])];
  unique(roles, ctx, ["inputs"]);
  if (roles.length !== expected.length || expected.some(role => !roles.includes(role)))
    ctx.addIssue({ code: "custom", path: ["inputs"], message: "Input roles must match the selected operation" });
}

/** Inspect data descriptors first; size checks must not invoke caller getters/toJSON. */
function jsonPayload(maximum: number) {
  return z.unknown().superRefine((input, ctx) => {
    const ancestors = new Set<object>();
    let nodes = 0;
    function visit(value: unknown, depth: number): boolean {
      if (++nodes > 30_000 || depth > 32) return false;
      if (value === null || typeof value === "boolean") return true;
      if (typeof value === "number") return Number.isFinite(value);
      if (typeof value === "string") return value.length <= maximum;
      if (typeof value !== "object" || ancestors.has(value)) return false;
      const array = Array.isArray(value), proto = Object.getPrototypeOf(value);
      if (array ? proto !== Array.prototype : proto !== Object.prototype && proto !== null) return false;
      ancestors.add(value);
      let entries = 0;
      for (const key of Reflect.ownKeys(value)) {
        if (array && key === "length") continue;
        if (typeof key !== "string" || unsafe.has(key) || (array && !/^(?:0|[1-9]\d*)$/.test(key))) return false;
        const descriptor = Object.getOwnPropertyDescriptor(value, key)!;
        if (!("value" in descriptor) || !descriptor.enumerable || !visit(descriptor.value, depth + 1)) return false;
        entries++;
      }
      ancestors.delete(value);
      return !array || entries === value.length;
    }
    try {
      if (visit(input, 0) && new TextEncoder().encode(JSON.stringify(input)).length <= maximum) return;
    } catch { /* Invalid object shape is a validation failure. */ }
    ctx.addIssue({ code: "custom", message: "Expected bounded plain JSON data" });
  });
}

const request = z.strictObject({
  schema_version: z.literal(1),
  output: z.strictObject({ id: IntegrationRefV1.shape.id, version: positive, title }),
  inputs: z.strictObject({ primary: IntegrationRefV1, events: IntegrationRefV1.optional(), domain: IntegrationRefV1.optional() }),
  plan: DatasetDerivationPlanV1, computed: computed.optional(),
}).superRefine((value, ctx) => {
  inputRoles(value.plan, Object.keys(value.inputs), ctx);
  outputNames(value.plan, value.computed ?? [], ctx);
  if (new TextEncoder().encode(JSON.stringify(value.plan)).length > 65_536)
    ctx.addIssue({ code: "custom", path: ["plan"], message: "Plan exceeds 64 KiB" });
});
export const DatasetDerivationRequestV1 = jsonPayload(163_840).pipe(request);
export type DatasetDerivationRequestV1 = z.infer<typeof DatasetDerivationRequestV1>;

export const DatasetDerivationCapabilitiesV1 = z.strictObject({
  schema_version: z.literal(1), contract: z.literal("dataset-derivation-v1"),
  operations: z.tuple([z.literal("group_count"), z.literal("window_count")]),
  max_inputs: z.literal(2), max_records_per_input: z.literal(100_000), max_windows: z.literal(4),
});
export const DatasetDerivationResultV1 = z.strictObject({
  reference: IntegrationRefV1, revision: positive,
  rowCount: z.number().int().min(0).max(100_000), status: z.literal("complete"),
});
export type DatasetDerivationResultV1 = z.infer<typeof DatasetDerivationResultV1>;

// Match the actual project_material producer, without inventing original_version.
export const ProjectMaterialBindingV1 = z.strictObject({
  kind: z.literal("project_material"),
  material_id: z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/i),
  sha256: IntegrationRefV1.shape.digest, byte_length: z.number().int().min(1).max(8_388_608),
});
export const ProjectMaterialDatasetDefinitionV1 = DatasetDefinitionV1.safeExtend({ lineage: ProjectMaterialBindingV1 });
const sourceBinding = z.strictObject({
  alias, dataset_ref: IntegrationRefV1, resource_revision: positive, original_binding: ProjectMaterialBindingV1,
});
export const DatasetDerivationLineageV1 = z.strictObject({
  kind: z.literal("derived"), contract_version: z.literal(1), inputs: z.array(sourceBinding).min(1).max(2),
  plan: DatasetDerivationPlanV1, computed: computed.optional(),
  engine_revision: z.string().min(1).max(128).regex(/^[^\u0000-\u001f\u007f]+(?![\s\S])/),
  derivation_digest: IntegrationRefV1.shape.digest,
}).superRefine((value, ctx) => {
  inputRoles(value.plan, value.inputs.map(input => input.alias), ctx);
  outputNames(value.plan, value.computed ?? [], ctx);
}).describe("Host-produced lineage, not authority or proof of hashes. The host binds current tenant/project, stored references and canonical originals.");
export const DerivedDatasetDefinitionV1 = DatasetDefinitionV1.safeExtend({ lineage: DatasetDerivationLineageV1 })
  .superRefine((definition, ctx) => {
    const { plan, computed: calculations = [] } = definition.lineage;
    const names = plan.kind === "group_count" ? [...plan.group_by.map(key => key.as), "window_id"] : plan.anchor_fields;
    const expected = new Set(["aggregate_key", ...names, plan.count.as, ...calculations.flatMap(item => [item.as, item.reason_as])]);
    const fields = new Map(definition.fields.map(item => [item.id, item]));
    if (fields.size !== expected.size || [...expected].some(id => !fields.has(id)) ||
        definition.key_fields.length !== 1 || definition.key_fields[0] !== "aggregate_key")
      ctx.addIssue({ code: "custom", path: ["fields"], message: "Derived fields and identity must match the plan outputs" });
    const requireType = (id: string, kind: string, nullable: boolean) => {
      const item = fields.get(id);
      if (!item || item.type.kind !== kind || item.nullable !== nullable)
        ctx.addIssue({ code: "custom", path: ["fields", id], message: "Invalid generated output type" });
    };
    requireType("aggregate_key", "identifier", false);
    requireType(plan.count.as, "count", false);
    if (plan.kind === "group_count") {
      requireType("window_id", "identifier", false);
      for (const key of plan.group_by) if (key.bucket === "day") {
        const item = fields.get(key.as);
        if (!item || item.type.kind !== "date") ctx.addIssue({ code: "custom", path: ["fields", key.as], message: "Day output must be a date" });
      }
    }
    for (const calculation of calculations) {
      requireType(calculation.as, "number", true);
      requireType(calculation.reason_as, "text", false);
    }
  });
export type DerivedDatasetDefinitionV1 = z.infer<typeof DerivedDatasetDefinitionV1>;

const loadedInput = z.strictObject({
  alias, dataset_ref: IntegrationRefV1, resource_revision: positive, definition: ProjectMaterialDatasetDefinitionV1,
});
type Input = z.infer<typeof loadedInput>;
type Field = z.infer<typeof DatasetFieldV1>;
const sameRef = (a: z.infer<typeof IntegrationRefV1>, b: z.infer<typeof IntegrationRefV1>) =>
  a.id === b.id && a.version === b.version && a.digest === b.digest;
function compatible(a: Field, b: Field): boolean {
  if (["text", "identifier"].includes(a.type.kind) && ["text", "identifier"].includes(b.type.kind)) return true;
  if (a.type.kind !== b.type.kind) return false;
  const left = { ...a.type }, right = { ...b.type };
  if (left.kind === "number") delete left.precision;
  if (right.kind === "number") delete right.precision;
  return JSON.stringify(left) === JSON.stringify(right);
}

/** Validates host-loaded descriptors; scope, record digests and material access remain host checks. */
export const DatasetDerivationBoundRequestV1 = jsonPayload(524_288).pipe(z.strictObject({
  request: DatasetDerivationRequestV1, bindings: z.array(loadedInput).min(1).max(2),
}).superRefine(({ request: value, bindings }, ctx) => {
  const fail = (message: string, path: (string | number)[] = []) => ctx.addIssue({ code: "custom", path, message });
  inputRoles(value.plan, bindings.map(input => input.alias), ctx);
  const inputs = new Map(bindings.map(input => [input.alias, input]));
  for (const input of bindings) {
    const expected = value.inputs[input.alias];
    if (!expected || !sameRef(expected, input.dataset_ref) || input.definition.id !== input.dataset_ref.id ||
        input.definition.version !== input.dataset_ref.version) fail("Loaded input does not match the exact requested reference", ["bindings"]);
    if (input.definition.fields.some(item => unsafe.has(item.id))) fail("Unsafe source field identity", ["bindings"]);
  }
  const getField = (input: Input | undefined, id: string): Field | undefined => {
    const found = input?.definition.fields.find(item => item.id === id);
    if (!found) fail("Unknown input field", ["plan", id]);
    return found;
  };
  const checkFilters = (input: Input | undefined, list: z.infer<typeof filters> = []) => {
    for (const filter of list) {
      const item = getField(input, filter.field);
      if (!item || filter.op === "is_null") continue;
      if (!DatasetReadV1.safeParse({ schema_version: 1, dataset_ref: input!.dataset_ref, fields: [item],
        rows: [{ key: "validation", values: { [item.id]: filter.value } }], next_cursor: null, status: "ready",
        freshness: { fetched_at: "2000-01-01T00:00:00Z", synced_at: null, source_updated_at: null } }).success)
        fail("Filter value does not match its source field", ["plan", filter.field]);
    }
  };
  const primary = inputs.get("primary"), plan = value.plan;
  const outputs = new Map<string, Field>();
  const countInput = plan.kind === "group_count" ? primary : inputs.get("events");
  for (const id of plan.count.distinct_by ?? countInput?.definition.key_fields ?? []) {
    const item = getField(countInput, id);
    if (item?.nullable) fail("Distinct identity fields must be non-null", ["plan", "count"]);
  }
  if (plan.kind === "group_count") {
    checkFilters(primary, plan.filters);
    for (const window of plan.windows) checkFilters(primary, window.filters);
    for (const key of plan.group_by) {
      const item = getField(primary, key.field);
      if (!item) continue;
      if (key.bucket === "day" && (!["instant", "date"].includes(item.type.kind) ||
          (item.type.kind === "instant" ? key.time_zone === undefined : key.time_zone !== undefined)))
        fail("Day grouping requires an instant with timezone or a date without conversion", ["plan", "group_by"]);
      outputs.set(key.as, { ...item, id: key.as, ...(key.bucket === "day" ? { type: { kind: "date" as const } } : {}) });
    }
    if (plan.domain) {
      const domain = inputs.get("domain");
      for (const key of plan.domain.keys) {
        const source = outputs.get(key.group_field), dimension = getField(domain, key.domain_field);
        if (source && dimension && (dimension.nullable || !compatible(source, dimension))) fail("Domain key types must match group output", ["plan", "domain"]);
      }
      if (plan.domain.order_field) {
        const order = getField(domain, plan.domain.order_field);
        if (order && (order.nullable || !["count", "number"].includes(order.type.kind))) fail("Domain order must be a non-null count or number", ["plan", "domain"]);
      }
    }
  } else {
    const events = inputs.get("events");
    checkFilters(primary, plan.anchor_filters); checkFilters(events, plan.event_filters);
    const at = getField(primary, plan.at_field), time = getField(events, plan.event_time_field);
    if (at && (at.type.kind !== "instant" || at.nullable)) fail("Anchor time must be a non-null instant", ["plan", "at_field"]);
    if (time && time.type.kind !== "instant") fail("Event time must be an instant", ["plan", "event_time_field"]);
    for (const pair of plan.partition_by) {
      const a = getField(primary, pair.anchor_field), b = getField(events, pair.event_field);
      if (a && b && (a.nullable || b.nullable || !compatible(a, b))) fail("Partition fields must have compatible non-null types", ["plan", "partition_by"]);
    }
    for (const id of plan.anchor_fields) { const item = getField(primary, id); if (item) outputs.set(id, item); }
  }
  outputs.set(plan.count.as, { id: plan.count.as, label: plan.count.label, type: { kind: "count" }, nullable: false, sensitivity: "restricted" });
  for (const calculation of value.computed ?? []) for (const id of [calculation.numerator, calculation.denominator]) {
    const operand = outputs.get(id);
    if (!operand || !numeric.has(operand.type.kind)) fail("Quotient operands must name numeric operation outputs, not other calculations", ["computed", id]);
  }
}));
