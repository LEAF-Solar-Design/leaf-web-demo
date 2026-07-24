/**
 * "Mount your LLM" — per-session model + bring-your-own credential on the frozen
 * `POST /turn` wire. Hermetic: FakeConverseRunner captures every ConverseRunInput
 * (the exact object whose `.model` ConverseSdkRunner hands to sdk.query
 * options.model), so a fake capture PROVES the chosen model reaches the runner
 * without spending a live credit (honors LEAF_AGENT_MOCK's no-credits contract).
 *
 * Proven here:
 *   - a turn carrying {model} routes THAT model to the runner;
 *   - no wire model => the env default (LEAF_SPINE_MODEL, else claude-sonnet-5)
 *     is preserved;
 *   - a supplied credential_grant is used for the runner's grant INSTEAD of the
 *     tenant's linked grant (the oauth provider is never consulted);
 *   - the supplied credential value never lands in serialized session state.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { redactTokens } from "../src/redact.js";
import type { AgentGrant, OAuthGrantProvider } from "../src/ports/index.js";
import type { ConverseTurnInput, HarnessTurnEvent } from "../src/ports/converse.js";

/** OAuth provider that FAILS if consulted — proves a wire credential bypasses it. */
class UnusedOAuthProvider implements OAuthGrantProvider {
  consulted = 0;
  async getGrant(_tenantId: string): Promise<AgentGrant> {
    this.consulted += 1;
    throw new Error("oauth.getGrant must not be called when a wire credential is supplied");
  }
}

/** Records the tenant grant it hands back so tests can assert linked-grant use. */
class RecordingOAuthProvider implements OAuthGrantProvider {
  consulted = 0;
  constructor(private readonly grant: AgentGrant = { kind: "oauth", oauthToken: "TENANT-LINKED-GRANT" }) {}
  async getGrant(_tenantId: string): Promise<AgentGrant> {
    this.consulted += 1;
    return this.grant;
  }
}

function makeAdapter(oauth: OAuthGrantProvider) {
  const runner = new FakeConverseRunner();
  const store = new FakeSessionStore();
  const grants: AgentGrant[] = [];
  const adapter = new SpineTurnAdapter({
    oauth,
    appRun: new FakeAppRunClient(),
    gate: new FakeGateClient(),
    store,
    runnerFor: (grant) => {
      grants.push(grant);
      return runner;
    },
  });
  return { adapter, runner, store, grants };
}

function turnInput(partial: Partial<ConverseTurnInput>): ConverseTurnInput {
  return {
    tenant_id: "demo-tenant",
    session_id: "app-session-1",
    turn_id: "app-turn-1",
    drawing_id: "rooftop_demo",
    messages: [],
    ...partial,
  };
}

async function drain(gen: AsyncIterable<HarnessTurnEvent>): Promise<HarnessTurnEvent[]> {
  const events: HarnessTurnEvent[] = [];
  for await (const ev of gen) events.push(ev);
  return events;
}

/** Serialize everything a session persists, so a secret leak anywhere shows up. */
function serializedState(store: FakeSessionStore): string {
  return JSON.stringify({
    sessions: [...store.sessions.values()],
    turns: [...store.turns.values()],
    events: [...store.events.values()],
    confirmations: [...store.confirmations.values()],
    usage: store.usage,
  });
}

describe("mount your LLM — per-session model", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("routes a turn's {model} to the runner (sdk.query options.model)", async () => {
    const { adapter, runner } = makeAdapter(new RecordingOAuthProvider());
    await drain(adapter.runTurn(turnInput({ text: "hi", model: "claude-opus-4-8" })));

    expect(runner.runs.length).toBe(1);
    expect(runner.runs[0]!.model).toBe("claude-opus-4-8");
  });

  it("preserves the env default (LEAF_SPINE_MODEL) when no wire model is set", async () => {
    vi.stubEnv("LEAF_SPINE_MODEL", "claude-haiku-4-5");
    const { adapter, runner } = makeAdapter(new RecordingOAuthProvider());
    await drain(adapter.runTurn(turnInput({ text: "hi" })));

    expect(runner.runs[0]!.model).toBe("claude-haiku-4-5");
  });

  it("falls back to claude-sonnet-5 when neither wire model nor env is set", async () => {
    vi.stubEnv("LEAF_SPINE_MODEL", undefined as unknown as string); // delete the var
    const { adapter, runner } = makeAdapter(new RecordingOAuthProvider());
    await drain(adapter.runTurn(turnInput({ text: "hi" })));

    expect(runner.runs[0]!.model).toBe("claude-sonnet-5");
  });
});

