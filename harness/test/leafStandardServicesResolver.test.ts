import { describe, expect, it, vi } from "vitest";

import {
  AuthorStandardServicesRunner,
  LeafStandardServicesResolver,
  StandardServicesOAuthGrantProvider,
  standardServicesResolverFromEnv,
  withAuthorStandardServicesAuthority,
} from "../src/ports/impl/leafStandardServicesResolver.js";
import type {
  AgentRunInput,
  AgentRunner,
  OAuthGrantProvider,
} from "../src/ports/index.js";
import {
  composeRunnerCapabilities,
  STANDARD_SERVICES_FACADE_TOOLS,
} from "../src/vendor/mushy-author/index.js";

const context = {
  tenant_id: "tenant-a",
  session_id: "session-a",
  subscription_mount_id: "account-a",
  authority_session_id: "session-a",
  authority_turn_id: "turn-a",
};

const approvalStore = {
  async create() { return true; },
  async approve() { return true; },
  async claim() { return null; },
  async complete() { return false; },
  async markUncertain() { return false; },
};

function attachment(overrides: Record<string, unknown> = {}) {
  return {
    bearer_token: "bearer.token.value.1234567890",
    channel_secret: "channel-secret-value-1234567890",
    expires_at: "2099-01-01T00:00:00.000Z",
    identity: {
      tenant_id: "tenant-a",
      subject_id: "auth0:alice",
      session_id: "session-a",
      authority_turn_id: "turn-a",
      subscription_mount_id: "account-a",
      runner_profile_id: "spine",
      ...overrides,
    },
  };
}

function resolver(fetchImpl: typeof fetch, now: () => number = () => 1_000) {
  return new LeafStandardServicesResolver({
    appOrigin: "https://app.example",
    brokerEndpoint: "https://staging-api.leafdesign.ai",
    dispatchSecret: "dispatch-secret-value",
    environment: "staging",
    approvalStore,
    fetchImpl,
    now,
  });
}

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

