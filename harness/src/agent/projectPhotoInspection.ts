import { createHash } from "node:crypto";
import { z } from "zod";

export const PHOTO_FAILURE = "Project photo inspection unavailable.";
export const PHOTO_MAX_RESPONSE = 1536 * 1024;
const PHOTO_MAX_ATTEMPTS = 3;
type PhotoRead = (body: ProjectPhotoInspectionInput) => Promise<unknown>;
export type PhotoReadFailureClass = "transport" | "authorization" | "integrity" | "source_changed" | "unavailable";
type PhotoReadError = { photoFailureClass?: PhotoReadFailureClass };
type PhotoFailureReason = "transport_exhausted" | "access_denied" | "source_changed" | "unavailable" | "integrity_failure";
class PhotoInspectionFailure extends Error {
  constructor(readonly reason: PhotoFailureReason) { super(PHOTO_FAILURE); }
}
// Only the client's typed transport failures are retryable. An opaque status,
// socket code or abort may conceal an authority or response-integrity failure.
function retryable(error: unknown): boolean {
  return !!error && typeof error === "object" && (error as PhotoReadError).photoFailureClass === "transport";
}
function failureReason(error: unknown): PhotoFailureReason {
  if (error instanceof PhotoInspectionFailure) return error.reason;
  if (retryable(error)) return "transport_exhausted";
  const kind = (error as PhotoReadError | null)?.photoFailureClass;
  if (kind === "authorization") return "access_denied";
  if (kind === "source_changed") return "source_changed";
  if (kind === "unavailable") return "unavailable";
  if (kind === "integrity") return "integrity_failure";
  const status = (error as { status?: number } | null)?.status;
  return status === 401 || status === 403 ? "access_denied" : status === 409 ? "source_changed"
    : status === 404 || (typeof status === "number" && status >= 500) ? "unavailable" : "integrity_failure";
}
const uuid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/)
  .refine(value => value !== "00000000-0000-0000-0000-000000000000");
const digest = z.string().regex(/^[0-9a-f]{64}(?![\s\S])/);
const revision = z.number().int().min(0).max(2147483647);
const identity = z.string().min(1).max(200).refine(value => value.trim() === value && !/[\p{C}]/u.test(value));
const pair = (value: { expected_revision?: number; expected_digest?: string | null }) =>
  value.expected_revision === undefined ? value.expected_digest === undefined
    : value.expected_revision === 0 ? value.expected_digest === null : typeof value.expected_digest === "string";
export const ProjectPhotoInspectionShape = {
  op: z.enum(["catalog", "preview"]), cursor: uuid.optional(), material_id: uuid.optional(),
  expected_revision: revision.optional(), expected_digest: digest.nullable().optional(),
  expected_preview_sha256: digest.optional(),
};
export const ProjectPhotoInspectionRequest = z.strictObject(ProjectPhotoInspectionShape).refine(value =>
  pair(value) && (value.op === "catalog"
    ? value.material_id === undefined && value.expected_preview_sha256 === undefined &&
      (value.cursor === undefined || value.expected_revision !== undefined)
    : value.cursor === undefined && value.material_id !== undefined &&
      value.expected_preview_sha256 !== undefined && value.expected_revision !== undefined));
export type ProjectPhotoInspectionInput = z.infer<typeof ProjectPhotoInspectionRequest>;
export function photoRequest(value: unknown): ProjectPhotoInspectionInput {
  try {
    if (Buffer.byteLength(JSON.stringify(value), "utf8") > 4096) throw new Error();
    return ProjectPhotoInspectionRequest.parse(value);
  } catch { throw new Error(PHOTO_FAILURE); }
}
const text = (max: number) => z.string().max(max).refine(value =>
  !/[\p{Cc}\p{Cf}\p{Cs}<>]/u.test(value.replaceAll("\n", "")) &&
  !/(?:[a-z][a-z0-9+.-]*:\/\/|\b(?:data|javascript|file):|\bwww\.)/i.test(value));
