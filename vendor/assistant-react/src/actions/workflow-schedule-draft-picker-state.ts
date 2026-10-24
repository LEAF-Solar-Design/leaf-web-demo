import { IntegrationRefV1 } from "../../../assistant-contract/src/integration";
import type { ScheduleDraftAvailability } from "./workflow-schedule-draft-form-state";

export type ScheduleDraftPickerAvailability = ScheduleDraftAvailability;
/** Local callback adapter, not a persisted integration contract. */
export type ScheduleDraftPageQuery = { limit: 25; after?: string };
export type ScheduleDraftReferencePage = { references: IntegrationRefV1[]; nextCursor: string | null };

export const scheduleDraftPickerCopy = {
  idle: "Load saved schedule draft references to choose a draft.",
  pending: "Loading schedule draft references.",
  failure: "Schedule draft references could not be loaded. Retry the read or refresh the first page.",
  empty: "No schedule draft references on this page. There are no more pages.",
  emptyContinuation: "No schedule draft references on this page. Another page is available.",
  unavailable: "Schedule draft discovery is unavailable.",
  forbidden: "You do not have permission to discover schedule drafts.",
  selected: "Draft reference selected. Nothing is scheduled or authorized to run.",
  selectionFailure: "The draft reference could not be selected. Choose a draft again.",
} as const;

/** Reject the entire page; never partially display unverified references. */
export function parseScheduleDraftReferencePage(value: unknown, after?: string): ScheduleDraftReferencePage | undefined {
  try {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
    const keys = Object.keys(value);
    if (keys.length !== 2 || !keys.includes("references") || !keys.includes("nextCursor")) return undefined;
    const page = value as Record<string, unknown>;
    const rawReferences = page.references;
    const nextCursor = page.nextCursor;
    if (!Array.isArray(rawReferences) || rawReferences.length > 25) return undefined;
    if (nextCursor !== null && (typeof nextCursor !== "string" || nextCursor.length === 0 || nextCursor.length > 2048 || nextCursor === after)) return undefined;
    const references: IntegrationRefV1[] = [];
    for (let index = 0; index < rawReferences.length; index++) {
      const parsed = IntegrationRefV1.safeParse(rawReferences[index]);
      if (!parsed.success) return undefined;
      references.push(parsed.data);
    }
    return { references, nextCursor: nextCursor as string | null };
  } catch {
    return undefined;
  }
}

export function cloneScheduleDraftReference(reference: IntegrationRefV1): IntegrationRefV1 {
  return { id: reference.id, version: reference.version, digest: reference.digest };
}

export function scheduleDraftPageMessage(page: ScheduleDraftReferencePage): string {
  if (page.references.length === 0) return page.nextCursor === null ? scheduleDraftPickerCopy.empty : scheduleDraftPickerCopy.emptyContinuation;
  return `${page.references.length} schedule draft reference${page.references.length === 1 ? "" : "s"} loaded. ${page.nextCursor === null ? "There are no more pages." : "Another page is available."}`;
}
