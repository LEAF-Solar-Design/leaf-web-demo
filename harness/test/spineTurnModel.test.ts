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
  // PRODUCTION-LENGTH: clears the 24-char redaction floor, so tests that use
  // this provider actually exercise the linked-grant scrub path. The old
  // 19-char value sat below the floor and made one test vacuous
  // (sol-critic PR #123 round 7).
  constructor(private readonly grant: AgentGrant = { kind: "oauth", oauthToken: "TENANT-LINKED-GRANT-abcdefgh" }) {}
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
    expect(grants[0]).toEqual({ kind: "oauth", oauthToken: "TENANT-LINKED-GRANT-abcdefgh" });
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

describe("mount your LLM — a credential pasted into the prompt", () => {
  // A 26-char credential: clears the >=24 floor, but is deliberately invisible to
  // TOKENISH (not sk-ant-*, under 40 chars), so these tests prove VALUE-based
  // scrubbing rather than passing by accident on the pattern.
  const PASTED = "BYO-credential-value-1234!";

  function build() {
    const runner = new FakeConverseRunner();
    const store = new FakeSessionStore();
    const gate = new FakeGateClient();
    const adapter = new SpineTurnAdapter({
      oauth: new UnusedOAuthProvider(),
      appRun: new FakeAppRunClient(),
      gate,
      store,
      runnerFor: () => runner,
    });
    return { adapter, runner, store, gate };
  }

  it("never reaches the model, so it cannot be echoed back", async () => {
    const { adapter, runner } = build();
    await drain(
      adapter.runTurn(
        turnInput({
          text: `please use ${PASTED} for me`,
          credential_grant: { kind: "api_key", api_key: PASTED },
        }),
      ),
    );

    // The runner is what talks to the SDK. If the prompt is clean, the model
    // never sees the value and cannot emit it in a text_delta or a tool param.
    expect(JSON.stringify(runner.runs)).not.toContain(PASTED);
    expect(JSON.stringify(runner.runs)).toContain("[REDACTED]");
  });

  it("is scrubbed out of prior messages too, not just this turn's text", async () => {
    const { adapter, runner } = build();
    await drain(
      adapter.runTurn(
        turnInput({
          text: "carry on",
          messages: [{ role: "user", text: `earlier I said ${PASTED}` }],
          credential_grant: { kind: "api_key", api_key: PASTED },
        }),
      ),
    );

    expect(JSON.stringify(runner.runs)).not.toContain(PASTED);
  });

  it("never reaches the durable transcript or the wire", async () => {
    const { adapter, store } = build();
    const events = await drain(
      adapter.runTurn(
        turnInput({
          text: `key is ${PASTED}`,
          credential_grant: { kind: "api_key", api_key: PASTED },
        }),
      ),
    );

    expect(serializedState(store)).not.toContain(PASTED);
    expect(JSON.stringify(events)).not.toContain(PASTED);
  });

  it("keeps every downstream copy IDENTICAL, so approval hashes cannot mismatch", async () => {
    // This is the property the round-3/4 sink-scrubbing approach could not hold:
    // it scrubbed the harness copy while the app gate kept the raw one, so the
    // two args hashes disagreed and approval replay failed as args_mismatch.
    // Scrubbing the source means every copy derives from the same scrubbed text.
    const { adapter, runner, store, gate } = build();
    await drain(
      adapter.runTurn(
        turnInput({
          text: `use ${PASTED} now`,
          credential_grant: { kind: "api_key", api_key: PASTED },
        }),
      ),
    );

    for (const view of [
      JSON.stringify(runner.runs),
      JSON.stringify(gate.checks ?? []),
      serializedState(store),
    ]) {
      expect(view).not.toContain(PASTED);
    }
  });

  it("leaves ordinary content alone, including token-shaped strings", async () => {
    // No wire credential, so the PRODUCTION-LENGTH linked grant is resolved and
    // the scrub path really runs — the earlier version of this test used a
    // below-floor grant, so it passed even with the scrub removed.
    //
    // The 40-char Git SHA is the load-bearing part: the input path must use
    // LITERAL removal, not the TOKENISH pattern pass, or an ordinary prompt
    // mentioning a commit would reach the model with it rewritten to
    // [REDACTED]. (sol-critic PR #123 round 7, blocker 2.)
    const { adapter, runner } = makeAdapter(new RecordingOAuthProvider());
    const text =
      "check commit e3b0c44298fc1c149afbf4c8996fb92427ae41e4 and key sk-ant-api03-looksreal";
    await drain(adapter.runTurn(turnInput({ text })));

    expect(JSON.stringify(runner.runs)).toContain(text);
    expect(JSON.stringify(runner.runs)).not.toContain("[REDACTED]");
  });
});
