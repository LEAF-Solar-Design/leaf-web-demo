import { EventEmitter } from "node:events";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HarnessPorts } from "../src/ports/index.js";
import type { createHarness } from "../src/server.js";

const startup = vi.hoisted(() => ({
  createHarness: vi.fn((_ports: HarnessPorts, _options?: Parameters<typeof createHarness>[1]) => ({
    listen: vi.fn(),
  })),
  getGrant: vi.fn(),
  acquireGrant: vi.fn(),
  settleGrant: vi.fn(),
  startGitWorker: vi.fn(() => true),
  stopGitWorker: vi.fn(),
}));

// Exercise serve.ts itself, but keep process, database, git, grants and network
// startup bounded. The proposal service and standard-services OAuth wrapper stay real.
vi.mock("../src/server.js", () => ({
  createHarness: startup.createHarness,
  createEnabledProductionInstantExecutorClient: vi.fn(),
}));
vi.mock("pg", () => ({ Pool: class {
  constructor() { throw new Error("unexpected database startup"); }
} }));
vi.mock("../src/runtimeSafety.js", () => ({
  validateProductionHarnessEnv: vi.fn(),
  authorRunnerMode: () => "fake",
}));
vi.mock("../src/ports/impl/gitWorker.js", () => ({
  startGitWorker: startup.startGitWorker,
  stopGitWorker: startup.stopGitWorker,
}));
vi.mock("../src/ports/impl/oauthGrantProvider.js", () => ({
  createTenantGrantStore: () => ({}),
  resolveGrantsDir: () => "C:/tmp/leaf-grants",
  resolveGrantStoreBackend: () => "file",
  OAuthGrantProviderImpl: class {
    getGrant = startup.getGrant;
    acquireGrant = startup.acquireGrant;
    settleGrant = startup.settleGrant;
  },
}));
vi.mock("../src/ports/impl/tenantRepoProvider.js", () => ({
  TenantRepoProviderImpl: class {},
  PgTenantRepoLeaseCoordinator: class {
    constructor() { throw new Error("unexpected lease database startup"); }
  },
}));
vi.mock("../src/ports/impl/tenantForgeAuthority.js", () => ({
  createTenantForgeConfigurationFromEnv: () => undefined,
}));
vi.mock("../src/ports/impl/projectForgeAuthority.js", () => ({
  createProjectForgeAuthorityFromEnv: () => undefined,
  createProjectRepositoryEditsForService: () => undefined,
}));
vi.mock("../src/ports/impl/httpUpstreamSink.js", () => ({
  upstreamSinkFromEnv: () => undefined,
}));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.resetModules();
  vi.clearAllMocks();
});

describe("portfolio proposal production composition", () => {
  it("mounts a real service using the same tenant OAuth provider as the production ports", async () => {
    vi.resetModules();
    vi.stubEnv("HARNESS_PORT", "8150");
    vi.stubEnv("LEAF_AGENT_MOCK", "1");
    vi.stubEnv("LEAF_HARNESS_SESSION_STORE", "file");
    vi.stubEnv("LEAF_TENANT_MCP_BROKER_URL", "");
    vi.stubEnv("LEAF_STANDARD_SERVICES_ENV", "local");
    vi.stubEnv("LEAF_RUNTIME_ENV", "local");
    vi.stubEnv("LEAF_APP_URL", "");
    vi.stubEnv("LEAF_APP_DISPATCH_SECRET", "");
    vi.stubEnv("GLUG_MUSHY_AUTHOR_ENABLED", "0");
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const exit = vi.spyOn(process, "exit").mockImplementation(() => {
      throw new Error("unexpected production startup exit");
    });
    const server = new EventEmitter();
    const listen = vi.fn(() => server);
    startup.createHarness.mockReturnValue({ listen });
    startup.acquireGrant.mockRejectedValue(new Error("deliberate acquisition failure"));
    const signals = ["SIGINT", "SIGTERM"] as const;
    const originalListeners = signals.map(signal => new Set(process.listeners(signal)));
    try {
      await import("../scripts/serve.js");
      // main() is intentionally fire-and-forget; wait only for its bounded startup.
      await vi.waitFor(() => expect(startup.createHarness).toHaveBeenCalledTimes(1), { timeout: 1000 });
      expect(exit).not.toHaveBeenCalled();
      expect(startup.startGitWorker).toHaveBeenCalledTimes(1);
      expect(listen).toHaveBeenCalledExactlyOnceWith(8150);
      const [ports, options] = startup.createHarness.mock.calls[0];
      // resetModules reloads the class, so compare against that same module instance.
      const { PortfolioProposalService: ProductionService } = await import("../src/agent/portfolioProposal.js");
      expect(options?.portfolioProposals).toBeInstanceOf(ProductionService);
      const acquire = vi.spyOn(ports.oauth, "acquireGrant");
      const service = options!.portfolioProposals!;
      await expect(service.propose({
        tenant_id: "production-test-tenant",
        source: { project_id: "portfolio", revision: 1, digest64hex: "a".repeat(64) },
        instruction: "Make the title concise",
        fields: [{ id: "title", value: "Supplied title", max_length: 100 }],
      })).rejects.toMatchObject({ code: "grant_unavailable" });
      expect(acquire).toHaveBeenCalledExactlyOnceWith("production-test-tenant");
      expect(startup.acquireGrant).toHaveBeenCalledExactlyOnceWith("production-test-tenant");
      expect(startup.getGrant).not.toHaveBeenCalled();
      expect(startup.settleGrant).not.toHaveBeenCalled();
      for (const [index, signal] of signals.entries()) {
        expect(process.listeners(signal).filter(listener => !originalListeners[index].has(listener))).toHaveLength(1);
      }
    } finally {
      for (const [index, signal] of signals.entries()) {
        for (const listener of process.listeners(signal)) {
          if (!originalListeners[index].has(listener)) process.removeListener(signal, listener);
        }
      }
      server.removeAllListeners();
    }
  });
});
