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
