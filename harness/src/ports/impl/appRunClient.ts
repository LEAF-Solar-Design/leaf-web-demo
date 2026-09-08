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
} from "../index.js";

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