describe("mount your LLM — bring-your-own credential", () => {
  it("uses a wire api_key grant for the runner, bypassing the oauth provider", async () => {
    const oauth = new UnusedOAuthProvider();
    const { adapter, grants } = makeAdapter(oauth);
    await drain(
      adapter.runTurn(
        turnInput({
          text: "hi",
          credential_grant: { kind: "api_key", api_key: "sk-ant-api-BYO-SECRET" },
        }),
      ),
    );

    expect(oauth.consulted).toBe(0);
    expect(grants.length).toBe(1);
    expect(grants[0]).toEqual({ kind: "api_key", apiKey: "sk-ant-api-BYO-SECRET" });
  });

  it("maps a wire oauth grant to the internal camelCase shape", async () => {
    const oauth = new UnusedOAuthProvider();
    const { adapter, grants } = makeAdapter(oauth);
    await drain(
      adapter.runTurn(
        turnInput({
          text: "hi",
          credential_grant: { kind: "oauth", oauth_token: "sk-ant-oat-BYO-SECRET" },
        }),
      ),
    );

    expect(oauth.consulted).toBe(0);
    expect(grants[0]).toEqual({ kind: "oauth", oauthToken: "sk-ant-oat-BYO-SECRET" });
  });

  it("resolves the tenant's linked grant when no wire credential is supplied", async () => {
    const oauth = new RecordingOAuthProvider();
    const { adapter, grants } = makeAdapter(oauth);
    await drain(adapter.runTurn(turnInput({ text: "hi" })));

    expect(oauth.consulted).toBe(1);
    expect(grants[0]).toEqual({ kind: "oauth", oauthToken: "TENANT-LINKED-GRANT" });
  });

  it("scrubs a SHORT credential out of the DURABLE transcript, not just the wire", async () => {
    // sol-critic PR #117 round 3, blocker 1. The round-2 version of this test
    // asserted only on adapter output, so it stayed green even with no scrub
    // before store.appendEvent — which is the append that actually persists.
    // ConverseLoop persists BEFORE it calls onEvent, and the leak is not limited
    // to thrown errors: a plain text_delta can carry the value. Assert on the
    // STORE first; that is the assertion that fails without the scrubbing store.
    const SHORT = "short-key!";
    const runner = new FakeConverseRunner();
    const store = new FakeSessionStore();
    const adapter = new SpineTurnAdapter({
      oauth: new UnusedOAuthProvider(),
      appRun: new FakeAppRunClient(),
      gate: new FakeGateClient(),
      store,
      runnerFor: () => runner,
    });

    // A successful turn whose ordinary streamed text quotes the credential —
    // the runner never throws, so its error wrapper is not involved at all.
    const origRun = runner.run.bind(runner);
    runner.run = async function* (runInput) {
      yield { type: "text_delta", text: `you gave me ${SHORT}` };
      yield* origRun(runInput);
    };

    const events = await drain(
      adapter.runTurn(
        turnInput({ text: "hi", credential_grant: { kind: "api_key", api_key: SHORT } }),
      ),
    );

    expect(serializedState(store)).not.toContain(SHORT); // the durable transcript
    expect(JSON.stringify(events)).not.toContain(SHORT); // and the wire
    expect(serializedState(store)).toContain("[REDACTED]");
  });

  it("does not corrupt events when the credential is a JSON delimiter", async () => {
    // sol-critic PR #117 round 3, blocker 2. `"` is a credential the app accepts
    // today. Scrubbing by serialize-replace-reparse turned every delimiter into
    // [REDACTED] and threw on parse; ConverseLoop swallows that callback error,
    // so the turn died silently. Scrubbing string LEAVES cannot corrupt shape.
    const QUOTE = '"';
    const runner = new FakeConverseRunner();
    const store = new FakeSessionStore();
    const adapter = new SpineTurnAdapter({
      oauth: new UnusedOAuthProvider(),
      appRun: new FakeAppRunClient(),
      gate: new FakeGateClient(),
      store,
      runnerFor: () => runner,
    });

    const events = await drain(
      adapter.runTurn(
        turnInput({ text: "hi", credential_grant: { kind: "api_key", api_key: QUOTE } }),
      ),
    );

    // The turn still produced wire events (it did not die on a parse error)…
    expect(events.length).toBeGreaterThan(0);
    // …and every event is a well-formed object, not corrupted text.
    for (const ev of events) expect(typeof ev.data).toBe("object");
  });

  it("scrubs a SHORT credential the pattern redactor cannot match, on the wire", async () => {
    // sol-critic PR #117 round 2, blocker 1: the app accepts ANY non-empty
    // string as a BYO credential, so TOKENISH ( sk-ant-* | 40+ chars ) misses a
    // short one and the pattern pass alone leaked it. This value is chosen to
    // defeat that regex on purpose — assert on the VALUE, not the pattern.
    const SHORT = "short-key!";
    expect(redactTokens(`SDK rejected ${SHORT}`)).toContain(SHORT); // the gap, pinned

    const runner = new FakeConverseRunner();
    const store = new FakeSessionStore();
    const adapter = new SpineTurnAdapter({
      oauth: new UnusedOAuthProvider(),
      appRun: new FakeAppRunClient(),
      gate: new FakeGateClient(),
      store,
      runnerFor: () => runner,
    });

    // Drive the fake's terminal-failure hook, then rewrite the emitted error to
    // quote the credential the way a real SDK/undici header error would.
    const origRun = runner.run.bind(runner);
    runner.run = async function* (runInput) {
      for await (const ev of origRun(runInput)) {
        if (ev.type === "done" && ev.error) {
          yield { ...ev, error: { ...ev.error, message: `SDK rejected ${SHORT}` } };
        } else {
          yield ev;
        }
      }
    };

    const events = await drain(
      adapter.runTurn(
        turnInput({
          text: "FAIL:error",
          credential_grant: { kind: "api_key", api_key: SHORT },
        }),
      ),
    );

    expect(JSON.stringify(events)).not.toContain(SHORT);
    expect(JSON.stringify(events)).toContain("[REDACTED]");
  });

  it("never persists the supplied credential in serialized session state", async () => {
    const SECRET = "sk-ant-api-DO-NOT-PERSIST-9271";
    const { adapter, store, grants } = makeAdapter(new UnusedOAuthProvider());
    await drain(
      adapter.runTurn(
        turnInput({
          text: "hi",
          model: "claude-sonnet-5",
          credential_grant: { kind: "api_key", api_key: SECRET },
        }),
      ),
    );

    // The secret reached the runner env (this is the whole point)…
    expect(JSON.stringify(grants)).toContain(SECRET);
    // …but never the durable transcript/session state.
    expect(serializedState(store)).not.toContain(SECRET);
    // The model id is NOT secret and legitimately appears (turn_started event).
    expect(serializedState(store)).toContain("claude-sonnet-5");
  });
});
