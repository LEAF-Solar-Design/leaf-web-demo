import type { LabDocumentInspection, LabDocumentPort } from "../vendor/mushy-author/ports/labDocumentPort.js";
import { ForgePublicationError } from "../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

export const LAB_INSPECT_MAX_BODY_BYTES = 4 * 1024;

export type LabInspectResponseBody = LabDocumentInspection | {
  error: { code: string; message?: string; pointer?: string };
};

export async function handleLabInspect(
  port: LabDocumentPort,
  tenantId: string | undefined,
  body: unknown,
): Promise<{ status: number; body: LabInspectResponseBody }> {
  if (!tenantId || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$(?![\s\S])/.test(tenantId)) {
    return { status: 400, body: { error: { code: "lab_tenant_required", message: "A valid tenant header is required" } } };
  }
  const invalid = (pointer: string, message: string) => ({
    status: 422, body: { error: { code: "lab_inspect_invalid", message, pointer } },
  });
  if (body === null || typeof body !== "object" || Array.isArray(body)) return invalid("", "Expected an object");
  for (const key of Object.keys(body)) {
    if (key !== "limit") return invalid(`/${key.replaceAll("~", "~0").replaceAll("/", "~1")}`, "Unknown field");
  }
  const limit = (body as Record<string, unknown>).limit;
  if (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1 || limit > 200) {
    return invalid("/limit", "Expected an integer from 1 to 200");
  }
  try {
    return { status: 200, body: await port.inspect(tenantId, { limit }) };
  } catch (error) {
    if (error instanceof ForgePublicationError && error.state === "refused") {
      return { status: 403, body: { error: { code: "lab_repository_refused" } } };
    }
    return { status: 500, body: { error: { code: "lab_inspect_failed", message: "Lab inspect failed" } } };
  }
}