describe("LeafStandardServicesResolver", () => {
  it("exchanges exact spine authority with redirects disabled", async () => {
    const fetchImpl = vi.fn(async (_url: Parameters<typeof fetch>[0], init?: RequestInit) => {
      expect(String(_url)).toBe("https://app.example/internal/mcp/gateway/attachment");
      expect(init?.redirect).toBe("error");
      expect(init?.headers).toMatchObject({
        "x-tenant-id": "tenant-a",
        "x-dispatch-secret": "dispatch-secret-value",
      });
      expect(JSON.parse(String(init?.body))).toEqual({
        session_id: context.session_id,
        authority_session_id: context.authority_session_id,
        authority_turn_id: context.authority_turn_id,
        subscription_mount_id: context.subscription_mount_id,
        runner_profile_id: "spine",
      });
      return jsonResponse(attachment());
    }) as typeof fetch;

    const result = await resolver(fetchImpl).resolve(context, "spine");

    expect(result.identity).toEqual(attachment().identity);
    expect(result.environment).toBe("staging");
    expect(Object.keys(result.provider)).not.toContain("browser_session");
  });

  it("binds author to the author profile and rejects cross-profile substitution", async () => {
    const authorFetch = vi.fn(async () => jsonResponse(attachment({ runner_profile_id: "author" }))) as typeof fetch;
    await expect(resolver(authorFetch).resolve(context, "author")).resolves.toMatchObject({
      identity: { runner_profile_id: "author" },
    });

    const swapped = vi.fn(async () => jsonResponse(attachment({ runner_profile_id: "author" }))) as typeof fetch;
    await expect(resolver(swapped).resolve(context, "spine"))
      .rejects.toThrow("identity_mismatch:runner_profile_id");
  });

  it.each([
    ["tenant_id", "tenant-b"],
    ["session_id", "session-b"],
    ["authority_turn_id", "turn-b"],
    ["subscription_mount_id", "account-b"],
  ])("rejects a response that swaps %s", async (field, value) => {
    const fetchImpl = vi.fn(async () => jsonResponse(attachment({ [field]: value }))) as typeof fetch;
    await expect(resolver(fetchImpl).resolve(context, "spine"))
      .rejects.toThrow(`identity_mismatch:${field}`);
  });

  it("rejects expired credentials and oversized responses", async () => {
    const expired = vi.fn(async () => jsonResponse({
      ...attachment(),
      expires_at: "1970-01-01T00:00:00.000Z",
    })) as typeof fetch;
    await expect(resolver(expired).resolve(context, "spine"))
      .rejects.toThrow("standard_services_attachment_expired");

    const oversized = vi.fn(async () => new Response("x", {
      status: 200,
      headers: { "content-length": String(64 * 1024 + 1) },
    })) as typeof fetch;
    await expect(resolver(oversized).resolve(context, "spine"))
      .rejects.toThrow("response_too_large");
  });

  it.each([
    "2099-01-01T00:00:00Z",
    "2099-01-01T00:00:00.000+00:00",
    "2099-01-01 00:00:00.000Z",
    "2099-02-30T00:00:00.000Z",
    "2099-01-01T00:00:00.000Zextra",
  ])("rejects a non-canonical attachment expiry %s", async (expires_at) => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ...attachment(), expires_at })) as typeof fetch;
    await expect(resolver(fetchImpl).resolve(context, "spine"))
      .rejects.toThrow("exchange_expiry_invalid");
  });

  it("fails closed on redirects, missing config, and unsafe endpoints", async () => {
    const redirect = vi.fn(async () => {
      throw new TypeError("redirect mode is set to error");
    }) as typeof fetch;
    await expect(resolver(redirect).resolve(context, "spine"))
      .rejects.toThrow("standard_services_exchange_unavailable");

    // Intent is LEAF_TENANT_MCP_BROKER_URL, matching serve.ts's approval-store
    // predicate. LEAF_APP_URL (+ dispatch secret) alone is the converse
    // back-edge pair scripts/start-leaf.py has always exported — reading it as
    // standard-services intent broke every local managed proof, so locally it
    // resolves to "not configured", never a throw.
    expect(standardServicesResolverFromEnv({
      LEAF_APP_URL: "https://app.example",
    })).toBeUndefined();
    expect(standardServicesResolverFromEnv({
      LEAF_APP_URL: "http://127.0.0.1:8230",
      LEAF_APP_DISPATCH_SECRET: "converse-back-edge-secret",
    })).toBeUndefined();
    // The broker URL set with the rest missing IS a partial config: hard error.
    expect(() => standardServicesResolverFromEnv({
      LEAF_TENANT_MCP_BROKER_URL: "https://staging-api.leafdesign.ai",
    })).toThrow("standard services require");
    expect(() => standardServicesResolverFromEnv({
      LEAF_RUNTIME_ENV: "production",
    })).toThrow("required in staging and production");
    // Deployed environments stay fail-closed no matter which pair is present.
    expect(() => standardServicesResolverFromEnv({
      LEAF_APP_URL: "https://app.example",
      LEAF_APP_DISPATCH_SECRET: "converse-back-edge-secret",
      LEAF_RUNTIME_ENV: "production",
    })).toThrow("required in staging and production");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "https://app.example/path",
      brokerEndpoint: "https://api.leafdesign.ai",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
      approvalStore,
    })).toThrow("LEAF_APP_URL");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "https://app.example",
      brokerEndpoint: "http://api.leafdesign.ai",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
      approvalStore,
    })).toThrow("canonical tenant broker origin");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "http://app.example",
      brokerEndpoint: "https://api.leafdesign.ai",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
      approvalStore,
    })).toThrow("LEAF_APP_URL must use HTTPS");
  });

  it.each([
    ["staging", "https://exfil.example"],
    ["staging", "https://staging-api.leafdesign.ai/other"],
    ["staging", "https://staging-api.leafdesign.ai?next=https://exfil.example"],
    ["staging", "https://user:pass@staging-api.leafdesign.ai"],
    ["production", "https://staging-api.leafdesign.ai"],
    ["production", "https://api.leafdesign.ai/mcp"],
    ["local", "https://exfil.example"],
  ] as const)("rejects an unpinned %s broker destination %s", (environment, brokerEndpoint) => {
    expect(() => new LeafStandardServicesResolver({
      appOrigin: environment === "local" ? "http://127.0.0.1:3000" : "https://app.example",
      brokerEndpoint,
      dispatchSecret: "dispatch-secret-value",
      environment,
      approvalStore,
    })).toThrow("LEAF_TENANT_MCP_BROKER_URL");
  });

  it("accepts only canonical deployed origins and explicit local loopback", () => {
    const staging = standardServicesResolverFromEnv({
      LEAF_APP_URL: "https://app.example",
      LEAF_TENANT_MCP_BROKER_URL: "https://staging-api.leafdesign.ai/",
      LEAF_APP_DISPATCH_SECRET: "dispatch-secret-value",
      LEAF_RUNTIME_ENV: "production",
      LEAF_STANDARD_SERVICES_ENV: "staging",
    }, undefined, approvalStore);
    expect((staging as unknown as { brokerEndpoint: URL }).brokerEndpoint.toString())
      .toBe("https://staging-api.leafdesign.ai/mcp");
    const local = standardServicesResolverFromEnv({
      LEAF_APP_URL: "http://127.0.0.1:3000",
      LEAF_TENANT_MCP_BROKER_URL: "http://127.0.0.1:18900",
      LEAF_APP_DISPATCH_SECRET: "dispatch-secret-value",
      LEAF_RUNTIME_ENV: "local",
    }, undefined, approvalStore);
    expect((local as unknown as { brokerEndpoint: URL }).brokerEndpoint.toString())
      .toBe("http://127.0.0.1:18900/mcp");
  });

  it.each(["staging", "production"] as const)(
    "requires the service tuple for explicit %s standard services",
    (environment) => {
      expect(() => standardServicesResolverFromEnv({
        LEAF_RUNTIME_ENV: "production",
        LEAF_STANDARD_SERVICES_ENV: environment,
      })).toThrow("standard services are required in staging and production");
    },
  );

  it.each(["", "preview"])(
    "rejects the explicit invalid standard services environment %j",
    (environment) => {
      expect(() => standardServicesResolverFromEnv({
        LEAF_RUNTIME_ENV: "development",
        LEAF_STANDARD_SERVICES_ENV: environment,
      })).toThrow("LEAF_RUNTIME_ENV must be local, staging, or production");
    },
  );

  it.each([
    ["local", "production"],
    ["staging", "production"],
    ["production", "local"],
  ] as const)(
    "rejects the incompatible runtime %s and standard-services %s pair",
    (runtime, standardServices) => {
      expect(() => standardServicesResolverFromEnv({
        LEAF_APP_URL: "https://app.example",
        LEAF_TENANT_MCP_BROKER_URL: "https://api.leafdesign.ai",
        LEAF_APP_DISPATCH_SECRET: "dispatch-secret-value",
        LEAF_RUNTIME_ENV: runtime,
        LEAF_STANDARD_SERVICES_ENV: standardServices,
      }, undefined, approvalStore)).toThrow(
        "LEAF_STANDARD_SERVICES_ENV is incompatible with LEAF_RUNTIME_ENV",
      );
    },
  );

  it.each([
    ["staging", "local"],
    ["production", "local"],
  ] as const)(
    "rejects the zero-tuple runtime %s and standard-services %s pair",
    (runtime, standardServices) => {
      expect(() => standardServicesResolverFromEnv({
        LEAF_RUNTIME_ENV: runtime,
        LEAF_STANDARD_SERVICES_ENV: standardServices,
      })).toThrow(
        "LEAF_STANDARD_SERVICES_ENV is incompatible with LEAF_RUNTIME_ENV",
      );
    },
  );
});

