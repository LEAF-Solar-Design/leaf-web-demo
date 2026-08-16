/**
 * P8 Unit 4 coordination client. The harness sends closed, digest-only Unit 3
 * requests and strictly parses closed responses. No prompt, diff, source
 * bytes, file content, repository path, credential, or model output crosses
 * this boundary, and no response body ever reaches an error message.
 */

import {
  PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
  type ProjectRepositoryEditAuthorizePublishRequest,
  type ProjectRepositoryEditCoordination,
  type ProjectRepositoryEditPublishMatrix,
  type ProjectRepositoryEditRecordStagedRequest,
  type ProjectRepositoryEditRecordStagedResponse,
  type ProjectRepositoryEditRecoverPublishRequest,
  type ProjectRepositoryEditSettlePublishRequest,
  type ProjectRepositoryEditSettlementResponse,
  type ProjectRepositoryStagedReceipt,
} from "../index.js";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const LOWER_SHA = /^[0-9a-f]{40}$/;
const LOWER_DIGEST = /^[0-9a-f]{64}$/;
const PRIVATE_REF = /^refs\/leaf\/changes\/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const REASON_CODE = /^[a-z0-9_]{1,64}$/;

const RECEIPT_FIELDS = [
  "contract",
  "edit_id",
  "state",
  "operation",
  "source_edit_id",
  "actor_binding_id",
  "tenant_id",
  "organization_id",
  "project_id",
  "repo_key",
  "writer_lease_id",
  "writer_lease_generation",
  "base_commit",
  "staged_head_commit",
  "staged_tree",
  "changed_paths",
  "diff_digest",
  "instruction_digest",
  "idempotency_key",
] as const;

const RECORD_STAGED_REQUEST_FIELDS = [
  "contract", "action", "receipt", "receipt_digest", "expected_version", "transition_key",
] as const;
const AUTHORITY_FIELDS = ["tenant_id", "organization_id", "project_id", "repo_key"] as const;
const AUTHORIZE_PUBLISH_REQUEST_FIELDS = [
  "contract", "action", ...AUTHORITY_FIELDS, "edit_id", "actor_binding_id", "confirmation_id",
  "receipt_digest", "publish_lease_id", "publish_lease_generation", "expected_version",
  "transition_key",
] as const;
const SETTLE_PUBLISH_REQUEST_FIELDS = [
  "contract", "action", ...AUTHORITY_FIELDS, "edit_id", "actor_binding_id", "publish_lease_id",
  "publish_lease_generation", "private_ref_commit", "main_commit", "main_tree",
  "expected_version", "transition_key",
] as const;
const RECOVER_PUBLISH_REQUEST_FIELDS = [
  ...SETTLE_PUBLISH_REQUEST_FIELDS.filter((field) =>
    field !== "publish_lease_id" && field !== "publish_lease_generation"),
  "recovery_lease_id", "recovery_lease_generation", "reason_code",
] as const;

const RECORD_STAGED_RESPONSE_FIELDS = [
  "contract", "action", "edit_id", "state", "version",
] as const;
const PUBLISH_MATRIX_FIELDS = [
  "contract", "action", "edit_id", "state", "version", "receipt_digest",
  "expected_main_commit", "staged_head_commit", "staged_tree", "private_ref",
  "publish_lease_id", "publish_lease_generation",
] as const;
const SETTLEMENT_RESPONSE_FIELDS = [
  "contract", "action", "edit_id", "state", "version",
] as const;

type FetchLike = (url: string, init: {
  method: string;
  headers: Record<string, string>;
  body: string;
}) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

export interface ProjectRepositoryEditCoordinationClientOptions {
  baseUrl: string;
  dispatchSecret: string;
  fetchImpl?: FetchLike;
}

function fail(message: string): never {
  throw new Error(`project repository edit coordination: ${message}`);
}

function closedKeys(value: unknown, fields: readonly string[], label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(`${label} must be a closed object`);
  }
  const keys = Object.keys(value as Record<string, unknown>).sort();
  const expected = [...fields].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    fail(`${label} has missing or extra fields`);
  }
  return value as Record<string, unknown>;
}

function uuid(value: unknown, field: string): string {
  if (typeof value !== "string" || !CANONICAL_UUID.test(value)) fail(`${field} must be a canonical UUID`);
  return value;
}

function sha(value: unknown, field: string): string {
  if (typeof value !== "string" || !LOWER_SHA.test(value)) fail(`${field} must be a lowercase Git SHA`);
  return value;
}

function digest(value: unknown, field: string): string {
  if (typeof value !== "string" || !LOWER_DIGEST.test(value)) fail(`${field} must be a lowercase SHA-256 digest`);
  return value;
}

function positiveInt(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    fail(`${field} must be a positive integer`);
  }
  return value;
}

function transitionKey(value: unknown): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 200) {
    fail("transition_key length is invalid");
  }
  return value;
}

function authorityFields(record: Record<string, unknown>): void {
  for (const field of AUTHORITY_FIELDS) uuid(record[field], field);
}

