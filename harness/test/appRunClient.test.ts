import { describe, expect, it } from "vitest";

import {
  AppRunClientError,
  HttpAppRunClient,
} from "../src/ports/impl/appRunClient.js";


describe("HttpAppRunClient authoring errors", () => {
  it("preserves the requested mode and the server reason code", async () => {
    let requestBody: Record<string, unknown> | undefined;
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async (_input, init) => {
        requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return new Response(JSON.stringify({
          error: {
            error_code: "BAD_PARAMS",
            message: "Tool authoring is not enabled for this workspace in this environment.",
          },
          reason_code: "customization_stage_disabled",
        }), {
          status: 404,
          headers: { "content-type": "application/json" },
        });
      },
    });

    let caught: unknown;
    try {
      await client.authorTool("tenant-a", "make a tool", "one_off", "request-a");
    } catch (error) {
      caught = error;
    }

    expect(requestBody).toEqual({ description: "make a tool", mode: "one_off" });
    expect(caught).toBeInstanceOf(AppRunClientError);
    expect(caught).toMatchObject({
      message: "Tool authoring is not enabled for this workspace in this environment.",
      status: 404,
      errorCode: "customization_stage_disabled",
    });
  });
});

describe("HttpAppRunClient authoring identity", () => {
  function clientCapturingHeaders(sink: { headers?: Headers }) {
    return new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async (_input, init) => {
        sink.headers = new Headers(init?.headers ?? {});
        return new Response(JSON.stringify({ change_set_id: "cs-1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
  }

  it("names the app session and turn that authenticated the call", async () => {
    // The back edge authenticates as a tenant and cannot assert a user. It
    // sends the app-owned authority tuple so the app can resolve the author
    // from its OWN record of who opened that turn.
    const sink: { headers?: Headers } = {};
    await clientCapturingHeaders(sink).authorTool(
      "tenant-a", "make a tool", "build", "request-a",
      { sessionId: "app-session-1", turnId: "app-turn-1" },
    );

    expect(sink.headers?.get("x-authority-session-id")).toBe("app-session-1");
    expect(sink.headers?.get("x-authority-turn-id")).toBe("app-turn-1");
    // It still never asserts an identity of its own.
    expect(sink.headers?.get("x-author-subject")).toBeNull();
    expect(sink.headers?.get("x-tenant-id")).toBe("tenant-a");
  });

  it("omits the authority headers when it has no tuple", async () => {
    const sink: { headers?: Headers } = {};
    await clientCapturingHeaders(sink).authorTool(
      "tenant-a", "make a tool", "build", "request-a",
    );

    expect(sink.headers?.has("x-authority-session-id")).toBe(false);
    expect(sink.headers?.has("x-authority-turn-id")).toBe(false);
  });
});
