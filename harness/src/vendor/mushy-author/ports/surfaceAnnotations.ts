export type SurfaceMode =
  | "use"
  | "annotate.temporary"
  | "annotate.latched"
  | "annotate.editing"
  | "annotate.applying"
  | "annotate.reviewing";

export type BindingQuality = "bound" | "generated" | "described" | "stale";

// ---------------------------------------------------------------------------
// FROZEN status types (Wave 1)
// ---------------------------------------------------------------------------

export type BatchStatus =
  | "draft" | "staged" | "previewing" | "review_required" | "accepting"
  | "accepted" | "failed" | "stale" | "rejected" | "reverting" | "reverted" | "dismissed";

export type MarkStatus = "draft" | "staged" | "verified" | "failed" | "stale";

export const TERMINAL_BATCH_STATUSES = ["accepted", "rejected", "reverted", "dismissed"] as const;
export const ACTIVE_BATCH_STATUSES =
  ["draft", "staged", "previewing", "review_required", "accepting", "reverting"] as const;
export const ATTENTION_BATCH_STATUSES =
  ["failed", "stale", "accepted", "reverted", "rejected"] as const;

// ---------------------------------------------------------------------------
// FROZEN safe locator (Wave 1)
// ---------------------------------------------------------------------------

export type SafeLocatorAttribute = "data-mushy-source-id" | "id" | "name" | "data-testid" | "data-test";

export interface SafeLocator {
  attribute: SafeLocatorAttribute;
  value: string;
  preferred: boolean;
}

export const SAFE_LOCATOR_PRECEDENCE: readonly SafeLocatorAttribute[] =
  ["data-mushy-source-id", "id", "name", "data-testid", "data-test"] as const;

export const SAFE_LOCATOR_VALUE_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;

