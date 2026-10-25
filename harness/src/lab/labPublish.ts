import { GitGenerationConflictError, LAB_DOCUMENT_REF, type LabDocumentPort } from "../vendor/mushy-author/ports/labDocumentPort.js";
import { GitRefConflictError } from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { ForgePublicationError } from "../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

export const LAB_PUBLISH_MAX_BODY_BYTES = 1024;

export type LabPublishResponseBody =
  | { ref: string; published: string }
  | { error: { code: string; message?: string; pointer?: string; expected_sha?: string | null; observed_sha?: string | null } };

export async function handleLabPublish(
  port: LabDocumentPort,
  tenantId: string | undefined,
  body: unknown,
): Promise<{ status: number; body: LabPublishResponseBody }> {
  if (!tenantId || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$(?![\s\S])/.test(tenantId)) {
    return { status: 400, body: { error: { code: "lab_tenant_required", message: "A valid tenant header is required" } } };
  }
  const invalid = (pointer: string, message: string) => ({
    status: 422, body: { error: { code: "lab_publish_invalid", message, pointer } },
  });
  if (body === null || typeof body !== "object" || Array.isArray(body)) return invalid("", "Expected an object");
  for (const key of Object.keys(body)) {
    if (!["expected_remote_sha", "generation", "target_sha"].includes(key)) return invalid(`/${key.replaceAll("~", "~0").replaceAll("/", "~1")}`, "Unknown field");
  }
  const fields = body as Record<string, unknown>;
  const expectedSha = fields.expected_remote_sha;
  if (expectedSha !== null && (typeof expectedSha !== "string" || !/^[0-9a-f]{40}$(?![\s\S])/.test(expectedSha) || expectedSha === "0".repeat(40))) {
    return invalid("/expected_remote_sha", "Expected null or a nonzero lowercase commit SHA");
  }
  if (Object.hasOwn(fields, "generation") && (typeof fields.generation !== "string" || !/^[1-9][0-9]{0,18}$(?![\s\S])/.test(fields.generation))) {
    return invalid("/generation", "Expected a decimal generation of 1 to 19 digits without leading zero");
  }
  if (Object.hasOwn(fields, "target_sha") && (typeof fields.target_sha !== "string" || !/^[0-9a-f]{40}$(?![\s\S])/.test(fields.target_sha))) {
    return invalid("/target_sha", "Expected a lowercase commit SHA");
  }
  try {
    const options = {
      ...(typeof fields.generation === "string" ? { generation: fields.generation } : {}),
      ...(typeof fields.target_sha === "string" ? { targetSha: fields.target_sha } : {}),
    };
    const published = Object.keys(options).length
      ? await port.publishRef(tenantId, expectedSha, options)
      : await port.publishRef(tenantId, expectedSha);
    return { status: 200, body: { ref: LAB_DOCUMENT_REF, published: published.commit } };
  } catch (error) {
    if (error instanceof GitGenerationConflictError) return { status: 409, body: { error: { code: "lab_generation_conflict" } } };
    if (error instanceof GitRefConflictError) {
      return { status: 409, body: { error: { code: "lab_publish_conflict", expected_sha: error.expectedSha, observed_sha: error.observedSha } } };
    }
    if (error instanceof ForgePublicationError) {
      const status = { "not-published": 503, refused: 403, unknown: 502 }[error.state];
      const code = { "not-published": "lab_publish_not_published", refused: "lab_publish_refused", unknown: "lab_publish_unknown" }[error.state];
      return { status, body: { error: { code } } };
    }
    return { status: 500, body: { error: { code: "lab_publish_failed", message: "Lab publish failed" } } };
  }
}
