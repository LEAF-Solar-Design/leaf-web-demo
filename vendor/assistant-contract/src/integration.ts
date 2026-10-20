import { z } from "zod";

// The final negative lookahead is an absolute end anchor; `$` alone permits a final newline.
const Slug = z.string().min(1).max(128).regex(/^[a-z0-9]+(?:[-_][a-z0-9]+)*(?![\s\S])/);
const FieldId = z.string().min(1).max(128).regex(/^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])/);
const Title = z.string().min(1).max(120);
const DisplayUnit = z.string().min(1).max(40).regex(/^[^\u0000-\u001f\u007f]+(?![\s\S])/);
const Header = z.string().min(1).max(128).regex(/^[!#$%&'*+.^_`|~0-9A-Za-z-]+(?![\s\S])/);
const Pointer = z.string().max(1024).regex(/^(?:\/(?:[^~\/\u0000-\u001f\u007f]|~[01])*)*(?![\s\S])/);
const Cursor = z.string().max(2048);
const Scalar = z.union([z.string(), z.number(), z.boolean(), z.null()]);
const DateOnly = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}(?![\s\S])/).refine(validDate, "Invalid calendar date");
const Instant = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?Z(?![\s\S])/)
  .refine((value) => validDate(value.slice(0, 10)), "Invalid calendar date");

function validDate(value: string): boolean {
  const [year, month, day] = value.split("-").map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1];
}

function unique(values: string[], ctx: z.RefinementCtx, path: (string | number)[]): void {
  const seen = new Set<string>();
  values.forEach((value, index) => {
    if (seen.has(value)) ctx.addIssue({ code: "custom", path: [...path, index], message: "Duplicate identity" });
    seen.add(value);
  });
}

