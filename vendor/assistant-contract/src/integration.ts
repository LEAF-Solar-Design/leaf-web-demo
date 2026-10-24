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
  purpose: z.enum(["access_token", "refresh_token", "api_key", "webhook_signing_secret"]),
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
  credential_refs: z.array(CredentialReferenceV1.extend({
    purpose: z.enum(["access_token", "refresh_token", "api_key"]),
  })).max(3),
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
const ActionDigest = z.string().regex(/^[a-f0-9]{64}(?![\s\S])/);
const ActionRevision = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const ActionUuid = z.string().uuid().length(36);
const ActionOutcome = z.enum(["not_dispatched", "applied", "not_applied", "unknown"]);
const MaxActionArgumentFields = 100;
const ActionFieldId = z.string().min(1).max(128)
  .regex(/^(?!(?:__proto__|prototype|constructor)(?![\s\S]))[A-Za-z_][A-Za-z0-9_]*(?![\s\S])/);

const ActionFieldShape = {
  id: ActionFieldId,
  label: Title,
  required: z.boolean(),
  nullable: z.boolean(),
  description: z.string().min(1).max(500).optional(),
};

export const IntegrationActionFieldV1 = z.union([
  z.strictObject({
    ...ActionFieldShape,
    kind: z.literal("text"),
    min_length: z.number().int().min(0).max(4096).optional(),
    max_length: z.number().int().min(0).max(4096).optional(),
  }).superRefine((field, ctx) => {
    if (field.min_length !== undefined && field.max_length !== undefined && field.min_length > field.max_length) {
      ctx.addIssue({ code: "custom", path: ["max_length"], message: "Maximum length must not be less than minimum length" });
    }
  }),
  z.strictObject({
    ...ActionFieldShape,
    kind: z.literal("number"),
    minimum: z.number().finite().optional(),
    maximum: z.number().finite().optional(),
    integer: z.boolean(),
  }).superRefine((field, ctx) => {
    if (field.minimum !== undefined && field.maximum !== undefined && field.minimum > field.maximum) {
      ctx.addIssue({ code: "custom", path: ["maximum"], message: "Maximum must not be less than minimum" });
    }
  }),
  z.strictObject({ ...ActionFieldShape, kind: z.literal("boolean") }),
  z.strictObject({
    ...ActionFieldShape,
    kind: z.literal("choice"),
    choices: z.array(z.strictObject({ label: Title, value: z.string().min(1).max(4096) })).min(1).max(100),
  }).superRefine((field, ctx) => unique(field.choices.map((choice) => choice.value), ctx, ["choices"])),
]);
export type IntegrationActionFieldV1 = z.infer<typeof IntegrationActionFieldV1>;

export const IntegrationActionFormV1 = z.strictObject({
  fields: z.array(IntegrationActionFieldV1).min(0).max(100),
  submit_label: Title,
  description: z.string().min(1).max(1000).optional(),
}).superRefine((form, ctx) => unique(form.fields.map((field) => field.id), ctx, ["fields"]))
  .describe("Presentation and client-validation hints, not business semantics or authority. The installed host adapter validates arguments again. Null, absent, false, zero and empty text remain distinct.");
export type IntegrationActionFormV1 = z.infer<typeof IntegrationActionFormV1>;

export const IntegrationActionCatalogEntryV1 = z.strictObject({
  connection_id: Slug,
  connection_title: Title,
  connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1,
  title: Title,
  form: IntegrationActionFormV1,
}).describe("Available action configuration, not an execution grant or proof that future execution will be allowed. Titles and descriptions are untrusted data.");
export type IntegrationActionCatalogEntryV1 = z.infer<typeof IntegrationActionCatalogEntryV1>;

