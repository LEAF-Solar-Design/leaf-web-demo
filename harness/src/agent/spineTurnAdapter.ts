/**
 * SpineTurnAdapter — mounts the §18 spine engine (ConverseLoop) behind the
 * FROZEN sessions wire (`POST /turn`, ports/converse.ts ConverseRunner). This is
 * the spine unification the 2026-07-21 merge resolution deferred: the app's turn
 * engine (server/turn_runner.py) keeps owning sessions/turn-locks/approvals
 * client-side, while EVERY tool execution inside the turn now goes through the
 * loop's gate-before-exec + submitRun-only discipline (invariant v2,
 * test/converseRuntimeSeparation.test.ts).
 *
 * Identity mapping: the loop keeps its OWN durable store (FileSessionStore keyed
 * by tenant+drawing, its own turn ids and seq) — none of that leaks onto the
 * wire. HarnessTurnEvent is `{type, data}` only; the app assigns its own seq as
 * it relays. The loop-side store exists for sdk_session_id resume, transcript
 * mirroring, and the confirmation mirror rows the split-turn resume validates.
 *
 * Confirm resume: the app's approvals row is AUTHORITATIVE (single-redeem
 * consume happens app-side before /turn is ever called; the gate's args-bound
 * pending record is redeemed at the resume's gate consult). The loop's local
 * mirror row normally exists from the propose turn; if it is missing (store
 * wiped, first mount over pre-spine approvals) it is re-seeded from the wire's
 * `confirm.proposal` — {tool, params} exactly, the propose-time consult shape.
 * NOTE the reseed changes the failure MODE, not the outcome: the gate binds an
 * approval to the loop-session identity that proposed it, so after a store wipe
 * the redemption is DENIED (foreign session — that binding is the security
 * property). With the reseed, that denial arrives as a streamed, calmly-relayed
 * tool result the model can explain; without it, the loop would crash pre-stream
 * on ConfirmationInvalid and burn the approval opaquely. Either way: fail-safe,
 * never rogue-allow; the user re-proposes and gets a fresh chip.
 *
 * Context: the frozen wire carries no ContextPacket field (the app-side
 * server/context_packet.py has no live caller yet — recorded chip-2/5 work). The
 * adapter builds a packet-lite from wire fields only: {drawing_id} plus, when the
 * loop has no SDK session to resume, the wire's bounded prior `messages` as data.
 * On resume, it keeps those messages out of the normal prompt and supplies them
 * only to the runner's missing-session recovery prompt.
 *
 * Known debt (recorded in the mount plan): `opts.signal` stops YIELDS immediately
 * on client disconnect, but does not abort the in-flight SDK call — the loop
 * finishes the turn into its store, bounded by ConverseSdkRunner's turn timeout
 * (LEAF_SPINE_TURN_TIMEOUT_S, default 120 s) and maxTurns. AgentSdkTurnRunner
 * wired the signal all the way down; restoring that on the spine path needs an
 * abort seam through SpineConverseRunner.run and is deliberately not smuggled
 * into the mount chip.
 */

import { ConverseLoop } from "./converseLoop.js";
import { wireGrantToAgentGrant } from "../ports/wireGrant.js";
import { grantSecrets, scrubDeep } from "../redact.js";
import type {
  AgentGrant,
  AppRunClient,
  ConfirmationRecord,
  ConverseEvent,
  ConverseRunOptions,
  ConverseEventType,
  ConverseRunner,
  ConverseTurnInput,
  GateClient,
  HarnessTurnEvent,
  OAuthGrantProvider,
  SessionStore,
  SpineConverseRunner,
} from "../ports/index.js";

/** The 9 event types the frozen wire admits (ports/converse.ts). The loop's
 *  turn_started / confirmation_resolved / session_state are the turn engine's
 *  to emit on its own hop and are dropped here, not relayed twice. */
const WIRE_EVENT_TYPES = new Set<HarnessTurnEvent["type"]>([
  "text_delta",
  "tool_call",
  "tool_result",
  "job_linked",
  "proposed_run",
  "confirmation_required",
  "turn_usage",
  "turn_complete",
  "error",
]);

