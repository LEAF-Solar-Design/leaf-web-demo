import { z } from "zod";
import { IntegrationRefV1, DatasetQueryV1, DatasetElementV1 } from "../contracts/integration.js";
import type { DatasetFieldV1, DatasetReadV1 } from "../contracts/integration.js";
import type { GateCheckResult } from "./index.js";

const ConfirmationId = z.string().regex(/^[a-f0-9]{16}(?![\s\S])/);

export const IntegrationDatasetsInput = z.strictObject({
  limit: z.number().int().min(1).max(50).default(50),
  after: z.string().min(1).max(1024).regex(/^[A-Za-z0-9_-]+(?![\s\S])/).optional(),
  confirmation_id: ConfirmationId.optional(),
});
export type IntegrationDatasetsInput = z.infer<typeof IntegrationDatasetsInput>;

export const IntegrationQueryInput = z.strictObject({
  dataset_ref: IntegrationRefV1,
  query: DatasetQueryV1,
  confirmation_id: ConfirmationId.optional(),
});
export type IntegrationQueryInput = z.infer<typeof IntegrationQueryInput>;

export const IntegrationSaveElementInput = z.strictObject({
  recipe: DatasetElementV1,
  expectedRevision: z.number().int().positive().optional(),
  confirmation_id: ConfirmationId.optional(),
});
export type IntegrationSaveElementInput = z.infer<typeof IntegrationSaveElementInput>;

/** Only the authenticated containing execution context supplies this authority. */
export interface IntegrationAuthority { sessionId: string; turnId: string }
export interface IntegrationToolResult<T> { gate: GateCheckResult; result?: T }
export interface IntegrationDatasetsResult {
  datasets: Array<{
    dataset_ref: IntegrationRefV1;
    title: string;
    fields: DatasetFieldV1[];
    freshness: DatasetReadV1["freshness"] | null;
    status: "ready" | "empty" | "stale" | "unavailable";
  }>;
  next_cursor: string | null;
}
export interface IntegrationSaveElementResult {
  element: DatasetElementV1;
  /** A positive integer, enforced by the host's canonical save contract. */
  revision: number;
}

export const INTEGRATION_TOOL_INPUTS = {
  integration_datasets: IntegrationDatasetsInput,
  integration_query: IntegrationQueryInput,
  integration_save_element: IntegrationSaveElementInput,
} as const;
