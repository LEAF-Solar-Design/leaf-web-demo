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
import { Pool } from "pg";

import {
  createEnabledProductionInstantExecutorClient,
  createHarness,
} from "../src/server.js";
import { redactTokens } from "../src/redact.js";
import { authorRunnerMode, validateProductionHarnessEnv } from "../src/runtimeSafety.js";
import { createShutdownHandler } from "../src/shutdown.js";
import { DEFAULT_TENANT } from "../src/ports/index.js";
import type { AgentGrant, HarnessPorts, OAuthGrantProvider } from "../src/ports/index.js";
import type { ConverseRunner } from "../src/ports/converse.js";
import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { AgentSdkRunner } from "../src/ports/impl/agentSdkRunner.js";
import { BrokerApsClientHttp } from "../src/ports/impl/brokerApsClient.js";
import { ConverseSdkRunner } from "../src/ports/impl/converseSdkRunner.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { HttpGateClient } from "../src/ports/impl/gateClient.js";
import { HttpInstantExecutorClient } from "../src/ports/impl/instantExecutorClient.js";
import {
  assertHarnessCatalog,
  type HarnessColumn,
  type HarnessConstraint,
  type HarnessIndex,
} from "../src/ports/impl/harnessSchema.js";
import {
  createSessionStore,
  type SessionStoreHandle,
} from "../src/ports/impl/sessionStoreFactory.js";
import { createTenantGrantStore, OAuthGrantProviderImpl } from "../src/ports/impl/oauthGrantProvider.js";
import { startGitWorker, stopGitWorker } from "../src/ports/impl/gitWorker.js";
import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";
import {
  AuthorStandardServicesRunner,
  StandardServicesOAuthGrantProvider,
  standardServicesResolverFromEnv,
} from "../src/ports/impl/leafStandardServicesResolver.js";
import type { StandardServicesResolver } from "../src/vendor/mushy-author/ports/impl/standardServicesRuntime.js";
import { CustomizationCoordinationClient } from "../src/ports/impl/customizationCoordinationClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeTurnRunner } from "../src/ports/fakes/fakeTurnRunner.js";

const HARNESS_PORT = Number(process.env.HARNESS_PORT || 8150);
validateProductionHarnessEnv();
const gitWorkerUp = startGitWorker();
console.log(`[harness] git worker: ${gitWorkerUp ? "started (clean spawn context)" : "UNAVAILABLE - in-process fallback"}`);
const REPO_ROOT = process.env.LEAF_REPO_ROOT ?? "C:/tmp/leaf-web-demo";
const TENANTS_DIR = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
// Bare repositories are durable lifecycle authority, separate from effective
// tenant worktrees. In production /data deployments this never falls back to OS temp.
const TENANT_GIT_DIR = process.env.LEAF_TENANT_GIT_DIR ?? `${TENANTS_DIR.replace(/\/$/, "")}/tenant-git`;
const SINGLE_REPO_OVERRIDE = (process.env.LEAF_TENANT_REPO ?? "").trim(); // demo back-compat
const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:8140";
// Fixture used to auto-provision a brand-new tenant's repo. Absolute so it works both
// compiled (dist/scripts/serve.js) and via strip-types (scripts/serve.ts).
const TENANT_FIXTURE =
  process.env.LEAF_TENANT_FIXTURE ?? join(REPO_ROOT, "harness", "test", "fixtures", "tenant-repo");