function validateReceipt(receipt: ProjectRepositoryStagedReceipt): void {
  const record = closedKeys(receipt, RECEIPT_FIELDS, "receipt");
  if (record.contract !== "leaf.project-repository-edit.v2") fail("receipt contract is invalid");
  if (record.state !== "staged") fail("receipt state is invalid");
  if (record.operation !== "edit" && record.operation !== "rollback") fail("receipt operation is invalid");
  if (record.operation === "edit" && record.source_edit_id !== null) fail("receipt source_edit_id is invalid");
  if (record.operation === "rollback") uuid(record.source_edit_id, "source_edit_id");
  uuid(record.edit_id, "edit_id");
  uuid(record.actor_binding_id, "actor_binding_id");
  authorityFields(record);
  uuid(record.writer_lease_id, "writer_lease_id");
  positiveInt(record.writer_lease_generation, "writer_lease_generation");
  sha(record.base_commit, "base_commit");
  sha(record.staged_head_commit, "staged_head_commit");
  sha(record.staged_tree, "staged_tree");
  if (!Array.isArray(record.changed_paths) || record.changed_paths.length < 1 ||
      record.changed_paths.length > 1000 ||
      record.changed_paths.some((path) => typeof path !== "string")) {
    fail("changed_paths must contain 1 to 1000 paths");
  }
  digest(record.diff_digest, "diff_digest");
  digest(record.instruction_digest, "instruction_digest");
  if (typeof record.idempotency_key !== "string" ||
      record.idempotency_key.length < 1 || record.idempotency_key.length > 200) {
    fail("idempotency_key length is invalid");
  }
}

function validateRecordStagedRequest(request: ProjectRepositoryEditRecordStagedRequest): void {
  const record = closedKeys(request, RECORD_STAGED_REQUEST_FIELDS, "record_staged request");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("contract is invalid");
  if (record.action !== "record_staged") fail("action is invalid");
  validateReceipt(request.receipt);
  digest(record.receipt_digest, "receipt_digest");
  if (record.expected_version !== 0) fail("expected_version must be 0 for record_staged");
  transitionKey(record.transition_key);
}

function validateAuthorizePublishRequest(request: ProjectRepositoryEditAuthorizePublishRequest): void {
  const record = closedKeys(request, AUTHORIZE_PUBLISH_REQUEST_FIELDS, "authorize_publish request");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("contract is invalid");
  if (record.action !== "authorize_publish") fail("action is invalid");
  authorityFields(record);
  uuid(record.edit_id, "edit_id");
  uuid(record.actor_binding_id, "actor_binding_id");
  uuid(record.confirmation_id, "confirmation_id");
  digest(record.receipt_digest, "receipt_digest");
  uuid(record.publish_lease_id, "publish_lease_id");
  positiveInt(record.publish_lease_generation, "publish_lease_generation");
  positiveInt(record.expected_version, "expected_version");
  transitionKey(record.transition_key);
}

function validateSettlePublishRequest(request: ProjectRepositoryEditSettlePublishRequest): void {
  const record = closedKeys(request, SETTLE_PUBLISH_REQUEST_FIELDS, "settle_publish request");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("contract is invalid");
  if (record.action !== "settle_publish") fail("action is invalid");
  authorityFields(record);
  uuid(record.edit_id, "edit_id");
  uuid(record.actor_binding_id, "actor_binding_id");
  uuid(record.publish_lease_id, "publish_lease_id");
  positiveInt(record.publish_lease_generation, "publish_lease_generation");
  sha(record.private_ref_commit, "private_ref_commit");
  sha(record.main_commit, "main_commit");
  sha(record.main_tree, "main_tree");
  positiveInt(record.expected_version, "expected_version");
  transitionKey(record.transition_key);
}

function validateRecoverPublishRequest(request: ProjectRepositoryEditRecoverPublishRequest): void {
  const record = closedKeys(request, RECOVER_PUBLISH_REQUEST_FIELDS, "recover_publish request");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("contract is invalid");
  if (record.action !== "recover_publish") fail("action is invalid");
  authorityFields(record);
  uuid(record.edit_id, "edit_id");
  uuid(record.actor_binding_id, "actor_binding_id");
  uuid(record.recovery_lease_id, "recovery_lease_id");
  positiveInt(record.recovery_lease_generation, "recovery_lease_generation");
  sha(record.private_ref_commit, "private_ref_commit");
  sha(record.main_commit, "main_commit");
  sha(record.main_tree, "main_tree");
  positiveInt(record.expected_version, "expected_version");
  transitionKey(record.transition_key);
  if (typeof record.reason_code !== "string" || !REASON_CODE.test(record.reason_code)) {
    fail("reason_code is invalid");
  }
}