export interface SpineTurnAdapterPorts {
  /** Per-tenant grant resolution; GrantRequiredError propagates pre-stream (401). */
  oauth: OAuthGrantProvider;
  appRun: AppRunClient;
  gate: GateClient;
  store: SessionStore;
  /** Per-turn LLM runner factory (real: ConverseSdkRunner with the tenant grant). */
  runnerFor: (grant: AgentGrant) => SpineConverseRunner;
}

export interface SpineTurnAdapterOptions {
  /** Spine model id. Default: LEAF_SPINE_MODEL env, else the loop's default. */
  model?: string;
  /** Confirmation TTL seconds for loop mirror rows (wire contract section 7: 300). */
  confirmationTtlS?: number;
}

export class SpineTurnAdapter implements ConverseRunner {
  constructor(
    private readonly ports: SpineTurnAdapterPorts,
    private readonly opts: SpineTurnAdapterOptions = {},
  ) {}

  async *runTurn(
    input: ConverseTurnInput,
    opts?: ConverseRunOptions,
  ): AsyncGenerator<HarnessTurnEvent> {
    // 1. Grant FIRST, before any yield. A per-session bring-your-own credential
    //    on the wire ("mount your LLM") is used for THIS turn instead of the
    //    tenant's linked grant — mapped to the internal AgentGrant, injected into
    //    the runner's scrubbed env, never logged, never persisted. Otherwise the
    //    tenant's linked grant is resolved; a missing one must surface as the
    //    non-stream 401 {grant_required:true}, never a half-open 200 stream.
    const grant: AgentGrant = input.credential_grant
      ? wireGrantToAgentGrant(input.credential_grant)
      : await this.ports.oauth.getGrant(input.tenant_id);

    // 2. The loop's own session row (idempotent per tenant+drawing).
    const session = await this.ports.store.createOrGetSession(
      input.tenant_id,
      input.drawing_id,
    );

    // 3. Resilience seed for the confirm mirror (see header).
    if (input.confirm) {
      await this.seedConfirmationMirror(session.session_id, input);
    }

    // The credential must be scrubbed BEFORE it can be persisted, not after.
    // ConverseLoop's emit() calls store.appendEvent and only then hands the event
    // to onEvent, so any wire-level filtering is too late for the transcript —
    // and the leak is not limited to thrown errors: an ordinary text_delta can
    // carry the value (a user who pastes their own key into the prompt has it
    // echoed straight back). Wrapping the store puts the scrub at the single
    // point every durable event must pass through, whatever produced it, while
    // keeping the secret inside this adapter — the loop stays credential-free.
    // (sol-critic PR #117 round 3, blocker 1.)
    // A Proxy, not a spread: a store is typically a class instance whose methods
    // live on the prototype, so spreading would drop every one of them.
    const secrets = grantSecrets(grant);
    const inner = this.ports.store;
    const store: SessionStore = secrets.length
      ? (new Proxy(inner, {
          get(target, prop, receiver) {
            if (prop === "appendEvent") {
              return (
                sessionId: string,
                turnId: string,
                type: ConverseEventType,
                data: Record<string, unknown>,
              ) => target.appendEvent(sessionId, turnId, type, scrubDeep(data, secrets));
            }
            const v = Reflect.get(target, prop, receiver);
            // Rebind to the real store so its own state stays reachable.
            return typeof v === "function" ? v.bind(target) : v;
          },
        }) as SessionStore)
      : inner;

    const loop = new ConverseLoop(
      {
        runner: this.ports.runnerFor(grant),
        appRun: this.ports.appRun,
        // The loop owns separate durable session/turn ids for SDK resume and
        // confirmation binding. Preserve the authenticated app wire ids as a
        // second, explicit authority tuple for the app-side entitlement lookup.
        gate: {
          check: (action, args, ctx) =>
            this.ports.gate.check(action, args, {
              ...ctx,
              authoritySessionId: input.session_id,
              authorityTurnId: input.turn_id,
            }),
        },
        store,
      },
      {
        // Per-session model ("mount your LLM"): the wire value wins, then the
        // adapter default, else the loop's env default (LEAF_SPINE_MODEL) stays
        // the fallback. Only set when non-empty so the loop keeps its env default.
        ...((input.model ?? this.opts.model)
          ? { model: input.model ?? this.opts.model }
          : {}),
        ...(this.opts.confirmationTtlS
          ? { confirmationTtlS: this.opts.confirmationTtlS }
          : {}),
      },
    );

    // 4. Packet-lite (see header): wire fields only, prior messages as data.
    const priorMessages =
      !session.sdk_session_id && input.messages.length
        ? { prior_messages: input.messages }
        : {};
    const contextPacket: Record<string, unknown> = {
      drawing_id: input.drawing_id,
      ...priorMessages,
    };

    // 5. Bridge the loop's push sink onto this pull generator.
    const queue: HarnessTurnEvent[] = [];
    let wake: (() => void) | null = null;
    let finished = false;
    const push = (): void => {
      wake?.();
      wake = null;
    };
    // ONE abort listener for the whole turn (per-wait listeners would pile up
    // one per event on the signal); it only wakes the wait — the loop below
    // observes `aborted` and returns.
    opts?.signal?.addEventListener("abort", push, { once: true });
    const onEvent = (ev: ConverseEvent): void => {
      const type = ev.type as HarnessTurnEvent["type"];
      if (!WIRE_EVENT_TYPES.has(type)) return;
      // Scrubbed here too, NOT just in the store wrapper above: scrubDeep copies
      // rather than mutates, so the loop still holds the original object and
      // hands THAT to this callback. The store wrapper protects the transcript;
      // this protects the wire. Both use the same deep walk, so they agree.
      queue.push({ type, data: scrubDeep(ev.data, secrets) });
      push();
    };

    // Loop-level rejections here (turn lock, bad message, invalid confirm) are
    // pre-stream throws: the route's catch renders the non-stream error shape.
    const { done } = await loop.handleMessage({
      sessionId: session.session_id,
      tenantId: input.tenant_id,
      ...(input.text !== undefined ? { text: input.text } : {}),
      ...(input.confirm
        ? {
            confirm: {
              confirmationId: input.confirm.confirmation_id,
              approved: input.confirm.approved,
            },
          }
        : {}),
      contextPacket,
      priorMessages: input.messages,
      onEvent,
    });
    void done.finally(() => {
      finished = true;
      push();
    });

    while (true) {
      while (queue.length) {
        if (opts?.signal?.aborted) return;
        yield queue.shift()!;
      }
      if (finished) return;
      if (opts?.signal?.aborted) return;
      await new Promise<void>((resolve) => {
        wake = resolve;
      });
    }
  }

  /** Re-create a missing confirmation mirror row from the wire's app-authoritative
   *  proposal so a store-loss never strands a consumed approval. args are exactly
   *  {tool, params} — the propose-time gate consult shape (hash compatibility). */
  private async seedConfirmationMirror(
    loopSessionId: string,
    input: ConverseTurnInput,
  ): Promise<void> {
    const confirm = input.confirm!;
    const existing = await this.ports.store.getConfirmation(confirm.confirmation_id);
    if (existing) return;
    const ttlS = this.opts.confirmationTtlS ?? 300;
    const now = Date.now();
    const rec: ConfirmationRecord = {
      confirmation_id: confirm.confirmation_id,
      session_id: loopSessionId,
      turn_id: input.turn_id,
      action: "run_capability",
      args_json: JSON.stringify({
        tool: confirm.proposal.tool,
        params: confirm.proposal.params,
      }),
      kind: "run_capability",
      status: "pending",
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + ttlS * 1000).toISOString(),
      decided_at: null,
      decided_by: null,
    };
    await this.ports.store.putConfirmation(rec);
  }
}