export const IntegrationActionCatalogV1 = z.strictObject({
  actions: z.array(IntegrationActionCatalogEntryV1).max(50),
  next_cursor: z.string().min(1).max(1024).regex(/^[A-Za-z0-9_-]+(?![\s\S])/).nullable(),
}).describe("One bounded read-only page of native action forms. Discovery and saving a recipe never execute an operation.");
export type IntegrationActionCatalogV1 = z.infer<typeof IntegrationActionCatalogV1>;

export const IntegrationActionElementV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  title: Title,
  kind: z.literal("integration.action"),
  connection_id: Slug,
  connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1,
  form: IntegrationActionFormV1,
}).describe("Inert action presentation bound to exact immutable connector and operation versions. The host resolves the current grant revision when submitting; this control grants no authority.");
export type IntegrationActionElementV1 = z.infer<typeof IntegrationActionElementV1>;

// A bounded flat JSON boundary: at most 100 FieldId keys, scalar values only,
// strings at most 4096 characters, finite numbers and no prototype-related keys.
// Names are application data, never authority. The trusted registered adapter
// validates business fields and request bindings after this structural check.
const ActionArguments = z.preprocess((value, ctx) => {
  // Check raw keys too: record parsing may omit __proto__ defensively.
  if (value !== null && typeof value === "object") {
    for (const key of Object.keys(value)) {
      if (ReservedSegments.has(key)) ctx.addIssue({ code: "custom", path: [key], message: "Reserved field ID" });
    }
  }
  return value;
}, z.record(ActionFieldId, z.union([
  z.string().max(4096), z.number().finite(), z.boolean(), z.null(),
])).refine((args) => Object.keys(args).length <= MaxActionArgumentFields, "At most 100 argument fields"))
  .meta({ maxProperties: MaxActionArgumentFields })
  .describe("Flat JSON record of at most 100 FieldId keys, excluding __proto__, prototype and constructor; scalar values only, strings at most 4096 characters and finite numbers. Application data, never authorization; the trusted registered adapter validates business fields and request bindings.");

export const IntegrationActionSubmissionV1 = z.strictObject({
  schema_version: z.literal(1),
  connection_id: Slug,
  connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1,
  idempotency_key: z.string().min(16).max(128).regex(/^[A-Za-z0-9_-]+(?![\s\S])/),
  expected_grant_revision: ActionRevision,
  arguments: ActionArguments,
});
export type IntegrationActionSubmissionV1 = z.infer<typeof IntegrationActionSubmissionV1>;

// Compare all fractional digits: Date.parse would discard sub-millisecond order.
function actionTimeOrdered(created: string, updated: string): boolean {
  const [createdSecond, createdFraction = ""] = created.slice(0, -1).split(".");
  const [updatedSecond, updatedFraction = ""] = updated.slice(0, -1).split(".");
  if (createdSecond !== updatedSecond) return createdSecond < updatedSecond;
  const width = Math.max(createdFraction.length, updatedFraction.length);
  return createdFraction.padEnd(width, "0") <= updatedFraction.padEnd(width, "0");
}

// Server-produced execution evidence, never permission. A claim persisted before
// sending is conservatively unknown, even if a crash preceded network dispatch.
export const IntegrationActionRunV1 = z.strictObject({
  schema_version: z.literal(1),
  run_id: ActionUuid,
  revision: ActionRevision,
  connection_id: Slug,
  connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1,
  submission_digest: ActionDigest,
  authority_binding_digest: ActionDigest,
  grant_revision: ActionRevision,
  created_at: Instant,
  updated_at: Instant,
  state: z.enum(["accepted", "running", "succeeded", "failed", "uncertain"]),
  effect_outcome: ActionOutcome,
}).superRefine((run, ctx) => {
  const outcomes = {
    accepted: ["not_dispatched"], running: ["unknown"], succeeded: ["applied"],
    failed: ["not_dispatched", "not_applied"], uncertain: ["unknown"],
  };
  if (!outcomes[run.state].includes(run.effect_outcome)) {
    ctx.addIssue({ code: "custom", path: ["effect_outcome"], message: "Outcome incompatible with run state" });
  }
  if (!actionTimeOrdered(run.created_at, run.updated_at)) {
    ctx.addIssue({ code: "custom", path: ["updated_at"], message: "Updated time must not precede creation" });
  }
});
export type IntegrationActionRunV1 = z.infer<typeof IntegrationActionRunV1>;

