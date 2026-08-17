/**
 * Wiring contract for the vendored upstream-capture sink.
 *
 * The deep behavior (single serialization, credential guards, dedupe key)
 * belongs to mushy-code and is tested there; its tests are not vendored. What
 * THIS repo owns is the wiring: the strangler-shim path resolves after a
 * vendor sync, the sink stays unwired without its env pair so authoring is
 * unchanged, and a dead queue can never reject into the authoring path.
 *
 * NOTE the contract: capture() takes ALREADY-SERIALIZED BYTES, not an object.
 * That is a security property, not a style choice — the caller serializes
 * once and scans the parsed form of those exact bytes, so a re-serializing
 * consumer would reopen the stateful-toJSON hole the upstream fix closed.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { UpstreamCapture } from "../src/ports/index.js";
import {
  HttpUpstreamSink,
  upstreamSinkFromEnv,
} from "../src/ports/impl/httpUpstreamSink.js";

function event(): UpstreamCapture {
  return {
    contract: "mushy.upstream-capture.v1",
    invocation_id: "inv-1",
    consumer: "tenant-a",
    platform: "leaf-web-demo",
    route: "stage",
    prompt: "add a north-arrow tool",
    authoring_status: "authored",
    captured_at: new Date().toISOString(),
  };
}

describe("upstream sink wiring", () => {
  it("stays unwired unless BOTH the URL and the token are set", () => {
    expect(upstreamSinkFromEnv({})).toBeUndefined();
    expect(
      upstreamSinkFromEnv({ UPSTREAM_SINK_URL: "https://queue.example/i" }),
    ).toBeUndefined();
    expect(upstreamSinkFromEnv({ UPSTREAM_SINK_TOKEN: "t" })).toBeUndefined();
  });

  it("constructs from the env triple and exposes its platform label", () => {
    const sink = upstreamSinkFromEnv({
      UPSTREAM_SINK_URL: "https://queue.example/api/upstream/ingest",
      UPSTREAM_SINK_TOKEN: "queue-key",
      UPSTREAM_SINK_PLATFORM: "leaf-web-demo",
    });
    expect(sink).toBeInstanceOf(HttpUpstreamSink);
    // The loop reads this to build the complete payload before serializing.
    expect(sink?.platform).toBe("leaf-web-demo");
  });

  it("resolves when the queue is down, so authoring cannot observe it", async () => {
    const sink = new HttpUpstreamSink({
      url: "https://queue.example/api/upstream/ingest",
      token: "queue-key",
      fetchImpl: (() => Promise.reject(new Error("queue down"))) as unknown as typeof fetch,
    });
    await expect(sink.capture(JSON.stringify(event()))).resolves.toBeUndefined();
  });

  it("transmits the caller's bytes verbatim with bearer auth", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    const sink = new HttpUpstreamSink({
      url: "https://queue.example/api/upstream/ingest",
      token: "queue-key",
      fetchImpl: ((url: string, init: RequestInit) => {
        seen.push({ url, init });
        return Promise.resolve(new Response("{}", { status: 200 }));
      }) as unknown as typeof fetch,
    });
    const body = JSON.stringify(event());
    await sink.capture(body);

    expect(seen).toHaveLength(1);
    const headers = seen[0].init.headers as Record<string, string>;
    expect(headers.authorization).toBe("Bearer queue-key");
    // VERBATIM: the exact string, not a re-serialization of a parse of it.
    expect(seen[0].init.body).toBe(body);
  });

  // EVERY composition root, not just one. The sink is reachable in production
  // only if the root the CONTAINER runs constructs it and spreads it into
  // HarnessPorts. PR #583 wired only server.ts startReal(), which the
  // deployment never execs -- scripts/start-harness.sh runs
  // `node dist/scripts/serve.js` -- so every deployed authoring saw
  // ports.upstreamSink === undefined while this suite, typecheck and the gate
  // all stayed green. Enumerate all roots here so a wiring that misses one, or
  // a newly added root, goes red. A vendor sync that renamed the export also
  // leaves these red.
  const COMPOSITION_ROOTS = ["../scripts/serve.ts", "../src/server.ts"];
  it.each(COMPOSITION_ROOTS)("wires the sink into composition root %s", (root) => {
    const source = readFileSync(
      fileURLToPath(new URL(root, import.meta.url)),
      "utf8",
    );
    expect(source).toContain("upstreamSinkFromEnv");
    // The branch, not a spread: HarnessPorts is a discriminated union, so
    // opting in must supply the sink AND both posture flags together.
    expect(source).toContain("upstreamSink,");
    expect(source).toContain("dangerouslyExportModelOutput: false");
    expect(source).toContain("trustRunnerFailureCategories: false");
  });

  it("execs serve.js as the container entrypoint, so COMPOSITION_ROOTS cannot silently go stale", () => {
    // If the deployment stops running serve.js, the root list above is
    // measuring the wrong file and this assertion, not a silent gap, catches it.
    const entrypoint = readFileSync(
      fileURLToPath(new URL("../scripts/start-harness.sh", import.meta.url)),
      "utf8",
    );
    expect(entrypoint).toContain("dist/scripts/serve.js");
  });
});
