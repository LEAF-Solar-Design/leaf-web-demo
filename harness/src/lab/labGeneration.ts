import { GitGenerationConflictError, type LabDocumentPort, type LabGenerationReceipt } from "../vendor/mushy-author/ports/labDocumentPort.js";
import { GitRefConflictError } from "../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { ForgePublicationError } from "../vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

export const LAB_GENERATION_MAX_BODY_BYTES = 1024;
export const LAB_FENCE_MAX_BODY_BYTES = 1024;

type LabGenerationError = { error: { code: string; message?: string; pointer?: string } };
export type LabGenerationResponseBody = { generation: string | null; generation_oid: string | null; head: string | null } | LabGenerationError;
export type LabFenceResponseBody = { receipt: LabGenerationReceipt; head: string | null } | LabGenerationError;
const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const generation = (value: unknown): value is string => typeof value === "string" && /^[1-9][0-9]{0,18}$(?![\s\S])/.test(value);
const sha = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{40}$(?![\s\S])/.test(value);
const pointerKey = (key: string) => key.replaceAll("~", "~0").replaceAll("/", "~1");
const tenantValid = (tenantId: string | undefined): tenantId is string =>
  typeof tenantId === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$(?![\s\S])/.test(tenantId);
const tenantRequired = () => ({ status: 400, body: { error: { code: "lab_tenant_required", message: "A valid tenant header is required" } } });
const invalid = (operation: string, pointer: string, message: string) => ({
  status: 422, body: { error: { code: `lab_${operation}_invalid`, pointer, message } },
});
function failure(operation: string, error: unknown) {
  if (error instanceof GitGenerationConflictError) return { status: 409, body: { error: { code: "lab_generation_conflict" } } };
  if (error instanceof GitRefConflictError) return { status: 409, body: { error: { code: "lab_ref_conflict" } } };
  if (error instanceof ForgePublicationError && error.state === "refused") return { status: 403, body: { error: { code: "lab_repository_refused" } } };
  return { status: 500, body: { error: { code: `lab_${operation}_failed`, message: `Lab ${operation} failed` } } };
}

export async function handleLabGeneration(port: LabDocumentPort, tenantId: string | undefined, body: unknown): Promise<{ status: number; body: LabGenerationResponseBody }> {
  if (!tenantValid(tenantId)) return tenantRequired();
  if (!isObject(body)) return invalid("generation", "", "Expected an object");
  for (const key of Object.keys(body)) return invalid("generation", `/${pointerKey(key)}`, "Unknown field");
  try {
    const result = await port.generation(tenantId);
    return { status: 200, body: { generation: result.generation, generation_oid: result.generationOid, head: result.head } };
  } catch (error) {
    return failure("generation", error);
  }
}

export async function handleLabFence(port: LabDocumentPort, tenantId: string | undefined, body: unknown): Promise<{ status: number; body: LabFenceResponseBody }> {
  if (!tenantValid(tenantId)) return tenantRequired();
  if (!isObject(body)) return invalid("fence", "", "Expected an object");
  for (const key of Object.keys(body)) {
    if (!["transition_id", "expected_generation", "new_generation", "expected_head"].includes(key)) return invalid("fence", `/${pointerKey(key)}`, "Unknown field");
  }
  if (typeof body.transition_id !== "string" || !/^[0-9a-f]{32}$(?![\s\S])/.test(body.transition_id)) return invalid("fence", "/transition_id", "Expected a lowercase 32 digit hex transition ID");
  if (body.expected_generation !== null && !generation(body.expected_generation)) return invalid("fence", "/expected_generation", "Expected null or a decimal generation of 1 to 19 digits without leading zero");
  if (!generation(body.new_generation)) return invalid("fence", "/new_generation", "Expected a decimal generation of 1 to 19 digits without leading zero");
  if (body.expected_head !== null && !sha(body.expected_head)) return invalid("fence", "/expected_head", "Expected null or a lowercase commit SHA");
  try {
    return { status: 200, body: await port.fence(tenantId, {
      transitionId: body.transition_id, expectedGeneration: body.expected_generation,
      newGeneration: body.new_generation, expectedHead: body.expected_head,
    }) };
  } catch (error) {
    return failure("fence", error);
  }
}