// Digests refer to separately scoped material and never prove authorization.
// The host must keep uncertain receipts append-only and add a new receipt for
// later reconciliation; a schema cannot enforce that persistence obligation.
export const IntegrationActionReceiptV1 = z.strictObject({
  schema_version: z.literal(1),
  receipt_id: ActionUuid,
  run_id: ActionUuid,
  sequence: ActionRevision,
  submission_digest: ActionDigest,
  operation_ref: IntegrationRefV1,
  observed_at: Instant,
  effect_outcome: ActionOutcome,
  result_digest: ActionDigest.optional(),
  failure_code: z.enum([
    "invalid_input", "forbidden", "revoked", "conflict", "quota", "timeout",
    "unavailable", "invalid_response", "outcome_unknown",
  ]).optional(),
  reconciliation: z.strictObject({
    operation_ref: IntegrationRefV1,
    observation_receipt_id: ActionUuid,
  }).optional(),
}).superRefine((receipt, ctx) => {
  if (receipt.failure_code === "outcome_unknown" && receipt.effect_outcome !== "unknown") {
    ctx.addIssue({ code: "custom", path: ["failure_code"], message: "Unknown outcome failure requires unknown outcome" });
  }
  if (receipt.effect_outcome === "applied" && receipt.failure_code !== undefined) {
    ctx.addIssue({ code: "custom", path: ["failure_code"], message: "Applied outcome cannot carry a failure" });
  }
  if (receipt.effect_outcome === "not_dispatched" && receipt.reconciliation !== undefined) {
    ctx.addIssue({ code: "custom", path: ["reconciliation"], message: "Undispatched actions cannot carry reconciliation" });
  }
});
export type IntegrationActionReceiptV1 = z.infer<typeof IntegrationActionReceiptV1>;

export const IntegrationActionSnapshotV1 = z.strictObject({
  run: IntegrationActionRunV1,
  receipts: z.array(IntegrationActionReceiptV1).max(100),
}).superRefine((snapshot, ctx) => {
  const { run, receipts } = snapshot;
  unique(receipts.map((receipt) => receipt.receipt_id.toLowerCase()), ctx, ["receipts"]);
  receipts.forEach((receipt, index) => {
    if (receipt.run_id !== run.run_id || receipt.submission_digest !== run.submission_digest ||
        receipt.operation_ref.id !== run.operation_ref.id ||
        receipt.operation_ref.version !== run.operation_ref.version ||
        receipt.operation_ref.digest !== run.operation_ref.digest) {
      ctx.addIssue({ code: "custom", path: ["receipts", index], message: "Receipt must belong to this run, submission and exact operation" });
    }
    if (index > 0 && receipt.sequence <= receipts[index - 1].sequence) {
      ctx.addIssue({ code: "custom", path: ["receipts", index, "sequence"], message: "Receipt sequences must strictly increase" });
    }
    if (!actionTimeOrdered(run.created_at, receipt.observed_at) || !actionTimeOrdered(receipt.observed_at, run.updated_at)) {
      ctx.addIssue({ code: "custom", path: ["receipts", index, "observed_at"], message: "Receipt observation must be within the run lifetime" });
    }
  });
  if (run.state === "accepted" || run.state === "running") {
    if (receipts.length !== 0) {
      ctx.addIssue({ code: "custom", path: ["receipts"], message: "Nonterminal runs cannot carry receipts" });
    }
  } else if (receipts.length === 0 || receipts[receipts.length - 1].effect_outcome !== run.effect_outcome) {
    ctx.addIssue({ code: "custom", path: ["receipts"], message: "Terminal runs require a latest receipt matching their outcome" });
  }
});
export type IntegrationActionSnapshotV1 = z.infer<typeof IntegrationActionSnapshotV1>;

