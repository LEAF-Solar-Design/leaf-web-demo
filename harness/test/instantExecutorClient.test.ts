import { execFileSync, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createServer as createHttpsServer, request as httpsRequest } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { HttpInstantExecutorClient, HttpInstantExecutorProxyClient, InstantExecutorClientError } from "../src/ports/impl/instantExecutorClient.js";
import { createInstantExecutorProxy } from "../src/ports/impl/instantExecutorProxy.js";
import type { InstantExecutorClient, InstantInvocation, InstantInvocationResponse, InstantSessionAssignment } from "../src/ports/index.js";

const digest = `sha256:${"a".repeat(64)}`;
const assignment = (endpoint: string): InstantSessionAssignment => ({
  contract: "leaf.instant-execution/v1", assignment_id: "11111111-1111-4111-8111-111111111111", tenant_id: "tenant-demo", session_id: "22222222-2222-4222-8222-222222222222", executor_id: "executor-local-001", executor_endpoint: endpoint, binding_epoch: 1, lease_id: "77777777-7777-4777-8777-777777777777", lease_token: "x".repeat(32), execution_class: "instant", effective_catalog_digest: digest, code_digest: digest, artifact_digest: digest, drawing_context: invocation.drawing_context, issued_at: "2026-01-01T00:00:00Z", expires_at: "2099-01-01T00:00:00Z",
});
const invocation: InstantInvocation = { contract: "leaf.instant-execution/v1", invocation_id: "44444444-4444-4444-8444-444444444444", tenant_id: "tenant-demo", session_id: "22222222-2222-4222-8222-222222222222", assignment_id: "11111111-1111-4111-8111-111111111111", binding_epoch: 1, lease_id: "77777777-7777-4777-8777-777777777777", effective_catalog_digest: digest, code_digest: digest, artifact_digest: digest, deadline_at: "2099-01-01T00:00:00Z", capability: { capability_id: "drawing.read", tool_id: "instant-read", tool_version: "1.0.0" }, params: {}, drawing_context: { drawing_id: "rooftop-demo", version_id: "55555555-5555-4555-8555-555555555555", content_digest: digest, geometry_ref: "drawing-context:rooftop-ref-001" } };

type MtlsFixture = { dir: string; caFile: string; wrongCaFile: string; clientCertificateFile: string; clientPrivateKeyFile: string; serverCertificateFile: string; serverPrivateKeyFile: string };
type RuntimeFixture = {
  assignment: InstantSessionAssignment;
  invocation: InstantInvocation;
  status(): Promise<{ invocation_count: number; warm_process_ids: Record<string, number | null>; current_process_ids: Record<string, number | null>; outbound_attempts: Record<string, number> }>;
  close(): Promise<void>;
};

function runOpenSsl(args: string[]): void {
  execFileSync("openssl", args, { stdio: "ignore" });
}

