/**
 * SERVE — the runnable harness HTTP sidecar (UI wave 3, Contract 1).
 *
 * Composes the SAME four real ports as scripts/drive.ts and starts the long-lived
 * harness HTTP server so the product's Build lane can reach a real Agent SDK author
 * loop over HTTP:
 *
 *   server/routers/author.py -> POST {LEAF_AUTHOR_HARNESS_URL}/author -> THIS server
 *
 * Unlike drive.ts (a one-shot milestone driver that also runs + writes a receipt),
 * serve.ts just listens; each POST /author spawns exactly ONE design-time session
 * and tears it down, and the run path (POST /run-registered) never touches the SDK.
 *
 * Ports (all via env, same knobs the milestone driver used):
 *   - OAuthGrantProviderImpl(EnvOrFileGrantStore)  — Concern-2 grant; the store reads
 *     env CLAUDE_CODE_OAUTH_TOKEN else the file at LEAF_GRANT_FILE
 *     (default C:/tmp/hosted-oauth-spike/.grant/token). This code reads the grant
 *     PATH only; it never opens, prints, or logs the token.
 *   - TenantRepoProviderImpl(inPlace) at LEAF_TENANT_REPO (default the demo tenant) —
 *     a `build` commit and a later `run` read the SAME registry.json + tool files.
 *   - BrokerApsClientHttp at BROKER_URL — APS execution only through the broker.
 *   - AgentSdkRunner — the ONLY Anthropic egress, on the author path only.
 *
 * Secret discipline: the grant VALUE is resolved inside EnvOrFileGrantStore and only
 * ever flows into a scrubbed SDK child env (AgentSdkRunner). This file never reads,
 * prints, or logs it; any token-shaped string is redacted from anything we emit.
 *
 * Run (compiled): `node dist/scripts/serve.js`  (npm run serve). Compile first with
 * `npx tsc -p tsconfig.build.json`.
 */

import type { Server } from "node:http";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { AgentSdkRunner } from "../src/ports/impl/agentSdkRunner.js";
import { BrokerApsClientHttp } from "../src/ports/impl/brokerApsClient.js";
import { OAuthGrantProviderImpl, EnvOrFileGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";

const HARNESS_PORT = Number(process.env.HARNESS_PORT || 8150);
const TENANT_REPO = process.env.LEAF_TENANT_REPO ?? "C:/tmp/leaf-tenants/demo-tenant";
const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:8140";

// Defense in depth: redact any token-shaped value from anything we log. We never
// read or print the grant ourselves, but a stray error string must never leak one.
const TOKENISH = /\b(sk-ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{40,})\b/g;
function log(msg: string): void {
  process.stderr.write(msg.replace(TOKENISH, "[REDACTED]") + "\n");
}

/** Compose the SAME real ports the milestone driver wires (drive.ts). */
function buildPorts(): HarnessPorts {
  return {
    oauth: new OAuthGrantProviderImpl({ store: new EnvOrFileGrantStore() }),
    tenantRepo: new TenantRepoProviderImpl({
      locator: { async repoRef() { return TENANT_REPO; } },
      inPlace: true,
    }),
    broker: new BrokerApsClientHttp({ brokerUrl: BROKER_URL }),
    agentRunner: new AgentSdkRunner({ maxTurns: 40, maxTotalTokens: 500_000 }),
  };
}

function main(): void {
  const server: Server = createHarness(buildPorts()).listen(HARNESS_PORT);
  server.on("listening", () => {
    log(
      `[harness] listening on http://127.0.0.1:${HARNESS_PORT}` +
        `  tenant_repo=${TENANT_REPO}  broker=${BROKER_URL}`,
    );
    log(`[harness] grant resolved lazily per request via EnvOrFileGrantStore (token never logged).`);
  });
  server.on("error", (err: Error) => {
    log(`[harness] server error: ${err.message}`);
    process.exit(1);
  });

  const shutdown = (sig: string): void => {
    log(`[harness] ${sig} -> closing`);
    server.close(() => process.exit(0));
    // hard-stop backstop if close() hangs on a live connection
    setTimeout(() => process.exit(0), 2000).unref();
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

main();
