/**
 * Fake AppRunClient - an in-memory stand-in for the harness -> app back-edge.
 * NO network. Serves a small catalog (reads + write tools, one live-APS), completes
 * dispatched jobs instantly with schema-valid section-3 envelopes, and records
 * EVERY call (method log + submit payloads) so tests can prove:
 *   - submitRun is the only side-effecting surface,
 *   - its payload is {tool, params, dwg} (a tool NAME - never code),
 *   - reads waited inline and writes did not.
 */

import type {
  AppRunClient,
  CapabilityEntry,
  ResultEnvelope,
  SubmitRunRequest,
  SubmitRunResponse,
} from "../index.js";

export const FAKE_CATALOG: CapabilityEntry[] = [
  {
    name: "count-by-layer",
    catalog_digest: `sha256:${"1".repeat(64)}`,
    description: "Count entities per layer of the drawing.",
    capabilities: ["drawing.read"],
    params_schema: {
      type: "object",
      properties: { layer: { type: "string", description: "optional layer filter" } },
      required: [],
    },
  },
  {
    name: "list-layers",
    catalog_digest: `sha256:${"2".repeat(64)}`,
    description: "List the drawing's layer names.",
    capabilities: ["drawing.read"],
  },
  {
    name: "add-panel",
    catalog_digest: `sha256:${"3".repeat(64)}`,
    tool_manifest_sha256: `sha256:${"3".repeat(64)}`,
    catalog_commit: "a".repeat(40),
    effective_catalog_digest: "b".repeat(64),
    description: "Add a solar panel block to the drawing (writes a new version).",
    capabilities: ["drawing.read", "drawing.write"],
  },
  {
    // Mirrors the server catalog's aps_live projection so tests can exercise
    // the submit_live_solve (R4, real-USD) gate rung.
    name: "solve-live",
    catalog_digest: `sha256:${"4".repeat(64)}`,
    tool_manifest_sha256: `sha256:${"4".repeat(64)}`,
    catalog_commit: "a".repeat(40),
    effective_catalog_digest: "b".repeat(64),
    description: "Run the live APS solve on the drawing (real-USD compute).",
    capabilities: ["drawing.read", "drawing.write"],
    aps_live: true,
  },
];

function envelopeFor(tool: string): ResultEnvelope {
  return {
    ok: true,
    tool,
    version: "1.0.0",
    result: tool === "count-by-layer" ? { counts: { Panels: 3, "0": 1 }, total: 4 } : { note: `mock run of ${tool}` },
    overlay: null,
    timing_ms: 5,
    cost: null,
    error: null,
    degraded_mode: false,
  };
}

export class FakeAppRunClient implements AppRunClient {
  /** Every submitRun payload, verbatim (the invariant spy inspects these). */
  readonly submitCalls: SubmitRunRequest[] = [];
  readonly authorCalls: Array<{
    tenantId: string;
    description: string;
    mode: "build" | "one_off";
    idempotencyKey: string;
  }> = [];
  readonly publicationCalls: Array<{ tenantId: string; changeSetId: string }> = [];
  /** Ordered method-name log: proof of which surfaces were touched, and when. */
  readonly methodLog: string[] = [];
  catalog: CapabilityEntry[] = [...FAKE_CATALOG];
  drawingHead = 3;
  private readonly jobs = new Map<string, Record<string, unknown>>();
  private counter = 0;

  async getCapabilities(_tenantId: string): Promise<CapabilityEntry[]> {
    this.methodLog.push("getCapabilities");
    return this.catalog;
  }

  async getDrawingState(
    _tenantId: string,
    drawingId: string,
    what: "summary" | "versions" | "checkout",
  ): Promise<Record<string, unknown>> {
    this.methodLog.push("getDrawingState");
    if (what === "summary") {
      return { drawing_id: drawingId, layers: ["Panels", "0"], entity_total: 4 };
    }
    if (what === "versions") {
      return {
        drawing_id: drawingId,
        head: this.drawingHead,
        latest: this.drawingHead,
        versions: [{ v: this.drawingHead, tool: "add-panel", created: "2026-07-20T00:00:00Z" }],
        checkout: null,
      };
    }
    return { drawing_id: drawingId, checkout: null };
  }

