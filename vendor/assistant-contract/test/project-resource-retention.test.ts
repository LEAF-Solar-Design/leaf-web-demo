import { describe, expect, it } from "vitest";
import { ProjectResourceRetentionAdmissionRecordV1 as Admission, ProjectResourceRetentionCheckpointV1 as Checkpoint,
  ProjectResourceRetentionIntentV1 as Intent, ProjectResourceRetentionTableSeedV1 as Seed } from "../src/index";
import { IntegrationManualWorkflowStepV1 } from "../src/integration";

const digest = "a".repeat(64);
const otherDigest = "b".repeat(64);
const uuid = "11111111-1111-4111-8111-111111111111";
const ref = (id: string) => ({ id, version: 1, digest });
const table = { id: "synthetic-table", revision: 1, digest };
const intent = () => ({ schema_version: 1, kind: "project_resource", operation_ref: ref("project-log-retention"),
  table_ref: table, definition_ref: ref("retention-definition") });
const checkpoint = () => ({ schema_version: 1, state: "prepared", run_id: uuid, step_id: "retention",
  occurrence_key: "synthetic-occurrence", table_revision: 1, admitted_batches: 0, cursor: null });
const seed = () => ({ schema_version: 1, table_id: "synthetic-table", disposable: true,
  dataset_ref: ref("synthetic-dataset"), material_ref: ref("synthetic-material"), material_digest: digest,
  fields: { id: "id", source: "source", level: "level", timestamp: "time" },
  retention_clock: "2026-04-10T12:30:00.000Z", active_chunks: [{ id: `dataset-batch:${digest}`, digest }],
  input_digest: digest, checkpoint: checkpoint() });
const admission = () => ({ schema_version: 1, kind: "internal_occurrence", tenant_id: uuid, project_id: uuid,
  actor_id: uuid, run_id: uuid, occurrence_key: "synthetic-occurrence", schedule_ref: ref("synthetic-schedule"),
  definition_ref: ref("retention-definition"), operation_ref: ref("project-log-retention"), table_ref: table,
  disposable: true, admitted_at: "2026-04-10T12:30:00.000Z", deadline_at: "2026-04-10T12:34:00.000Z",
  external_activation: false });

describe("dormant project-resource retention data", () => {
  it("exports strict intent with dry-run default and immutable pinned refs", () => {
    const input = Object.freeze(intent());
    const before = JSON.stringify(input);
    const parsed = Intent.parse(input);
    expect(parsed.mode).toBe("dry_run");
    expect(parsed.table_ref).toEqual(table);
    expect(parsed.definition_ref).toEqual(ref("retention-definition"));
    expect(JSON.stringify(input)).toBe(before);
  });
  it.each(["dry_run", "archive", "verify_archive", "apply", "restore"])("accepts data mode %s without executing", (mode) => {
    expect(Intent.parse({ ...intent(), mode }).mode).toBe(mode);
  });
  it.each(["tenant_id", "project_id", "actor_id", "scope", "admitted_at", "deadline_at", "now", "clock",
    "grant", "credentials", "connection_id", "connector_ref", "expected_grant_revision", "executor", "external_activation"])(
    "rejects public authority field %s", (key) => {
      expect(Intent.safeParse({ ...intent(), [key]: "untrusted" }).success).toBe(false);
    });
  it.each([
    { ...ref("other-operation") }, { ...ref("project-log-retention"), version: 2 },
    { ...ref("project-log-retention"), digest: "not-a-digest" },
  ])("rejects unreviewed or malformed operation ref %j", (operation_ref) => {
    expect(Intent.safeParse({ ...intent(), operation_ref }).success).toBe(false);
  });
  it("rejects nested unknown fields and malformed table revisions", () => {
    expect(Intent.safeParse({ ...intent(), table_ref: { ...table, revision: 0 } }).success).toBe(false);
    expect(Intent.safeParse({ ...intent(), table_ref: { ...table, scope: "foreign" } }).success).toBe(false);
    expect(Intent.safeParse({ ...intent(), definition_ref: { ...ref("definition"), injected: true } }).success).toBe(false);
  });
  it("keeps provider step V1 unchanged and refuses this intent as a runnable provider step", () => {
    const provider = { id: "relay", kind: "action", connection_id: "relay", connector_ref: ref("relay"),
      operation_ref: ref("turn-on"), arguments: { enabled: true } };
    expect(IntegrationManualWorkflowStepV1.parse(provider)).toEqual(provider);
    expect(IntegrationManualWorkflowStepV1.safeParse({ id: "retention", ...intent() }).success).toBe(false);
    expect(IntegrationManualWorkflowStepV1.safeParse({ ...provider, kind: "project_resource" }).success).toBe(false);
  });
  it("roundtrips ordered original chunk identities and initial prepared checkpoint", () => {
    const input = seed();
    input.active_chunks.push({ id: `dataset-batch:${otherDigest}`, digest: otherDigest });
    const before = JSON.stringify(input);
    expect(Seed.parse(input)).toEqual(input);
    expect(JSON.stringify(input)).toBe(before);
    expect(Checkpoint.parse(checkpoint())).toEqual(checkpoint());
  });
  it.each([
    { disposable: false }, { material_digest: otherDigest },
    { active_chunks: [{ id: `dataset-batch:${digest}`, digest: otherDigest }] },
    { active_chunks: [{ id: `dataset-batch:${digest}`, digest }, { id: `dataset-batch:${digest}`, digest }] },
    { fields: { id: "id", source: "id", level: "level", timestamp: "time" } },
    { fields: { id: "constructor", source: "source", level: "level", timestamp: "time" } },
    { checkpoint: { ...checkpoint(), admitted_batches: 1 } },
    { checkpoint: { ...checkpoint(), table_revision: 2 } },
    { checkpoint: { ...checkpoint(), cursor: "after-a-row" } },
    { checkpoint: { ...checkpoint(), state: "running" } },
    { records: [{ id: "not-inline" }] },
  ])("refuses invalid prepared seed %j", (patch) => {
    expect(Seed.safeParse({ ...seed(), ...patch }).success).toBe(false);
  });
  it.each(["2026-02-30T12:30:00.000Z", "2026-04-10T12:30:00Z", "2026-04-10T12:30:00.000+00:00",
    "2026-04-10T12:30:00.000Z\n"])("rejects noncanonical clock %s", (retention_clock) => {
    expect(Seed.safeParse({ ...seed(), retention_clock }).success).toBe(false);
  });
  it("accepts server evidence with fixed 240s relation without reading any runtime clock", () => {
    expect(Admission.parse(admission())).toEqual(admission());
  });
  it.each(["2026-04-10T12:33:59.999Z", "2026-04-10T12:34:00.001Z", "2026-04-10T12:29:00.000Z"])(
    "rejects parent deadline %s", (deadline_at) => {
      expect(Admission.safeParse({ ...admission(), deadline_at }).success).toBe(false);
    });
  it.each([{ external_activation: true }, { disposable: false }, { kind: "timer" },
    { admitted_at: "2026-02-30T12:30:00.000Z" }, { claim_token: "not-authority" }, { lease_ms: 240000 }])(
    "rejects activation or fabricated lease/admission fields %j", (patch) => {
      expect(Admission.safeParse({ ...admission(), ...patch }).success).toBe(false);
    });
  it("accepts independent retention time and server admission time as distinct data", () => {
    expect(Seed.parse(seed()).retention_clock).not.toBe(Admission.parse({ ...admission(),
      admitted_at: "2026-04-11T00:00:00.000Z", deadline_at: "2026-04-11T00:04:00.000Z" }).admitted_at);
  });
});
