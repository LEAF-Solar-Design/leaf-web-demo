import { describe, expect, it, vi } from "vitest";

import {
  AppRunClientError,
  HttpAppRunClient,
  ProjectPhotoClientError,
} from "../src/ports/impl/appRunClient.js";

describe("bounded current-project photo callback", () => {
  const org = "11111111-1111-4111-8111-111111111111";
  const authority = { sessionId: "app-session", turnId: "app-turn" };
  const body = { op: "catalog" as const };
  const client = (fetchImpl: typeof fetch) => new HttpAppRunClient({
    baseUrl: "https://app.invalid", dispatchSecret: "private-dispatch-value", fetchImpl,
  });
  it("uses only the fixed callback and existing authority headers", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("{}", { headers: { "content-type": "application/json" } }));
    expect(await client(fetcher).inspectProjectPhoto(org, authority.sessionId, body, authority)).toEqual({});
    expect(fetcher).toHaveBeenCalledWith("https://app.invalid/api/sessions/app-session/materials/inspect", expect.objectContaining({
      method: "POST", redirect: "error", body: JSON.stringify(body), headers: {
        "content-type": "application/json", "x-tenant-id": org, "x-dispatch-secret": "private-dispatch-value",
        "x-authority-session-id": "app-session", "x-authority-turn-id": "app-turn",
      },
    }));
  });
  it.each(["declared", "actual", "origin", "redirect", "redirected", "status", "mime", "utf8", "json", "secret"])(
    "closes the %s response boundary with a fixed error", async kind => {
      const bytes = kind === "actual" ? new Uint8Array(1536 * 1024 + 1) : kind === "utf8" ? new Uint8Array([255])
        : new TextEncoder().encode(kind === "json" ? "{" : kind === "secret" ? '{"error":"private-dispatch-value"}' : "{}");
      const response = new Response(bytes, { status: kind === "status" ? 503 : kind === "redirect" ? 302 : 200,
        headers: { "content-type": kind === "mime" ? "image/jpeg" : "application/json",
          ...(kind === "declared" ? { "content-length": String(1536 * 1024 + 1) } : {}) } });
      if (kind === "origin") Object.defineProperty(response, "url", { value: "https://foreign.invalid/inspect" });
      if (kind === "redirected") Object.defineProperty(response, "redirected", { value: true });
      await expect(client(async () => response).inspectProjectPhoto(org, authority.sessionId, body, authority))
        .rejects.toMatchObject({ message: "Project photo inspection unavailable.", status: 502,
          photoFailureClass: kind === "status" ? "transport" : "integrity" });
      expect(response.body?.locked).toBe(false);
    },
  );
  it.each([
    [401, "authorization"], [403, "authorization"], [404, "unavailable"], [409, "source_changed"],
    [408, "transport"], [429, "transport"], [503, "transport"], [504, "transport"],
    [413, "integrity"], [422, "integrity"], [500, "integrity"], [502, "integrity"],
  ])("preserves a typed failure for HTTP %s while retaining status 502", async (status, kind) => {
    const response = new Response("PRIVATE backend detail", { status: Number(status) });
    const error = await client(async () => response).inspectProjectPhoto(org, authority.sessionId, body, authority).catch(error => error);
    expect(error).toBeInstanceOf(AppRunClientError);
    expect(error).toBeInstanceOf(ProjectPhotoClientError);
    expect(error).toMatchObject({ message: "Project photo inspection unavailable.", status: 502, photoFailureClass: kind });
    expect(JSON.stringify(error)).not.toContain("PRIVATE");
    expect(error.cause).toBeUndefined();
    expect(response.body?.locked).toBe(false);
  });
  it.each([false, true])("classifies recognized network failures, including fetch causes (nested=%s)", async nested => {
    const network = Object.assign(new Error("PRIVATE socket detail"), { code: "ECONNRESET" });
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(nested ? new TypeError("fetch failed", { cause: network }) : network);
    const error = await client(fetcher).inspectProjectPhoto(org, authority.sessionId, body, authority).catch(error => error);
    expect(error).toMatchObject({ status: 502, photoFailureClass: "transport" });
    expect(error.cause).toBeUndefined();
    expect(error.message).toBe("Project photo inspection unavailable.");
    expect(JSON.stringify(error)).not.toContain("PRIVATE");
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it.each([
    new TypeError("fetch failed", { cause: new Error("unexpected redirect") }),
    Object.assign(new Error("PRIVATE opaque failure"), { status: 503 }),
  ])("keeps opaque fetch failures and rejected redirects non-retryable (%#)", async failure => {
    await expect(client(async () => { throw failure; }).inspectProjectPhoto(org, authority.sessionId, body, authority))
      .rejects.toMatchObject({ status: 502, photoFailureClass: "integrity" });
  });
  it.each([["ECONNRESET", "transport"], ["Z_DATA_ERROR", "integrity"], ["", "integrity"]])(
    "distinguishes stream network failures from corrupt or opaque streams (%s)", async (code, kind) => {
      const response = new Response(new ReadableStream<Uint8Array>({
        start(controller) { controller.error(new TypeError("PRIVATE stream failure", {
          cause: Object.assign(new Error("PRIVATE cause"), { code }),
        })); },
      }), { headers: { "content-type": "application/json" } });
      const error = await client(async () => response).inspectProjectPhoto(org, authority.sessionId, body, authority).catch(error => error);
      expect(error).toMatchObject({ status: 502, photoFailureClass: kind });
      expect(error.cause).toBeUndefined();
      expect(response.body?.locked).toBe(false);
    },
  );
  it("rejects malformed bodies and mismatched authority before any fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(client(fetcher).inspectProjectPhoto(org, "other", body, authority))
      .rejects.toMatchObject({ status: 502, photoFailureClass: "authorization" });
    await expect(client(fetcher).inspectProjectPhoto(org, authority.sessionId,
      { ...body, project_id: "foreign" } as typeof body, authority)).rejects.toMatchObject({ status: 502, photoFailureClass: "integrity" });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("cancels stalled response streams at the total 45-second deadline", async () => {
    vi.useFakeTimers();
    let cancelled = false;
    let signal: AbortSignal | undefined;
    const response = new Response(new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } }),
      { headers: { "content-type": "application/json" } });
    try {
      const pending = client(async (_url, init) => { signal = init?.signal as AbortSignal; return response; })
        .inspectProjectPhoto(org, authority.sessionId, body, authority);
      const rejected = expect(pending).rejects.toMatchObject({ status: 502, photoFailureClass: "transport" });
      await vi.advanceTimersByTimeAsync(45000);
      await rejected;
      expect(signal?.aborted).toBe(true);
      expect(cancelled).toBe(true);
      expect(response.body?.locked).toBe(false);
    } finally { vi.useRealTimers(); }
  });
});