export const IntegrationActionSubmissionResultV1 = z.union([
  z.strictObject({ status: z.literal("admitted"), run: IntegrationActionRunV1, replayed: z.boolean() }),
  z.strictObject({ status: z.literal("refused"), code: z.literal("idempotency_conflict") }),
]);
export type IntegrationActionSubmissionResultV1 = z.infer<typeof IntegrationActionSubmissionResultV1>;

export const IntegrationManualWorkflowStepV1 = z.strictObject({
  id: Slug,
  kind: z.literal("action"),
  connection_id: Slug,
  connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1,
  arguments: ActionArguments,
}).describe("Literal scalar business data only; argument names never grant authority. Scalar validation cannot identify secret contents: the host must keep secret values out of saved arguments.");
export type IntegrationManualWorkflowStepV1 = z.infer<typeof IntegrationManualWorkflowStepV1>;

export const IntegrationManualWorkflowDefinitionV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  version: ActionRevision,
  title: Title,
  trigger: z.literal("manual"),
  authority_mode: z.literal("caller_bound"),
  steps: z.array(IntegrationManualWorkflowStepV1).min(1).max(8),
}).superRefine((definition, ctx) => unique(definition.steps.map((step) => step.id), ctx, ["steps"]))
  .describe("Finite ordered manual actions. Saving or parsing a definition never authorizes execution and introduces no standing or unattended authority. A separate IntegrationRefV1 pins this definition; the host owns immutable version persistence and digest equality.");
export type IntegrationManualWorkflowDefinitionV1 = z.infer<typeof IntegrationManualWorkflowDefinitionV1>;

export const IntegrationManualWorkflowSubmissionV1 = z.strictObject({
  schema_version: z.literal(1),
  workflow_ref: IntegrationRefV1,
  idempotency_key: IntegrationActionSubmissionV1.shape.idempotency_key,
  step_grants: z.array(z.strictObject({
    step_id: Slug,
    expected_grant_revision: ActionRevision,
  })).min(1).max(8),
}).superRefine((submission, ctx) => unique(submission.step_grants.map((grant) => grant.step_id), ctx, ["step_grants"]))
  .describe("The authenticated host establishes scope and caller and rechecks membership, grants and required approvals before each step or explicit resume. The host must match the resolved definition's exact step order. No caller argument overrides, child idempotency keys or authority are accepted.");
export type IntegrationManualWorkflowSubmissionV1 = z.infer<typeof IntegrationManualWorkflowSubmissionV1>;

const WorkflowStepIdentity = { run_id: ActionUuid, step_id: Slug };
export const IntegrationManualWorkflowStepSnapshotV1 = z.union([
  z.strictObject({ ...WorkflowStepIdentity, state: z.literal("pending") }),
  z.strictObject({
    ...WorkflowStepIdentity,
    state: z.literal("blocked"),
    failure_code: IntegrationActionReceiptV1.shape.failure_code.unwrap().exclude(["outcome_unknown"]),
    effect_outcome: z.literal("not_dispatched"),
  }),
  z.strictObject({ ...WorkflowStepIdentity, state: z.literal("action"), action: IntegrationActionSnapshotV1 }),
]).describe("Parent-bound step evidence. Pending evidence never proves that a write did not occur; the runtime must reconcile its persisted intended child identity. No raw provider results or copied arguments.");
export type IntegrationManualWorkflowStepSnapshotV1 = z.infer<typeof IntegrationManualWorkflowStepSnapshotV1>;

