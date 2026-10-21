import { describe, expect, it } from "vitest";
import { ConnectionV1, CredentialReferenceV1, SCHEMA_DEFS, TransformMappingV1 } from "../src/index";

const credential: CredentialReferenceV1 = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", purpose: "access_token", version: 1,
};
const connection: ConnectionV1 = {
  schema_version: 1, id: "sales-account", title: "Sales account",
  connector_ref: { id: "sales", version: 1, digest: "ab".repeat(32) },
  origin: "https://api.example.com", state: "setup", operation_ids: ["list-sales"],
  credential_refs: [credential], provider_account_id: null, identity_verified: false,
  grant_revision: 1, owner_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  health: { checked_at: null, code: "unknown" },
};
const mapping: TransformMappingV1 = {
  schema_version: 1,
  fields: [
    { field_id: "id", pointer: "/data/id", conversion: "identity" },
    { field_id: "total", pointer: "/data/amount", conversion: "string_number" },
    { field_id: "created_at", pointer: "/data/created_at", conversion: "iso_instant" },
  ],
};
const mappedPointer = (pointer: string) => ({ ...mapping, fields: [{ ...mapping.fields[0], pointer }] });

describe("connection metadata", () => {
  it("exports all three definitions and round trips the exact mapping and references", () => {
    for (const [schema, sample] of [
      [CredentialReferenceV1, credential], [ConnectionV1, connection], [TransformMappingV1, mapping],
    ] as const) expect(schema.parse(JSON.parse(JSON.stringify(sample)))).toEqual(sample);
    expect(SCHEMA_DEFS.CredentialReferenceV1).toBe(CredentialReferenceV1);
    expect(SCHEMA_DEFS.ConnectionV1).toBe(ConnectionV1);
    expect(SCHEMA_DEFS.TransformMappingV1).toBe(TransformMappingV1);
  });

  it("accepts lifecycle states, empty credentials, all purposes and health codes", () => {
    for (const state of ["setup", "active", "degraded", "reconnect_required", "disabled", "revoked"] as const) {
      const sample = { ...connection, state, credential_refs: [], health: { checked_at: null, code: state === "revoked" ? "revoked" : "unknown" } };
      expect(ConnectionV1.safeParse(sample).success).toBe(true);
    }
    const credential_refs = ["access_token", "refresh_token", "api_key"].map((purpose, index) => ({
      ...credential, id: `${index}aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`, purpose,
    }));
    for (const code of ["unknown", "ok", "auth", "quota", "timeout", "invalid_response", "unavailable", "revoked"]) {
      expect(ConnectionV1.safeParse({
        ...connection, credential_refs, provider_account_id: "provider-42", identity_verified: true,
        health: { checked_at: "2024-02-29T12:00:00.123456Z", code },
      }).success).toBe(true);
    }
    expect(CredentialReferenceV1.safeParse({ ...credential, version: 2147483647 }).success).toBe(true);
  });

  it("rejects invalid identity, lifecycle, revisions, health times and connector references", () => {
    for (const patch of [
      { identity_verified: true }, { provider_account_id: "" }, { provider_account_id: "x".repeat(257) },
      { state: "revoked" }, { state: "connected" }, { schema_version: 2 }, { owner_id: "owner-1" },
      { id: "Sales" }, { title: "" }, { title: "x".repeat(121) },
      { grant_revision: 0 }, { grant_revision: 1.5 }, { grant_revision: "1" },
      { connector_ref: { ...connection.connector_ref, digest: "bad" } },
      { health: { checked_at: null, code: "healthy" } },
      { health: { checked_at: null, code: "ok", detail: "extra" } },
    ]) expect(ConnectionV1.safeParse({ ...connection, ...patch }).success).toBe(false);
    for (const checked_at of ["2026-02-30T00:00:00Z", "2026-10-20T24:00:00Z", "2026-10-20T00:00:00+00:00", "2026-10-20T00:00:00Z\n"]) {
      expect(ConnectionV1.safeParse({ ...connection, health: { checked_at, code: "ok" } }).success).toBe(false);
    }
    for (const patch of [{ id: "key-1" }, { purpose: "password" }, { version: 0 }, { version: 1.5 }, { version: 2147483648 }]) {
      expect(CredentialReferenceV1.safeParse({ ...credential, ...patch }).success).toBe(false);
    }
  });

  it("rejects duplicate grants, duplicate credential IDs/purposes and oversized arrays", () => {
    for (const operation_ids of [[], ["list-sales", "list-sales"], ["invalid.slug"], Array.from({ length: 101 }, (_, i) => `op-${i}`)]) {
      expect(ConnectionV1.safeParse({ ...connection, operation_ids }).success).toBe(false);
    }
    expect(ConnectionV1.safeParse({ ...connection, operation_ids: Array.from({ length: 100 }, (_, i) => `op-${i}`) }).success).toBe(true);
    for (const credential_refs of [
      [credential, credential],
      [credential, { ...credential, id: credential.id.toUpperCase(), purpose: "refresh_token" }],
      [credential, { ...credential, id: connection.owner_id }],
      Array(4).fill(credential),
    ]) expect(ConnectionV1.safeParse({ ...connection, credential_refs }).success).toBe(false);
  });

  it.each([
    "http://example.com", "https://user:pass@example.com", "https://user@example.com",
    "https://example.com/", "https://example.com/path", "https://example.com?", "https://example.com#",
    "https://example.com:8443", "https://example.com\\evil", "https://example.com\n", "https://",
  ])("rejects unsafe origin %j", (origin) => {
    expect(ConnectionV1.safeParse({ ...connection, origin }).success).toBe(false);
  });

  it("refuses secret-bearing and scope-bearing extensions at each strict boundary", () => {
    for (const key of ["value", "secret", "access_token", "environment", "uri", "file_path", "key_id", "provider_metadata", "organization_id", "permissions"]) {
      expect(CredentialReferenceV1.safeParse({ ...credential, [key]: "forbidden" }).success).toBe(false);
      expect(ConnectionV1.safeParse({ ...connection, [key]: "forbidden" }).success).toBe(false);
      expect(ConnectionV1.safeParse({ ...connection, credential_refs: [{ ...credential, [key]: "forbidden" }] }).success).toBe(false);
    }
    expect(ConnectionV1.safeParse({ ...connection, connector_ref: { ...connection.connector_ref, token: "forbidden" } }).success).toBe(false);
  });
});