export function resolveSafeLocator(stableAttributes: Record<string, string>): SafeLocator | null {
  for (const attr of SAFE_LOCATOR_PRECEDENCE) {
    const value = stableAttributes[attr];
    if (value !== undefined && SAFE_LOCATOR_VALUE_PATTERN.test(value)) {
      return { attribute: attr, value, preferred: attr === "data-mushy-source-id" };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// FROZEN capture viewport and tolerances (Wave 1)
// ---------------------------------------------------------------------------

export interface CaptureViewport { width: number; height: number; scale: number; }

export const VIEWPORT_DIMENSION_TOLERANCE_PX = 2;
export const VIEWPORT_SCALE_TOLERANCE = 0;
export const RECT_CONTAINMENT_TOLERANCE_PX = 2;

export function validateCaptureViewport(input: unknown): CaptureViewport {
  const vp = object(input, "surface_annotation_invalid_viewport");
  const width = finite(vp.width, "surface_annotation_invalid_viewport");
  const height = finite(vp.height, "surface_annotation_invalid_viewport");
  const scale = finite(vp.scale, "surface_annotation_invalid_viewport");
  if (width < 1 || width > 20000 || height < 1 || height > 20000 || scale <= 0 || scale > 10) {
    throw new Error("surface_annotation_invalid_viewport");
  }
  return { width, height, scale };
}

export function checkViewportDrift(
  mark: {
    capture_viewport: CaptureViewport;
    evidence: { rect: { x: number; y: number; width: number; height: number } };
    scroll: { x: number; y: number };
  },
  target: { viewport: CaptureViewport },
): { ok: true } | { ok: false; code: "annotation_viewport_drift"; reason: string } {
  if (Math.abs(mark.capture_viewport.width - target.viewport.width) > VIEWPORT_DIMENSION_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "viewport width drifted beyond tolerance" };
  }
  if (Math.abs(mark.capture_viewport.height - target.viewport.height) > VIEWPORT_DIMENSION_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "viewport height drifted beyond tolerance" };
  }
  if (mark.capture_viewport.scale !== target.viewport.scale) {
    return { ok: false, code: "annotation_viewport_drift", reason: "viewport scale changed" };
  }
  const rect = mark.evidence.rect;
  const x0 = rect.x - mark.scroll.x;
  const y0 = rect.y - mark.scroll.y;
  if (x0 < -RECT_CONTAINMENT_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "rect overflows viewport left edge" };
  }
  if (y0 < -RECT_CONTAINMENT_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "rect overflows viewport top edge" };
  }
  if (x0 + rect.width > target.viewport.width + RECT_CONTAINMENT_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "rect overflows viewport right edge" };
  }
  if (y0 + rect.height > target.viewport.height + RECT_CONTAINMENT_TOLERANCE_PX) {
    return { ok: false, code: "annotation_viewport_drift", reason: "rect overflows viewport bottom edge" };
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// RFC 4: attempt identity, preview manifest, validation, attention
// ---------------------------------------------------------------------------

export interface AnnotationAttempt {
  attempt_id: string;
  batch_id: string;
  base_commit: string;
  preview_commit?: string;
  preview_tree?: string;
  preview_ref?: string;
  submitted_notes: Array<{ annotation_id: string; operator_request: string }>;
  outcome: "pending" | "review_required" | "accepted" | "rejected" | "failed";
  error?: string;
  failed_gate?: string;
  validation_evidence_id?: string;
  requested_targets?: string[];
  requested_targets_digest?: string;
  submit_idempotency_key?: string;
  accept_idempotency_key?: string;
  reject_idempotency_key?: string;
  undo_idempotency_key?: string;
  started_at: string;
  ended_at?: string;
}

export interface PreviewManifest {
  batch_id: string;
  attempt_id: string;
  base_commit: string;
  preview_commit: string;
  preview_tree: string;
  preview_ref: string;
  changed_files: string[];
  requested_targets_digest: string;
  validation: ValidationResult;
  created_at: string;
}

export interface ValidationResult {
  ok: boolean;
  evidence_id: string;
  failed_gate?: string;
  requested_targets: string[];
  changed_selectors: string[];
  matched_targets: string[];
  unrequested_changes: Array<{ locator: string; property: string; before: string; after: string }>;
  new_keyframes: string[];
  new_custom_properties: string[];
}

export interface AttentionEntry {
  batch_id: string;
  status: BatchStatus;
  updated_at: string;
  mark_count: number;
  last_error?: string;
  accepted_commit?: string;
  accepted_attempt_id?: string;
  offered_actions: Array<"resume" | "dismiss" | "view_history">;
}

export function previewRefFor(batchId: string, attemptId: string): string {
  return `refs/mushy/annotation-preview/${batchId}/${attemptId}`;
}

export const ANNOTATION_BATCH_TRAILER = "Annotation-Batch";
export const ANNOTATION_ATTEMPT_TRAILER = "Annotation-Attempt";

// ---------------------------------------------------------------------------
// Legal batch transitions (FROZEN)
// ---------------------------------------------------------------------------

export const LEGAL_BATCH_TRANSITIONS: Readonly<Record<BatchStatus, readonly BatchStatus[]>> = {
  // draft and staged reach `dismissed` so an operator can abandon a batch they
  // have not previewed. Reject only exists after a preview, so without this an
  // active batch could leave the tray ONLY by being previewed: mark something,
  // change your mind, and the tray is stuck. Dismissing writes no commit and
  // weakens no containment property.
  draft:            ["staged", "dismissed"],
  staged:           ["draft", "previewing", "staged", "stale", "dismissed"],
  previewing:       ["review_required", "failed"],
  review_required:  ["accepting", "rejected", "stale"],
  accepting:        ["accepted", "failed"],
  accepted:         ["reverting", "dismissed"],
  reverting:        ["reverted", "accepted"],
  failed:           ["staged", "dismissed"],
  stale:            ["staged", "dismissed"],
  rejected:         ["dismissed"],
  reverted:         [],
  dismissed:        [],
};

export function isLegalBatchTransition(from: BatchStatus, to: BatchStatus): boolean {
  return LEGAL_BATCH_TRANSITIONS[from].includes(to);
}

export function assertLegalBatchTransition(from: BatchStatus, to: BatchStatus): void {
  if (!isLegalBatchTransition(from, to)) {
    throw new Error("annotation_illegal_transition");
  }
}

// ---------------------------------------------------------------------------
// Core record shapes
// ---------------------------------------------------------------------------

export interface InspectionTargetRef {
  inspection_target_id: string;
  /** Opaque target issued by the standard visual service for this same surface. */
  standard_visual_inspection_target_id?: string;
  tenant_id: string;
  human_id: string;
  session_id: string;
  allowed_origin: string;
  viewport: { width: number; height: number; scale: number };
  issued_at: string;
  expires_at: string;
}

export interface ElementEvidence {
  source_id?: string;
  role?: string;
  accessible_name?: string;
  tag: string;
  stable_attributes: Record<string, string>;
  ancestry_fingerprint: string;
  rendered_text_hash?: string;
  rendered_text_excerpt?: string;
  rect: { x: number; y: number; width: number; height: number };
  screenshot_artifact_id?: string;
}

export type ApprovedPreviewPatch =
  | { kind: "text"; value: string }
  | { kind: "style"; property: "color" | "backgroundColor" | "fontSize" | "borderRadius" | "padding"; value: string };
type ApprovedStyleProperty = Extract<ApprovedPreviewPatch, { kind: "style" }>["property"];

export interface AnnotationVerification {
  annotation_id: string;
  status: "resolved" | "partial" | "stale" | "failed";
  binding: BindingQuality;
  reason?: string;
  observed_status?: "resolved" | "partial" | "stale" | "failed";
  current_rect?: ElementEvidence["rect"];
}

export interface SurfaceAnnotation {
  annotation_id: string;
  kind: "element" | "region";
  binding: BindingQuality;
  route: string;
  scroll: { x: number; y: number };
  evidence: ElementEvidence;
  operator_request: string;
  preview_patch?: ApprovedPreviewPatch;
  status: MarkStatus;
  verification?: AnnotationVerification;
  capture_viewport: CaptureViewport;
  locator?: SafeLocator;
}

export interface AnnotationBatch {
  batch_id: string;
  tenant_id: string;
  human_id: string;
  session_id: string;
  inspection_target_id: string;
  standard_visual_inspection_target_id?: string;
  base_commit: string;
  mode: SurfaceMode;
  annotations: SurfaceAnnotation[];
  status: BatchStatus;
  created_at: string;
  submitted_at?: string;
  create_idempotency_key?: string;
  submit_idempotency_key?: string;
  attempt_id?: string;
  attempt_history: AnnotationAttempt[];
  preview_commit?: string;
  preview_tree?: string;
  preview_ref?: string;
  accept_idempotency_key?: string;
  accepted_attempt_id?: string;
  reject_idempotency_key?: string;
  rejected_attempt_id?: string;
  undo_idempotency_key?: string;
  updated_at?: string;
  accepted_commit?: string;
  revert_commit?: string;
  failed_gate?: string;
  error?: string;
  retry_count?: number;
}

export interface CreateAnnotationBatch {
  inspection_target_id: string;
  base_commit: string;
  idempotency_key: string;
}

export interface BatchVerification {
  results: AnnotationVerification[];
}

// ---------------------------------------------------------------------------
// AnnotationStore contract (FROZEN)
// ---------------------------------------------------------------------------

export interface AnnotationStore {
  createBatch(input: CreateAnnotationBatch): Promise<AnnotationBatch> | AnnotationBatch;
  addOrUpdateMark(batchId: string, annotationId: string, mark: SurfaceAnnotation): Promise<AnnotationBatch> | AnnotationBatch;
  removeMark(batchId: string, annotationId: string): Promise<AnnotationBatch> | AnnotationBatch;
  get(batchId: string): Promise<AnnotationBatch | null> | AnnotationBatch | null;
  current(): Promise<AnnotationBatch | null> | AnnotationBatch | null;
  attention(): Promise<AttentionEntry[]> | AttentionEntry[];
  beginPreview(batchId: string, submitIdempotencyKey: string): Promise<AnnotationBatch> | AnnotationBatch;
  recordPreview(batchId: string, manifest: PreviewManifest): Promise<AnnotationBatch> | AnnotationBatch;
  recordPreviewFailure(batchId: string, error: string, failedGate: string): Promise<AnnotationBatch> | AnnotationBatch;
  accept(batchId: string, acceptIdempotencyKey: string, appliedCommit: string): Promise<AnnotationBatch> | AnnotationBatch;
  reject(batchId: string, rejectIdempotencyKey: string): Promise<AnnotationBatch> | AnnotationBatch;
  retry(batchId: string, input: { base_commit: string; inspection_target_id?: string }): Promise<AnnotationBatch> | AnnotationBatch;
  dismiss(batchId: string): Promise<AnnotationBatch> | AnnotationBatch;
  beginRevert(batchId: string, undoIdempotencyKey: string): Promise<AnnotationBatch> | AnnotationBatch;
  recordRevert(batchId: string, revertCommit: string): Promise<AnnotationBatch> | AnnotationBatch;
  recordStale(batchId: string, reason: string): Promise<AnnotationBatch> | AnnotationBatch;
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

const BINDINGS = new Set<BindingQuality>(["bound", "generated", "described", "stale"]);
const MARK_STATUSES = new Set<MarkStatus>(["draft", "staged", "verified", "failed", "stale"]);
// Must contain every SAFE_LOCATOR_PRECEDENCE member: validateElementEvidence runs
// BEFORE resolveSafeLocator, so an attribute missing here is rejected outright and
// its locator can never be reached. `data-mushy-source-id` is the PREFERRED locator.
// `aria-label` is deliberately absent: page-authored prose is never a locator.
const ATTRIBUTES = new Set(["data-mushy-source-id", "id", "name", "data-testid", "data-test"]);

function object(value: unknown, message: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(message);
  return value as Record<string, unknown>;
}

function string(value: unknown, max: number, message: string, required = false): string | undefined {
  if (value === undefined && !required) return undefined;
  if (typeof value !== "string" || (required && !value) || value.length > max) throw new Error(message);
  return value;
}

function finite(value: unknown, message: string): number {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(message);
  return number;
}

function validateRect(input: unknown): ElementEvidence["rect"] {
  const value = object(input, "surface_annotation_invalid_rect");
  const rect = {
    x: finite(value.x, "surface_annotation_invalid_rect"),
    y: finite(value.y, "surface_annotation_invalid_rect"),
    width: finite(value.width, "surface_annotation_invalid_rect"),
    height: finite(value.height, "surface_annotation_invalid_rect"),
  };
  if (rect.width < 0 || rect.height < 0 || rect.width > 20_000 || rect.height > 20_000) {
    throw new Error("surface_annotation_invalid_rect");
  }
  return rect;
}

export function validateApprovedPreviewPatch(input: unknown): ApprovedPreviewPatch | undefined {
  if (input === undefined || input === null) return undefined;
  const patch = object(input, "surface_annotation_preview_not_approved");
  if (patch.kind === "text") {
    return { kind: "text", value: string(patch.value, 500, "surface_annotation_preview_invalid_text", true)! };
  }
  const patterns: Record<string, RegExp> = {
    color: /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i,
    backgroundColor: /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i,
    fontSize: /^(?:[8-9]|[1-8][0-9]|9[0-6])px$/,
    borderRadius: /^(?:0|[1-5]?[0-9]|6[0-4])px$/,
    padding: /^(?:0|[1-5]?[0-9]|6[0-4])px$/,
  };
  const property = string(patch.property, 30, "surface_annotation_preview_not_approved", true)!;
  const value = string(patch.value, 30, "surface_annotation_preview_not_approved", true)!;
  if (patch.kind !== "style" || !patterns[property]?.test(value)) throw new Error("surface_annotation_preview_not_approved");
  return { kind: "style", property: property as ApprovedStyleProperty, value };
}

export function validateElementEvidence(input: unknown): ElementEvidence {
  const evidence = object(input, "surface_annotation_invalid_evidence");
  const tag = string(evidence.tag, 64, "surface_annotation_invalid_tag", true)!;
  if (!/^[a-z][a-z0-9-]{0,63}$/i.test(tag)) throw new Error("surface_annotation_invalid_tag");
  const rawAttributes = object(evidence.stable_attributes ?? {}, "surface_annotation_invalid_attributes");
  const stable_attributes: Record<string, string> = {};
  for (const [name, raw] of Object.entries(rawAttributes)) {
    if (!ATTRIBUTES.has(name)) throw new Error("surface_annotation_unsupported_attribute");
    stable_attributes[name] = string(raw, 200, "surface_annotation_invalid_attribute", true)!;
  }
  return {
    ...(string(evidence.source_id, 300, "surface_annotation_invalid_source_id") ? { source_id: evidence.source_id as string } : {}),
    ...(string(evidence.role, 100, "surface_annotation_invalid_role") ? { role: evidence.role as string } : {}),
    ...(string(evidence.accessible_name, 300, "surface_annotation_invalid_accessible_name")
      ? { accessible_name: evidence.accessible_name as string } : {}),
    tag: tag.toLowerCase(), stable_attributes,
    ancestry_fingerprint: string(evidence.ancestry_fingerprint, 2_000, "surface_annotation_invalid_ancestry", true)!,
    ...(string(evidence.rendered_text_hash, 128, "surface_annotation_invalid_text_hash")
      ? { rendered_text_hash: evidence.rendered_text_hash as string } : {}),
    ...(string(evidence.rendered_text_excerpt, 500, "surface_annotation_invalid_text_excerpt")
      ? { rendered_text_excerpt: evidence.rendered_text_excerpt as string } : {}),
    rect: validateRect(evidence.rect),
    ...(string(evidence.screenshot_artifact_id, 300, "surface_annotation_invalid_artifact")
      ? { screenshot_artifact_id: evidence.screenshot_artifact_id as string } : {}),
  };
}

export function validateSurfaceAnnotation(input: unknown, issuedAnnotationId?: string): SurfaceAnnotation {
  const mark = object(input, "surface_annotation_invalid");
  const annotation_id = issuedAnnotationId ?? string(mark.annotation_id, 100, "surface_annotation_invalid_id", true)!;
  const kind = mark.kind;
  if (kind !== "element" && kind !== "region") throw new Error("surface_annotation_invalid_kind");
  if (!BINDINGS.has(mark.binding as BindingQuality)) throw new Error("surface_annotation_invalid_binding");
  const status = (mark.status ?? "draft") as string;
  if (!MARK_STATUSES.has(status as MarkStatus)) throw new Error("surface_annotation_invalid_status");
  const route = string(mark.route, 2_000, "surface_annotation_invalid_route", true)!;
  if (!route.startsWith("/")) throw new Error("surface_annotation_invalid_route");
  const scroll = object(mark.scroll ?? {}, "surface_annotation_invalid_scroll");
  const capture_viewport = validateCaptureViewport(mark.capture_viewport);
  const evidence = validateElementEvidence(mark.evidence);
  const locator = resolveSafeLocator(evidence.stable_attributes);
  return {
    annotation_id, kind, binding: mark.binding as BindingQuality, route,
    scroll: { x: finite(scroll.x ?? 0, "surface_annotation_invalid_scroll"), y: finite(scroll.y ?? 0, "surface_annotation_invalid_scroll") },
    evidence,
    operator_request: string(mark.operator_request, 4_000, "surface_annotation_invalid_request") ?? "",
    ...(mark.preview_patch ? { preview_patch: validateApprovedPreviewPatch(mark.preview_patch)! } : {}),
    status: status as MarkStatus,
    capture_viewport,
    ...(locator ? { locator } : {}),
  };
}

export function validateAnnotationVerification(input: unknown): AnnotationVerification {
  const item = object(input, "surface_annotation_invalid_verification");
  const status = item.status;
  if (!["resolved", "partial", "stale", "failed"].includes(String(status))) {
    throw new Error("surface_annotation_invalid_verification_status");
  }
  if (!BINDINGS.has(item.binding as BindingQuality)) throw new Error("surface_annotation_invalid_verification_binding");
  return {
    annotation_id: string(item.annotation_id, 100, "surface_annotation_invalid_verification_id", true)!,
    status: status as AnnotationVerification["status"],
    binding: item.binding as BindingQuality,
    ...(string(item.reason, 1_000, "surface_annotation_invalid_verification_reason")
      ? { reason: item.reason as string } : {}),
    ...(item.current_rect !== undefined ? { current_rect: validateRect(item.current_rect) } : {}),
  };
}

export function clearAttemptFields(batch: AnnotationBatch): AnnotationBatch {
  const copy = { ...batch };
  delete copy.error;
  delete copy.failed_gate;
  delete copy.preview_commit;
  delete copy.preview_tree;
  delete copy.preview_ref;
  delete copy.attempt_id;
  delete copy.submit_idempotency_key;
  delete copy.accept_idempotency_key;
  delete copy.reject_idempotency_key;
  delete copy.rejected_attempt_id;
  return copy;
}
