/**
 * Harness converse port v1 (FROZEN — leaf-backend-gaps.md §2.1, encoded verbatim).
 *
 * The turn-engine boundary for the conversational sessions lane: the FastAPI
 * turn engine (server/turn_runner.py, S3) POSTs ONE `ConverseTurnInput` to
 * `POST /turn` and drains an `application/x-ndjson` response, one
 * `HarnessTurnEvent` per line, always terminated by a `turn_complete` or
 * `error` event. A `ConverseRunner` is the harness-side implementation of
 * that stream (real = Agent SDK loop, fake = scripted for hermetic tests).
 *
 * Ownership split (see shared contract): the turn engine emits
 * `turn_started` + `session_state` and relays everything below; THIS port
 * emits `text_delta` / `tool_call` / `tool_result` / `job_linked` /
 * `proposed_run` / `confirmation_required` / `turn_usage` / `turn_complete`
 * / `error` — the 9 event types listed in `HarnessTurnEvent.type`.
 */

/**
 * Why a turn ended. Mirrors `turn_complete.data.stop_reason` on the wire
 * (leaf-backend-gaps.md §2.1.3) and the non-stream 401/429 rejections the
 * turn engine (S3) translates into `TurnRejected`.
 */
export type StopReason =
  | "end_turn"
  | "awaiting_approval"
  | "cap_hit"
  | "llm_rate_limited"
  | "llm_quota_exhausted"
  | "error"
  | "timeout";

/**
 * One NDJSON line from `POST /turn`. `data` is intentionally untyped per
 * event (each `type` has its own §2.1.3 field set — the turn engine only
 * needs to append it verbatim into `session_events.data_json`, never
 * interpret it).
 */
export interface HarnessTurnEvent {
  type:
    | "text_delta"
    | "tool_call"
    | "tool_result"
    | "job_linked"
    | "proposed_run"
    | "confirmation_required"
    | "turn_usage"
    | "turn_complete"
    | "error";
  data: Record<string, unknown>;
}

/**
 * A per-session bring-your-own Agent SDK credential ("mount your LLM", Concern
 * 2), in the snake_case shape the app forwards on the wire. Mapped to the
 * internal camelCase `AgentGrant` by ports/wireGrant.ts, then injected into the
 * runner's scrubbed child env for THIS turn only. NEVER logged, NEVER persisted.
 */
export type WireAgentGrant =
  | { kind: "api_key"; api_key: string }
  | { kind: "oauth"; oauth_token: string };

/**
 * Body of `POST /turn`. `messages` is prior turn context, bounded and
 * assembled by the turn engine (never the full unbounded transcript).
 * Exactly one of `text` / `confirm` drives the turn — a fresh user message,
 * or the resume of a turn that halted on `confirmation_required` (the
 * turn engine attaches the original `proposed_run` proposal from the
 * `approvals` row so the runner never has to re-derive it).
 */
export interface ConverseTurnInput {
  tenant_id: string;
  session_id: string;
  turn_id: string;
  drawing_id: string;
  /** Prior context, bounded, built by the turn engine. */
  messages: Array<{ role: "user" | "assistant"; text: string }>;
  text?: string;
  confirm?: {
    confirmation_id: string;
    approved: boolean;
    proposal: {
      tool: string;
      params: Record<string, unknown>;
      capability?: string;
    };
  };
  /**
   * OPTIONAL per-session model override ("mount your LLM"). When present it
   * overrides the runner's env-default model (LEAF_SPINE_MODEL, else
   * claude-sonnet-5) for THIS turn. The app validates it against the allowed
   * Claude family (server/turn_runner.py ALLOWED_MODELS); the harness validates
   * again defensively (ports/modelAllowlist.ts) before it reaches sdk.query().
   */
  model?: string;
  /**
   * OPTIONAL per-session bring-your-own credential. When present the turn runs on
   * THIS grant (injected into the scrubbed runner env) instead of the tenant's
   * linked grant. Ephemeral: never logged, never persisted in session state.
   */
  credential_grant?: WireAgentGrant;
}

/**
 * In-process (never on-the-wire) options for one `runTurn` drive. `signal`
 * fires when the HTTP client of `POST /turn` disconnects: `iterator.return()`
 * alone queues behind a pending `next()` per the async-generator spec, so a
 * hung/expensive FIRST model call would otherwise keep running after the
 * peer is gone — the runner must wire this signal into whatever underlying
 * cancellation it has (the Agent SDK's AbortController) so that pull is
 * abandoned promptly. Optional and additive: fakes may ignore it.
 */
export interface ConverseRunOptions {
  signal?: AbortSignal;
}

/**
 * The harness-side implementation of the converse turn loop. `runTurn`
 * drives one turn to completion (or halt-on-approval) and yields the
 * `HarnessTurnEvent`s the turn engine relays/persists as it goes; the
 * async iterable ending is equivalent to the NDJSON stream ending.
 */
export interface ConverseRunner {
  runTurn(input: ConverseTurnInput, opts?: ConverseRunOptions): AsyncIterable<HarnessTurnEvent>;
}