  async submitRun(req: SubmitRunRequest): Promise<SubmitRunResponse> {
    this.methodLog.push("submitRun");
    this.submitCalls.push(req);
    const jobId = `job-${++this.counter}`;
    const envelope = envelopeFor(req.tool);
    this.jobs.set(jobId, {
      job_id: jobId,
      tenant_id: req.tenantId,
      tool: req.tool,
      status: "complete",
      result: envelope,
    });
    if (req.wait) {
      return { job_id: jobId, status: "complete", result: envelope };
    }
    return { job_id: jobId, status: "submitted" };
  }

  async authorTool(
    tenantId: string,
    description: string,
    mode: "build" | "one_off",
    idempotencyKey: string,
  ): Promise<Record<string, unknown>> {
    this.methodLog.push("authorTool");
    this.authorCalls.push({ tenantId, description, mode, idempotencyKey });
    return {
      tool: { name: "panel-gap-checker", capabilities: ["drawing.read"] },
      preview: "Created panel-gap-checker.",
      source: "harness",
    };
  }

  async getJob(_tenantId: string, jobId: string): Promise<Record<string, unknown>> {
    this.methodLog.push("getJob");
    const row = this.jobs.get(jobId);
    if (!row) return { job_id: jobId, status: "unknown" };
    return row;
  }

  proposeOverlayCalls: Array<{
    tenantId: string;
    sessionId: string;
    tokens: Record<string, string>;
    requestText: string;
  }> = [];

  async proposeOverlay(
    tenantId: string,
    sessionId: string,
    tokens: Record<string, string>,
    requestText: string,
  ): Promise<Record<string, unknown>> {
    this.proposeOverlayCalls.push({ tenantId, sessionId, tokens, requestText });
    return {
      proposal_id: "fake-overlay-proposal",
      tokens,
      document_version: 0,
      expires_at: "2026-01-01T00:00:00Z",
      error: null,
    };
  }

  async requestPublication(
    tenantId: string,
    changeSetId: string,
  ): Promise<Record<string, unknown>> {
    this.methodLog.push("requestPublication");
    this.publicationCalls.push({ tenantId, changeSetId });
    return { change_set_id: changeSetId, status: "awaiting_approval" };
  }

  /** R7 self-edit calls, recorded verbatim for assertions. */
  customizeCalls: Array<{
    op: "propose" | "status" | "land";
    tenantId: string;
    title?: string;
    edits?: Array<{ path: string; content?: string; delete?: boolean }>;
    changeId?: string;
    commitSha?: string;
  }> = [];

  async customizePropose(
    tenantId: string,
    title: string,
    edits: Array<{ path: string; content?: string; delete?: boolean }>,
    _authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    this.methodLog.push("customizePropose");
    this.customizeCalls.push({ op: "propose", tenantId, title, edits });
    return {
      change_id: "chg-fake-1",
      state: "Approved",
      commit_sha: "f".repeat(40),
      branch: "admin-customize/chg-fake-1",
    };
  }

  async customizeStatus(tenantId: string, changeId: string): Promise<Record<string, unknown>> {
    this.methodLog.push("customizeStatus");
    this.customizeCalls.push({ op: "status", tenantId, changeId });
    return { change_id: changeId, state: "Approved", commit_sha: "f".repeat(40) };
  }

  async customizeLand(
    tenantId: string,
    changeId: string,
    commitSha: string,
    _authority?: { sessionId?: string; turnId?: string },
  ): Promise<Record<string, unknown>> {
    this.methodLog.push("customizeLand");
    this.customizeCalls.push({ op: "land", tenantId, changeId, commitSha });
    return { change_id: changeId, state: "Landed", branch: `admin-customize/${changeId}` };
  }
}