const assignment = z.strictObject({
  gallery_slug: z.string().regex(/^[a-z][a-z0-9-]{0,47}(?![\s\S])/),
  gallery_title: text(120), index: z.number().int().min(0).max(199), hero: z.boolean(),
  caption: text(200), alt: text(400),
});
const assignments = z.array(assignment).max(12).refine(rows =>
  new Set(rows.map(row => row.gallery_slug)).size === rows.length);
const source = z.strictObject({ project_id: uuid, revision, digest: digest.nullable() })
  .refine(value => value.revision === 0 ? value.digest === null : value.digest !== null);
const common = {
  org_id: uuid, project_id: uuid, session_id: identity, turn_id: identity, source,
  assignment_basis: z.enum(["saved_project_page", "no_saved_page"]),
};
const material = z.strictObject({
  material_id: uuid, name: text(255).refine(value => value.length > 0), media_type: z.enum(["image/jpeg", "image/png", "image/heif"]),
  byte_size: z.number().int().positive().max(16 * 1024 * 1024), sha256: digest, preview_sha256: digest,
  width: z.number().int().positive().max(40000000), height: z.number().int().positive().max(40000000),
  created_at: z.string().min(1).max(64).refine(value =>
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))),
  assignments,
}).refine(value => value.width * value.height <= 40000000);
const catalog = z.strictObject({
  ...common, schema: z.literal("leaf.project-material-catalog.v1"),
  materials: z.array(material).max(50), next_cursor: uuid.nullable(), page_count: z.number().int().min(0).max(50),
});
const preview = z.strictObject({
  ...common, schema: z.literal("leaf.project-material-image.v1"), material_id: uuid,
  original_sha256: digest, preview_sha256: digest, media_type: z.literal("image/jpeg"), sha256: digest,
  inspection_sha256: digest.optional(), derived_from_sha256: digest.optional(),
  byte_size: z.number().int().positive().max(1024 * 1024),
  data: z.string().min(1).max(1398104), assignments,
});
export type PhotoContent = { type: "text"; text: string } | { type: "image"; data: string; mimeType: "image/jpeg" };
export type PhotoResult = { content: string; isError?: boolean };
// Object identity is the private executor-to-SDK capability. JSON/event projection
// cannot accidentally serialize pixels, and arbitrary injected results cannot emit them.
const projections = new WeakMap<PhotoResult, PhotoContent[]>();
export function projectPhotoResult(value: PhotoResult): { content: PhotoContent[]; isError?: boolean } {
  const content = projections.get(value);
  return content ? { content: content.map(block => ({ ...block })) }
    : { content: [{ type: "text", text: PHOTO_FAILURE }], isError: true };
}

/** One instance per active executor. No context-packet sample grants membership. */
export class ProjectPhotoInspection {
  private witness?: z.infer<typeof source>;
  private next: string | null = null;
  private readonly rows = new Map<string, z.infer<typeof material>>();
  private pending: Promise<void> = Promise.resolve();
  private failed = false;
  private catalogFailure?: PhotoFailureReason;
  private readonly inspected = new Set<string>();
  private readonly failures = new Map<string, PhotoFailureReason>();
  constructor(private readonly authority: { orgId: string; sessionId: string; turnId: string }) {}

  /** Saved means present in the witnessed page, never merely proposed. */
  reconciliation(proposedIds: string[] = []) {
    const proposed = new Set(proposedIds);
    return {
      catalog_complete: !!this.witness && this.next === null && !this.failed,
      catalogued_ids: [...this.rows.keys()], inspected_ids: [...this.inspected],
      proposed_ids: [...proposed],
      saved_ids: [...this.rows.values()].filter(row => row.assignments.length > 0).map(row => row.material_id),
      unresolved: [...this.rows.values()].filter(row => row.assignments.length === 0).map(row => ({
        material_id: row.material_id,
        reason: this.failures.get(row.material_id) ?? (!this.inspected.has(row.material_id) ? "not_inspected"
          : proposed.has(row.material_id) ? "awaiting_explicit_save" : "inspected_not_proposed"),
      })),
    };
  }