export const IntegrationManualWorkflowRunV1 = z.strictObject({
  schema_version: z.literal(1),
  run_id: ActionUuid,
  revision: ActionRevision,
  workflow_ref: IntegrationRefV1,
  submission_digest: ActionDigest,
  authority_mode: z.literal("caller_bound"),
  authority_binding_digest: ActionDigest,
  created_at: Instant,
  updated_at: Instant,
  state: z.enum(["accepted", "running", "succeeded", "failed", "uncertain"]),
}).superRefine((run, ctx) => {
  if (!actionTimeOrdered(run.created_at, run.updated_at)) {
    ctx.addIssue({ code: "custom", path: ["updated_at"], message: "Updated time must not precede creation" });
  }
}).describe("Host-produced evidence, never a caller capability. The host produces the authority binding digest. There is no aggregate effect outcome: a failed workflow may contain applied steps.");
export type IntegrationManualWorkflowRunV1 = z.infer<typeof IntegrationManualWorkflowRunV1>;

function workflowRefsEqual(left: IntegrationRefV1, right: IntegrationRefV1): boolean {
  return left.id === right.id && left.version === right.version && left.digest === right.digest;
}

export const IntegrationManualWorkflowSnapshotV1 = z.strictObject({
  definition: IntegrationManualWorkflowDefinitionV1,
  run: IntegrationManualWorkflowRunV1,
  steps: z.array(IntegrationManualWorkflowStepSnapshotV1).min(1).max(8),
}).superRefine(({ definition, run, steps }, ctx) => {
  if (definition.id !== run.workflow_ref.id || definition.version !== run.workflow_ref.version) {
    ctx.addIssue({ code: "custom", path: ["definition"], message: "Definition must match the run reference ID and version" });
  }
  if (steps.length !== definition.steps.length) {
    ctx.addIssue({ code: "custom", path: ["steps"], message: "Exact definition step count required" });
  }
  unique(steps.flatMap((step) => step.state === "action" ? [step.action.run.run_id.toLowerCase()] : []), ctx, ["steps"]);
  let frontier: IntegrationManualWorkflowStepSnapshotV1 | undefined;
  let prefixEnded = false;
  for (const [index, step] of steps.entries()) {
    const expected = definition.steps[index];
    if (!expected || step.step_id !== expected.id || step.run_id !== run.run_id) {
      ctx.addIssue({ code: "custom", path: ["steps", index], message: "Step must match definition order and parent run" });
    }
    if (step.state === "action" && expected) {
      const child = step.action.run;
      if (child.connection_id !== expected.connection_id || !workflowRefsEqual(child.connector_ref, expected.connector_ref) ||
          !workflowRefsEqual(child.operation_ref, expected.operation_ref)) {
        ctx.addIssue({ code: "custom", path: ["steps", index, "action"], message: "Child must match the exact definition connection and references" });
      }
    }
    if (!prefixEnded && step.state === "action" && step.action.run.state === "succeeded") continue;
    if (!prefixEnded) {
      prefixEnded = true;
      frontier = step;
    } else if (step.state !== "pending") {
      ctx.addIssue({ code: "custom", path: ["steps", index], message: "Only pending steps may follow the frontier" });
    }
  }
  const frontierState = frontier?.state === "action" ? frontier.action.run.state : frontier?.state;
  const compatible = run.state === "running" ||
    (run.state === "accepted" && steps.every((step) => step.state === "pending")) ||
    (run.state === "succeeded" && !prefixEnded) ||
    (run.state === "failed" && (frontierState === "blocked" || frontierState === "failed")) ||
    (run.state === "uncertain" && frontierState === "uncertain");
  if (!compatible) {
    ctx.addIssue({ code: "custom", path: ["run", "state"], message: "Parent state incompatible with the successful prefix and frontier" });
  }
}).describe("Successful children form an ordered prefix with at most one nonpending frontier; later steps are pending. Running permits child settlement before parent settlement, including all children succeeded, and never overrides child effects. Identity, order and state checks are runtime refinements. The host verifies definition digest, child submission digests and input equality; schema does not prove hashes, append-only history or crash safety.");
export type IntegrationManualWorkflowSnapshotV1 = z.infer<typeof IntegrationManualWorkflowSnapshotV1>;

