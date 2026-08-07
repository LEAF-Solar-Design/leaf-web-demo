/** Exact Leaf mirror of the reviewed tenant-broker provider artifact contract. */
export const STANDARD_SERVICE_ARTIFACT_ID = /^[A-Za-z0-9_-]{16,256}$/;
export const MAX_STANDARD_SERVICE_ARTIFACT_IDS = 64;

export function requiredStandardServiceArtifactId(value: unknown): string {
  if (typeof value !== "string" || !STANDARD_SERVICE_ARTIFACT_ID.test(value)) {
    throw new Error("standard_services_artifact_id_invalid");
  }
  return value;
}

export function validStandardServiceArtifactIds(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length <= MAX_STANDARD_SERVICE_ARTIFACT_IDS
    && value.every((item) => typeof item === "string" && STANDARD_SERVICE_ARTIFACT_ID.test(item));
}