const FieldIds = z.array(FieldId).min(1).max(100).superRefine((ids, ctx) => unique(ids, ctx, []));
const Path = z.string().min(1).max(2048).regex(/^\/(?!\/)[^\\#\u0000-\u0020\u007f]*(?![\s\S])/)
  .refine((value) => {
    try {
      const decoded = decodeURIComponent(value);
      return !/[\\#\u0000-\u0020\u007f]/.test(decoded) && !decoded.startsWith("//") &&
        !decoded.split("?")[0].split("/").some((part) => part === "." || part === "..");
    } catch {
      return false;
    }
  }, "Path must not contain encoded traversal, separators, controls or malformed escapes");
const Origin = z.string().min(1).max(2048).regex(/^https:\/\/[^/?#@\\\s]+(?![\s\S])/)
  .refine((value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && url.port === "" && url.username === "" && url.password === "";
    } catch {
      return false;
    }
  }, "Expected an HTTPS origin on default port 443");
const TimeZone = z.string().min(1).max(128).refine((value) => {
  try {
    // Intl also accepts offset identifiers on some hosts; those are not IANA names.
    if (/^[+-]/.test(value)) return false;
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}, "Expected an IANA time zone");

export const IntegrationRefV1 = z.strictObject({
  id: Slug,
  version: z.number().int().positive(),
  digest: z.string().regex(/^[a-f0-9]{64}(?![\s\S])/),
});
export type IntegrationRefV1 = z.infer<typeof IntegrationRefV1>;

export const CredentialReferenceV1 = z.strictObject({
  id: z.string().uuid(),
  purpose: z.enum(["access_token", "refresh_token", "api_key"]),
  version: z.number().int().min(1).max(2147483647),
});
export type CredentialReferenceV1 = z.infer<typeof CredentialReferenceV1>;

// Metadata only: the authenticated host envelope supplies scope and authority.
export const ConnectionV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  title: Title,
  connector_ref: IntegrationRefV1,
  origin: Origin,
  state: z.enum(["setup", "active", "degraded", "reconnect_required", "disabled", "revoked"]),
  operation_ids: z.array(Slug).min(1).max(100),
  credential_refs: z.array(CredentialReferenceV1).max(3),
  provider_account_id: z.string().min(1).max(256).nullable(),
  identity_verified: z.boolean(),
  grant_revision: z.number().int().positive(),
  owner_id: z.string().uuid(),
  health: z.strictObject({
    checked_at: Instant.nullable(),
    code: z.enum(["unknown", "ok", "auth", "quota", "timeout", "invalid_response", "unavailable", "revoked"]),
  }),
}).superRefine((connection, ctx) => {
  unique(connection.operation_ids, ctx, ["operation_ids"]);
  unique(connection.credential_refs.map((ref) => ref.id.toLowerCase()), ctx, ["credential_refs"]);
  unique(connection.credential_refs.map((ref) => ref.purpose), ctx, ["credential_refs"]);
  if (connection.identity_verified && connection.provider_account_id === null) {
    ctx.addIssue({ code: "custom", path: ["provider_account_id"], message: "Verified identity requires a provider account ID" });
  }
  if (connection.state === "revoked" && connection.health.code !== "revoked") {
    ctx.addIssue({ code: "custom", path: ["health", "code"], message: "Revoked connections require revoked health" });
  }
});
export type ConnectionV1 = z.infer<typeof ConnectionV1>;

const ReservedSegments = new Set(["__proto__", "prototype", "constructor"]);
const MappingFieldId = FieldId.refine((value) => !ReservedSegments.has(value), "Reserved field ID");
const MappingPointer = Pointer.refine((value) => {
  const segments = value === "" ? [] : value.slice(1).split("/");
  return segments.length <= 32 && segments.every((segment) =>
    !ReservedSegments.has(segment.replace(/~1/g, "/").replace(/~0/g, "~")));
}, "Pointer must have at most 32 decoded segments and no reserved segments");

export const TransformMappingV1 = z.strictObject({
  schema_version: z.literal(1),
  fields: z.array(z.strictObject({
    field_id: MappingFieldId,
    pointer: MappingPointer,
    conversion: z.enum(["identity", "string_number", "iso_instant"]),
  })).min(1).max(100),
}).superRefine((mapping, ctx) => unique(mapping.fields.map((field) => field.field_id), ctx, ["fields"]));
export type TransformMappingV1 = z.infer<typeof TransformMappingV1>;

export const DatasetFieldV1 = z.strictObject({
  id: FieldId,
  label: Title,
  // Ordinary unions keep canonical anyOf emission stable across supported Zod
  // versions. Every strict branch still requires its unique kind literal.
  type: z.union([
    z.strictObject({ kind: z.literal("text") }),
    z.strictObject({ kind: z.literal("boolean") }),
    z.strictObject({ kind: z.literal("identifier") }),
    z.strictObject({ kind: z.literal("count") }),
    z.strictObject({ kind: z.literal("number"), unit: DisplayUnit.optional(), precision: z.number().int().min(0).max(12).optional() }),
    z.strictObject({ kind: z.literal("ratio"), scale: z.enum(["fraction", "percent"]) }),
    z.strictObject({ kind: z.literal("money"), currency: z.string().regex(/^[A-Z]{3}(?![\s\S])/), scale: z.enum(["minor", "major"]) }),
    z.strictObject({ kind: z.literal("instant") }),
    z.strictObject({ kind: z.literal("date") }),
    z.strictObject({ kind: z.literal("duration"), unit: z.enum(["ms", "s", "minutes", "hours"]) }),
  ]),
  nullable: z.boolean(),
  sensitivity: z.enum(["public", "internal", "restricted"]),
});
export type DatasetFieldV1 = z.infer<typeof DatasetFieldV1>;

const Fields = z.array(DatasetFieldV1).min(1).max(100)
  .superRefine((fields, ctx) => unique(fields.map((field) => field.id), ctx, []));

export const DatasetDefinitionV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  version: z.number().int().positive(),
  title: Title,
  fields: Fields,
  key_fields: z.array(FieldId).min(1).max(8),
  time_field: FieldId.optional(),
  reporting_time_zone: TimeZone,
  lineage: z.strictObject({
    connection_id: Slug,
    operation_ref: IntegrationRefV1,
    transform_ref: IntegrationRefV1.optional(),
  }),
  retention_days: z.number().int().min(1).max(3650),
}).superRefine((dataset, ctx) => {
  unique(dataset.key_fields, ctx, ["key_fields"]);
  dataset.key_fields.forEach((id, index) => {
    const field = dataset.fields.find((field) => field.id === id);
    if (!field || field.nullable || !["identifier", "text", "count"].includes(field.type.kind)) {
      ctx.addIssue({ code: "custom", path: ["key_fields", index], message: "Key must name a non-null identifier, text or count field" });
    }
  });
  if (dataset.time_field !== undefined) {
    const field = dataset.fields.find((field) => field.id === dataset.time_field);
    if (!field || !["instant", "date"].includes(field.type.kind)) {
      ctx.addIssue({ code: "custom", path: ["time_field"], message: "Time field must name an instant or date field" });
    }
  }
});
export type DatasetDefinitionV1 = z.infer<typeof DatasetDefinitionV1>;

export const OperationV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  title: Title,
  method: z.enum(["GET", "POST", "PUT", "PATCH", "DELETE"]),
  path: Path,
  effect: z.enum(["read", "write", "external_communication", "irreversible"]),
  pagination: z.union([
    z.strictObject({ kind: z.literal("none"), records_pointer: Pointer }),
    z.strictObject({ kind: z.literal("cursor"), records_pointer: Pointer, next_cursor_pointer: Pointer, cursor_parameter: FieldId }),
    z.strictObject({ kind: z.literal("page"), records_pointer: Pointer, page_parameter: FieldId, first_page: z.number().int().min(0), page_size_parameter: FieldId }),
  ]),
  limits: z.strictObject({
    timeout_ms: z.number().int().min(100).max(30000),
    max_response_bytes: z.number().int().min(1).max(5242880),
    max_records: z.number().int().min(1).max(1000),
    max_pages: z.number().int().min(1).max(100),
  }),
  idempotency: z.union([
    z.strictObject({ kind: z.literal("none") }),
    z.strictObject({ kind: z.literal("read") }),
    z.strictObject({ kind: z.literal("provider_key"), header: Header }),
  ]),
  reconciliation_operation_id: Slug.optional(),
}).superRefine((operation, ctx) => {
  if ((operation.effect === "read") !== (operation.idempotency.kind === "read")) {
    ctx.addIssue({ code: "custom", path: ["idempotency"], message: "Read effects require read idempotency; other effects cannot claim it" });
  }
  if (operation.effect === "read" && !["GET", "POST"].includes(operation.method)) {
    ctx.addIssue({ code: "custom", path: ["method"], message: "Only GET or a declared future POST adapter may be read" });
  }
});
export type OperationV1 = z.infer<typeof OperationV1>;

