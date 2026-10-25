import { LabHistoryError, type LabDocumentPort } from "../vendor/mushy-author/ports/labDocumentPort.js";
import { GitRefConflictError } from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { ForgePublicationError } from "../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

export const LAB_OPS_MAX_BODY_BYTES = 8 * 1024;
export const LAB_CHANGES_MAX_BODY_BYTES = 2 * 1024;
type LabHistoryErrorBody = { error: { code: string; message?: string; pointer?: string } };
export type LabOpsResponseBody = { ops: Record<string, { commit: string; doc_id: string; revision: number; sha256: string } | null> } | LabHistoryErrorBody;
export type LabChangesResponseBody = { commits: { commit: string; parent: string | null; changed_paths: string[]; trailers: Record<string, string> }[]; next: string | null } | LabHistoryErrorBody;
const isObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const sha = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{40}$(?![\s\S])/.test(value);
const tenantValid = (tenantId: string | undefined): tenantId is string => typeof tenantId === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$(?![\s\S])/.test(tenantId);
const tenantRequired = () => ({ status: 400, body: { error: { code: "lab_tenant_required", message: "A valid tenant header is required" } } });
const invalid = (operation: string, pointer: string, message: string) => ({ status: 422, body: { error: { code: `lab_${operation}_invalid`, pointer, message } } });
const pointerKey = (key: string) => key.replaceAll("~", "~0").replaceAll("/", "~1");
function failure(operation: string, error: unknown) {
  if (error instanceof LabHistoryError) return { status: error.code === "lab_history_invalid" ? 422 : 409, body: { error: { code: error.code } } };
  if (error instanceof GitRefConflictError) return { status: 409, body: { error: { code: "lab_ref_conflict" } } };
  if (error instanceof ForgePublicationError && error.state === "refused") return { status: 403, body: { error: { code: "lab_repository_refused" } } };
  return { status: 500, body: { error: { code: `lab_${operation}_failed`, message: `Lab ${operation} failed` } } };
}

export async function handleLabOps(port: LabDocumentPort, tenantId: string | undefined, body: unknown): Promise<{ status: number; body: LabOpsResponseBody }> {
  if (!tenantValid(tenantId)) return tenantRequired();
  if (!isObject(body)) return invalid("ops", "", "Expected an object");
  for (const key of Object.keys(body)) if (!["op_ids", "head"].includes(key)) return invalid("ops", `/${pointerKey(key)}`, "Unknown field");
  if (!Array.isArray(body.op_ids) || body.op_ids.length < 1 || body.op_ids.length > 64) return invalid("ops", "/op_ids", "Expected 1 to 64 distinct operation IDs");
  const opIds: string[] = [];
  for (const [index, id] of body.op_ids.entries()) {
    if (typeof id !== "string" || !/^[0-9a-f]{32}$(?![\s\S])/.test(id) || opIds.includes(id)) return invalid("ops", `/op_ids/${index}`, "Expected a distinct lowercase 32 digit hex operation ID");
    opIds.push(id);
  }
  if (!sha(body.head)) return invalid("ops", "/head", "Expected a lowercase commit SHA");
  try {
    const result = await port.lookupOps(tenantId, { opIds, head: body.head });
    return { status: 200, body: { ops: Object.fromEntries(Object.entries(result.ops).map(([id, op]) => [id, op === null ? null : {
      commit: op.commit, doc_id: op.docId, revision: op.revision, sha256: op.sha256,
    }])) } };
  } catch (error) {
    return failure("ops", error);
  }
}

export async function handleLabChanges(port: LabDocumentPort, tenantId: string | undefined, body: unknown): Promise<{ status: number; body: LabChangesResponseBody }> {
  if (!tenantValid(tenantId)) return tenantRequired();
  if (!isObject(body)) return invalid("changes", "", "Expected an object");
  for (const key of Object.keys(body)) if (!["since", "head", "limit", "after"].includes(key)) return invalid("changes", `/${pointerKey(key)}`, "Unknown field");
  if (body.since !== null && !sha(body.since)) return invalid("changes", "/since", "Expected null or a lowercase commit SHA");
  if (!sha(body.head)) return invalid("changes", "/head", "Expected a lowercase commit SHA");
  if (Object.hasOwn(body, "limit") && (typeof body.limit !== "number" || !Number.isInteger(body.limit) || body.limit < 1 || body.limit > 1000)) return invalid("changes", "/limit", "Expected an integer from 1 to 1000");
  if (Object.hasOwn(body, "after") && !sha(body.after)) return invalid("changes", "/after", "Expected a lowercase commit SHA");
  try {
    const result = await port.changes(tenantId, { since: body.since, head: body.head,
      ...(typeof body.limit === "number" ? { limit: body.limit } : {}),
      ...(typeof body.after === "string" ? { after: body.after } : {}),
    });
    return { status: 200, body: { commits: result.commits.map(entry => ({
      commit: entry.commit, parent: entry.parent, changed_paths: entry.changedPaths, trailers: entry.trailers,
    })), next: result.next } };
  } catch (error) {
    return failure("changes", error);
  }
}
