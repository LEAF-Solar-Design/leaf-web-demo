import { LAB_DOCUMENT_REF, type LabDocumentPort } from "../vendor/mushy-author/ports/labDocumentPort.js";
import { GitRefConflictError } from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { ForgePublicationError } from "../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

export const LAB_COMMIT_MAX_BODY_BYTES = 4 * 1024 * 1024;

export type LabCommitResponseBody =
  | { ref: string; commit: string; published: string | null }
  | {
    error: {
      code: string;
      message?: string;
      pointer?: string;
      expected_sha?: string | null;
      observed_sha?: string | null;
    };
    committed?: string;
  };

const SHA = /^[0-9a-f]{40}$(?![\s\S])/;
const DOCUMENT_PATH = /^lab\/(graphs|widgets)\/[A-Za-z0-9][A-Za-z0-9_-]*\.json$(?![\s\S])/;
const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const isSha = (value: unknown): value is string | null =>
  value === null || (typeof value === "string" && SHA.test(value));
const pointerKey = (key: string): string => key.replaceAll("~", "~0").replaceAll("/", "~1");

/** tenant from the server header only; no retry; unknown publication is never success */
export async function handleLabCommit(
  port: LabDocumentPort,
  tenantId: string | undefined,
  body: unknown,
): Promise<{ status: number; body: LabCommitResponseBody }> {
  if (!tenantId || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$(?![\s\S])/.test(tenantId)) {
    return { status: 400, body: { error: { code: "lab_tenant_required", message: "A valid tenant header is required" } } };
  }
  const invalid = (pointer: string, message: string) => ({
    status: 422, body: { error: { code: "lab_commit_invalid", message, pointer } },
  });
  if (!isObject(body)) return invalid("", "Expected an object");
  for (const key of Object.keys(body)) {
    if (!["expected_sha", "documents", "message", "publish"].includes(key)) {
      return invalid(`/${pointerKey(key)}`, "Unknown field");
    }
  }
  if (!isSha(body.expected_sha) || body.expected_sha === "0".repeat(40)) {
    return invalid("/expected_sha", "Expected null or a nonzero lowercase commit SHA");
  }
  if (!Array.isArray(body.documents) || body.documents.length < 1 || body.documents.length > 64) {
    return invalid("/documents", "Expected 1 to 64 documents");
  }
  const documents: { path: string; content: string }[] = [];
  const paths = new Set<string>();
  for (const [index, doc] of body.documents.entries()) {
    const pointer = `/documents/${index}`;
    if (!isObject(doc)) return invalid(pointer, "Expected a document object");
    for (const key of Object.keys(doc)) {
      if (key !== "path" && key !== "content") return invalid(`${pointer}/${pointerKey(key)}`, "Unknown field");
    }
    if (typeof doc.path !== "string" || !DOCUMENT_PATH.test(doc.path) || paths.has(doc.path)) {
      return invalid(`${pointer}/path`, "Expected a unique Lab document path");
    }
    if (typeof doc.content !== "string" || Buffer.byteLength(doc.content, "utf8") > 1024 * 1024) {
      return invalid(`${pointer}/content`, "Expected JSON text of at most 1 MiB");
    }
    try {
      if (!isObject(JSON.parse(doc.content))) return invalid(`${pointer}/content`, "Expected a JSON object");
    } catch {
      return invalid(`${pointer}/content`, "Expected a JSON object");
    }
    paths.add(doc.path);
    documents.push({ path: doc.path, content: doc.content });
  }
  if (typeof body.message !== "string" || !body.message.trim() || body.message.length > 512) {
    return invalid("/message", "Expected a nonblank message of 1 to 512 characters");
  }
  let publish: { expectedSha: string | null } | undefined;
  if (Object.hasOwn(body, "publish")) {
    if (!isObject(body.publish)) return invalid("/publish", "Expected a publish object");
    for (const key of Object.keys(body.publish)) {
      if (key !== "expected_remote_sha") return invalid(`/publish/${pointerKey(key)}`, "Unknown field");
    }
    if (!isSha(body.publish.expected_remote_sha)) return invalid("/publish/expected_remote_sha", "Expected null or a lowercase commit SHA");
    publish = { expectedSha: body.publish.expected_remote_sha };
  }
  const failed = () => ({ status: 500, body: { error: { code: "lab_commit_failed", message: "Lab commit failed" } } });
  const conflict = (error: GitRefConflictError, code: string) => ({
    code, expected_sha: error.expectedSha, observed_sha: error.observedSha,
  });
  let commit: string;
  try {
    ({ commit } = await port.commit({ tenantId, expectedSha: body.expected_sha, documents, message: body.message }));
  } catch (error) {
    if (error instanceof GitRefConflictError) return { status: 409, body: { error: conflict(error, "lab_ref_conflict") } };
    if (error instanceof ForgePublicationError) return { status: 403, body: { error: { code: "lab_repository_refused" } } };
    return failed();
  }
  if (!publish) return { status: 200, body: { ref: LAB_DOCUMENT_REF, commit, published: null } };
  try {
    const published = await port.publishRef(tenantId, publish.expectedSha);
    return { status: 200, body: { ref: LAB_DOCUMENT_REF, commit, published: published.commit } };
  } catch (error) {
    if (error instanceof GitRefConflictError) return { status: 409, body: { error: conflict(error, "lab_publish_conflict"), committed: commit } };
    if (error instanceof ForgePublicationError) {
      const status = { "not-published": 503, refused: 403, unknown: 502 }[error.state];
      const code = { "not-published": "lab_publish_not_published", refused: "lab_publish_refused", unknown: "lab_publish_unknown" }[error.state];
      return { status, body: { error: { code }, committed: commit } };
    }
    return failed();
  }
}
