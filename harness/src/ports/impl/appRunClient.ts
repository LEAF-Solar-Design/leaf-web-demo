/**
 * REAL AppRunClient — the harness -> app back-edge. Every request carries the
 * RESOLVED tenant on `X-Tenant-Id` plus the shared back-edge secret on
 * `X-Dispatch-Secret` (wire contract section 0: same trust model as the broker's
 * X-Broker-Secret — the app trusts the tenant header ONLY when the secret is
 * valid; with LEAF_APP_DISPATCH_SECRET unset the back-edge is disabled and the
 * app answers 401).
 *
 * Side effects are limited to registered tool dispatch and gate-approved
 * authoring. This client never accepts executable code or drawing deltas.
 *
 * Secret discipline: the dispatch secret is env-injected at construction, sent
 * only as a header, never logged, never echoed into results.
 */

import type {
  AppRunClient,
  CapabilityEntry,
  SubmitRunRequest,
  SubmitRunResponse,
  IntegrationAuthority,
  IntegrationActionCatalogInput,
  IntegrationDatasetsInput,
  IntegrationDatasetsResult,
  IntegrationQueryInput,
  IntegrationSaveElementInput,
  IntegrationSaveElementResult,
  IntegrationToolResult,
} from "../index.js";
import { INTEGRATION_TOOL_INPUTS } from "../index.js";
import { z } from "zod";
import {
  DatasetReadV1, IntegrationActionCatalogV1, IntegrationElementV1, IntegrationRefV1,
} from "../../vendor/mushy-author/contracts/integration.js";

export type IntegrationTool = keyof typeof INTEGRATION_TOOL_INPUTS;
type IntegrationInputs = {
  integration_action_catalog: IntegrationActionCatalogInput;
  integration_datasets: IntegrationDatasetsInput;
  integration_query: IntegrationQueryInput;
  integration_save_element: IntegrationSaveElementInput;
};
type IntegrationResults = {
  integration_action_catalog: IntegrationActionCatalogV1;
  integration_datasets: IntegrationDatasetsResult;
  integration_query: DatasetReadV1;
  integration_save_element: IntegrationSaveElementResult;
};

const IntegrationErrorCode = z.enum([
  "invalid_input", "unauthenticated", "denied", "unavailable", "conflict",
  "invalid_response", "timeout", "response_too_large", "outcome_unknown", "not_found",
]);
type IntegrationErrorCode = z.infer<typeof IntegrationErrorCode>;
export function integrationErrorMessage(code: IntegrationErrorCode): string {
  const safeCode = IntegrationErrorCode.safeParse(code);
  if (!safeCode.success) return "Integration request failed: unavailable.";
  return safeCode.data === "outcome_unknown"
    ? "The save outcome is unknown. Inspect saved views before retrying."
    : `Integration request failed: ${safeCode.data}.`;
}

/** Fixed messages only; never retain a transport exception or response body. */
export class IntegrationClientError extends Error {
  constructor(readonly code: IntegrationErrorCode) {
    super(integrationErrorMessage(code));
    this.name = "IntegrationClientError";
  }
}

const DiscoveryResult = z.strictObject({
  datasets: z.array(z.strictObject({
    dataset_ref: IntegrationRefV1,
    title: z.string().min(1).max(120),
    fields: DatasetReadV1.shape.fields,
    freshness: DatasetReadV1.shape.freshness.nullable(),
    status: z.enum(["ready", "empty", "stale", "unavailable"]),
  })).max(50),
  next_cursor: z.string().min(1).max(1024).regex(/^[A-Za-z0-9_-]+(?![\s\S])/).nullable(),
});
const SaveResult = z.strictObject({
  element: IntegrationElementV1,
  revision: z.number().int().positive().refine(Number.isSafeInteger),
});
const HostEnvelope = z.strictObject({
  gate: z.strictObject({
    decision: z.enum(["allow", "deny", "awaiting_approval"]),
    confirmation_id: z.string().regex(/^[a-f0-9]{16}(?![\s\S])/).optional(),
    reason: z.string().max(512).optional(),
    policy: z.string().max(128).optional(),
    rung: z.union([z.number().int().min(0).max(7), z.string().regex(/^R[0-7](?![\s\S])/)]).optional(),
  }),
  result: z.unknown().optional(),
});