describe("declarative transform mapping", () => {
  it.each(["", "/", "/data/0/id", "/a~1b/~0", "/~01", "/a//b", "/a~1constructor"])("accepts RFC 6901 pointer %j", (pointer) => {
    expect(TransformMappingV1.parse(mappedPointer(pointer)).fields[0].pointer).toBe(pointer);
  });

  it.each([
    "data", "/~", "/~2", "/a~00~3", "/a\n", "/a\u0000", "/constructor", "/a/prototype/b", "/__proto__/x",
    `/${Array(33).fill("a").join("/")}`, `/${"a".repeat(1024)}`,
  ])("rejects unsafe or oversized pointer %j", (pointer) => {
    expect(TransformMappingV1.safeParse(mappedPointer(pointer)).success).toBe(false);
  });

  it("accepts pointer and field count boundaries and refuses duplicate or reserved field IDs", () => {
    expect(TransformMappingV1.safeParse(mappedPointer(`/${Array(32).fill("a").join("/")}`)).success).toBe(true);
    expect(TransformMappingV1.safeParse(mappedPointer(`/${"a".repeat(1023)}`)).success).toBe(true);
    const fields = Array.from({ length: 100 }, (_, i) => ({ ...mapping.fields[0], field_id: `field_${i}` }));
    expect(TransformMappingV1.safeParse({ ...mapping, fields }).success).toBe(true);
    for (const invalidFields of [[], [...fields, { ...mapping.fields[0], field_id: "extra" }], [mapping.fields[0], mapping.fields[0]]]) {
      expect(TransformMappingV1.safeParse({ ...mapping, fields: invalidFields }).success).toBe(false);
    }
    for (const field_id of ["__proto__", "prototype", "constructor", "", "a.b", "a\n", "a".repeat(129)]) {
      expect(TransformMappingV1.safeParse({ ...mapping, fields: [{ ...mapping.fields[0], field_id }] }).success).toBe(false);
    }
  });

  it("refuses implicit conversions, general expressions and unknown keys", () => {
    for (const conversion of [undefined, "number", "eval", "fetch", "model"]) {
      expect(TransformMappingV1.safeParse({ ...mapping, fields: [{ ...mapping.fields[0], conversion }] }).success).toBe(false);
    }
    expect(TransformMappingV1.safeParse({ ...mapping, schema_version: 2 }).success).toBe(false);
    expect(TransformMappingV1.safeParse({ ...mapping, expression: "x + 1" }).success).toBe(false);
    expect(TransformMappingV1.safeParse({ ...mapping, fields: [{ ...mapping.fields[0], code: "x + 1" }] }).success).toBe(false);
  });
});