export const IntegrationManualWorkflowSubmissionResultV1 = z.union([
  z.strictObject({ status: z.literal("admitted"), run: IntegrationManualWorkflowRunV1, replayed: z.boolean() }),
  z.strictObject({ status: z.literal("refused"), code: z.literal("idempotency_conflict") }),
]);
export type IntegrationManualWorkflowSubmissionResultV1 = z.infer<typeof IntegrationManualWorkflowSubmissionResultV1>;

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
  // Pins reviewed semantics; the host requires a reviewed adapter for effects.
  // This reference does not grant execution authority.
  adapter_ref: IntegrationRefV1.optional(),
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

export const IntegrationElementV1 = z.union([DatasetElementV1, IntegrationActionElementV1]);
export type IntegrationElementV1 = z.infer<typeof IntegrationElementV1>;

export const IntegrationManualWorkflowRunV2 = z.strictObject({
  ...IntegrationManualWorkflowRunV1.shape,
  schema_version: z.literal(2),
  state: z.enum(["accepted", "running", "succeeded", "failed", "uncertain", "cancel_requested", "cancelled"]),
  requested_at: Instant.optional(),
}).superRefine((run, ctx) => {
  const cancellation = run.state === "cancel_requested" || run.state === "cancelled";
  if (cancellation ? run.requested_at === undefined : Object.prototype.hasOwnProperty.call(run, "requested_at")) {
    ctx.addIssue({ code: "custom", path: ["requested_at"], message: "Request time is required exactly in cancellation states" });
  }
  if (!actionTimeOrdered(run.created_at, run.updated_at)) {
    ctx.addIssue({ code: "custom", path: ["updated_at"], message: "Updated time must not precede creation" });
  }
  if (run.requested_at !== undefined && (!actionTimeOrdered(run.created_at, run.requested_at) ||
      !actionTimeOrdered(run.requested_at, run.updated_at))) {
    ctx.addIssue({ code: "custom", path: ["requested_at"], message: "Request time must be between creation and update" });
  }
}).describe("Opt-in V2 host evidence, never authority or an aggregate effect outcome. Time and state checks are runtime refinements. The host must persist cancellation before inspecting the frontier, keep requested_at immutable across cancellation history and never regress a cancellation to an ordinary state. These history guarantees cannot be proved by a standalone schema.");
export type IntegrationManualWorkflowRunV2 = z.infer<typeof IntegrationManualWorkflowRunV2>;

export const IntegrationManualWorkflowSnapshotV2 = z.strictObject({
  definition: IntegrationManualWorkflowDefinitionV1,
  run: IntegrationManualWorkflowRunV2,
  steps: z.array(IntegrationManualWorkflowStepSnapshotV1).min(1).max(8),
}).superRefine((snapshot, ctx) => {
  const { requested_at, ...run } = snapshot.run;
  const cancellation = run.state === "cancel_requested" || run.state === "cancelled";
  // A detached validation-only projection reuses every V1 structural refinement.
  // It neither mutates the snapshot nor changes the public parsed state.
  const structural = IntegrationManualWorkflowSnapshotV1.safeParse({
    ...snapshot,
    run: { ...run, schema_version: 1, state: cancellation ? "running" : run.state },
  });
  if (!structural.success) for (const issue of structural.error.issues) ctx.addIssue({ ...issue });
  if (run.state === "cancelled") snapshot.steps.forEach((step, index) => {
    if (step.state === "action" && (step.action.run.state === "running" ||
        step.action.run.state === "uncertain" || step.action.run.effect_outcome === "unknown")) {
      ctx.addIssue({ code: "custom", path: ["steps", index, "action"], message: "Cancelled workflows cannot contain running or unknown child effects" });
    }
  });
}).describe("V1 identity, order, exact references, successful prefix and frontier checks remain runtime refinements. Cancellation stops future admissions and claims; it does not abort or compensate claimed provider actions. A winning request remains cancel_requested while a child is running or unknown; explicit reconciliation may permit cancelled, while unsupported reconciliation may leave it pending. An all-applied prefix may settle cancelled with completed effects visible. Accepted/not_dispatched is safe only when the host atomically bars future claims. Pending never proves no effect. Schema checks prove neither private child identity lookup nor an atomic dispatch fence, hashes, input equality or append-only history. Original child receipts remain unchanged; this contract implements no fence or provider work.");
export type IntegrationManualWorkflowSnapshotV2 = z.infer<typeof IntegrationManualWorkflowSnapshotV2>;

