/**
 * REAL BrokerApsClient - POSTs to the credential broker (CONTRACT-ADDENDUM
 * section 8). The harness NEVER holds the APS credential; the broker process is
 * the only code that can. Live path is operator-gated (a running broker on
 * BROKER_URL); it COMPILES + is exercised structurally now.
 *
 *   POST {BROKER_URL}/broker/run
 *     body: { tenant_id, tool, params, dwg, aps_live }   (snake_case wire shape)
 *     -> extended CONTRACT section 3 envelope (adds degraded_mode; section 10)
 *
 * Per-tenant kill-switch denials surface as a TENANT_DISABLED envelope; a broker
 * that is down surfaces as BROKER_UNREACHABLE. Both are returned AS envelopes.
 */

import type { BrokerApsClient, BrokerRunRequest, ResultEnvelope } from "../index.js";

export interface BrokerApsClientOptions {
  /** Defaults to env BROKER_URL, then http://127.0.0.1:8140. */
  brokerUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export class BrokerApsClientHttp implements BrokerApsClient {
  private readonly baseUrl: string;
  private readonly doFetch: typeof fetch;
  private readonly timeoutMs: number;

  constructor(opts: BrokerApsClientOptions = {}) {
    this.baseUrl = (opts.brokerUrl ?? process.env.BROKER_URL ?? "http://127.0.0.1:8140").replace(/\/+$/, "");
    this.doFetch = opts.fetchImpl ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 600_000;
  }

  async runTool(req: BrokerRunRequest): Promise<ResultEnvelope> {
    const body = {
      tenant_id: req.tenantId,
      tool: req.tool,
      params: req.params,
      dwg: req.dwg,
      aps_live: req.apsLive,
    };
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await this.doFetch(`${this.baseUrl}/broker/run`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...(process.env.LEAF_BROKER_SECRET ? { "X-Broker-Secret": process.env.LEAF_BROKER_SECRET } : {}),
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      const json = (await res.json()) as ResultEnvelope;
      return json;
    } catch (err) {
      // Broker unreachable -> a well-formed BROKER_UNREACHABLE envelope.
      return {
        ok: false,
        tool: req.tool.name,
        version: req.tool.version ?? "1.0.0",
        result: {},
        overlay: null,
        timing_ms: 0,
        cost: null,
        error: {
          error_code: "BROKER_UNREACHABLE",
          message: `broker at ${this.baseUrl} unreachable: ${(err as Error).message}`,
          retryable: true,
        },
        degraded_mode: false,
      };
    } finally {
      clearTimeout(timer);
    }
  }
}