export const ConnectorDefinitionV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  version: z.number().int().positive(),
  title: Title,
  state: z.enum(["draft", "published", "retired"]),
  protocol: z.literal("https-rest"),
  origins: z.array(Origin).min(1).max(8),
  auth: z.union([
    z.strictObject({ kind: z.literal("none") }),
    z.strictObject({ kind: z.literal("bearer") }),
    z.strictObject({ kind: z.literal("api_key"), header: Header }),
  ]),
  operations: z.array(OperationV1).min(1).max(100),
}).superRefine((connector, ctx) => {
  // Origin validation may have failed without aborting this refinement.
  unique(connector.origins.map((origin) => {
    try { return new URL(origin).origin; } catch { return origin; }
  }), ctx, ["origins"]);
  unique(connector.operations.map((operation) => operation.id), ctx, ["operations"]);
  connector.operations.forEach((operation, index) => {
    if (operation.reconciliation_operation_id !== undefined &&
        !connector.operations.some((candidate) => candidate.id === operation.reconciliation_operation_id && candidate.effect === "read")) {
      ctx.addIssue({ code: "custom", path: ["operations", index, "reconciliation_operation_id"], message: "Reconciliation must name a declared read operation" });
    }
  });
});
export type ConnectorDefinitionV1 = z.infer<typeof ConnectorDefinitionV1>;