describe("bounded project portfolio proposal reader", () => {
  const body = { instruction: "Shorten", expected_revision: 7, expected_digest: "a".repeat(64) };
  const authority = { sessionId: "app-session", turnId: "app-turn" };
  const client = (fetchImpl: typeof fetch) => new HttpAppRunClient({
    baseUrl: "https://app.invalid", dispatchSecret: "private-dispatch-value", fetchImpl,
  });

  it("uses the fixed session route, authority headers, and a bounded JSON stream", async () => {
    const response = new Response('{"source":{}}', {
      headers: { "content-type": "application/json; charset=utf-8", "content-length": "13" },
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response);
    expect(await client(fetcher).proposeProjectPage("tenant", "app-session", body, authority)).toEqual({ source: {} });
    expect(response.body?.locked).toBe(false);
    expect(fetcher).toHaveBeenCalledWith("https://app.invalid/api/sessions/app-session/page/proposals", expect.objectContaining({
      method: "POST", redirect: "error", body: JSON.stringify(body), headers: {
        "content-type": "application/json", "x-tenant-id": "tenant",
        "x-dispatch-secret": "private-dispatch-value",
        "x-authority-session-id": "app-session", "x-authority-turn-id": "app-turn",
      },
    }));
  });

  it.each(["declared", "actual", "redirect", "redirected", "origin", "status", "mime", "utf8", "json", "secret"])(
    "rejects %s responses with fixed errors and disposes the reader", async failure => {
      let cancelled = false;
      const bytes = failure === "actual" ? new Uint8Array(1024 * 1024 + 1)
        : failure === "utf8" ? new Uint8Array([0xff])
        : new TextEncoder().encode(failure === "json" ? "{" : failure === "secret"
          ? '{"text":"private-dispatch-value"}' : "{}");
      const stream = new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(bytes); },
        cancel() { cancelled = true; },
      });
      const response = new Response(stream, { status: failure === "status" ? 201 : failure === "redirect" ? 302 : 200,
        headers: { "content-type": failure === "mime" ? "text/plain" : "application/json",
          ...(failure === "declared" ? { "content-length": String(1024 * 1024 + 1) } : {}) } });
      if (failure === "redirected") Object.defineProperty(response, "redirected", { value: true });
      if (failure === "origin") Object.defineProperty(response, "url", { value: "https://foreign.invalid/page" });
      // Valid small streams need an end to reach decode/parse checks.
      if (["utf8", "json", "secret"].includes(failure)) {
        const finite = new Response(bytes, { headers: { "content-type": "application/json" } });
        await expect(client(async () => finite).proposeProjectPage("tenant", "app-session", body, authority))
          .rejects.toMatchObject({ message: "Portfolio proposal request failed." });
        expect(finite.body?.locked).toBe(false);
        await response.body?.cancel();
      } else {
        await expect(client(async () => response).proposeProjectPage("tenant", "app-session", body, authority))
          .rejects.toMatchObject({ message: "Portfolio proposal request failed." });
        expect(cancelled).toBe(true);
        expect(response.body?.locked).toBe(false);
      }
    },
  );

  it("enforces a 45-second total deadline during a stalled body and disposes it", async () => {
    vi.useFakeTimers();
    try {
      let cancelled = false;
      const response = new Response(new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } }), {
        headers: { "content-type": "application/json" },
      });
      const pending = client(async () => response).proposeProjectPage("tenant", "app-session", body, authority);
      const assertion = expect(pending).rejects.toMatchObject({ message: "Portfolio proposal request failed." });
      await vi.advanceTimersByTimeAsync(45_000);
      await assertion;
      expect(cancelled).toBe(true);
      expect(response.body?.locked).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it("cancels a late response from a fetch that ignores abort", async () => {
    vi.useFakeTimers();
    try {
      let resolveFetch!: (response: Response) => void;
      const fetcher: typeof fetch = () => new Promise(resolve => { resolveFetch = resolve; });
      const pending = client(fetcher).proposeProjectPage("tenant", "app-session", body, authority);
      const assertion = expect(pending).rejects.toBeInstanceOf(AppRunClientError);
      await vi.advanceTimersByTimeAsync(45_000);
      await assertion;
      let cancelled = false;
      resolveFetch(new Response(new ReadableStream({ cancel() { cancelled = true; } })));
      await Promise.resolve();
      await Promise.resolve();
      expect(cancelled).toBe(true);
    } finally { vi.useRealTimers(); }
  });

  it("rejects caller-selected authority and extra fields before fetching", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(client(fetcher).proposeProjectPage("tenant", "foreign", body, authority)).rejects.toBeInstanceOf(AppRunClientError);
    await expect(client(fetcher).proposeProjectPage("tenant", "app-session", { ...body, subject: "other" } as typeof body, authority))
      .rejects.toBeInstanceOf(AppRunClientError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});


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

