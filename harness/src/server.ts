/**
 * Harness HTTP server - a thin shell over AuthorLoop. Ports are INJECTED, so the
 * hermetic tests wire fakes and the operator-gated `startReal()` wires the real
 * impls. See harness/contract/HARNESS-CONTRACT.md for the full API.
 *
 * Routes:
 *   GET  /health          -> { ok: true }
 *   POST /author          -> author route (build | one-off). Body {description, mode?}.
 *                            build:   200 { tool, code, preview }         (CONTRACT section 4)
 *                            one-off: 200 { tool, code, preview, run }
 *   POST /run-registered  -> run route (design-time-ONLY invariant). Body
 *                            {tool, params?, dwg?, aps_live?}. 200 section-3 envelope.
 *                            NEVER constructs the Agent SDK / touches AgentRunner.
 *
 * Tenant id comes from the X-Tenant-Id header stub (default "demo-tenant"),
 * matching the backbone. Concern 1 (Auth0 platform identity) is resolved upstream
 * and is NOT this harness's job; Concern 2 (Claude grant) is the OAuthGrantProvider.
 */

import { createServer as createHttpServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { AuthorLoop, AuthorLoopError } from "./agent/authorLoop.js";
import { classifyRoute } from "./routing.js";
import type { HarnessPorts } from "./ports/index.js";

export const DEFAULT_TENANT = "demo-tenant";

function tenantOf(req: IncomingMessage): string {
  const h = req.headers["x-tenant-id"];
  const v = Array.isArray(h) ? h[0] : h;
  return v && v.trim() ? v.trim() : DEFAULT_TENANT;
}

function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8").trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw) as Record<string, unknown>);
      } catch (e) {
        reject(new AuthorLoopError(`invalid JSON body: ${(e as Error).message}`, 400));
      }
    });
    req.on("error", reject);
  });
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(payload);
}

export interface Harness {
  handler: (req: IncomingMessage, res: ServerResponse) => Promise<void>;
  listen(port?: number): Server;
  loop: AuthorLoop;
}

export function createHarness(ports: HarnessPorts): Harness {
  const loop = new AuthorLoop(ports);

  const handler = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const path = (req.url ?? "").split("?")[0];
    const method = req.method ?? "GET";
    try {
      if (method === "GET" && path === "/health") {
        return send(res, 200, { ok: true, service: "leaf-tenant-author-harness" });
      }

      if (method === "POST" && path === "/author") {
        const body = await readJsonBody(req);
        const tenant = tenantOf(req);
        const description = typeof body.description === "string" ? body.description : "";
        if (!description.trim()) {
          return send(res, 400, { error: { message: "description is required" } });
        }
        const route = classifyRoute({ path, body });
        if (route === "one-off") {
          const out = await loop.oneOff(tenant, description);
          return send(res, 200, out);
        }
        // default author route = build (author + register + one commit)
        const out = await loop.build(tenant, description);
        return send(res, 200, out);
      }

      if (method === "POST" && path === "/run-registered") {
        const body = await readJsonBody(req);
        const tenant = tenantOf(req);
        const toolName = typeof body.tool === "string" ? body.tool : "";
        if (!toolName) {
          return send(res, 400, { error: { message: "tool (registered tool name) is required" } });
        }
        const params = (body.params as Record<string, unknown>) ?? {};
        const dwg = typeof body.dwg === "string" ? body.dwg : "rooftop_demo";
        const apsLive = body.aps_live === true;
        const envelope = await loop.run(tenant, toolName, params, dwg, apsLive);
        return send(res, 200, envelope);
      }

      return send(res, 404, { error: { message: `no route for ${method} ${path}` } });
    } catch (err) {
      if (err instanceof AuthorLoopError) {
        return send(res, err.status, {
          error: { message: err.message, diagnostics: err.diagnostics ?? [] },
        });
      }
      return send(res, 500, { error: { message: (err as Error).message } });
    }
  };

  return {
    handler,
    loop,
    listen(port = 0): Server {
      const server = createHttpServer((req, res) => {
        void handler(req, res);
      });
      server.listen(port);
      return server;
    },
  };
}

/**
 * OPERATOR-GATED real wiring. Not auto-run and not exercised by the hermetic gate.
 * It only COMPILES here to prove the real ports slot in unchanged. To run live the
 * operator must: register the app for "sign in with Claude", install
 * @anthropic-ai/claude-agent-sdk, run the broker (BROKER_URL), and provide the
 * per-tenant grant store + repo locator. See HARNESS-CONTRACT.md.
 */
export async function startReal(port = 8130): Promise<Server> {
  const { AgentSdkRunner } = await import("./ports/impl/agentSdkRunner.js");
  const { BrokerApsClientHttp } = await import("./ports/impl/brokerApsClient.js");
  const { OAuthGrantProviderImpl } = await import("./ports/impl/oauthGrantProvider.js");
  const { TenantRepoProviderImpl } = await import("./ports/impl/tenantRepoProvider.js");

  const ports: HarnessPorts = {
    agentRunner: new AgentSdkRunner(),
    broker: new BrokerApsClientHttp(),
    oauth: new OAuthGrantProviderImpl({
      store: { async get() { return null; } }, // operator supplies the real store
    }),
    tenantRepo: new TenantRepoProviderImpl({
      locator: { async repoRef(t) { throw new Error(`no repo locator configured for ${t}`); } },
    }),
  };
  return createHarness(ports).listen(port);
}