let sessionStoreHandle: SessionStoreHandle | null = null;
let instantExecutorClient: HttpInstantExecutorClient | null = null;

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
 * SPINE MOUNT (chip-spine-harness-mount, census #12): compose the §18 spine
 * engine — ConverseLoop behind the frozen `POST /turn` wire via
 * SpineTurnAdapter — as the LIVE converse runner. Every tool execution inside a
 * turn now runs gate-before-exec (HttpGateClient -> POST /internal/agent/gate)
 * and dispatches only registered tool NAMES through HttpAppRunClient.submitRun
 * (invariant v2). The pre-mount AgentSdkTurnRunner (SDK-direct, broker-direct
 * tools, no app gate consult) is deliberately NO LONGER reachable from serve.
 *
 * FAIL CLOSED: both LEAF_APP_URL and LEAF_APP_DISPATCH_SECRET must be set or
 * the converse lane stays unwired and `POST /turn` answers 501 — there is no
 * ungated fallback. The author/Build lane is unaffected either way.
 */
function spineTurnRunner(
  oauth: OAuthGrantProvider,
  standardServicesResolver: StandardServicesResolver | undefined,
): ConverseRunner | undefined {
  const appUrl = (process.env.LEAF_APP_URL ?? "").trim();
  const dispatchSecret = (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim();
  if (!appUrl || !dispatchSecret) {
    log(
      "[harness] converse lane DISABLED (fail closed): set LEAF_APP_URL and " +
        "LEAF_APP_DISPATCH_SECRET to mount the spine turn engine — POST /turn answers 501.",
    );
    return undefined;
  }
  // Durable loop-side store (SDK resume ids, transcript mirror, confirmation
  // mirrors). File remains the default. PostgreSQL is explicit and fails closed
  // when its URL is absent; schema creation never happens at process startup.
  sessionStoreHandle = createSessionStore();
  log(`[harness] converse session authority: ${sessionStoreHandle.kind}`);
  instantExecutorClient = createEnabledProductionInstantExecutorClient(
    (options) => new HttpInstantExecutorClient(options),
  ) ?? null;
  return new SpineTurnAdapter({
    oauth,
    appRun: new HttpAppRunClient({ baseUrl: appUrl, dispatchSecret }),
    gate: new HttpGateClient({ appBaseUrl: appUrl, dispatchSecret }),
    store: sessionStoreHandle.store,
    runnerFor: (grant: AgentGrant) => new ConverseSdkRunner({
      grant,
      ...(standardServicesResolver ? { standardServicesResolver } : {}),
    }),
    ...(standardServicesResolver ? { standardServicesResolver } : {}),
    ...(instantExecutorClient ? { instantExecutor: instantExecutorClient } : {}),
  });
}

/** Compose the real multi-tenant ports. */
function buildPorts(): HarnessPorts {
  // F18 seam: the backend is selected by $LEAF_GRANT_STORE (default `file` →
  // FileTenantGrantStore under $LEAF_GRANTS_DIR). An explicit `vault` request with no
  // vault wired must fail LOUDLY at boot — never silently persist tokens to disk.
  const grantStore = createTenantGrantStore();
  // The tenant Claude grant stays on this trusted host. Generated source crosses
  // the structured submit boundary as data, and only the broker may execute it
  // inside the E2B tool sandbox.
  // LEAF_AGENT_MOCK=1: fully hermetic mode — scripted fakes for BOTH the design-time
  // author loop and the converse/turn loop. Never loads the Agent SDK, never touches
  // Anthropic, never spends LLM credit. Takes priority over LEAF_SANDBOX.
  const mockAgent = (process.env.LEAF_AGENT_MOCK ?? "").trim() === "1";
  if (mockAgent) {
    log("[harness] LEAF_AGENT_MOCK=1 — agentRunner=FakeAgentRunner, converseRunner=FakeTurnRunner (scripted; no SDK, no network).");
  } else {
    log("[harness] author runner: AgentSdkRunner (structured source submission; generated code executes only through broker)");
    log("[harness] converse runner: spine ConverseLoop via SpineTurnAdapter (gate-before-exec; SDK loads on first /turn).");
  }
  const standardServicesResolver = standardServicesResolverFromEnv();
  const oauth = new StandardServicesOAuthGrantProvider(
    new OAuthGrantProviderImpl({ store: grantStore }),
  );
  const tenantRepo = new TenantRepoProviderImpl({
    locator: { async repoRef(tenantId: string) { return tenantRepoDir(tenantId); } },
    inPlace: true,
    bareBase: TENANT_GIT_DIR,
    autoProvisionFrom: TENANT_FIXTURE,
  });
  const broker = new BrokerApsClientHttp({ brokerUrl: BROKER_URL });
  return {
    oauth,
    grantAdmin: grantStore,
    tenantRepo,
    broker,
    agentRunner: authorRunnerMode() === "fake"
      ? new FakeAgentRunner()
      : new AuthorStandardServicesRunner(
          new AgentSdkRunner({
            maxTurns: 40,
            maxTotalTokens: 500_000,
            ...(standardServicesResolver ? { standardServicesResolver } : {}),
          }),
          Boolean(standardServicesResolver),
        ),
    ...(mockAgent
      ? { converseRunner: new FakeTurnRunner() }
      : (() => {
          const spine = spineTurnRunner(oauth, standardServicesResolver);
          return spine ? { converseRunner: spine } : {};
        })()),
    ...((process.env.LEAF_APP_URL ?? "").trim() && (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim()
      ? {
          customizationCoordination: new CustomizationCoordinationClient({
            baseUrl: (process.env.LEAF_APP_URL ?? "").trim(),
            dispatchSecret: (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim(),
          }),
        }
      : {}),
  };
}

async function validatePostgresStartup(): Promise<void> {
  const kind = (process.env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase();
  if (kind === "file") return;
  if (kind !== "postgres") {
    throw new Error("LEAF_HARNESS_SESSION_STORE must be file or postgres");
  }
  const connectionString = (
    process.env.LEAF_HARNESS_DATABASE_URL ??
    process.env.DATABASE_URL ??
    ""
  ).trim();
  if (!connectionString) {
    throw new Error(
      "LEAF_HARNESS_SESSION_STORE=postgres requires LEAF_HARNESS_DATABASE_URL or DATABASE_URL",
    );
  }
  const pool = new Pool({
    connectionString,
    max: 1,
    application_name: "leaf-platform-harness-startup",
  });
  try {
    const tableNames = [
      "harness_sessions",
      "harness_turns",
      "harness_events",
      "harness_confirmations",
      "harness_usage",
      "harness_tenant_repo_leases",
    ];
    const columnRows = await pool.query<HarnessColumn>(
      `SELECT table_name, column_name, data_type, is_nullable
       FROM information_schema.columns
       WHERE table_schema = current_schema()
         AND table_name = ANY($1::text[])`,
      [tableNames],
    );
    const constraintRows = await pool.query<HarnessConstraint>(
      `SELECT c.relname AS table_name, pg_get_constraintdef(pc.oid) AS definition
       FROM pg_constraint pc
       JOIN pg_class c ON c.oid = pc.conrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE n.nspname = current_schema()
         AND c.relname = ANY($1::text[])`,
      [tableNames],
    );
    const indexNames = [
      "harness_one_active_session",
      "harness_one_active_turn",
      "idx_harness_events_turn",
      "idx_harness_confirmations_session",
      "idx_harness_usage_session",
      "idx_harness_tenant_repo_leases_expiry",
    ];
    const indexRows = await pool.query<HarnessIndex>(
      `SELECT indexname, indexdef
       FROM pg_indexes
       WHERE schemaname = current_schema()
         AND indexname = ANY($1::text[])`,
      [indexNames],
    );
    assertHarnessCatalog({
      columns: columnRows.rows,
      constraints: constraintRows.rows,
      indexes: indexRows.rows,
    });
  } finally {
    await pool.end();
  }
}

async function main(): Promise<void> {
  await validatePostgresStartup();
  const server: Server = createHarness(buildPorts()).listen(HARNESS_PORT);
  server.on("listening", () => {
    log(
      `[harness] listening on http://127.0.0.1:${HARNESS_PORT}` +
        `  tenants_dir=${TENANTS_DIR}  tenant_git_dir=${TENANT_GIT_DIR}  demo_override=${SINGLE_REPO_OVERRIDE || "(none)"}  broker=${BROKER_URL}`,
    );
    log(`[harness] per-tenant grant store active; grant admin at PUT/GET/DELETE /grants/{tenantId} (token never logged).`);
  });
  server.on("error", (err: Error) => {
    log(`[harness] server error: ${err.message}`);
    process.exit(1);
  });

  const shutdown = createShutdownHandler({
    server,
    log,
    exit: (code) => process.exit(code),
    cleanup: () => {
      stopGitWorker();
      // Sessions are durable in Postgres, so exit need not block on the pool
      // close; attempt a best-effort graceful close and log any failure.
      void (sessionStoreHandle?.close() ?? Promise.resolve()).catch(
        (error: unknown) => {
          log(`[harness] session store close failed: ${(error as Error).message}`);
        },
      );
      instantExecutorClient?.close();
    },
  });
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

void main().catch((error: unknown) => {
  log(`[harness] startup failed: ${(error as Error).message}`);
  process.exit(1);
});
