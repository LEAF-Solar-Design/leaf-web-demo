/** Direct, keep-alive HTTP(S) client for the instant executor RPC. */

import { readFileSync, statSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import { isIP } from "node:net";
import { createSecureContext } from "node:tls";
import type {
  InstantExecutorClient,
  InstantInvocation,
  InstantInvocationResponse,
  InstantSessionAssignment,
} from "../index.js";

export class InstantExecutorClientError extends Error {
  constructor(message: string, readonly code: "timeout" | "cancelled" | "transport" | "response" | "configuration") {
    super(message);
    this.name = "InstantExecutorClientError";
  }
}

export interface HttpInstantExecutorClientOptions {
  timeoutMs?: number;
  /** Require TLS material at construction, for production composition. */
  requireTls?: boolean;
  /** PEM trust anchor for executor server certificates. Required with the client cert and key. */
  caFile?: string;
  /** PEM client certificate for executor mTLS. Required with the CA and key. */
  clientCertificateFile?: string;
  /** PEM private key for executor mTLS. Required with the CA and client certificate. */
  clientPrivateKeyFile?: string;
  /** Verified TLS identity when the assigned endpoint is a private task IP. */
  tlsServerName?: string;
}

type TlsMaterial = { ca: Buffer; cert: Buffer; key: Buffer };

function configuredText(value: string | undefined): string | undefined {
  const text = value?.trim();
  return text || undefined;
}

function loadTlsMaterial(opts: HttpInstantExecutorClientOptions): TlsMaterial | undefined {
  const caFile = configuredText(opts.caFile);
  const clientCertificateFile = configuredText(opts.clientCertificateFile);
  const clientPrivateKeyFile = configuredText(opts.clientPrivateKeyFile);
  if (!caFile && !clientCertificateFile && !clientPrivateKeyFile) return undefined;
  if (!caFile || !clientCertificateFile || !clientPrivateKeyFile) {
    throw new InstantExecutorClientError(
      "instant executor TLS configuration requires CA, client certificate, and private key files",
      "configuration",
    );
  }

  try {
    for (const file of [caFile, clientCertificateFile, clientPrivateKeyFile]) {
      if (!statSync(file).isFile()) throw new Error("not a regular file");
    }
    const material = {
      ca: readFileSync(caFile),
      cert: readFileSync(clientCertificateFile),
      key: readFileSync(clientPrivateKeyFile),
    };
    // Parse the PEM material now. This rejects malformed or unusable key material before
    // an invocation can start, while keeping the bytes confined to the HTTPS agent.
    createSecureContext(material);
    return material;
  } catch {
    throw new InstantExecutorClientError("invalid instant executor TLS credential configuration", "configuration");
  }
}

function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "localhost") return true;
  if (isIP(host) === 4) return host.split(".")[0] === "127";
  return host === "::1";
}

export class HttpInstantExecutorClient implements InstantExecutorClient {
  private readonly httpAgent = new http.Agent({ keepAlive: true });
  private readonly httpsAgent: https.Agent;
  private readonly timeoutMs: number;
  private readonly tlsConfigured: boolean;
  private readonly tlsServerName?: string;

  constructor(opts: HttpInstantExecutorClientOptions = {}) {
    this.timeoutMs = opts.timeoutMs ?? 15_000;
    this.tlsServerName = configuredText(opts.tlsServerName);
    if (this.tlsServerName && !/^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/i.test(this.tlsServerName)) {
      throw new InstantExecutorClientError("invalid instant executor TLS server name", "configuration");
    }
    const tls = loadTlsMaterial(opts);
    if (opts.requireTls && !tls) {
      throw new InstantExecutorClientError(
        "instant executor TLS configuration requires CA, client certificate, and private key files",
        "configuration",
      );
    }
    this.tlsConfigured = tls !== undefined;
    this.httpsAgent = new https.Agent({
      keepAlive: true,
      rejectUnauthorized: true,
      ...(tls ?? {}),
    });
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
    if (url.protocol === "http:" && !isLoopbackHostname(url.hostname)) {
      return Promise.reject(new InstantExecutorClientError("plain HTTP executor endpoint must be loopback", "configuration"));
    }
    if (url.protocol === "https:" && !this.tlsConfigured) {
      return Promise.reject(new InstantExecutorClientError("instant executor TLS credentials are required for HTTPS", "configuration"));
    }
    return new Promise<T>((resolve, reject) => {
      let timedOut = false;
      let cancelled = false;
      const req = requestFn(url, {
        method: "POST",
        agent: url.protocol === "https:" ? this.httpsAgent : this.httpAgent,
        rejectUnauthorized: true,
        ...(url.protocol === "https:" && this.tlsServerName ? { servername: this.tlsServerName } : {}),
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