function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = Object.keys(left), b = Object.keys(right);
  return a.length === b.length && a.every((key) => Object.hasOwn(right, key) &&
    sameJson((left as Record<string, unknown>)[key], (right as Record<string, unknown>)[key]));
}

/** Also used at the executor seam: injected clients must obey the same contract. */
export function validateIntegrationResponse<K extends IntegrationTool>(
  tool: K, input: IntegrationInputs[K], value: unknown, wire = false,
): IntegrationToolResult<IntegrationResults[K]> {
  const parsed = HostEnvelope.safeParse(value);
  if (!parsed.success) throw new IntegrationClientError("invalid_response");
  const { gate, result } = parsed.data;
  if ((wire && gate.rung !== undefined && typeof gate.rung !== "number") ||
      (gate.decision === "awaiting_approval") !== (gate.confirmation_id !== undefined) ||
      (gate.decision === "allow") !== Object.hasOwn(parsed.data, "result") ||
      (gate.decision === "allow" && result === undefined)) {
    throw new IntegrationClientError("invalid_response");
  }
  const { rung, ...metadata } = gate;
  const portGate = { ...metadata, ...(rung !== undefined ? { rung: typeof rung === "number" ? `R${rung}` : rung } : {}) };
  if (gate.decision !== "allow") return { gate: portGate };
  const schema = tool === "integration_action_catalog" ? IntegrationActionCatalogV1
    : tool === "integration_datasets" ? DiscoveryResult : tool === "integration_query" ? DatasetReadV1 : SaveResult;
  const checked = schema.safeParse(result);
  if (!checked.success) throw new IntegrationClientError("invalid_response");
  if (tool === "integration_datasets") {
    if ((checked.data as IntegrationDatasetsResult).datasets.length > (input as IntegrationDatasetsInput).limit) {
      throw new IntegrationClientError("invalid_response");
    }
  } else if (tool === "integration_action_catalog") {
    if ((checked.data as IntegrationActionCatalogV1).actions.length > (input as IntegrationActionCatalogInput).limit) {
      throw new IntegrationClientError("invalid_response");
    }
  } else if (tool === "integration_query") {
    if (!sameJson((checked.data as DatasetReadV1).dataset_ref, (input as IntegrationQueryInput).dataset_ref)) {
      throw new IntegrationClientError("invalid_response");
    }
  } else if (!sameJson((checked.data as IntegrationSaveElementResult).element, (input as IntegrationSaveElementInput).recipe)) {
    throw new IntegrationClientError("invalid_response");
  }
  return { gate: portGate, result: checked.data as IntegrationResults[K] };
}

