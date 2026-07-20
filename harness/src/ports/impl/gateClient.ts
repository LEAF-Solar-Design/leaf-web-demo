/**
 * REAL GateClient - POST {appBaseUrl}/internal/agent/gate, the harness -> app
 * back-edge policy consult that precedes EVERY spine tool execution (wire
 * contract sections 0 + 2). Carries X-Dispatch-Secret (+ X-Tenant-Id, same
 * trust model as the broker's X-Broker-Secret) and the JSON body
 * {tenant_id, session_id, turn_id, action, args}.
 *
 * FAIL CLOSED: any transport failure - connection refused, timeout (10s),
 * non-2xx, malformed body - returns decision "deny". An unreachable gate must
 * never become an implicit allow.
 *
 * Secret discipline: the dispatch secret is env-injected at construction, sent
 * only as a header, never logged, never echoed into results.
 */

import type {
  GateCheckContext,
  GateCheckResult,
  GateClient,
  GateDecision,
} from "../index.js";

export interface HttpGateClientOptions {
  /** App base URL, e.g. http://127.0.0.1:8130 (same base as the AppRunClient). */
  appBaseUrl: string;
  /** LEAF_APP_DISPATCH_SECRET - the back-edge shared secret. */
  dispatchSecret: string;
  /** Consult timeout in ms (default 10s per wire contract). */
  timeoutMs?: number;
  /** Injectable for tests; defaults to global fetch (Node >= 18). */
  fetchImpl?: typeof fetch;
}

const GATE_PATH = "/internal/agent/gate";

function isDecision(v: unknown): v is GateDecision {
  return v === "allow" || v === "deny" || v === "awaiting_approval";
}

export class HttpGateClient implements GateClient {
  private readonly baseUrl: string;
  private readonly secret: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: HttpGateClientOptions) {
    this.baseUrl = opts.appBaseUrl.replace(/\/+$/, "");
    this.secret = opts.dispatchSecret;
    this.timeoutMs = opts.timeoutMs ?? 10_000;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  async check(
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): Promise<GateCheckResult> {
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.baseUrl}${GATE_PATH}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-tenant-id": ctx.tenantId,
          "x-dispatch-secret": this.secret,
        },
        body: JSON.stringify({
          tenant_id: ctx.tenantId,
          session_id: ctx.sessionId,
          turn_id: ctx.turnId,
          action,
          args,
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      // Connection failure / timeout: FAIL CLOSED (wire contract lane spec).
      return { decision: "deny", reason: "gate_unreachable" };
    }
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok || !isDecision(body.decision)) {
      return {
        decision: "deny",
        reason: res.ok ? "gate_malformed_response" : `gate_error_http_${res.status}`,
      };
    }
    return {
      decision: body.decision,
      ...(typeof body.confirmation_id === "string" ? { confirmation_id: body.confirmation_id } : {}),
      ...(typeof body.reason === "string" ? { reason: body.reason } : {}),
      ...(typeof body.policy === "string" ? { policy: body.policy } : {}),
      // The app serializes rung as a NUMBER (agent_policy.json rung: 3); accept
      // either representation so the real verdict never silently loses it.
      ...(typeof body.rung === "string"
        ? { rung: body.rung }
        : typeof body.rung === "number" && Number.isFinite(body.rung)
          ? { rung: String(body.rung) }
          : {}),
    };
  }
}
