/**
 * SERVE — the runnable harness HTTP sidecar (UI wave 3 Contract 1; wave 4 multi-tenant).
 *
 * Composes the real ports and starts the long-lived harness HTTP server so the
 * product's Build lane can reach a real Agent SDK author loop over HTTP:
 *
 *   server/routers/author.py -> POST {LEAF_AUTHOR_HARNESS_URL}/author -> THIS server
 *   server/routers/tenant.py -> PUT/GET/DELETE {…}/grants/{tenantId} -> THIS server
 *
 * Each POST /author spawns exactly ONE design-time session for the request's tenant and
 * tears it down; the run path (POST /run-registered) never touches the SDK.
 *
 * Wave 4 — MULTI-TENANT:
 *   - OAuthGrantProviderImpl(FileTenantGrantStore) — per-tenant Concern-2 grant. Each
 *     tenant links its OWN "sign in with Claude" token; the store persists one file per
 *     tenant under $LEAF_GRANTS_DIR/<tenant>.token (default C:/tmp/leaf-grants). The demo
 *     tenant falls back to the env/file grant (LEAF_GRANT_FILE / CLAUDE_CODE_OAUTH_TOKEN)
 *     when it has no per-tenant file, so the proven demo loop is unchanged. PRODUCTION
 *     swaps FileTenantGrantStore for a vault/DPAPI store (same interface).
 *   - The SAME store instance is the grantAdmin backing PUT/GET/DELETE /grants/{tenant}.
 *   - TenantRepoProviderImpl(inPlace, autoProvisionFrom) — the tenant's mushy repo lives
 *     at $LEAF_TENANTS_DIR/<tenant> (default C:/tmp/leaf-tenants). demo-tenant honours
 *     $LEAF_TENANT_REPO for back-compat. A brand-new tenant's repo is auto-provisioned
 *     from the fixture on first authoring.
 *   - BrokerApsClientHttp at BROKER_URL — APS execution only through the broker.
 *   - AgentSdkRunner — the ONLY Anthropic egress, on the author path only.
 *
 * DEPLOYMENT CONSISTENCY: the app (deps.resolve_tenant_repo_dir) and THIS harness must
 * resolve a tenant to the SAME repo dir. Set $LEAF_TENANTS_DIR on BOTH processes (and,
 * for the demo tenant, the same $LEAF_TENANT_REPO). The proven demo loop uses only the
 * demo tenant, where both sides agree via $LEAF_TENANT_REPO.
 *
 * Secret discipline: the grant VALUE is resolved inside FileTenantGrantStore and only
 * ever flows into a scrubbed SDK child env (AgentSdkRunner). This file never reads,
 * prints, or logs a token; any token-shaped string is redacted from anything we emit.
 *
 * Run (compiled): `node dist/scripts/serve.js`  (npm run serve). Compile first with
 * `npx tsc -p tsconfig.build.json`.
 */

import type { Server } from "node:http";
import { join } from "node:path";

import { createHarness } from "../src/server.js";
import { redactTokens } from "../src/redact.js";
import { DEFAULT_TENANT } from "../src/ports/index.js";
import type { HarnessPorts } from "../src/ports/index.js";
import type { ConverseRunner, ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";
import { AgentSdkRunner } from "../src/ports/impl/agentSdkRunner.js";
import { E2bAgentRunner } from "../src/ports/impl/e2bAgentRunner.js";
import { BrokerApsClientHttp } from "../src/ports/impl/brokerApsClient.js";
import { createTenantGrantStore, OAuthGrantProviderImpl } from "../src/ports/impl/oauthGrantProvider.js";
import { startGitWorker } from "../src/ports/impl/gitWorker.js";
import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeTurnRunner } from "../src/ports/fakes/fakeTurnRunner.js";

