import { describe, expect, it } from "vitest";
import {
  ConnectorDefinitionV1, DatasetDefinitionV1, DatasetElementV1, DatasetFieldV1,
  DatasetQueryV1, DatasetReadV1, IntegrationRefV1, OperationV1,
} from "../src/index";

const ref = { id: "sales", version: 1, digest: "ab".repeat(32) };
const fields: DatasetFieldV1[] = [
  { id: "id", label: "ID", type: { kind: "identifier" }, nullable: false, sensitivity: "internal" },
  { id: "day", label: "Day", type: { kind: "date" }, nullable: false, sensitivity: "public" },
  { id: "total", label: "Total", type: { kind: "money", currency: "USD", scale: "minor" }, nullable: true, sensitivity: "restricted" },
];
const operation = {
  schema_version: 1, id: "list-sales", title: "List sales", method: "GET", path: "/v1/sales",
  effect: "read", pagination: { kind: "none", records_pointer: "/data" },
  limits: { timeout_ms: 1000, max_response_bytes: 1024, max_records: 100, max_pages: 1 },
  idempotency: { kind: "read" },
};
const connector = {
  schema_version: 1, id: "sales", version: 1, title: "Sales", state: "draft", protocol: "https-rest",
  origins: ["https://api.example.com"], auth: { kind: "bearer" }, operations: [operation],
};
const dataset = {
  schema_version: 1, id: "sales", version: 1, title: "Sales", fields, key_fields: ["id"], time_field: "day",
  reporting_time_zone: "America/Chicago", lineage: { connection_id: "sales-account", operation_ref: ref }, retention_days: 30,
};
const query = { fields: ["day", "total"], limit: 100, filters: [{ field: "total", op: "gte", value: 0 }], sort: [{ field: "day", direction: "asc" }] };
const read = {
  schema_version: 1, dataset_ref: ref, fields,
  rows: [{ key: "sale-1", values: { id: "sale-1", day: "2024-02-29", total: null } }],
  next_cursor: null, status: "ready",
  freshness: { source_updated_at: null, synced_at: null, fetched_at: "2026-10-20T12:30:00Z" },
};
const element = { schema_version: 1, id: "sales-view", title: "Sales", dataset_ref: ref, query };