  /** A single tool call traverses all pages, retaining every validated member. */
  async inspectCatalog(raw: unknown, read: PhotoRead): Promise<PhotoResult> {
    try { if (photoRequest(raw).op !== "catalog") throw new Error(); }
    catch { return { content: PHOTO_FAILURE, isError: true }; }
    let result = await this.inspect(raw, read);
    if (result.isError) return result;
    const first = JSON.parse(result.content);
    while (this.next && this.witness) {
      result = await this.inspect({ op: "catalog", cursor: this.next,
        expected_revision: this.witness.revision, expected_digest: this.witness.digest }, read);
      if (result.isError) return result;
    }
    // PHOTO_MAX_RESPONSE bounds each transport page, not the whole catalog.
    const materials = [...this.rows.values()];
    const output = { content: JSON.stringify({ ...first, materials, page_count: materials.length,
      next_cursor: null, reconciliation: this.reconciliation() }) };
    projections.set(output, [{ type: "text", text: output.content }]);
    return output;
  }

  private async readWithRetry(body: ProjectPhotoInspectionInput, read: PhotoRead): Promise<unknown> {
    for (let attempt = 1; ; attempt++) {
      try { return await read(body); }
      catch (error) {
        if (!retryable(error) || attempt === PHOTO_MAX_ATTEMPTS) throw error;
        if (body.op === "preview") {
          // Reacquire source and membership witnesses before another pixel read.
          // Probe reads do not recursively retry or change this traversal.
          const probe = new ProjectPhotoInspection(this.authority);
          const fresh = await probe.inspectCatalog({ op: "catalog", expected_revision: body.expected_revision,
            expected_digest: body.expected_digest }, async request => {
            try { return await read(request); } catch (error) { throw new PhotoInspectionFailure(failureReason(error)); }
          });
          if (fresh.isError) throw new PhotoInspectionFailure(probe.catalogFailure ?? "integrity_failure");
          if (JSON.stringify(probe.witness) !== JSON.stringify(this.witness) ||
              JSON.stringify(probe.rows.get(body.material_id!)) !== JSON.stringify(this.rows.get(body.material_id!))) {
            throw new PhotoInspectionFailure("source_changed");
          }
        }
      }
    }
  }

  async inspect(raw: unknown, read: PhotoRead): Promise<PhotoResult> {
    // The model may request many previews in one SDK turn. Serialize witness
    // validation instead of rejecting all but the first parallel tool call.
    const previous = this.pending;
    let release!: () => void;
    this.pending = new Promise<void>(resolve => { release = resolve; });
    await previous;
    try { return await this.inspectOne(raw, read); }
    finally { release(); }
  }

