import { z } from "zod";
import {
  IntegrationRefV1,
  IntegrationActionFieldV1,
  IntegrationActionFormV1,
  IntegrationActionCatalogEntryV1,
  IntegrationActionCatalogV1,
  IntegrationActionElementV1,
  IntegrationActionSnapshotV1,
  IntegrationActionSubmissionV1,
  IntegrationActionRunV1,
  IntegrationActionReceiptV1,
  IntegrationActionSubmissionResultV1,
  IntegrationManualWorkflowStepV1,
  IntegrationManualWorkflowDefinitionV1,
  IntegrationManualWorkflowSubmissionV1,
  IntegrationManualWorkflowStepSnapshotV1,
  IntegrationManualWorkflowRunV1,
  IntegrationManualWorkflowSnapshotV1,
  IntegrationManualWorkflowSubmissionResultV1,
  CredentialReferenceV1,
  ConnectionV1,
  TransformMappingV1,
  DatasetFieldV1,
  DatasetDefinitionV1,
  OperationV1,
  ConnectorDefinitionV1,
  DatasetQueryV1,
  DatasetReadV1,
  DatasetElementV1,
  IntegrationElementV1,
} from "./integration";
import { ApproveRequestV1, ConfirmCardV1 } from "./card";
import { ProposalV1 } from "./proposal";
import { ErrorV1, Id, ReasonV1, Seq } from "./reason";
import {
  BlockV1,
  EventsPageV1,
  GoldenConversationV1,
  ItemV1,
  ThreadEventV1,
  ThreadV1,
  ToolCallV1,
  TurnRequestV1,
  WorkerEventV1,
  WorkerEventsBatchV1,
} from "./thread";

// Emission is pure and deterministic: the same zod source always yields the
// same documents, so the committed schema/*.json files are checked by a plain
// deep-equality test. zod inlines every reused schema; dedupeRefs then swaps
// any subtree that equals a named definition for a $ref, top-down, so each
// definition appears exactly once.

export const CONTRACT_VERSION = "1.0.0";

/** Named definitions, in emission order. A new top-level schema is added here once. */
export const SCHEMA_DEFS = {
  ReasonV1,
  ErrorV1,
  ProposalV1,
  ConfirmCardV1,
  ApproveRequestV1,
  ToolCallV1,
  BlockV1,
  ItemV1,
  ThreadV1,
  ThreadEventV1,
  EventsPageV1,
  TurnRequestV1,
  WorkerEventV1,
  WorkerEventsBatchV1,
  GoldenConversationV1,
  IntegrationRefV1,
  IntegrationActionFieldV1,
  IntegrationActionFormV1,
  IntegrationActionCatalogEntryV1,
  IntegrationActionCatalogV1,
  IntegrationActionElementV1,
  IntegrationActionSnapshotV1,
  IntegrationActionSubmissionV1,
  IntegrationActionRunV1,
  IntegrationActionReceiptV1,
  IntegrationActionSubmissionResultV1,
  IntegrationManualWorkflowStepV1,
  IntegrationManualWorkflowDefinitionV1,
  IntegrationManualWorkflowSubmissionV1,
  IntegrationManualWorkflowStepSnapshotV1,
  IntegrationManualWorkflowRunV1,
  IntegrationManualWorkflowSnapshotV1,
  IntegrationManualWorkflowSubmissionResultV1,
  CredentialReferenceV1,
  ConnectionV1,
  TransformMappingV1,
  DatasetFieldV1,
  DatasetDefinitionV1,
  OperationV1,
  ConnectorDefinitionV1,
  DatasetQueryV1,
  DatasetReadV1,
  DatasetElementV1,
  IntegrationElementV1,
} as const;

export type SchemaName = keyof typeof SCHEMA_DEFS;
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type JsonObject = { [key: string]: Json };

function emitRaw(schema: z.ZodType): JsonObject {
  const json = z.toJSONSchema(schema) as JsonObject;
  delete json.$schema;
  return json;
}

/** Key-sorted serialization, so structural equality does not depend on key order. */
function canonical(value: Json): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function dedupeRefs(node: Json, isRoot: boolean, index: Map<string, string>, refPrefix: string): Json {
  if (Array.isArray(node)) return node.map((child) => dedupeRefs(child, false, index, refPrefix));
  if (node === null || typeof node !== "object") return node;
  if (!isRoot) {
    const name = index.get(canonical(node));
    if (name !== undefined) return { $ref: `${refPrefix}${name}` };
  }
  const out: JsonObject = {};
  for (const key of Object.keys(node)) out[key] = dedupeRefs(node[key], false, index, refPrefix);
  return out;
}