describe("author authority and grant separation", () => {
  it("uses lease account as mount while keeping the raw model grant separate", async () => {
    const oauth: OAuthGrantProvider = {
      async getGrant() { return { kind: "api_key", apiKey: "model-secret" }; },
      async acquireGrant() {
        return {
          grant: { kind: "api_key", apiKey: "model-secret" },
          account_id: "account-a",
          lease_id: "lease-a",
        };
      },
    };
    let observed: AgentRunInput | undefined;
    const inner: AgentRunner = {
      async run(input) {
        observed = input;
        return { tool: {} as never, code: "", preview: "", files: [] };
      },
    };
    const mountedOauth = new StandardServicesOAuthGrantProvider(oauth);
    const runner = new AuthorStandardServicesRunner(inner, true);

    await withAuthorStandardServicesAuthority({
      tenant_id: "tenant-a",
      session_id: "session-a",
      authority_session_id: "session-a",
      authority_turn_id: "turn-a",
    }, async () => {
      await mountedOauth.acquireGrant("tenant-a");
      await runner.run({ grant: { kind: "api_key", apiKey: "different-raw-secret" } } as AgentRunInput);
    });

    expect(observed?.grant).toEqual({ kind: "api_key", apiKey: "different-raw-secret" });
    expect(observed?.standardServicesContext).toEqual(context);
  });

  it("rejects missing authority and cross-tenant lease binding", async () => {
    const oauth = new StandardServicesOAuthGrantProvider({
      async getGrant() { return { kind: "api_key", apiKey: "model-secret" }; },
      async acquireGrant() {
        return {
          grant: { kind: "api_key", apiKey: "model-secret" },
          account_id: "account-a",
          lease_id: "lease-a",
        };
      },
    });
    const runner = new AuthorStandardServicesRunner({
      async run() { return { tool: {} as never, code: "", preview: "", files: [] }; },
    }, true);
    expect(() => runner.run({} as AgentRunInput)).toThrow("authority_missing");
    await expect(withAuthorStandardServicesAuthority({
      tenant_id: "tenant-a",
      session_id: "session-a",
      authority_session_id: "session-a",
      authority_turn_id: "turn-a",
    }, () => oauth.acquireGrant("tenant-b"))).rejects.toThrow("tenant_mismatch");
  });
});

describe("ordinary mounted-user surface", () => {
  it.each(["author", "spine"] as const)("mounts only the broker facade for %s", (profile) => {
    const privateName = profile;
    const allowed = STANDARD_SERVICES_FACADE_TOOLS.map((name) => `mcp__services__${name}`);
    const composition = composeRunnerCapabilities({
      profile,
      private_mcp_servers: { [privateName]: { private: true } },
      private_allowed_tools: [`mcp__${privateName}__private_tool`],
      services: { server: { facade: true }, allowed_tools: allowed },
    });

    expect(Object.keys(composition.mcpServers)).toEqual([privateName, "services"]);
    expect(composition.allowedTools).toEqual([
      `mcp__${privateName}__private_tool`,
      ...allowed,
    ]);
    expect(JSON.stringify(composition)).not.toMatch(/browser_session|operator|cadwalk|vm_ssh/);
  });
});