const HARNESS_PORT = Number(process.env.HARNESS_PORT || 8150);
const gitWorkerUp = startGitWorker();
console.log(`[harness] git worker: ${gitWorkerUp ? "started (clean spawn context)" : "UNAVAILABLE - in-process fallback"}`);
const REPO_ROOT = process.env.LEAF_REPO_ROOT ?? "C:/tmp/leaf-web-demo";
const TENANTS_DIR = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
const SINGLE_REPO_OVERRIDE = (process.env.LEAF_TENANT_REPO ?? "").trim(); // demo back-compat
const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:8140";
// Fixture used to auto-provision a brand-new tenant's repo. Absolute so it works both
// compiled (dist/scripts/serve.js) and via strip-types (scripts/serve.ts).
const TENANT_FIXTURE =
  process.env.LEAF_TENANT_FIXTURE ?? join(REPO_ROOT, "harness", "test", "fixtures", "tenant-repo");

// Defense in depth: redact any token-shaped value from anything we log. We never
// read or print the grant ourselves, but a stray error string must never leak one.
// One shared pattern for every harness logging site: src/redact.ts.
function log(msg: string): void {
  process.stderr.write(redactTokens(msg) + "\n");
}

/** Reject anything but a single, traversal-free path component (mirrors the Python
 *  resolver + the grant-store filename guard) so a tenant id can never escape the base. */
function safeComponent(tenantId: string): string {
  const tid = (tenantId ?? "").trim();
  const base = tid.replace(/\\/g, "/").split("/").pop() ?? "";
  if (!base || base === "." || base === ".." || base !== tid || !/^[A-Za-z0-9._-]+$/.test(base)) {
    throw new Error(`invalid tenant id: ${JSON.stringify(tenantId)}`);
  }
  return base;
}

/** Resolve a tenant to its mushy-repo dir. demo-tenant honours $LEAF_TENANT_REPO. */
function tenantRepoDir(tenantId: string): string {
  if (tenantId === DEFAULT_TENANT && SINGLE_REPO_OVERRIDE) return SINGLE_REPO_OVERRIDE;
  return join(TENANTS_DIR, safeComponent(tenantId));
}

/**
 * Non-literal dynamic import (mirrors agentSdkRunner.ts's own `dynImport`) so tsc never
 * tries to resolve this path at compile time — the real converse turn runner (H2:
 * src/ports/impl/agentSdkTurnRunner.ts) may not exist yet in every checkout.
 */
function dynImportTurnRunner(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

/**
 * Lazily construct the real Agent-SDK-backed ConverseRunner (H2's
 * `AgentSdkTurnRunner`, src/ports/impl/agentSdkTurnRunner.ts). The import only fires on
 * the FIRST real `/turn` call it serves — never at startup, never in mock mode — so:
 *   - a checkout without H2's file yet still boots and serves every other route;
 *   - the SDK is never loaded unless a real turn is actually driven;
 *   - a missing/broken module surfaces as a clean per-request error (caught here),
 *     not a process crash.
 * LEAF_AGENT_MOCK=1 never constructs this wrapper at all (see buildPorts below).
 */
function lazyAgentSdkTurnRunner(ports: {
  oauth: OAuthGrantProviderImpl;
  tenantRepo: TenantRepoProviderImpl;
  broker: BrokerApsClientHttp;
}): ConverseRunner {
  let real: ConverseRunner | undefined;
  let loadError: Error | undefined;

  async function ensureReal(): Promise<ConverseRunner> {
    if (real) return real;
    if (loadError) throw loadError;
    try {
      // Constructor is (ports, opts) — AgentSdkTurnRunnerPorts first (oauth/
      // tenantRepo/broker, the SAME instances buildPorts hands the harness),
      // options second. The earlier opts-as-first-arg guess crashed on
      // this.ports.oauth at the first real turn.
      const mod = (await dynImportTurnRunner(["..", "src", "ports", "impl", "agentSdkTurnRunner.js"])) as {
        AgentSdkTurnRunner: new (
          p: { oauth: unknown; tenantRepo: unknown; broker: unknown },
          opts?: { maxTurns?: number; maxTotalTokens?: number },
        ) => ConverseRunner;
      };
      real = new mod.AgentSdkTurnRunner(ports, { maxTurns: 40, maxTotalTokens: 500_000 });
      return real;
    } catch (err) {
      loadError = err instanceof Error ? err : new Error(String(err));
      log(`[harness] AgentSdkTurnRunner unavailable (${loadError.message}) — /turn will error until it lands.`);
      throw loadError;
    }
  }

  return {
    async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
      const runner = await ensureReal();
      yield* runner.runTurn(input);
    },
  };
}

