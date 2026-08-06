/**
 * Wiring contract for the vendored upstream-capture sink.
 *
 * The deep behavior (dedupe key, timeout, secret scrubbing) is tested
 * upstream in mushy-code; tests are not vendored. What THIS repo owns is the
 * wiring, and the defect this file exists to catch is a sink wired into a
 * composition root the deployment never calls: `startReal()` in server.ts is
 * NOT the production path. The container runs `scripts/start-harness.sh`,
 * which execs `node dist/scripts/serve.js`, whose `buildPorts()` is the real
 * composition root. A sink wired only into `startReal()` reads as correct in
 * every unit test and captures nothing in production.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { AuthorLoop } from "../src/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import type { HarnessPorts, UpstreamCapture } from "../src/ports/index.js";
import {
  HttpUpstreamSink,
  upstreamSinkFromEnv,
} from "../src/ports/impl/httpUpstreamSink.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const HARNESS_ROOT = join(HERE, "..");
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

/** Every file that composes a live harness. Adding a launcher adds a row. */
const COMPOSITION_ROOTS = [
  join(HARNESS_ROOT, "scripts", "serve.ts"), // the CANONICAL production path
  join(HARNESS_ROOT, "src", "server.ts"),
];

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

function makePorts(overrides: Partial<HarnessPorts> = {}): HarnessPorts {
  return {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo: new FakeTenantRepoProvider(FIXTURE),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
    ...overrides,
  };
}

describe("upstream sink is wired into the path the deployment actually runs", () => {
  it("the container entrypoint leads to serve.ts, not server.ts", () => {
    const startScript = readFileSync(
      join(HARNESS_ROOT, "scripts", "start-harness.sh"),
      "utf8",
    );
    expect(startScript).toContain("dist/scripts/serve.js");
    // If this ever stops being true, the composition-root list below is stale.
    expect(startScript).not.toContain("dist/src/server.js");
  });

  it("every composition root wires the sink", () => {
    for (const root of COMPOSITION_ROOTS) {
      const source = readFileSync(root, "utf8");
      expect(source, `${root} composes a harness`).toContain("createHarness(");
      expect(source, `${root} must wire upstreamSinkFromEnv`).toContain(
        "upstreamSinkFromEnv",
      );
      expect(source, `${root} must pass the port through`).toContain(
        "upstreamSink",
      );
    }
  });
});

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

describe("authoring is unobservable to sink health", () => {
  it("a sink that NEVER settles does not delay or fail authoring", async () => {
    let released!: () => void;
    const pending = new Promise<void>((resolve) => {
      released = resolve;
    });
    const loop = new AuthorLoop(
      makePorts({ upstreamSink: { capture: () => pending } }),
    );
    // If capture were awaited anywhere on the authoring path, this never
    // resolves and the test times out.
    const response = await loop.buildLegacyAuthOff(
      "tenant-a",
      "count panels by layer",
    );
    expect(response.tool.name).toBeTruthy();
    released();
  });

  it("a sink that THROWS synchronously does not fail authoring", async () => {
    const loop = new AuthorLoop(
      makePorts({
        upstreamSink: {
          capture: () => {
            throw new Error("sink exploded");
          },
        },
      }),
    );
    const response = await loop.buildLegacyAuthOff(
      "tenant-a",
      "count panels by layer",
    );
    expect(response.tool.name).toBeTruthy();
  });

  it("the ports object is unchanged when the env is unset", () => {
    const base = makePorts();
    const sink = upstreamSinkFromEnv({});
    const composed: HarnessPorts = {
      ...base,
      ...(sink ? { upstreamSink: sink } : {}),
    };
    expect(Object.keys(composed).sort()).toEqual(Object.keys(base).sort());
    expect(composed).not.toHaveProperty("upstreamSink");
  });
});