/** Every named definition, with shared subtrees replaced by `$ref: <refPrefix><Name>`. */
export function emitDefinitions(refPrefix: string): Record<SchemaName, Json> {
  const raw = (Object.keys(SCHEMA_DEFS) as SchemaName[]).map((name) => [name, emitRaw(SCHEMA_DEFS[name])] as const);
  const index = new Map<string, string>();
  for (const [name, json] of raw) {
    const key = canonical(json);
    const clash = index.get(key);
    if (clash !== undefined) throw new Error(`definitions ${clash} and ${name} emit identical schemas`);
    index.set(key, name);
  }
  const out = {} as Record<SchemaName, Json>;
  for (const [name, json] of raw) out[name] = dedupeRefs(json, true, index, refPrefix);
  return out;
}

/** schema/assistant.schema.json: one JSON Schema 2020-12 bundle holding every definition. */
export function emitJsonSchema(): JsonObject {
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "urn:leaf:assistant-contract:v1",
    title: "Leaf Automation assistant contract",
    version: CONTRACT_VERSION,
    $defs: emitDefinitions("#/$defs/"),
  };
}

function param(name: string, where: "path" | "query", schema: z.ZodType): JsonObject {
  return { name, in: where, required: where === "path", schema: emitRaw(schema) };
}

function body(name: SchemaName): JsonObject {
  return { "application/json": { schema: { $ref: `#/components/schemas/${name}` } } };
}

function response(description: string, name?: SchemaName): JsonObject {
  return name === undefined ? { description } : { description, content: body(name) };
}

/** schema/openapi.json: OpenAPI 3.1. The mount prefix is consumer config (K-D8), so paths are relative. */
export function emitOpenApi(): JsonObject {
  const sessionId = param("session_id", "path", Id);
  return {
    openapi: "3.1.0",
    info: {
      title: "Leaf Automation assistant contract",
      version: CONTRACT_VERSION,
    },
    jsonSchemaDialect: "https://json-schema.org/draft/2020-12/schema",
    servers: [{ url: "/assistant/v1" }],
    paths: {
      "/sessions/{session_id}/thread": {
        get: {
          operationId: "getThread",
          parameters: [sessionId],
          responses: {
            "200": response("The thread, items in seq order.", "ThreadV1"),
            "404": response("No such session in the caller's scope.", "ErrorV1"),
          },
        },
      },
      "/sessions/{session_id}/turns": {
        post: {
          operationId: "createTurn",
          parameters: [sessionId],
          requestBody: { required: true, content: body("TurnRequestV1") },
          responses: {
            "202": response("The turn is queued.", "ThreadEventV1"),
            "429": response("Rate limited; the reason carries retry_at.", "ErrorV1"),
          },
        },
      },
      "/sessions/{session_id}/events": {
        get: {
          operationId: "readEvents",
          parameters: [
            sessionId,
            param("after", "query", Seq),
            param("wait", "query", z.number().int().min(0).max(15)),
          ],
          responses: {
            "200": response("Events after the given seq, long-polled for at most wait seconds.", "EventsPageV1"),
          },
        },
      },
      "/cards/{card_id}/approve": {
        post: {
          operationId: "approveCard",
          parameters: [param("card_id", "path", Id)],
          requestBody: { required: true, content: body("ApproveRequestV1") },
          responses: {
            "200": response("Approved; the card carries its resolution.", "ConfirmCardV1"),
            "409": response("The digest does not match, or the card was already used.", "ErrorV1"),
            "410": response("The card expired.", "ErrorV1"),
          },
        },
      },
      "/proposals/{proposal_id}": {
        get: {
          operationId: "getProposal",
          parameters: [param("proposal_id", "path", Id)],
          responses: {
            "200": response("The stored proposal.", "ProposalV1"),
            "404": response("No such proposal in the caller's scope.", "ErrorV1"),
          },
        },
      },
      "/worker/jobs/{job_id}/events": {
        post: {
          operationId: "appendWorkerEvents",
          parameters: [param("job_id", "path", Id)],
          requestBody: { required: true, content: body("WorkerEventsBatchV1") },
          responses: {
            "204": response("Accepted."),
            "400": response(
              "Refused: a worker may send only status, text, tool, error, card_ref and artifact_ref events.",
              "ErrorV1",
            ),
          },
        },
      },
    },
    components: {
      schemas: emitDefinitions("#/components/schemas/"),
    },
  };
}

/** The exact bytes committed under schema/. */
export function serialize(doc: Json): string {
  return `${JSON.stringify(doc, null, 2)}\n`;
}