function reflectsSecret(value: unknown, secret: string): boolean {
  if (!secret) return false;
  const variants = [secret, encodeURIComponent(secret), Buffer.from(secret).toString("base64"),
    Buffer.from(secret).toString("base64url"), Buffer.from(secret).toString("hex"), Buffer.from(secret).toString("hex").toUpperCase(),
    [...secret].map((c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`).join("")];
  const visit = (item: unknown): boolean => {
    if (typeof item === "string") {
      let decoded = item;
      for (let i = 0; i < 3; i++) {
        if (variants.some((variant) => decoded.includes(variant))) return true;
        try { decoded = decodeURIComponent(decoded); } catch { /* inspect original below */ }
        decoded = decoded.replace(/&#(?:x([a-f0-9]+)|([0-9]+));/gi,
          (_match, hex: string, decimal: string) => {
            const point = parseInt(hex ?? decimal, hex ? 16 : 10);
            return point <= 0x10ffff ? String.fromCodePoint(point) : "";
          });
      }
      return false;
    }
    return !!item && typeof item === "object" && Object.entries(item).some(([key, nested]) => visit(key) || visit(nested));
  };
  return visit(value);
}

export interface HttpAppRunClientOptions {
  /** App base URL, e.g. http://127.0.0.1:8130 (from LEAF_APP_URL or equivalent). */
  baseUrl: string;
  /** LEAF_APP_DISPATCH_SECRET — the back-edge shared secret. */
  dispatchSecret: string;
  /** Injectable for tests; defaults to global fetch (Node >= 18). */
  fetchImpl?: typeof fetch;
}

export class AppRunClientError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly errorCode?: string,
  ) {
    super(message);
    this.name = "AppRunClientError";
  }
}

export class HttpAppRunClient implements AppRunClient {
  private readonly baseUrl: string;
  private readonly secret: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: HttpAppRunClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.secret = opts.dispatchSecret;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  private headers(tenantId: string): Record<string, string> {
    return {
      "content-type": "application/json",
      "x-tenant-id": tenantId,
      "x-dispatch-secret": this.secret,
    };
  }

  integrationDatasets(tenantId: string, input: IntegrationDatasetsInput, authority: IntegrationAuthority) {
    return this.integrationRequest("integration_datasets", tenantId, input, authority);
  }

  integrationActionCatalog(tenantId: string, input: IntegrationActionCatalogInput, authority: IntegrationAuthority) {
    return this.integrationRequest("integration_action_catalog", tenantId, input, authority);
  }

  integrationQuery(tenantId: string, input: IntegrationQueryInput, authority: IntegrationAuthority) {
    return this.integrationRequest("integration_query", tenantId, input, authority);
  }

  integrationSaveElement(tenantId: string, input: IntegrationSaveElementInput, authority: IntegrationAuthority) {
    return this.integrationRequest("integration_save_element", tenantId, input, authority);
  }

  /** One request, one total deadline, bounded streaming body, and no redirects. */
  private async integrationRequest<K extends IntegrationTool>(
    tool: K, tenantId: string, input: IntegrationInputs[K], authority: IntegrationAuthority,
  ): Promise<IntegrationToolResult<IntegrationResults[K]>> {
    const normalized = INTEGRATION_TOOL_INPUTS[tool].safeParse(input);
    const validId = (id: unknown): id is string => typeof id === "string" &&
      id.length > 0 && id.length <= 200 && id.trim() === id && !/[\u0000-\u001f\u007f-\u009f]/.test(id);
    if (!z.string().uuid().safeParse(tenantId).success || tenantId === "00000000-0000-0000-0000-000000000000" || tenantId !== tenantId.toLowerCase() ||
        !normalized.success || !validId(authority?.sessionId) || !validId(authority?.turnId)) {
      throw new IntegrationClientError("invalid_input");
    }
    if (!this.secret) throw new IntegrationClientError("unavailable");
    const paths = {
      integration_datasets: "/internal/integrations/datasets",
      integration_action_catalog: "/internal/integrations/action-catalog",
      integration_query: "/internal/integrations/query",
      integration_save_element: "/internal/integrations/elements",
    } as const;
    const body = JSON.stringify(normalized.data);
    if (Buffer.byteLength(body, "utf8") > 128 * 1024) throw new IntegrationClientError("invalid_input");
    const controller = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let timedOut = false;
    let failed = true;
    let definiteFailure = false;
    const cancel = () => {
      controller.abort();
      void reader?.cancel().catch(() => {});
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        cancel();
        reject(new IntegrationClientError(tool === "integration_save_element" ? "outcome_unknown" : "timeout"));
      }, 15_000);
    });
    try {
      const work = async () => {
        const response = await this.fetchImpl(`${this.baseUrl}${paths[tool]}`, {
          method: "POST", redirect: "error", signal: controller.signal,
          headers: {
            ...this.headers(tenantId),
            "x-authority-session-id": authority.sessionId,
            "x-authority-turn-id": authority.turnId,
          },
          body,
        });
        // A non-cooperating injected fetch must not leave a late body open.
        if (controller.signal.aborted) {
          void response.body?.cancel().catch(() => {});
          throw new IntegrationClientError("timeout");
        }
        if (!response.body) throw new IntegrationClientError("invalid_response");
        reader = response.body.getReader();
        const chunks: Uint8Array[] = [];
        let size = 0;
        while (true) {
          const next = await reader.read();
          if (next.done) break;
          size += next.value.byteLength;
          if (size > 256 * 1024) throw new IntegrationClientError("response_too_large");
          chunks.push(next.value);
        }
        let text: string;
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)); }
        catch { throw new IntegrationClientError("invalid_response"); }
        if (reflectsSecret(text, this.secret)) throw new IntegrationClientError("invalid_response");
        let json: unknown;
        try { json = JSON.parse(text); } catch { throw new IntegrationClientError("invalid_response"); }
        if (reflectsSecret(json, this.secret)) throw new IntegrationClientError("invalid_response");
        if (!response.ok) {
          const failure = z.strictObject({ error: z.strictObject({
            code: IntegrationErrorCode, correlationId: z.string().uuid(),
          }) }).safeParse(json);
          if (!failure.success) throw new IntegrationClientError("invalid_response");
          definiteFailure = [400, 401, 403, 404, 409].includes(response.status);
          throw new IntegrationClientError(response.status === 409 ? "conflict" : failure.data.error.code);
        }
        return validateIntegrationResponse(tool, normalized.data as IntegrationInputs[K], json, true);
      };
      const result = await Promise.race([work(), deadline]);
      failed = false;
      return result;
    } catch (error) {
      if (timedOut) throw new IntegrationClientError(tool === "integration_save_element" ? "outcome_unknown" : "timeout");
      // Once submitted, malformed or lost success cannot prove no save occurred.
      if (tool === "integration_save_element" && !definiteFailure) {
        throw new IntegrationClientError("outcome_unknown");
      }
      if (error instanceof IntegrationClientError) throw error;
      throw new IntegrationClientError(tool === "integration_save_element" ? "outcome_unknown" : "unavailable");
    } finally {
      clearTimeout(timer);
      if (failed) cancel();
      try { reader?.releaseLock(); } catch { /* pending reads are cancelled above */ }
    }
  }

  private async request(
    tenantId: string,
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>,
    extraHeaders?: Record<string, string>,
  ): Promise<Record<string, unknown>> {
    return (await this.requestWithStatus(tenantId, method, path, body, extraHeaders)).body;
  }

  private async requestWithStatus(
    tenantId: string,
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>,
    extraHeaders?: Record<string, string>,
  ): Promise<{ status: number; body: Record<string, unknown> }> {
    const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      headers: { ...this.headers(tenantId), ...extraHeaders },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const json = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    // App error bodies are section-10 envelopes: {error: {error_code, message, ...}}.
    if (!res.ok && res.status !== 202) {
      const err = (json.error ?? {}) as Record<string, unknown>;
      const reasonCode = typeof json.reason_code === "string" ? json.reason_code : undefined;
      throw new AppRunClientError(
        typeof err.message === "string" ? err.message : `app back-edge ${res.status} on ${path}`,
        res.status,
        reasonCode ?? (typeof err.error_code === "string" ? err.error_code : undefined),
      );
    }
    return { status: res.status, body: json };
  }

  /** GET /api/capabilities → flatten the family grouping into one entry list. */
  async getCapabilities(tenantId: string): Promise<CapabilityEntry[]> {
    const body = await this.request(tenantId, "GET", "/api/capabilities");
    const families = Array.isArray(body.families) ? body.families : [];
    const out: CapabilityEntry[] = [];
    for (const fam of families as Record<string, unknown>[]) {
      const caps = Array.isArray(fam.capabilities) ? fam.capabilities : [];
      for (const entry of caps as CapabilityEntry[]) out.push(entry);
    }
    return out;
  }

  async getDrawingState(
    tenantId: string,
    drawingId: string,
    what: "summary" | "versions" | "checkout",
  ): Promise<Record<string, unknown>> {
    const id = encodeURIComponent(drawingId);
    if (what === "summary") {
      // The bounded digest omits raw geometry (routers/drawings.py).
      return this.request(tenantId, "GET", `/api/drawings/${id}/summary`);
    }
    const versions = await this.request(tenantId, "GET", `/api/drawings/${id}/versions`);
    if (what === "versions") return versions;
    // checkout rides the versions view ({checkout: {...}|null}).
    return { drawing_id: drawingId, checkout: versions.checkout ?? null };
  }

  async authorTool(
    tenantId: string,
    description: string,
    mode: "build" | "one_off",
    idempotencyKey: string,
    authority?: { sessionId?: string; turnId?: string },
    targetToolName?: string,
  ): Promise<Record<string, unknown>> {
    // The back edge authenticates as a tenant and cannot assert a user. Naming
    // the app-owned session/turn that authenticated this turn lets the app read
    // the author from its OWN record — the same authority tuple the gate takes.
    return this.request(
      tenantId,
      "POST",
      "/api/author",
      {
        description, mode,
        ...(targetToolName ? { target_tool_name: targetToolName } : {}),
      },
      {
        "idempotency-key": idempotencyKey,
        ...(authority?.sessionId
          ? { "x-authority-session-id": authority.sessionId }
          : {}),
        ...(authority?.turnId
          ? { "x-authority-turn-id": authority.turnId }
          : {}),
      },
    );
  }

  async customizePropose(
    tenantId: string,
    title: string,
    edits: Array<{ path: string; content?: string; delete?: boolean }>,
    authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "POST",
      "/api/platform/customize",
      { title, edits },
      {
        ...(authority?.sessionId
          ? { "x-authority-session-id": authority.sessionId }
          : {}),
        ...(authority?.turnId ? { "x-authority-turn-id": authority.turnId } : {}),
      },
    );
  }

  async customizeStatus(tenantId: string, changeId: string): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "GET",
      `/api/platform/customize/${encodeURIComponent(changeId)}`,
    );
  }

  async customizeLand(
    tenantId: string,
    changeId: string,
    commitSha: string,
    authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "POST",
      `/api/platform/customize/${encodeURIComponent(changeId)}/land`,
      { commit_sha: commitSha },
      {
        ...(authority?.sessionId
          ? { "x-authority-session-id": authority.sessionId }
          : {}),
        ...(authority?.turnId ? { "x-authority-turn-id": authority.turnId } : {}),
      },
    );
  }

  async customizeList(tenantId: string, limit: number): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "GET",
      `/api/platform/customize?limit=${encodeURIComponent(String(limit))}`,
    );
  }

  async customizeReadSource(tenantId: string, path: string): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "GET",
      `/api/platform/source?path=${encodeURIComponent(path)}`,
    );
  }

  async customizeListSource(tenantId: string, dir: string): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "GET",
      `/api/platform/source/tree${dir ? `?dir=${encodeURIComponent(dir)}` : ""}`,
    );
  }

  /**
   * POST /api/run → 202 {job_id, status}. With `wait`, then POLL GET /api/jobs/{id}
   * up to waitTimeoutS (default 15s) so a fast read returns its result inline. The
   * async-submit-then-poll shape (rather than ?wait=1, which blocks the app worker
   * up to the full job timeout) keeps the job_id in hand even when the budget runs
   * out — the caller gets {job_id, status} and the model can job_status later.
   */
  async submitRun(req: SubmitRunRequest): Promise<SubmitRunResponse> {
    const response = await this.requestWithStatus(
      req.tenantId,
      "POST",
      `/api/run`,
      {
        tool: req.tool,
        params: req.params,
        dwg: req.dwg,
        catalog_digest: req.catalogDigest,
        ...(req.drawingVersion !== undefined ? { dwg_version: req.drawingVersion } : {}),
        ...(req.expectedDrawingHead !== undefined
          ? { expected_drawing_head: req.expectedDrawingHead }
          : {}),
        ...(req.catalogCommit !== undefined ? { catalog_commit: req.catalogCommit } : {}),
        ...(req.effectiveCatalogDigest !== undefined
          ? { effective_catalog_digest: req.effectiveCatalogDigest }
          : {}),
        ...(req.toolManifestSha256 !== undefined
          ? { tool_manifest_sha256: req.toolManifestSha256 }
          : {}),
      },
      {
        ...(req.authoritySessionId
          ? { "x-authority-session-id": req.authoritySessionId }
          : {}),
        ...(req.authorityTurnId
          ? { "x-authority-turn-id": req.authorityTurnId }
          : {}),
      },
    );
    if (response.status !== 202) {
      throw new AppRunClientError("app run submission was not accepted", response.status);
    }
    const jobId = typeof response.body.job_id === "string"
      ? response.body.job_id.trim()
      : "";
    if (!jobId) {
      throw new AppRunClientError("app run submission omitted job identity", response.status);
    }
    let status = String(response.body.status ?? "submitted");
    if (!req.wait) return { job_id: jobId, status };

    const deadline = Date.now() + (req.waitTimeoutS ?? 15) * 1000;
    while (Date.now() < deadline) {
      const row = await this.getJob(req.tenantId, jobId);
      status = String(row.status ?? status);
      if (status === "complete" || status === "failed") {
        return {
          job_id: jobId,
          status,
          ...(row.result !== undefined
            ? { result: row.result as SubmitRunResponse["result"] }
            : {}),
        };
      }
      await new Promise((r) => setTimeout(r, 250));
    }
    return { job_id: jobId, status };
  }

  async proposeOverlay(
    tenantId: string,
    sessionId: string,
    tokens: Record<string, string>,
    requestText: string,
  ): Promise<Record<string, unknown>> {
    return this.request(tenantId, "POST", "/api/overlay/proposals", {
      session_id: sessionId,
      tokens,
      request_text: requestText,
    });
  }

  async requestPublication(
    tenantId: string,
    changeSetId: string,
  ): Promise<Record<string, unknown>> {
    return this.request(tenantId, "POST", "/api/author/publication-requests", {
      change_set_id: changeSetId,
    });
  }

  async getJob(tenantId: string, jobId: string): Promise<Record<string, unknown>> {
    return this.request(tenantId, "GET", `/api/jobs/${encodeURIComponent(jobId)}`);
  }

  /** POST /api/campaigns/conversation/finish. Same authority-header transport
   * as `authorTool`: the app resolves org/project from this session/turn's
   * OWN record, never from a request field. */
  async finishProject(
    tenantId: string,
    finish: {
      title: string;
      prompt: string;
      delivery_profile: string;
      intended_user: string;
      workflow: string;
      artifact_refs: string[];
    },
    idempotencyKey: string,
    authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "POST",
      "/api/campaigns/conversation/finish",
      { ...finish },
      {
        "idempotency-key": idempotencyKey,
        ...(authority?.sessionId
          ? { "x-authority-session-id": authority.sessionId }
          : {}),
        ...(authority?.turnId
          ? { "x-authority-turn-id": authority.turnId }
          : {}),
      },
    );
  }

  /** POST /api/campaigns/conversation/status. */
  async projectCompletionStatus(
    tenantId: string,
    campaignId: string,
    releaseId?: string,
    authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    return this.request(
      tenantId,
      "POST",
      "/api/campaigns/conversation/status",
      { campaign_id: campaignId, ...(releaseId ? { release_id: releaseId } : {}) },
      {
        ...(authority?.sessionId
          ? { "x-authority-session-id": authority.sessionId }
          : {}),
        ...(authority?.turnId
          ? { "x-authority-turn-id": authority.turnId }
          : {}),
      },
    );
  }
}
