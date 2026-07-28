/** Direct, keep-alive HTTP(S) client for the instant executor RPC. */

import http from "node:http";
import https from "node:https";
import type {
  InstantExecutorClient,
  InstantInvocation,
  InstantInvocationResponse,
  InstantSessionAssignment,
} from "../index.js";

export class InstantExecutorClientError extends Error {
  constructor(message: string, readonly code: "timeout" | "cancelled" | "transport" | "response") {
    super(message);
    this.name = "InstantExecutorClientError";
  }
}

export class HttpInstantExecutorClient implements InstantExecutorClient {
  private readonly httpAgent = new http.Agent({ keepAlive: true });
  private readonly httpsAgent = new https.Agent({ keepAlive: true });
  private readonly timeoutMs: number;

  constructor(opts: { timeoutMs?: number } = {}) {
    this.timeoutMs = opts.timeoutMs ?? 15_000;
  }

  close(): void {
    this.httpAgent.destroy();
    this.httpsAgent.destroy();
  }

  async invoke(
    assignment: InstantSessionAssignment,
    invocation: InstantInvocation,
    opts?: { signal?: AbortSignal },
  ): Promise<InstantInvocationResponse> {
    return this.request<InstantInvocationResponse>(assignment, "/v1/invoke", invocation, opts?.signal);
  }

  async cancel(
    assignment: InstantSessionAssignment,
    invocation: Pick<InstantInvocation, "invocation_id" | "tenant_id" | "session_id">,
  ): Promise<Record<string, unknown>> {
    return this.request(assignment, `/v1/invocations/${encodeURIComponent(invocation.invocation_id)}/cancel`, {
      contract: "leaf.instant-execution/v1",
      cancellation_id: crypto.randomUUID(),
      ...invocation,
      requested_at: new Date().toISOString(),
      state: "requested",
    });
  }

  private request<T>(
    assignment: InstantSessionAssignment,
    path: string,
    body: object,
    signal?: AbortSignal,
  ): Promise<T> {
    const endpoint = new URL(assignment.executor_endpoint);
    const url = new URL(path, endpoint.pathname.endsWith("/") ? endpoint : `${endpoint}/`);
    const payload = JSON.stringify(body);
    const requestFn = url.protocol === "https:" ? https.request : url.protocol === "http:" ? http.request : null;
    if (!requestFn) return Promise.reject(new InstantExecutorClientError("unsupported executor protocol", "transport"));
    return new Promise<T>((resolve, reject) => {
      let timedOut = false;
      let cancelled = false;
      const req = requestFn(url, {
        method: "POST",
        agent: url.protocol === "https:" ? this.httpsAgent : this.httpAgent,
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
          authorization: `Bearer ${assignment.lease_token}`,
        },
      }, (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => { text += chunk; });
        res.on("end", () => {
          let parsed: unknown;
          try { parsed = JSON.parse(text); } catch {
            reject(new InstantExecutorClientError("invalid executor response", "response"));
            return;
          }
          if (parsed && typeof parsed === "object" && "status" in parsed) {
            resolve(parsed as T);
            return;
          }
          if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) resolve(parsed as T);
          else reject(new InstantExecutorClientError(`executor returned ${res.statusCode ?? 0}`, "response"));
        });
      });
      const timer = setTimeout(() => { timedOut = true; req.destroy(); }, this.timeoutMs);
      const onAbort = () => { cancelled = true; req.destroy(); };
      signal?.addEventListener("abort", onAbort, { once: true });
      req.on("error", () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        reject(new InstantExecutorClientError(
          timedOut ? "instant executor timeout" : cancelled ? "instant executor cancelled" : "instant executor transport error",
          timedOut ? "timeout" : cancelled ? "cancelled" : "transport",
        ));
      });
      req.on("close", () => { clearTimeout(timer); signal?.removeEventListener("abort", onAbort); });
      req.end(payload);
    });
  }
}
