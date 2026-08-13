import { describe, expect, it, vi } from "vitest";

import { createStandardServicesFacade } from "../src/vendor/mushy-author/index.js";
// Imported from its own module rather than the package barrel: the vendored
// upstream stopped re-exporting this fake from index.ts, while still shipping
// the module itself. Same class, same behaviour, one hop closer.
import { FakeStandardServiceProvider } from "../src/vendor/mushy-author/ports/fakes/fakeStandardServiceProvider.js";

type Handler = (args: Record<string, unknown>) => Promise<{
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
}>;

function facadeHandlers(provider: FakeStandardServiceProvider): Map<string, Handler> {
  const handlers = new Map<string, Handler>();
  const sdk = {
    tool(name: string, _description: string, _schema: Record<string, unknown>, handler: Handler) {
      handlers.set(name, handler);
      return { name };
    },
    createSdkMcpServer(options: { name: string; tools: unknown[] }) {
      return options;
    },
  };
  const scalar = () => ({});
  const z = {
    string: scalar,
    enum: (_values: readonly string[]) => ({}),
    record: (_inner: unknown) => ({}),
    unknown: scalar,
  };
  createStandardServicesFacade({
    sdk,
    z,
    provider,
    identity: {
      tenant_id: "tenant-a",
      subject_id: "human-a",
      session_id: "session-a",
      authority_turn_id: "turn-a",
      subscription_mount_id: "mount-a",
      runner_profile_id: "spine",
    },
    environment: "staging",
  });
  return handlers;
}

function catalogHandler(provider: FakeStandardServiceProvider): Handler {
  return facadeHandlers(provider).get("services_catalog")!;
}

describe("standard services public errors", () => {
  it("returns only exact finite public provider codes", async () => {
    const provider = new FakeStandardServiceProvider();
    const handler = catalogHandler(provider);
    const secret = "provider-secret-0123456789abcdef";

    provider.catalog = async () => {
      throw new Error(`standard_service_tool_not_permitted:${secret}`);
    };
    const suffixed = await handler({ query: "" });
    expect(suffixed.content[0]!.text).toBe("standard_service_provider_failure");
    expect(JSON.stringify(suffixed)).not.toContain(secret);

    provider.catalog = async () => {
      throw { reason: secret };
    };
    const nonError = await handler({ query: "" });
    expect(nonError.content[0]!.text).toBe("standard_service_provider_failure");
    expect(JSON.stringify(nonError)).not.toContain(secret);

    provider.catalog = async () => {
      throw new Error("standard_service_tool_not_permitted");
    };
    expect(await handler({ query: "" })).toEqual({
      content: [{ type: "text", text: "standard_service_tool_not_permitted" }],
      isError: true,
    });
  });

  it("does not inspect or expose hostile provider error messages", async () => {
    const provider = new FakeStandardServiceProvider();
    const handler = catalogHandler(provider);
    const secret = "hostile-message-getter-0123456789abcdef";
    const hostile = new Error("placeholder");
    Object.defineProperty(hostile, "message", {
      get() {
        throw new Error(secret);
      },
    });
    provider.catalog = async () => {
      throw hostile;
    };

    const response = await handler({ query: "" });
    expect(response).toEqual({
      content: [{ type: "text", text: "standard_service_provider_failure" }],
      isError: true,
    });
    expect(JSON.stringify(response)).not.toContain(secret);
  });

  it("keeps model confirmation pending without invoking the human-only provider method", async () => {
    const provider = new FakeStandardServiceProvider();
    const confirm = vi.spyOn(provider, "confirm");
    const handler = facadeHandlers(provider).get("services_confirm")!;

    const response = await handler({ approval_id: "approval_12345678" });

    expect(response).toEqual({
      content: [{ type: "text", text: "standard_service_approval_pending_human" }],
      isError: true,
    });
    expect(confirm).not.toHaveBeenCalled();
  });
});