describe("HttpAppRunClient overlay identity", () => {
  it.each([true, false])("preserves the target and sends only optional turn authority (%s)", async (withAuthority) => {
    let headers: Headers | undefined;
    let body: unknown;
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async (_input, init) => {
        headers = new Headers(init?.headers ?? {});
        body = JSON.parse(String(init?.body));
        return new Response(JSON.stringify({ proposal_id: "p-1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
    await client.proposeOverlay(
      "tenant-a", "target-session", { "color.border": "#123456" }, "preview",
      withAuthority ? { sessionId: "app-session", turnId: "app-turn" } : undefined,
    );
    expect(body).toEqual({
      session_id: "target-session", tokens: { "color.border": "#123456" }, request_text: "preview",
    });
    expect(headers?.get("x-authority-session-id")).toBe(withAuthority ? "app-session" : null);
    expect(headers?.get("x-authority-turn-id")).toBe(withAuthority ? "app-turn" : null);
    expect(headers?.get("x-tenant-id")).toBe("tenant-a");
    expect(headers?.get("x-dispatch-secret")).toBe("test-secret");
    for (const name of ["x-author-subject", "x-subject", "x-role", "x-tier", "x-project-id"]) {
      expect(headers?.has(name)).toBe(false);
    }
  });
});

describe("HttpAppRunClient run identity", () => {
  it("sends only the app-owned authority tuple as run headers", async () => {
    let headers = new Headers();
    let body: Record<string, unknown> = {};
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async (_input, init) => {
        headers = new Headers(init?.headers ?? {});
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return new Response(JSON.stringify({ job_id: "job-1", status: "submitted" }), {
          status: 202,
          headers: { "content-type": "application/json" },
        });
      },
    });

    await client.submitRun({
      tenantId: "tenant-a",
      authoritySessionId: "app-session-1",
      authorityTurnId: "app-turn-1",
      tool: "drape-onto-spheres",
      params: {},
      dwg: "drawing-a",
      catalogDigest: "digest-1",
    });

    expect(headers.get("x-authority-session-id")).toBe("app-session-1");
    expect(headers.get("x-authority-turn-id")).toBe("app-turn-1");
    expect(headers.get("x-author-subject")).toBeNull();
    expect(body).not.toHaveProperty("tier");
    expect(body).not.toHaveProperty("subject");
    expect(body).not.toHaveProperty("authoritySessionId");
    expect(body).not.toHaveProperty("authorityTurnId");
  });

  it.each([
    [200, { job_id: "job-1", status: "submitted" }, "was not accepted"],
    [202, { status: "submitted" }, "omitted job identity"],
    [202, { job_id: "   ", status: "submitted" }, "omitted job identity"],
  ])("refuses a run before acknowledgement for status %s and body %j", async (
    status,
    responseBody,
    expected,
  ) => {
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async () => new Response(JSON.stringify(responseBody), {
        status,
        headers: { "content-type": "application/json" },
      }),
    });

    await expect(client.submitRun({
      tenantId: "tenant-a",
      tool: "count-by-layer",
      params: {},
      dwg: "drawing-a",
      catalogDigest: "digest-1",
      wait: false,
    })).rejects.toThrow(expected);
  });

  it("returns the validated 202 job identity without polling when wait is false", async () => {
    let calls = 0;
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid",
      dispatchSecret: "test-secret",
      fetchImpl: async () => {
        calls += 1;
        return new Response(JSON.stringify({ job_id: "job-ack", status: "submitted" }), {
          status: 202,
          headers: { "content-type": "application/json" },
        });
      },
    });

    await expect(client.submitRun({
      tenantId: "tenant-a",
      tool: "count-by-layer",
      params: {},
      dwg: "drawing-a",
      catalogDigest: "digest-1",
      wait: false,
    })).resolves.toEqual({ job_id: "job-ack", status: "submitted" });
    expect(calls).toBe(1);
  });
});