export const DatasetQueryV1 = z.strictObject({
  fields: FieldIds,
  limit: z.number().int().min(1).max(1000),
  cursor: Cursor.optional(),
  filters: z.array(z.union([
    z.strictObject({ field: FieldId, op: z.enum(["eq", "neq", "gt", "gte", "lt", "lte"]), value: Scalar }),
    z.strictObject({ field: FieldId, op: z.literal("is_null") }),
  ])).max(20).optional(),
  sort: z.array(z.strictObject({ field: FieldId, direction: z.enum(["asc", "desc"]) })).max(4).optional(),
});
export type DatasetQueryV1 = z.infer<typeof DatasetQueryV1>;

function matchesField(field: DatasetFieldV1, value: z.infer<typeof Scalar>): boolean {
  if (value === null) return field.nullable;
  switch (field.type.kind) {
    case "text": return typeof value === "string";
    case "identifier": return typeof value === "string" && value.length > 0;
    case "boolean": return typeof value === "boolean";
    case "date": return DateOnly.safeParse(value).success;
    case "instant": return Instant.safeParse(value).success;
    case "count": return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
    case "money": return typeof value === "number" && Number.isFinite(value) &&
      (field.type.scale === "major" || Number.isSafeInteger(value));
    default: return typeof value === "number" && Number.isFinite(value);
  }
}

export const DatasetReadV1 = z.strictObject({
  schema_version: z.literal(1),
  dataset_ref: IntegrationRefV1,
  fields: Fields,
  rows: z.array(z.strictObject({
    key: z.string().min(1).max(2048),
    values: z.record(FieldId, Scalar),
  })).max(1000),
  next_cursor: Cursor.nullable(),
  status: z.enum(["ready", "empty", "partial", "stale", "unavailable", "forbidden", "invalid", "failed"]),
  freshness: z.strictObject({
    source_updated_at: Instant.nullable(),
    synced_at: Instant.nullable(),
    fetched_at: Instant,
  }),
}).superRefine((read, ctx) => {
  unique(read.rows.map((row) => row.key), ctx, ["rows"]);
  if (["empty", "unavailable", "forbidden", "invalid", "failed"].includes(read.status) && read.rows.length > 0) {
    ctx.addIssue({ code: "custom", path: ["rows"], message: "This status cannot carry rows" });
  }
  const fields = new Map(read.fields.map((field) => [field.id, field]));
  read.rows.forEach((row, index) => {
    for (const id of Object.keys(row.values)) {
      if (!fields.has(id)) ctx.addIssue({ code: "custom", path: ["rows", index, "values", id], message: "Undeclared field" });
    }
    for (const field of read.fields) {
      const present = Object.prototype.hasOwnProperty.call(row.values, field.id);
      if ((!present && !field.nullable) || (present && !matchesField(field, row.values[field.id]))) {
        ctx.addIssue({ code: "custom", path: ["rows", index, "values", field.id], message: "Missing required value or incompatible field value" });
      }
    }
  });
});
export type DatasetReadV1 = z.infer<typeof DatasetReadV1>;

const ElementShape = {
  schema_version: z.literal(1),
  id: Slug,
  title: Title,
  dataset_ref: IntegrationRefV1,
  query: DatasetQueryV1,
};
export const DatasetElementV1 = z.union([
  z.strictObject({
    ...ElementShape,
    kind: z.literal("dataset.table"),
    props: z.strictObject({ columns: FieldIds, row_limit: z.number().int().min(1).max(1000) }),
  }),
  z.strictObject({
    ...ElementShape,
    kind: z.literal("dataset.line"),
    props: z.strictObject({ x_field: FieldId, y_field: FieldId }),
  }),
]);
export type DatasetElementV1 = z.infer<typeof DatasetElementV1>;