describe("integration contracts", () => {
  it("round trips a read connector, dataset, query, read and both recipes", () => {
    const samples = [
      [IntegrationRefV1, ref], [OperationV1, operation], [ConnectorDefinitionV1, connector],
      [DatasetDefinitionV1, dataset], [DatasetQueryV1, query], [DatasetReadV1, read],
      [DatasetElementV1, { ...element, kind: "dataset.table", props: { columns: ["day", "total"], row_limit: 100 } }],
      [DatasetElementV1, { ...element, kind: "dataset.line", props: { x_field: "day", y_field: "total" } }],
    ] as const;
    for (const [schema, sample] of samples) expect(schema.parse(JSON.parse(JSON.stringify(sample)))).toEqual(sample);
    expect(DatasetDefinitionV1.safeParse({ ...dataset, reporting_time_zone: "UTC" }).success).toBe(true);
  });

  it("constructing and mutating a new read leaves earlier serialized snapshots unchanged", () => {
    const first = DatasetReadV1.parse(read);
    const snapshot = JSON.stringify(first);
    const second = DatasetReadV1.parse(read);
    second.rows[0].values.total = 123;
    second.fields[0].label = "Changed";
    expect(JSON.stringify(first)).toBe(snapshot);
    expect(JSON.stringify(read)).toBe(snapshot);
  });

  it("refuses unknown keys and secret/authority-bearing extensions", () => {
    for (const [schema, sample] of [
      [IntegrationRefV1, ref], [DatasetFieldV1, fields[0]], [DatasetDefinitionV1, dataset],
      [OperationV1, operation], [ConnectorDefinitionV1, connector], [DatasetQueryV1, query], [DatasetReadV1, read],
      [DatasetElementV1, { ...element, kind: "dataset.line", props: { x_field: "day", y_field: "total" } }],
    ] as const) expect(schema.safeParse({ ...sample, authority: true }).success).toBe(false);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, auth: { kind: "bearer", token: "secret" } }).success).toBe(false);
    expect(DatasetReadV1.safeParse({ ...read, rows: [{ ...read.rows[0], extra: true }] }).success).toBe(false);
  });

  it("rejects trailing controls in syntactic identifiers and UTC instants", () => {
    expect(IntegrationRefV1.safeParse({ ...ref, id: "sales\n" }).success).toBe(false);
    expect(IntegrationRefV1.safeParse({ ...ref, digest: `${ref.digest}\n` }).success).toBe(false);
    expect(DatasetFieldV1.safeParse({ ...fields[0], id: "id\n" }).success).toBe(false);
    expect(DatasetFieldV1.safeParse({ ...fields[2], type: { kind: "money", currency: "USD\n", scale: "minor" } }).success).toBe(false);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, auth: { kind: "api_key", header: "X-Key\n" } }).success).toBe(false);
    expect(DatasetReadV1.safeParse({ ...read, freshness: { ...read.freshness, fetched_at: "2026-10-20T12:00:00Z\n" } }).success).toBe(false);
  });

  it("accepts cursor and page pagination while rejecting incompatible parameters", () => {
    for (const pagination of [
      { kind: "cursor", records_pointer: "/data", next_cursor_pointer: "/next", cursor_parameter: "after" },
      { kind: "page", records_pointer: "/data", page_parameter: "page", first_page: 0, page_size_parameter: "size" },
    ]) expect(OperationV1.safeParse({ ...operation, pagination }).success).toBe(true);
    expect(OperationV1.safeParse({ ...operation, pagination: { ...operation.pagination, cursor_parameter: "after" } }).success).toBe(false);
    expect(OperationV1.safeParse({
      ...operation, pagination: { kind: "cursor", records_pointer: "/data", next_cursor_pointer: "/next", cursor_parameter: "after=value" },
    }).success).toBe(false);
  });

  it.each(["/a/~2", "data", "/~", "/a\n"])("rejects invalid JSON pointer %j", (pointer) => {
    expect(OperationV1.safeParse({ ...operation, pagination: { kind: "none", records_pointer: pointer } }).success).toBe(false);
  });
  it.each(["", "/a~1b/~0", "/"])("accepts JSON pointer %j", (pointer) => {
    expect(OperationV1.safeParse({ ...operation, pagination: { kind: "none", records_pointer: pointer } }).success).toBe(true);
  });
  it.each(["https://example.com/a", "//example.com", "/a#b", "/a\\b", "/../a", "/a/./b", "/%2e%2e/a", "/a%2f..%2fb", "/%5cfoo", "/a%0ab", "/a\n", "/%zz"])(
    "rejects unsafe path %j", (path) => expect(OperationV1.safeParse({ ...operation, path }).success).toBe(false),
  );
  it.each(["http://example.com", "https://user:pass@example.com", "https://example.com/", "https://example.com?x", "https://example.com#x", "https://example.com:8443", "https://example.com\\evil"])(
    "rejects invalid origin %j", (origin) => expect(ConnectorDefinitionV1.safeParse({ ...connector, origins: [origin] }).success).toBe(false),
  );

  it("rejects duplicates and invalid field/reconciliation references", () => {
    for (const patch of [
      { fields: [...fields, fields[0]] }, { key_fields: ["id", "id"] }, { key_fields: ["unknown"] },
      { key_fields: ["total"] }, { time_field: "id" }, { time_field: "unknown" }, { reporting_time_zone: "Mars/Olympus" },
      { fields: fields.map((field) => ({ ...field, nullable: true })) },
    ]) expect(DatasetDefinitionV1.safeParse({ ...dataset, ...patch }).success).toBe(false);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, operations: [operation, operation] }).success).toBe(false);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, origins: ["https://EXAMPLE.com", "https://example.com:443"] }).success).toBe(false);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, operations: [{ ...operation, reconciliation_operation_id: "missing" }] }).success).toBe(false);
    const write = { ...operation, id: "write-sale", method: "POST", effect: "write", idempotency: { kind: "none" } };
    expect(ConnectorDefinitionV1.safeParse({ ...connector, operations: [operation, { ...write, reconciliation_operation_id: operation.id }] }).success).toBe(true);
    expect(ConnectorDefinitionV1.safeParse({ ...connector, operations: [operation, { ...write, reconciliation_operation_id: write.id }] }).success).toBe(false);
  });

  it("enforces declared effects and idempotency without granting execution", () => {
    expect(OperationV1.safeParse({ ...operation, method: "POST" }).success).toBe(true);
    expect(OperationV1.safeParse({ ...operation, effect: "write" }).success).toBe(false);
    expect(OperationV1.safeParse({ ...operation, idempotency: { kind: "none" } }).success).toBe(false);
    expect(OperationV1.safeParse({ ...operation, method: "DELETE" }).success).toBe(false);
    for (const kind of ["none", "provider_key"]) {
      const idempotency = kind === "none" ? { kind } : { kind, header: "Idempotency-Key" };
      expect(OperationV1.safeParse({ ...operation, method: "POST", effect: "write", idempotency }).success).toBe(true);
    }
  });

  it("keeps units, currency and scales on the right descriptors", () => {
    for (const type of [
      { kind: "number", currency: "USD" }, { kind: "money", currency: "usd", scale: "minor" },
      { kind: "money", currency: "USD", scale: "fraction" }, { kind: "ratio", scale: "minor" },
      { kind: "ratio", scale: "fraction", unit: "USD" }, { kind: "duration", unit: "days" },
      { kind: "boolean", unit: "s" }, { kind: "number", precision: 13 },
    ]) expect(DatasetFieldV1.safeParse({ ...fields[0], type }).success).toBe(false);
  });

  it("validates each field's values without coercion", () => {
    const cases: Array<[DatasetFieldV1["type"], unknown, unknown[]]> = [
      [{ kind: "text" }, "text", [42, false]],
      [{ kind: "identifier" }, "abc", ["", 1]],
      [{ kind: "boolean" }, false, ["false", 0]],
      [{ kind: "count" }, 0, [-1, 1.5, "1", Infinity]],
      [{ kind: "number" }, 1.5, ["1.5", Infinity, NaN]],
      [{ kind: "money", currency: "USD", scale: "minor" }, -10, [0.5, "10"]],
      [{ kind: "money", currency: "USD", scale: "major" }, 0.5, ["0.5", Infinity]],
      [{ kind: "ratio", scale: "fraction" }, 0.5, ["50%", Infinity]],
      [{ kind: "duration", unit: "s" }, 1.25, ["1s", Infinity]],
      [{ kind: "date" }, "2000-02-29", ["1900-02-29", "2025-02-29", "2026-04-31", "2026-00-01", "2026-01-00"]],
      [{ kind: "instant" }, "2026-10-20T12:00:00.123456Z", ["2026-02-30T12:00:00Z", "2026-10-20T24:00:00Z", "2026-10-20T12:00:00+01:00"]],
    ];
    for (const [type, good, bad] of cases) {
      const sample = (value: unknown) => ({ ...read, fields: [{ ...fields[0], type }], rows: [{ key: "1", values: { id: value } }] });
      expect(DatasetReadV1.safeParse(sample(good)).success).toBe(true);
      for (const value of [...bad, null]) expect(DatasetReadV1.safeParse(sample(value)).success).toBe(false);
    }
  });

  it("enforces row identities, required values, freshness and status", () => {
    for (const rows of [
      [read.rows[0], read.rows[0]],
      [{ key: "1", values: { day: "2024-02-29" } }],
      [{ key: "1", values: { ...read.rows[0].values, unknown: 1 } }],
    ]) expect(DatasetReadV1.safeParse({ ...read, rows }).success).toBe(false);
    for (const status of ["empty", "unavailable", "forbidden", "invalid", "failed"]) {
      expect(DatasetReadV1.safeParse({ ...read, status }).success).toBe(false);
      expect(DatasetReadV1.safeParse({ ...read, status, rows: [] }).success).toBe(true);
    }
    for (const status of ["stale", "partial"]) expect(DatasetReadV1.safeParse({ ...read, status }).success).toBe(true);
    expect(DatasetReadV1.parse(read).rows[0].values.total).toBeNull();
    expect(DatasetReadV1.safeParse({ ...read, freshness: { ...read.freshness, fetched_at: "2026-02-30T00:00:00Z" } }).success).toBe(false);
  });

  it("bounds queries and rejects expressions and is_null operands", () => {
    for (const patch of [
      { fields: ["id", "id"] }, { fields: ["sum(total)"] }, { limit: 1001 }, { cursor: "x".repeat(2049) },
      { filters: [{ field: "total", op: "is_null", value: null }] },
      { filters: [{ field: "total", op: "eq" }] }, { filters: [{ field: "total", op: "eq", value: {} }] },
      { filters: Array(21).fill({ field: "total", op: "is_null" }) },
      { sort: Array(5).fill({ field: "day", direction: "asc" }) },
    ]) expect(DatasetQueryV1.safeParse({ ...query, ...patch }).success).toBe(false);
    expect(DatasetQueryV1.safeParse({ ...query, filters: [{ field: "total", op: "is_null" }] }).success).toBe(true);
  });
});
