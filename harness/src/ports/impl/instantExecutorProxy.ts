/** Loopback-only proxy that isolates executor mTLS keys from the Claude harness. */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { InstantExecutorClient, InstantInvocation, InstantSessionAssignment } from "../index.js";
import { HttpInstantExecutorClient, InstantExecutorClientError } from "./instantExecutorClient.js";

export interface InstantExecutorProxyOptions {
  proxySecret: string;
  executorClient: InstantExecutorClient;
  maxBodyBytes?: number;
}

function configuredText(value: string | undefined): string | undefined {
  const text = value?.trim();
  return text || undefined;
}

function loadSecret(env: NodeJS.ProcessEnv, name: string): string {
  const inline = configuredText(env[name]);
  const filename = configuredText(env[`${name}_FILE`]);
  if (inline && filename) throw new InstantExecutorClientError(`configure ${name} or ${name}_FILE, not both`, "configuration");
  if (filename) return readFileSync(filename, "utf8").trim();
  return inline ?? "";
}

function materializePem(env: NodeJS.ProcessEnv, name: string, filename: string): string {
  const file = configuredText(env[`${name}_FILE`]);
  const pem = configuredText(env[name]);
  if (file && pem) throw new InstantExecutorClientError(`configure ${name} or ${name}_FILE, not both`, "configuration");
  if (file) return file;
  if (!pem) throw new InstantExecutorClientError(`${name} is required`, "configuration");
  const directory = mkdtempSync(join(tmpdir(), "leaf-instant-executor-proxy-"));
  const path = join(directory, filename);
  try {
    writeFileSync(path, pem, { encoding: "utf8", mode: 0o600, flag: "wx" });
  } catch {
    throw new InstantExecutorClientError("instant executor proxy secret file already exists", "configuration");
  }
  return path;
}

function sameSecret(supplied: string | undefined, expected: string): boolean {
  if (!supplied) return false;
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

async function readJson(req: IncomingMessage, maxBodyBytes: number): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buffer.length;
    if (total > maxBodyBytes) throw new Error("request body is too large");
    chunks.push(buffer);
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("request body must be an object");
  return parsed as Record<string, unknown>;
}

function send(res: ServerResponse, status: number, body: Record<string, unknown>): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

export function createInstantExecutorProxy(opts: InstantExecutorProxyOptions): Server {
  const proxySecret = configuredText(opts.proxySecret);
  if (!proxySecret || proxySecret.length < 32) {
    throw new InstantExecutorClientError("instant executor proxy secret must contain at least 32 characters", "configuration");
  }
  const maxBodyBytes = opts.maxBodyBytes ?? 2 * 1024 * 1024;
  return createServer((req, res) => {
    void (async () => {
      if (req.method === "GET" && req.url === "/health") {
        send(res, 200, { ok: true, service: "instant-executor-proxy" });
        return;
      }
      if (req.method !== "POST" || req.url !== "/v1/invoke") {
        send(res, 404, { error: "not found" });
        return;
      }
      if (!sameSecret(req.headers["x-instant-proxy-secret"] as string | undefined, proxySecret)) {
        send(res, 401, { error: "proxy authentication required" });
        return;
      }
      const body = await readJson(req, maxBodyBytes);
      const assignment = body.assignment as InstantSessionAssignment | undefined;
      const invocation = body.invocation as InstantInvocation | undefined;
      if (!assignment || !invocation || typeof assignment !== "object" || typeof invocation !== "object") {
        send(res, 400, { error: "assignment and invocation are required" });
        return;
      }
      const response = await opts.executorClient.invoke(assignment, invocation);
      send(res, response.status === "succeeded" ? 200 : 403, response as unknown as Record<string, unknown>);
    })().catch((error) => {
      const message = error instanceof Error ? error.message : "instant executor proxy failure";
      send(res, 502, { error: message });
    });
  });
}

export function createInstantExecutorProxyFromEnv(env: NodeJS.ProcessEnv = process.env): Server {
  const proxySecret = loadSecret(env, "LEAF_INSTANT_EXECUTOR_PROXY_SECRET");
  const executorClient = new HttpInstantExecutorClient({
    requireTls: true,
    caFile: materializePem(env, "LEAF_INSTANT_EXECUTOR_CA_PEM", "executor-ca.pem"),
    clientCertificateFile: materializePem(env, "LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM", "executor-client-cert.pem"),
    clientPrivateKeyFile: materializePem(env, "LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM", "executor-client-key.pem"),
    tlsServerName: env.LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME,
    timeoutMs: env.LEAF_INSTANT_EXECUTOR_TIMEOUT_MS ? Number(env.LEAF_INSTANT_EXECUTOR_TIMEOUT_MS) : undefined,
  });
  return createInstantExecutorProxy({ proxySecret, executorClient });
}

export async function startInstantExecutorProxyFromEnv(env: NodeJS.ProcessEnv = process.env): Promise<Server> {
  const server = createInstantExecutorProxyFromEnv(env);
  const host = configuredText(env.LEAF_INSTANT_EXECUTOR_PROXY_HOST) ?? "127.0.0.1";
  if (host !== "127.0.0.1" && host !== "localhost" && host !== "::1" && host !== "0.0.0.0") {
    throw new InstantExecutorClientError("instant executor proxy must bind loopback or 0.0.0.0", "configuration");
  }
  const port = Number(configuredText(env.LEAF_INSTANT_EXECUTOR_PROXY_PORT) ?? "8170");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new InstantExecutorClientError("instant executor proxy port is invalid", "configuration");
  }
  await new Promise<void>((resolve) => server.listen(port, host, resolve));
  return server;
}