/** Compose the real multi-tenant ports. */
function buildPorts(): HarnessPorts {
  // F18 seam: the backend is selected by $LEAF_GRANT_STORE (default `file` →
  // FileTenantGrantStore under $LEAF_GRANTS_DIR). An explicit `vault` request with no
  // vault wired must fail LOUDLY at boot — never silently persist tokens to disk.
  const grantStore = createTenantGrantStore();
  // F2 (2A): when LEAF_SANDBOX=e2b, run the design-time author session INSIDE an
  // egress-locked E2B sandbox instead of in-process. Default (unset/anything else) keeps
  // AgentSdkRunner so the proven demo + hermetic tests are unchanged. LEAF_SANDBOX_BROKER_HOST
  // sets the ONE allowlisted egress host (defaults to the proven public stand-in).
  const useE2b = (process.env.LEAF_SANDBOX ?? "").trim().toLowerCase() === "e2b";
  // LEAF_AGENT_MOCK=1: fully hermetic mode — scripted fakes for BOTH the design-time
  // author loop and the converse/turn loop. Never loads the Agent SDK, never touches
  // Anthropic, never spends LLM credit. Takes priority over LEAF_SANDBOX.
  const mockAgent = (process.env.LEAF_AGENT_MOCK ?? "").trim() === "1";
  if (mockAgent) {
    log("[harness] LEAF_AGENT_MOCK=1 — agentRunner=FakeAgentRunner, converseRunner=FakeTurnRunner (scripted; no SDK, no network).");
  } else {
    log(`[harness] author runner: ${useE2b ? "E2bAgentRunner (LEAF_SANDBOX=e2b, egress-locked sandbox)" : "AgentSdkRunner (in-process; default)"}`);
    log("[harness] converse runner: AgentSdkTurnRunner (lazy; loaded on first /turn call).");
  }
  const oauth = new OAuthGrantProviderImpl({ store: grantStore });
  const tenantRepo = new TenantRepoProviderImpl({
    locator: { async repoRef(tenantId: string) { return tenantRepoDir(tenantId); } },
    inPlace: true,
    autoProvisionFrom: TENANT_FIXTURE,
  });
  const broker = new BrokerApsClientHttp({ brokerUrl: BROKER_URL });
  return {
    oauth,
    grantAdmin: grantStore,
    tenantRepo,
    broker,
    agentRunner: mockAgent
      ? new FakeAgentRunner()
      : useE2b
        ? new E2bAgentRunner({ brokerHost: process.env.LEAF_SANDBOX_BROKER_HOST || undefined })
        : new AgentSdkRunner({ maxTurns: 40, maxTotalTokens: 500_000 }),
    converseRunner: mockAgent ? new FakeTurnRunner() : lazyAgentSdkTurnRunner({ oauth, tenantRepo, broker }),
  };
}

function main(): void {
  const server: Server = createHarness(buildPorts()).listen(HARNESS_PORT);
  server.on("listening", () => {
    log(
      `[harness] listening on http://127.0.0.1:${HARNESS_PORT}` +
        `  tenants_dir=${TENANTS_DIR}  demo_override=${SINGLE_REPO_OVERRIDE || "(none)"}  broker=${BROKER_URL}`,
    );
    log(`[harness] per-tenant grant store active; grant admin at PUT/GET/DELETE /grants/{tenantId} (token never logged).`);
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