function createMtlsFixture(): MtlsFixture {
  const dir = mkdtempSync(join(tmpdir(), "leaf-instant-mtls-"));
  const configFile = join(dir, "openssl.cnf");
  writeFileSync(configFile, [
    "[req]", "distinguished_name = subject", "prompt = no", "", "[subject]", "CN = leaf-test", "",
    "[ca_extensions]", "basicConstraints = critical, CA:true", "keyUsage = critical, keyCertSign, cRLSign", "",
    "[server_extensions]", "basicConstraints = CA:false", "keyUsage = critical, digitalSignature, keyEncipherment", "extendedKeyUsage = serverAuth", "subjectAltName = DNS:localhost", "",
    "[client_extensions]", "basicConstraints = CA:false", "keyUsage = critical, digitalSignature, keyEncipherment", "extendedKeyUsage = clientAuth", "",
  ].join("\n"));
  const caFile = join(dir, "ca.pem");
  const caKeyFile = join(dir, "ca-key.pem");
  const wrongCaFile = join(dir, "wrong-ca.pem");
  const wrongCaKeyFile = join(dir, "wrong-ca-key.pem");
  const serverCertificateFile = join(dir, "server.pem");
  const serverPrivateKeyFile = join(dir, "server-key.pem");
  const serverCsrFile = join(dir, "server.csr");
  const clientCertificateFile = join(dir, "client.pem");
  const clientPrivateKeyFile = join(dir, "client-key.pem");
  const clientCsrFile = join(dir, "client.csr");
  runOpenSsl(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", caKeyFile, "-out", caFile, "-days", "1", "-config", configFile, "-extensions", "ca_extensions"]);
  runOpenSsl(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", wrongCaKeyFile, "-out", wrongCaFile, "-days", "1", "-config", configFile, "-extensions", "ca_extensions"]);
  runOpenSsl(["req", "-newkey", "rsa:2048", "-nodes", "-keyout", serverPrivateKeyFile, "-out", serverCsrFile, "-subj", "/CN=localhost", "-config", configFile]);
  runOpenSsl(["x509", "-req", "-in", serverCsrFile, "-CA", caFile, "-CAkey", caKeyFile, "-CAcreateserial", "-out", serverCertificateFile, "-days", "1", "-extfile", configFile, "-extensions", "server_extensions"]);
  runOpenSsl(["req", "-newkey", "rsa:2048", "-nodes", "-keyout", clientPrivateKeyFile, "-out", clientCsrFile, "-subj", "/CN=leaf-harness-client", "-config", configFile]);
  runOpenSsl(["x509", "-req", "-in", clientCsrFile, "-CA", caFile, "-CAkey", caKeyFile, "-CAcreateserial", "-out", clientCertificateFile, "-days", "1", "-extfile", configFile, "-extensions", "client_extensions"]);
  return { dir, caFile, wrongCaFile, clientCertificateFile, clientPrivateKeyFile, serverCertificateFile, serverPrivateKeyFile };
}

async function startRuntimeFixture(fixture: MtlsFixture): Promise<RuntimeFixture> {
  const repository = join(process.cwd(), "..");
  const venvPython = join(repository, ".venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "python.exe" : "python");
  const python = process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python");
  const child = spawn(python, ["-u", join(process.cwd(), "test", "fixtures", "runtime-mtls-fixture.py"), fixture.caFile, fixture.serverCertificateFile, fixture.serverPrivateKeyFile], {
    cwd: repository,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => { stderr += chunk; });
  const reader = createInterface({ input: child.stdout });
  const lines: string[] = [];
  let resolveLine: ((line: string) => void) | undefined;
  reader.on("line", (line) => {
    if (resolveLine) {
      const resolve = resolveLine;
      resolveLine = undefined;
      resolve(line);
    } else lines.push(line);
  });
  const next = async <T>(): Promise<T> => {
    const line = lines.shift() ?? await new Promise<string>((resolve, reject) => {
      resolveLine = resolve;
      child.once("error", reject);
      child.once("exit", (code) => reject(new Error(`runtime fixture exited before responding (${code}): ${stderr}`)));
    });
    return JSON.parse(line) as T;
  };
  const ready = await next<{ state: string; assignment: InstantSessionAssignment; invocation: InstantInvocation }>();
  if (ready.state !== "ready") throw new Error(`runtime fixture did not become ready: ${stderr}`);
  return {
    assignment: ready.assignment,
    invocation: ready.invocation,
    async status() {
      child.stdin.write(`${JSON.stringify({ command: "status" })}\n`);
      return next();
    },
    async close() {
      if (child.exitCode !== null) return;
      child.stdin.write(`${JSON.stringify({ command: "stop" })}\n`);
      await new Promise<void>((resolve, reject) => {
        child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`runtime fixture exited (${code}): ${stderr}`)));
        child.once("error", reject);
      });
      reader.close();
    },
  };
}

function rawHttpsRequest(url: string, ca?: Buffer, servername?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const req = httpsRequest(url, { method: "POST", ...(ca ? { ca } : {}), ...(servername ? { servername } : {}) }, (res) => {
      res.resume();
      res.on("end", resolve);
    });
    req.on("error", reject);
    req.end();
  });
}

