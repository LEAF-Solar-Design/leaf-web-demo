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
    brokerEndpoint: "https://broker.example/mcp",
    dispatchSecret: "dispatch-secret-value",
    environment: "staging",
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

  it("fails closed on redirects, missing config, and unsafe endpoints", async () => {
    const redirect = vi.fn(async () => {
      throw new TypeError("redirect mode is set to error");
    }) as typeof fetch;
    await expect(resolver(redirect).resolve(context, "spine"))
      .rejects.toThrow("standard_services_exchange_unavailable");

    expect(() => standardServicesResolverFromEnv({
      LEAF_APP_URL: "https://app.example",
    })).toThrow("standard services require");
    expect(() => standardServicesResolverFromEnv({
      LEAF_RUNTIME_ENV: "production",
    })).toThrow("required in staging and production");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "https://app.example/path",
      brokerEndpoint: "https://broker.example/mcp",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
    })).toThrow("LEAF_APP_URL");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "https://app.example",
      brokerEndpoint: "http://operator.example/mcp",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
    })).toThrow("must use HTTPS");
    expect(() => new LeafStandardServicesResolver({
      appOrigin: "http://app.example",
      brokerEndpoint: "https://broker.example/mcp",
      dispatchSecret: "dispatch-secret-value",
      environment: "production",
    })).toThrow("LEAF_APP_URL must use HTTPS");
  });
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
