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
 * `confirm.proposal` — {tool, params, dwg} exactly, the propose-time consult
 * shape. The drawing is server-stored proposal truth, never a client choice.
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
import { grantSecrets, isRedactableSecret, stripSecrets } from "../redact.js";
import type {
  IntentSynthesizer,
  AgentGrant,
  AppRunClient,
  ConfirmationRecord,
  ConverseEvent,
  ConverseRunOptions,
  ConverseRunner,
  ConverseTurnInput,
  GateClient,
  HarnessTurnEvent,
  OAuthGrantProvider,
  SessionStore,
  SpineConverseRunner,
  InstantExecutorClient,
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
  "question_required",
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
  /**
   * Optional per-turn intent classifier factory (real: HaikuIntentSynthesizer
   * with the same tenant grant). Absent = no signal, and the turn behaves
   * exactly as it did before intent synthesis existed.
   */
  intentFor?: (grant: AgentGrant) => IntentSynthesizer;
  instantExecutor?: InstantExecutorClient;
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
    const lease = !input.credential_grant && this.ports.oauth.acquireGrant
      ? await this.ports.oauth.acquireGrant(input.tenant_id)
      : null;
    const grant: AgentGrant = input.credential_grant
      ? wireGrantToAgentGrant(input.credential_grant)
      : lease?.grant ?? await this.ports.oauth.getGrant(input.tenant_id);

    // Scrub the credential out of the INBOUND CONTENT, exactly once, here.
    //
    // A user who pastes their own key into the prompt would otherwise have it
    // copied into the transcript, the app gate's pending record, confirmation
    // args_json, and the wire. Rounds 3-5 of the PR #117 review tried to scrub
    // each of those SINKS and that approach fails structurally:
    //   - the app gate keeps its own raw copy, so scrubbing the harness copy
    //     made the two args hashes disagree and approval replay died as
    //     args_mismatch;
    //   - walking event objects to redact keys dropped fields on collision and
    //     let `__proto__` mutate the rebuilt object's prototype.
    //
    // Scrubbing the SOURCE has neither failure mode. Every downstream copy is
    // derived from this one scrubbed input, so they all agree by construction
    // and no hash can mismatch. It touches only `text` strings — never a key,
    // never a structure — so it cannot drop a field or pollute a prototype. And
    // because the model never receives the credential, it cannot echo it back
    // into a text_delta or propose a tool parameter containing it.
    //
    // Only long, whitespace-free values are eligible (isRedactableSecret), so a
    // short or common string can never shred ordinary prose. LITERAL removal only
    // (stripSecrets, not redactSecrets): the pattern pass would rewrite any 40-char
    // token-shaped run, e.g. a Git SHA the user legitimately pasted, before the
    // model saw it. (sol-critic PR #123 round 7, blocker 2.)
    const secrets = grantSecrets(grant).filter(isRedactableSecret);
    if (secrets.length) {
      input = {
        ...input,
        ...(input.text !== undefined ? { text: stripSecrets(input.text, secrets) } : {}),
        messages: input.messages.map((m) => ({ ...m, text: stripSecrets(m.text, secrets) })),
      };
    }

    // The grant itself goes to the runner's env and NOWHERE else — never to this
    // store, the wire, or a log. Content is handled above, at the input, which is
    // why nothing downstream of here needs a scrubbing wrapper.
    const store = this.ports.store;

    // 2. The loop's own session row (idempotent per tenant+drawing).
    const session = await store.createOrGetSession(input.tenant_id, input.drawing_id);

    // 3. Resilience seed for the confirm mirror (see header).
    if (input.confirm) {
      await this.seedConfirmationMirror(store, session.session_id, input);
    }

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
        instantExecutor: this.ports.instantExecutor,
        ...(this.ports.intentFor ? { intent: this.ports.intentFor(grant) } : {}),
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
    let routingUsage = { cost_tokens: 0 };
    let routingStopReason: import("../ports/index.js").ConverseStopReason = "error";
    let routingRetryAfterS: number | undefined;
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
      if (ev.type === "turn_usage" && typeof ev.data.cost_tokens === "number") {
        routingUsage = { cost_tokens: Math.max(0, Math.trunc(ev.data.cost_tokens)) };
      } else if (ev.type === "turn_complete" && typeof ev.data.stop_reason === "string") {
        routingStopReason = ev.data.stop_reason as import("../ports/index.js").ConverseStopReason;
      } else if (ev.type === "error") {
        const detail = ev.data.error;
        if (detail && typeof detail === "object" && !Array.isArray(detail)) {
          const retryAfter = (detail as Record<string, unknown>).retry_after_s;
          if (typeof retryAfter === "number" && Number.isFinite(retryAfter) && retryAfter > 0) {
            routingRetryAfterS = retryAfter;
          }
        }
      }
      queue.push({ type, data: ev.data });
      push();
    };

    // Loop-level rejections here (turn lock, bad message, invalid confirm) are
    // pre-stream throws: the route's catch renders the non-stream error shape.
    const { done } = await loop.handleMessage({
      sessionId: session.session_id,
      tenantId: input.tenant_id,
      ...(input.text !== undefined ? { text: input.text } : {}),
      ...(input.images?.length ? { images: input.images } : {}),
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
      instantAssignment: opts?.instantAssignment,
      instantDrawingContext: opts?.instantDrawingContext,
      authoritySessionId: input.session_id,
      authorityTurnId: input.turn_id,
      onEvent,
    });
    void (async () => {
      try {
        await done;
        if (lease && this.ports.oauth.settleGrant) {
          await this.ports.oauth.settleGrant(input.tenant_id, lease.lease_id, {
            usage: routingUsage,
            stop_reason: routingStopReason,
            ...(routingRetryAfterS !== undefined ? { retry_after_s: routingRetryAfterS } : {}),
          });
        }
      } finally {
        finished = true;
        push();
      }
    })().catch(() => {
      // Routing telemetry must never turn a completed model turn into a broken stream.
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
   *  proposal so a store-loss never strands a consumed approval. Args are exactly
   *  {tool, params, dwg} — the normalized propose-time gate consult shape. */
  private async seedConfirmationMirror(
    store: SessionStore,
    loopSessionId: string,
    input: ConverseTurnInput,
  ): Promise<void> {
    const confirm = input.confirm!;
    const existing = await store.getConfirmation(confirm.confirmation_id);
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
        ...(typeof confirm.proposal.dwg === "string"
          ? { dwg: confirm.proposal.dwg }
          : {}),
      }),
      kind: "run_capability",
      status: "pending",
      created_at: new Date(now).toISOString(),
      expires_at: new Date(now + ttlS * 1000).toISOString(),
      decided_at: null,
      decided_by: null,
    };
    await store.putConfirmation(rec);
  }
}