describe("HttpInstantExecutorClient", () => {
  it("reuses a keep-alive direct executor connection and sends only invocation metadata", async () => {
    let connections = 0;
    let requests = 0;
    const server = createServer((req, res) => {
      requests += 1;
      expect(req.url).toBe("/v1/invoke");
      expect(req.headers.authorization).toBe(`Bearer ${"x".repeat(32)}`);
      res.setHeader("content-type", "application/json");
      res.statusCode = requests === 1 ? 200 : 403;
      res.end(JSON.stringify({ contract: "leaf.instant-execution/v1", invocation_id: invocation.invocation_id, tenant_id: invocation.tenant_id, session_id: invocation.session_id, status: requests === 1 ? "succeeded" : "failed", code_digest: digest, completed_at: new Date().toISOString(), ...(requests === 1 ? { result: {} } : { error: { code: "SESSION_EXPIRED" } }) }));
    });
    server.on("connection", () => { connections += 1; });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const client = new HttpInstantExecutorClient();
    const a = assignment(`http://127.0.0.1:${(address as import("node:net").AddressInfo).port}`);
    await client.invoke(a, invocation);
    const failed = await client.invoke(a, { ...invocation, invocation_id: "44444444-4444-4444-8444-444444444445" });
    client.close();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    expect(connections).toBe(1);
    expect(failed.status).toBe("failed");
    expect(failed.error?.code).toBe("SESSION_EXPIRED");
  });

  it("rejects untrusted and certificate-less clients, then invokes through configured mTLS", async () => {
    const fixture = createMtlsFixture();
    let authenticatedRequests = 0;
    const server = createHttpsServer({
      ca: readFileSync(fixture.caFile),
      cert: readFileSync(fixture.serverCertificateFile),
      key: readFileSync(fixture.serverPrivateKeyFile),
      requestCert: true,
      rejectUnauthorized: true,
    }, (_req, res) => {
      authenticatedRequests += 1;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ contract: "leaf.instant-execution/v1", invocation_id: invocation.invocation_id, tenant_id: invocation.tenant_id, session_id: invocation.session_id, status: "succeeded", code_digest: digest, completed_at: new Date().toISOString(), result: {} }));
    });
    try {
      await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
      const port = (server.address() as import("node:net").AddressInfo).port;
      const endpoint = `https://127.0.0.1:${port}`;
      await expect(rawHttpsRequest(endpoint)).rejects.toThrow();
      await expect(rawHttpsRequest(endpoint, readFileSync(fixture.caFile))).rejects.toThrow();

      const client = new HttpInstantExecutorClient({
        caFile: fixture.caFile,
        clientCertificateFile: fixture.clientCertificateFile,
        clientPrivateKeyFile: fixture.clientPrivateKeyFile,
        requireTls: true,
        tlsServerName: "localhost",
      });
      try {
        await expect(client.invoke(assignment(endpoint), invocation)).resolves.toMatchObject({ status: "succeeded" });
      } finally {
        client.close();
      }
      expect(authenticatedRequests).toBe(1);
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
      rmSync(fixture.dir, { recursive: true, force: true });
    }
  });

  it("uses the assigned endpoint directly against the real warm runtime mTLS listener", async () => {
    const fixture = createMtlsFixture();
    let runtime: RuntimeFixture | undefined;
    let validClient: HttpInstantExecutorClient | undefined;
    let wrongCaClient: HttpInstantExecutorClient | undefined;
    let wrongSniClient: HttpInstantExecutorClient | undefined;
    try {
      runtime = await startRuntimeFixture(fixture);
      validClient = new HttpInstantExecutorClient({
        caFile: fixture.caFile,
        clientCertificateFile: fixture.clientCertificateFile,
        clientPrivateKeyFile: fixture.clientPrivateKeyFile,
        requireTls: true,
        tlsServerName: "localhost",
      });
      wrongCaClient = new HttpInstantExecutorClient({
        caFile: fixture.wrongCaFile,
        clientCertificateFile: fixture.clientCertificateFile,
        clientPrivateKeyFile: fixture.clientPrivateKeyFile,
        requireTls: true,
        tlsServerName: "localhost",
      });
      wrongSniClient = new HttpInstantExecutorClient({
        caFile: fixture.caFile,
        clientCertificateFile: fixture.clientCertificateFile,
        clientPrivateKeyFile: fixture.clientPrivateKeyFile,
        requireTls: true,
        tlsServerName: "wrong-sni.invalid",
      });
      expect(runtime.assignment.executor_endpoint).toMatch(/^https:\/\/127\.0\.0\.1:/);
      await expect(rawHttpsRequest(runtime.assignment.executor_endpoint, readFileSync(fixture.caFile), "localhost")).rejects.toThrow();
      await expect(validClient.invoke(runtime.assignment, runtime.invocation)).resolves.toMatchObject({
        status: "succeeded", result: { warm: true, layer: "Panels" },
      });
      await expect(wrongCaClient.invoke(runtime.assignment, runtime.invocation)).rejects.toMatchObject({ code: "transport" });
      expect(() => new HttpInstantExecutorClient({ caFile: fixture.caFile, requireTls: true })).toThrow(InstantExecutorClientError);
      await expect(wrongSniClient.invoke(runtime.assignment, runtime.invocation)).rejects.toMatchObject({ code: "transport" });
      const status = await runtime.status();
      expect(status.invocation_count).toBe(1);
      expect(status.current_process_ids).toEqual(status.warm_process_ids);
      expect(status.outbound_attempts).toEqual({ dns: 0, urlopen: 0, http_client: 0 });
    } finally {
      validClient?.close();
      wrongCaClient?.close();
      wrongSniClient?.close();
      await runtime?.close();
      rmSync(fixture.dir, { recursive: true, force: true });
    }
  });

  it("fails closed for missing, partial, unreadable, and unsafe executor transport configuration", async () => {
    expect(() => new HttpInstantExecutorClient({ requireTls: true })).toThrow(InstantExecutorClientError);
    expect(() => new HttpInstantExecutorClient({ caFile: "ca.pem" })).toThrow(InstantExecutorClientError);
    expect(() => new HttpInstantExecutorClient({
      caFile: "missing-ca.pem",
      clientCertificateFile: "missing-cert.pem",
      clientPrivateKeyFile: "missing-key.pem",
    })).toThrow(InstantExecutorClientError);

    const client = new HttpInstantExecutorClient();
    try {
      await expect(client.invoke(assignment("https://127.0.0.1:1"), invocation)).rejects.toMatchObject({ code: "configuration" });
      await expect(client.invoke(assignment("http://192.0.2.1:8130"), invocation)).rejects.toMatchObject({ code: "configuration" });
    } finally {
      client.close();
    }
  });

  it("routes through a loopback proxy without exposing executor mTLS material to the harness client", async () => {
    const proxySecret = "p".repeat(32);
    let forwarded = 0;
    const fakeExecutor: InstantExecutorClient = {
      async invoke(seenAssignment, seenInvocation): Promise<InstantInvocationResponse> {
        forwarded += 1;
        expect(seenAssignment.executor_endpoint).toBe("https://10.20.4.5:8088");
        expect(seenInvocation.invocation_id).toBe(invocation.invocation_id);
        return {
          contract: "leaf.instant-execution/v1",
          invocation_id: seenInvocation.invocation_id,
          tenant_id: seenInvocation.tenant_id,
          session_id: seenInvocation.session_id,
          status: "succeeded",
          code_digest: seenInvocation.code_digest,
          completed_at: new Date().toISOString(),
          result: { ok: true },
        };
      },
    };
    const proxy = createInstantExecutorProxy({ proxySecret, executorClient: fakeExecutor });
    await new Promise<void>((resolve) => proxy.listen(0, "127.0.0.1", resolve));
    try {
      const port = (proxy.address() as import("node:net").AddressInfo).port;
      const denied = await fetch(`http://127.0.0.1:${port}/v1/invoke`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-instant-proxy-secret": "wrong" },
        body: JSON.stringify({ assignment: assignment("https://10.20.4.5:8088"), invocation }),
      });
      expect(denied.status).toBe(401);
      expect(forwarded).toBe(0);

      const client = new HttpInstantExecutorProxyClient({
        proxyUrl: `http://127.0.0.1:${port}`,
        proxySecret,
      });
      try {
        await expect(client.invoke(assignment("https://10.20.4.5:8088"), invocation)).resolves.toMatchObject({
          status: "succeeded",
          result: { ok: true },
        });
      } finally {
        client.close();
      }
      expect(forwarded).toBe(1);
    } finally {
      await new Promise<void>((resolve) => proxy.close(() => resolve()));
    }
  });

  it("keeps the retired proxy client unavailable to production composition", () => {
    expect(() => new HttpInstantExecutorProxyClient({
      proxyUrl: "http://127.0.0.1:8170",
      proxySecret: "short",
    })).toThrow(InstantExecutorClientError);
    expect(() => new HttpInstantExecutorProxyClient({
      proxyUrl: "http://192.0.2.3:8170",
      proxySecret: "p".repeat(32),
    })).toThrow(InstantExecutorClientError);
    const privateNetworkClient = new HttpInstantExecutorProxyClient({
      proxyUrl: "http://instant-proxy.leaf-platform-staging.local:8170",
      proxySecret: "p".repeat(32),
      allowNetworkProxy: true,
    });
    privateNetworkClient.close();
    const serverSource = readFileSync(join(process.cwd(), "src/server.ts"), "utf8");
    expect(serverSource).toContain("HttpInstantExecutorClient");
    expect(serverSource).toContain("LEAF_INSTANT_EXECUTION_ENABLED");
    expect(serverSource).toContain("the harness connects to the assigned executor directly");
    expect(serverSource).not.toContain("new HttpInstantExecutorProxyClient");
  });
});
