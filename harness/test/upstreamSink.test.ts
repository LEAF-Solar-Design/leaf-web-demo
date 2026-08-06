/**
 * Wiring contract for the vendored upstream-capture sink.
 *
 * The deep behavior (dedupe key, timeout, secret scrubbing) is tested
 * upstream in mushy-code; tests are not vendored. What THIS repo owns is the
 * wiring: the strangler-shim path resolves after a vendor sync, the sink
 * stays unwired without its env pair (authoring identical), and a dead queue
 * can never reject into the authoring path (fire-and-forget).
 */
import { describe, expect, it } from "vitest";

import type { UpstreamCapture } from "../src/ports/index.js";
import {
  HttpUpstreamSink,
  upstreamSinkFromEnv,
} from "../src/ports/impl/httpUpstreamSink.js";

function event(): UpstreamCapture {
  return {
    contract: "mushy.upstream-capture.v1",
    consumer: "tenant-a",
    platform: null,
    route: "stage",
    prompt: "add a north-arrow tool",
    authoring_status: "authored",
    captured_at: new Date().toISOString(),
  };
}

describe("upstream sink wiring", () => {
  it("stays unwired without the URL+token pair", () => {
    expect(upstreamSinkFromEnv({})).toBeUndefined();
    expect(
      upstreamSinkFromEnv({ UPSTREAM_SINK_URL: "https://queue.example/i" }),
    ).toBeUndefined();
    expect(upstreamSinkFromEnv({ UPSTREAM_SINK_TOKEN: "t" })).toBeUndefined();
  });

  it("constructs the sink from the env triple", () => {
    const sink = upstreamSinkFromEnv({
      UPSTREAM_SINK_URL: "https://queue.example/api/upstream/ingest",
      UPSTREAM_SINK_TOKEN: "queue-key",
      UPSTREAM_SINK_PLATFORM: "leaf-web-demo",
    });
    expect(sink).toBeInstanceOf(HttpUpstreamSink);
  });

  it("resolves capture when the queue is down (fire-and-forget)", async () => {
    const sink = new HttpUpstreamSink({
      url: "https://queue.example/api/upstream/ingest",
      token: "queue-key",
      fetchImpl: () => Promise.reject(new Error("queue down")),
    });
    await expect(sink.capture(event())).resolves.toBeUndefined();
  });

  it("sends the bearer token and platform label when the queue is up", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const sink = new HttpUpstreamSink({
      url: "https://queue.example/api/upstream/ingest",
      token: "queue-key",
      platform: "leaf-web-demo",
      fetchImpl: ((url: string, init: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve(new Response("{}", { status: 200 }));
      }) as unknown as typeof fetch,
    });
    await sink.capture(event());
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://queue.example/api/upstream/ingest");
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer queue-key");
    const body = JSON.parse(String(calls[0].init.body)) as UpstreamCapture;
    expect(body.platform).toBe("leaf-web-demo");
    expect(body.dedupe_key).toBeTruthy();
  });
});