export const IntegrationManualWorkflowCancellationRequestV1 = z.strictObject({
  schema_version: z.literal(1),
  run_id: ActionUuid,
  expected_revision: ActionRevision,
}).describe("Cancellation request shape only, granting no authority and settling no effect. The host reconciles repeated requests against the same parent and retains finish, expiry and reconciliation capability for already claimed children under unchanged authority.");
export type IntegrationManualWorkflowCancellationRequestV1 = z.infer<typeof IntegrationManualWorkflowCancellationRequestV1>;

export const IntegrationManualWorkflowCancellationResultV1 = z.strictObject({
  status: z.enum(["recorded", "already_recorded", "already_terminal"]),
  snapshot: IntegrationManualWorkflowSnapshotV2,
}).superRefine(({ status, snapshot }, ctx) => {
  const state = snapshot.run.state;
  if (status === "already_terminal" ? state !== "succeeded" && state !== "failed" :
      state !== "cancel_requested" && state !== "cancelled") {
    ctx.addIssue({ code: "custom", path: ["snapshot", "run", "state"], message: "Snapshot state incompatible with cancellation result status" });
  }
}).describe("Runtime status/state checks only. Recorded may already be safely cancelled. Existing succeeded/failed parents return unchanged without a cancellation request time or invented cancellation receipt. Host and caller must bind this snapshot to the requested run and expected revision; this result alone cannot prove that binding. Error transport remains separate.");
export type IntegrationManualWorkflowCancellationResultV1 = z.infer<typeof IntegrationManualWorkflowCancellationResultV1>;

export const IntegrationDailyWorkflowScheduleV1 = z.strictObject({
  kind: z.literal("daily"),
  timezone: z.string().min(1).max(128)
    .regex(/^[A-Za-z0-9_.+-]+(?:\/[A-Za-z0-9_.+-]+)*(?![\s\S])/)
    .describe("Lexical ASCII timezone key, preserved exactly. The host must verify recognized IANA calendar support before accepting a persisted draft."),
  hour: z.number().int().min(0).max(23),
  minute: z.number().int().min(0).max(59),
  gap_policy: z.literal("next_valid_minute"),
  fold_policy: z.literal("first_occurrence"),
}).describe("Daily calendar policy only, with no calendar calculation: a nonexistent local minute moves to the next valid local minute; an ambiguous local minute uses the first occurrence only. Parsing or saving this policy grants no execution authority.");
export type IntegrationDailyWorkflowScheduleV1 = z.infer<typeof IntegrationDailyWorkflowScheduleV1>;

export const IntegrationWorkflowScheduleDraftV1 = z.strictObject({
  schema_version: z.literal(1),
  id: Slug,
  version: ActionRevision,
  title: Title,
  workflow_ref: IntegrationRefV1,
  schedule: IntegrationDailyWorkflowScheduleV1,
  state: z.literal("draft"),
}).describe("Inert authoring draft recording intended immutable workflow identity and calendar policy. Parsing or saving it grants no execution authority. Reference existence, digest equality, immutable persistence, timezone support and eventual scheduling eligibility are host guarantees. A reference to a caller-bound manual definition does not make that definition eligible for unattended execution. This contract implements no persistence, timer, occurrence admission, activation, grant or execution path.");
export type IntegrationWorkflowScheduleDraftV1 = z.infer<typeof IntegrationWorkflowScheduleDraftV1>;