  private async inspectOne(raw: unknown, read: PhotoRead): Promise<PhotoResult> {
    if (this.failed) return { content: PHOTO_FAILURE, isError: true };
    let requested: ProjectPhotoInspectionInput | undefined;
    let reading = false;
    let authorityMismatch = false;
    try {
      uuid.parse(this.authority.orgId); identity.parse(this.authority.sessionId); identity.parse(this.authority.turnId);
      const body = photoRequest(raw);
      requested = body;
      if (this.witness) {
        if (body.expected_revision !== this.witness.revision || body.expected_digest !== this.witness.digest ||
            (body.op === "catalog" && (!this.next || body.cursor !== this.next))) throw new Error();
      } else if (body.op !== "catalog" || body.cursor !== undefined) throw new Error();
      const member = body.op === "preview" ? this.rows.get(body.material_id!) : undefined;
      if (body.op === "preview" && (!member || member.preview_sha256 !== body.expected_preview_sha256)) throw new Error();
      if (body.op === "preview" && this.failures.has(body.material_id!)) throw new Error();
      reading = true;
      const rawResult = await this.readWithRetry(body, read);
      if (Buffer.byteLength(JSON.stringify(rawResult), "utf8") > PHOTO_MAX_RESPONSE) throw new Error();
      const result = body.op === "catalog" ? catalog.parse(rawResult) : preview.parse(rawResult);
      if (result.org_id !== this.authority.orgId || result.session_id !== this.authority.sessionId ||
          result.turn_id !== this.authority.turnId || result.project_id !== result.source.project_id) {
        authorityMismatch = true;
        throw new Error();
      }
      if (result.assignment_basis !== (result.source.revision === 0 ? "no_saved_page" : "saved_project_page")) throw new Error();
      if ((this.witness && JSON.stringify(result.source) !== JSON.stringify(this.witness)) ||
          (body.expected_revision !== undefined && (body.expected_revision !== result.source.revision || body.expected_digest !== result.source.digest))) {
        throw new PhotoInspectionFailure("source_changed");
      }
      let blocks: PhotoContent[];
      let metadata: string;
      if (result.schema === "leaf.project-material-catalog.v1") {
        if (result.page_count !== result.materials.length ||
            new Set(result.materials.map(row => row.material_id)).size !== result.materials.length ||
            result.materials.some((row, index) => row.material_id <= (index ? result.materials[index - 1]!.material_id : body.cursor ?? "")) ||
            result.materials.some(row => this.rows.has(row.material_id) || (!result.source.revision && row.assignments.length)) ||
            (result.next_cursor !== null && (!result.materials.length || result.next_cursor !== result.materials.at(-1)!.material_id))) throw new Error();
        metadata = JSON.stringify(result);
        blocks = [{ type: "text", text: metadata }];
        this.witness = result.source;
        this.next = result.next_cursor;
        for (const row of result.materials) this.rows.set(row.material_id, row);
      } else {
        if (!member || result.material_id !== member.material_id || result.original_sha256 !== member.sha256 ||
            result.preview_sha256 !== member.preview_sha256 || result.sha256 !== member.preview_sha256 ||
            JSON.stringify(result.assignments) !== JSON.stringify(member.assignments)) throw new Error();
        const bytes = Buffer.from(result.data, "base64");
        // sha256 remains the immutable stored-preview witness used by saved
        // assignments. A bounded inspection derivative has a separate digest.
        if ((result.inspection_sha256 === undefined) !== (result.derived_from_sha256 === undefined) ||
            (result.derived_from_sha256 !== undefined && result.derived_from_sha256 !== member.preview_sha256)) throw new Error();
        if (bytes.length !== result.byte_size || bytes.toString("base64") !== result.data ||
            bytes.length < 5 || bytes[0] !== 255 || bytes[1] !== 216 || bytes[2] !== 255 ||
            bytes.at(-2) !== 255 || bytes.at(-1) !== 217 ||
            createHash("sha256").update(bytes).digest("hex") !== (result.inspection_sha256 ?? member.preview_sha256)) throw new Error();
        const { data, ...publicResult } = result;
        this.inspected.add(result.material_id);
        metadata = JSON.stringify({ ...publicResult, reconciliation: this.reconciliation() });
        blocks = [{ type: "text", text: metadata }, { type: "image", data, mimeType: "image/jpeg" }];
      }
      const output = { content: metadata };
      projections.set(output, blocks);
      return output;
    } catch (error) {
      if (requested?.op === "preview" && reading) {
        this.failures.set(requested.material_id!, failureReason(error));
        const output = { content: PHOTO_FAILURE, isError: true };
        // Preserve the generic SDK failure for a foreign response. The local
        // reconciliation still reports the authorized member's failed read.
        if (authorityMismatch) return output;
        projections.set(output, [{ type: "text", text: JSON.stringify({ error: PHOTO_FAILURE,
          material_id: requested.material_id, reason: failureReason(error), reconciliation: this.reconciliation() }) }]);
        return output;
      } else if (requested?.op === "catalog" && reading && !retryable(error)) {
        // Catalog authority/integrity drift invalidates the shared witness;
        // individual preview failures never discard other photos' membership.
        this.failed = true;
        this.catalogFailure = failureReason(error);
      }
      return { content: PHOTO_FAILURE, isError: true };
    }
  }
}
