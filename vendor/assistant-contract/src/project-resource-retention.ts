import { z } from "zod";
import { IntegrationRefV1 } from "./integration";

/** Dormant data contracts only. Parsing never grants scope, claims or execution. */
const Digest = z.string().regex(/^[a-f0-9]{64}(?![\s\S])/);
const Identity = z.string().min(1).max(128).regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*(?![\s\S])/);
const Revision = z.number().int().positive().max(2_147_483_647);
const UtcMillis = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z(?![\s\S])/)
  .refine((value) => {
    const time = Date.parse(value);
    return Number.isFinite(time) && new Date(time).toISOString() === value;
  }, "Canonical UTC milliseconds required");
const OperationRef = IntegrationRefV1.refine((ref) => ref.id === "project-log-retention" && ref.version === 1,
  "Reviewed project-log-retention version 1 reference required");
const TableRef = z.strictObject({ id: Identity, revision: Revision, digest: Digest });
const Mode = z.enum(["dry_run", "archive", "verify_archive", "apply", "restore"]);
const Field = z.string().min(1).max(128).regex(/^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])/)
  .refine((value) => !["__proto__", "prototype", "constructor"].includes(value), "Unsafe field");
const Fields = z.strictObject({ id: Field, source: Field, level: Field, timestamp: Field })
  .refine((fields) => new Set(Object.values(fields)).size === 4, "Field mappings must be distinct");
const ChunkRef = z.strictObject({ id: z.string().regex(/^dataset-batch:[a-f0-9]{64}(?![\s\S])/), digest: Digest })
  .refine((ref) => ref.id === `dataset-batch:${ref.digest}`, "Chunk identity must match content digest");

/** Not yet a variant of the provider manual-workflow or action V1 schemas. */
export const ProjectResourceRetentionIntentV1 = z.strictObject({
  schema_version: z.literal(1),
  kind: z.literal("project_resource"),
  operation_ref: OperationRef,
  table_ref: TableRef,
  definition_ref: IntegrationRefV1,
  mode: Mode.default("dry_run"),
});
export type ProjectResourceRetentionIntentV1 = z.infer<typeof ProjectResourceRetentionIntentV1>;

/** Initial preparation only. No admitted or committed batch is represented here. */
export const ProjectResourceRetentionCheckpointV1 = z.strictObject({
  schema_version: z.literal(1),
  state: z.literal("prepared"),
  run_id: z.string().uuid(),
  step_id: Identity,
  occurrence_key: Identity,
  table_revision: Revision,
  admitted_batches: z.literal(0),
  cursor: z.null(),
});
export type ProjectResourceRetentionCheckpointV1 = z.infer<typeof ProjectResourceRetentionCheckpointV1>;

/** References retain original content custody; this record contains no inline log rows. */
export const ProjectResourceRetentionTableSeedV1 = z.strictObject({
  schema_version: z.literal(1),
  table_id: Identity,
  disposable: z.literal(true),
  dataset_ref: IntegrationRefV1,
  material_ref: IntegrationRefV1,
  material_digest: Digest,
  fields: Fields,
  retention_clock: UtcMillis,
  active_chunks: z.array(ChunkRef).max(1000),
  input_digest: Digest,
  checkpoint: ProjectResourceRetentionCheckpointV1,
}).superRefine((seed, ctx) => {
  if (seed.checkpoint.table_revision !== 1) {
    ctx.addIssue({ code: "custom", path: ["checkpoint", "table_revision"], message: "Initial table revision must be 1" });
  }
  if (seed.material_ref.digest !== seed.material_digest) {
    ctx.addIssue({ code: "custom", path: ["material_digest"], message: "Material digest must match pinned reference" });
  }
  const seen = new Set<string>();
  seed.active_chunks.forEach((ref, index) => {
    if (seen.has(ref.id)) ctx.addIssue({ code: "custom", path: ["active_chunks", index], message: "Duplicate chunk reference" });
    seen.add(ref.id);
  });
});
export type ProjectResourceRetentionTableSeedV1 = z.infer<typeof ProjectResourceRetentionTableSeedV1>;

/** Server-produced evidence only. The future producer must authenticate scope,
 * mint a canonical occurrence and fix its trusted clock before creating this record.
 * Validating the 240-second relation does not establish that those steps happened.
 * No runtime clock, token, lease or provider authority is read or produced here.
 */
export const ProjectResourceRetentionAdmissionRecordV1 = z.strictObject({
  schema_version: z.literal(1),
  kind: z.literal("internal_occurrence"),
  tenant_id: z.string().uuid(),
  project_id: z.string().uuid(),
  actor_id: z.string().uuid(),
  run_id: z.string().uuid(),
  occurrence_key: Identity,
  schedule_ref: IntegrationRefV1,
  definition_ref: IntegrationRefV1,
  operation_ref: OperationRef,
  table_ref: TableRef,
  disposable: z.literal(true),
  admitted_at: UtcMillis,
  deadline_at: UtcMillis,
  external_activation: z.literal(false),
}).refine((record) => Date.parse(record.deadline_at) - Date.parse(record.admitted_at) === 240_000,
  { path: ["deadline_at"], message: "Parent deadline must be exactly 240 seconds after trusted admission" });
export type ProjectResourceRetentionAdmissionRecordV1 = z.infer<typeof ProjectResourceRetentionAdmissionRecordV1>;