function parseRecordStagedResponse(value: unknown): ProjectRepositoryEditRecordStagedResponse {
  const record = closedKeys(value, RECORD_STAGED_RESPONSE_FIELDS, "record_staged response");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("response contract is invalid");
  if (record.action !== "record_staged") fail("response action is invalid");
  if (record.state !== "staged") fail("response state is invalid");
  return Object.freeze({
    contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
    action: "record_staged",
    edit_id: uuid(record.edit_id, "edit_id"),
    state: "staged",
    version: positiveInt(record.version, "version"),
  });
}

function parsePublishMatrix(value: unknown): ProjectRepositoryEditPublishMatrix {
  const record = closedKeys(value, PUBLISH_MATRIX_FIELDS, "authorize_publish response");
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("response contract is invalid");
  if (record.action !== "authorize_publish") fail("response action is invalid");
  if (record.state !== "publishing") fail("response state is invalid");
  if (typeof record.private_ref !== "string" || !PRIVATE_REF.test(record.private_ref)) {
    fail("private_ref is invalid");
  }
  return Object.freeze({
    contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
    action: "authorize_publish",
    edit_id: uuid(record.edit_id, "edit_id"),
    state: "publishing",
    version: positiveInt(record.version, "version"),
    receipt_digest: digest(record.receipt_digest, "receipt_digest"),
    expected_main_commit: sha(record.expected_main_commit, "expected_main_commit"),
    staged_head_commit: sha(record.staged_head_commit, "staged_head_commit"),
    staged_tree: sha(record.staged_tree, "staged_tree"),
    private_ref: record.private_ref,
    publish_lease_id: uuid(record.publish_lease_id, "publish_lease_id"),
    publish_lease_generation: positiveInt(record.publish_lease_generation, "publish_lease_generation"),
  });
}

function parseSettlementResponse(
  value: unknown,
  action: "settle_publish" | "recover_publish",
): ProjectRepositoryEditSettlementResponse {
  const record = closedKeys(value, SETTLEMENT_RESPONSE_FIELDS, `${action} response`);
  if (record.contract !== PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT) fail("response contract is invalid");
  if (record.action !== action) fail("response action is invalid");
  if (record.state !== "published" && record.state !== "publishing" && record.state !== "conflicted") {
    fail("response state is invalid");
  }
  return Object.freeze({
    contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
    action,
    edit_id: uuid(record.edit_id, "edit_id"),
    state: record.state as "published" | "publishing" | "conflicted",
    version: positiveInt(record.version, "version"),
  });
}

export class ProjectRepositoryEditCoordinationClient implements ProjectRepositoryEditCoordination {
  private readonly baseUrl: string;
  private readonly dispatchSecret: string;
  private readonly fetchImpl: FetchLike;

  constructor(opts: ProjectRepositoryEditCoordinationClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.dispatchSecret = opts.dispatchSecret;
    this.fetchImpl = opts.fetchImpl ?? (fetch as unknown as FetchLike);
  }

  private async post(path: string, tenantId: string, body: unknown): Promise<unknown> {
    if (!this.baseUrl || !this.dispatchSecret) {
      fail("not configured");
    }
    let response: { ok: boolean; status: number; json: () => Promise<unknown> };
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-tenant-id": tenantId,
          "x-dispatch-secret": this.dispatchSecret,
        },
        body: JSON.stringify(body),
      });
    } catch {
      fail("unavailable");
    }
    if (!response.ok) {
      // The response body may contain internal diagnostics. It never reaches
      // this error, matching the harness's intentionally broad error logging.
      fail(`rejected request (${response.status})`);
    }
    try {
      return await response.json();
    } catch {
      fail("response is not valid JSON");
    }
  }

  async recordStaged(
    request: ProjectRepositoryEditRecordStagedRequest,
  ): Promise<ProjectRepositoryEditRecordStagedResponse> {
    validateRecordStagedRequest(request);
    const value = await this.post(
      "/internal/project-repository-edit/record-staged", request.receipt.tenant_id, request);
    return parseRecordStagedResponse(value);
  }

  async authorizePublish(
    request: ProjectRepositoryEditAuthorizePublishRequest,
  ): Promise<ProjectRepositoryEditPublishMatrix> {
    validateAuthorizePublishRequest(request);
    const value = await this.post(
      "/internal/project-repository-edit/authorize-publish", request.tenant_id, request);
    return parsePublishMatrix(value);
  }

  async settlePublish(
    request: ProjectRepositoryEditSettlePublishRequest,
  ): Promise<ProjectRepositoryEditSettlementResponse> {
    validateSettlePublishRequest(request);
    const value = await this.post(
      "/internal/project-repository-edit/settle-publish", request.tenant_id, request);
    return parseSettlementResponse(value, "settle_publish");
  }

  async recoverPublish(
    request: ProjectRepositoryEditRecoverPublishRequest,
  ): Promise<ProjectRepositoryEditSettlementResponse> {
    validateRecoverPublishRequest(request);
    const value = await this.post(
      "/internal/project-repository-edit/recover-publish", request.tenant_id, request);
    return parseSettlementResponse(value, "recover_publish");
  }
}
