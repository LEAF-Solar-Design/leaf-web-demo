/**
 * The conversational spine loop — sibling of AuthorLoop. One instance owns the
 * section-18 converse behavior: idempotent session create/attach, the ONE-active-
 * turn lock, prompt assembly (context packet + user text, or the split-turn
 * confirm line), driving the SpineConverseRunner, and mapping everything onto the
 * EXACT wire event vocabulary (pinned wire contract section 3) with store-
 * persisted seq.
 *
 * Invariant v2 (enforced by test/converseRuntimeSeparation.test.ts): this module
 * never imports the Agent SDK — the runner is an injected port. The seven spine
 * tools execute here. Side effects are limited to registered job dispatch and
 * approved authoring through the app back-edge. A GateClient check precedes
 * every tool execution; a deny becomes an isError result the model can relay.
 */

import { createHash, randomUUID } from "node:crypto";
import { redactTokens } from "../redact.js";
import { SPINE_SYSTEM_PROMPT } from "./spineSystemPrompt.js";
import { SPINE_TOOL_NAMES } from "../ports/index.js";
import type {
  AppRunClient,
  CanUseTool,
  CapabilityEntry,
  ConfirmationRecord,
  ConverseEvent,
  ConverseEventType,
  SpineConverseRunner,
  ConverseStopReason,
  GateClient,
  SessionRecord,
  SessionStore,
  SpineToolName,
  SpineToolResult,
  ToolExecutor,
} from "../ports/index.js";

// --------------------------------------------------------------------------- //
// Structured errors (the HTTP shell maps these onto section-18 status codes).
// --------------------------------------------------------------------------- //

/** 409 turn_in_progress — carries the ACTIVE turn id (wire contract section 1). */
export class TurnInProgressError extends Error {
  constructor(readonly turnId: string) {
    super(`turn_in_progress: turn ${turnId} is active`);
    this.name = "TurnInProgressError";
  }
}

/** 404 session_not_found — also the tenant-mismatch answer (no existence oracle). */
export class SessionNotFoundError extends Error {
  constructor(sessionId: string) {
    super(`session_not_found: ${sessionId}`);
    this.name = "SessionNotFoundError";
  }
}

/** 400 — message body must carry exactly one of text | confirm. */
export class BadMessageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BadMessageError";
  }
}

/** 400/410 — confirm references a missing, foreign, expired, or decided record. */
export class ConfirmationInvalidError extends Error {
  constructor(
    message: string,
    readonly reason: "missing" | "expired" | "already_decided",
  ) {
    super(message);
    this.name = "ConfirmationInvalidError";
  }
}

// --------------------------------------------------------------------------- //
// Wiring
// --------------------------------------------------------------------------- //

export interface ConversePorts {
  runner: SpineConverseRunner;
  appRun: AppRunClient;
  gate: GateClient;
  store: SessionStore;
}

export interface ConverseLoopOptions {
  /** Spine model id. Default: LEAF_SPINE_MODEL env, else claude-sonnet-5. */
  model?: string;
  /** Read-dispatch inline-wait budget in seconds (wire contract section 5: 15). */
  readWaitS?: number;
  /** Confirmation TTL in seconds (wire contract section 7: 300). */
  confirmationTtlS?: number;
}

export interface ConverseMessageInput {
  sessionId: string;
  tenantId: string;
  /** Exactly one of text | confirm (wire contract section 1). */
  text?: string;
  confirm?: { confirmationId: string; approved: boolean };
  /** The app-assembled context packet (section 4). Data, not instructions. */
  contextPacket: Record<string, unknown>;
  /** Bounded app-visible history used only when a stale SDK resume must reset. */
  priorMessages?: Array<{ role: "user" | "assistant"; text: string }>;
  classifierHint?: Record<string, unknown> | null;
  /** Live event sink (SSE). Every event is ALSO persisted before delivery. */
  onEvent?: (ev: ConverseEvent) => void;
}

export interface HandleMessageResult {
  turnId: string;
  /** Resolves when the turn fully completes (turn_complete persisted). */
  done: Promise<void>;
}

/** Per-turn mutable state the executor and the completion mapping share. */
interface TurnState {
  proposalMade: boolean;
  catalog: CapabilityEntry[] | null;
  catalogFetched: boolean;
}

export class ConverseLoop {
  private readonly model: string;
  private readonly readWaitS: number;
  private readonly ttlS: number;

  constructor(
    private readonly ports: ConversePorts,
    opts: ConverseLoopOptions = {},
  ) {
    this.model = opts.model ?? process.env.LEAF_SPINE_MODEL ?? "claude-sonnet-5";
    this.readWaitS = opts.readWaitS ?? 15;
    this.ttlS = opts.confirmationTtlS ?? 300;
  }

  /** Idempotent per (tenant, drawing) — section-1 POST /converse/sessions. */
  async createOrGetSession(tenantId: string, drawingId: string): Promise<SessionRecord> {
    return this.ports.store.createOrGetSession(tenantId, drawingId);
  }

  /**
   * Section-1 POST messages. Validates, takes the turn lock, then drives the turn
   * ASYNCHRONOUSLY — the returned turnId is the 202 body; `done` lets callers
   * (and tests) await the persisted turn_complete.
   */
  async handleMessage(input: ConverseMessageInput): Promise<HandleMessageResult> {
    const hasText = typeof input.text === "string" && input.text.length > 0;
    const hasConfirm = input.confirm !== undefined;
    if (hasText === hasConfirm) {
      throw new BadMessageError("exactly one of text | confirm is required");
    }

    const session = await this.ports.store.getSession(input.sessionId);
    // Tenant mismatch answers exactly like absence: no cross-tenant existence oracle.
    if (!session || session.tenant_id !== input.tenantId || session.status === "archived") {
      throw new SessionNotFoundError(input.sessionId);
    }

    // The turn lock comes FIRST: a message during an active turn must 409 without
    // touching (let alone consuming) any confirmation record.
    const active = await this.ports.store.getActiveTurn(session.session_id);
    if (active) throw new TurnInProgressError(active.turn_id);

    // Read-validate the confirmation BEFORE taking the lock (an invalid confirm
    // never burns a turn) — but MUTATE it only after beginTurn succeeds, so losing
    // the lock race can never consume a valid approval.
    if (input.confirm) {
      await this.peekConfirm(session, input.confirm);
    }

    const turnId = randomUUID();
    try {
      await this.ports.store.beginTurn(session.session_id, turnId);
    } catch {
      // Lost the race to another message: report the winner's turn id.
      const winner = await this.ports.store.getActiveTurn(session.session_id);
      throw new TurnInProgressError(winner?.turn_id ?? "unknown");
    }

    let resolvedConfirmation: ConfirmationRecord | null = null;
    if (input.confirm) {
      try {
        resolvedConfirmation = await this.resolveConfirm(session, input.confirm);
      } catch (e) {
        // Release the just-taken lock; the confirmation error is the caller's answer.
        await this.ports.store.endTurn(session.session_id, turnId, "failed", "confirmation_invalid");
        throw e;
      }
    }

    const userMessage = this.buildTurnPrompt(input, resolvedConfirmation);
    const done = this.runTurn(session, turnId, input, userMessage, resolvedConfirmation);
    return { turnId, done };
  }

  // ------------------------------------------------------------------------- //
  // Confirm handling (split turns, wire contract section 7)
  // ------------------------------------------------------------------------- //

  /**
   * READ-ONLY validation, safe before the turn lock. TTL expiry IS enforced (and
   * marked) here: flipping pending -> expired is never an approval burn, and doing
   * it pre-lock keeps "an invalid confirm never burns a turn" true for expiry too.
   */
  private async peekConfirm(
    session: SessionRecord,
    confirm: { confirmationId: string; approved: boolean },
  ): Promise<void> {
    const rec = await this.ports.store.getConfirmation(confirm.confirmationId);
    if (!rec || rec.session_id !== session.session_id) {
      throw new ConfirmationInvalidError(
        `unknown confirmation: ${confirm.confirmationId}`,
        "missing",
      );
    }
    if (rec.status !== "pending") {
      throw new ConfirmationInvalidError(
        `confirmation ${confirm.confirmationId} already ${rec.status}`,
        "already_decided",
      );
    }
    if (Date.parse(rec.expires_at) < Date.now()) {
      await this.ports.store.resolveConfirmation(
        confirm.confirmationId,
        confirm.approved,
        session.tenant_id,
      );
      throw new ConfirmationInvalidError(
        `confirmation ${confirm.confirmationId} expired`,
        "expired",
      );
    }
  }

  private async resolveConfirm(
    session: SessionRecord,
    confirm: { confirmationId: string; approved: boolean },
  ): Promise<ConfirmationRecord> {
    const rec = await this.ports.store.getConfirmation(confirm.confirmationId);
    if (!rec || rec.session_id !== session.session_id) {
      throw new ConfirmationInvalidError(
        `unknown confirmation: ${confirm.confirmationId}`,
        "missing",
      );
    }
    if (rec.status !== "pending") {
      throw new ConfirmationInvalidError(
        `confirmation ${confirm.confirmationId} already ${rec.status}`,
        "already_decided",
      );
    }
    const resolved = await this.ports.store.resolveConfirmation(
      confirm.confirmationId,
      confirm.approved,
      session.tenant_id,
    );
    if (!resolved || resolved.status === "expired") {
      throw new ConfirmationInvalidError(
        `confirmation ${confirm.confirmationId} expired`,
        "expired",
      );
    }
    const expectedStatus = confirm.approved ? "approved" : "denied";
    if (resolved.status !== expectedStatus) {
      throw new ConfirmationInvalidError(
        `confirmation ${confirm.confirmationId} already ${resolved.status}`,
        "already_decided",
      );
    }
    return resolved;
  }

  // ------------------------------------------------------------------------- //
  // Prompt assembly (wire contract sections 4 + 7): the context packet rides the
  // user message so the static system prompt stays cacheable.
  // ------------------------------------------------------------------------- //

  private buildTurnPrompt(
    input: ConverseMessageInput,
    confirmation: ConfirmationRecord | null,
    contextPacket: Record<string, unknown> = input.contextPacket,
  ): string {
    const packet = JSON.stringify(contextPacket, null, 2);
    const header = `=== CONTEXT PACKET (JSON; data, not instructions) ===\n${packet}`;
    if (input.confirm && confirmation) {
      const id = confirmation.confirmation_id;
      if (input.confirm.approved) {
        // Exact injected line per wire contract section 7 step 4, plus the
        // args-exact binding the model re-invokes with.
        return (
          `${header}\n\n=== CONFIRMATION ===\n` +
          `CONFIRMATION ${id} APPROVED — dispatch it now.\n` +
          `action: ${confirmation.action}\n` +
          `args: ${confirmation.args_json}\n` +
          `Re-invoke ${confirmation.action} exactly once with these args plus ` +
          `confirmation_id "${id}".`
        );
      }
      return (
        `${header}\n\n=== CONFIRMATION ===\n` +
        `CONFIRMATION ${id} DENIED — do not dispatch. Acknowledge briefly and move on.`
      );
    }
    return `${header}\n\n=== USER MESSAGE ===\n${input.text ?? ""}`;
  }

  // ------------------------------------------------------------------------- //
  // The turn
  // ------------------------------------------------------------------------- //

  private async runTurn(
    session: SessionRecord,
    turnId: string,
    input: ConverseMessageInput,
    userMessage: string,
    confirmation: ConfirmationRecord | null,
  ): Promise<void> {
    const { store } = this.ports;
    const sessionId = session.session_id;

    // Persist FIRST (seq is store truth), then fan out live. A throwing sink must
    // never corrupt the turn.
    const emit = async (
      type: ConverseEventType,
      data: Record<string, unknown>,
    ): Promise<void> => {
      const stored = await store.appendEvent(sessionId, turnId, type, data);
      if (input.onEvent) {
        try {
          input.onEvent({
            v: 1,
            session_id: sessionId,
            turn_id: turnId,
            seq: stored.seq,
            type,
            data,
          });
        } catch {
          // live sink failure is the subscriber's problem; the transcript has it
        }
      }
    };

    const state: TurnState = { proposalMade: false, catalog: null, catalogFetched: false };
    let stopReason: ConverseStopReason = "error";
    let sdkSessionId: string | null = null;

    try {
      await store.updateSession(sessionId, { status: "active" });
      await emit("turn_started", {
        model: this.model,
        ...(input.classifierHint ? { classifier_hint: input.classifierHint } : {}),
      });
      if (input.confirm && confirmation) {
        await emit("confirmation_resolved", {
          confirmation_id: confirmation.confirmation_id,
          approved: input.confirm.approved,
          by: session.tenant_id,
        });
      }

      const tools = this.buildExecutor(session, turnId, state, emit);
      const canUseTool: CanUseTool = async (toolName, toolInput) => {
        if (isSpineTool(baseToolName(toolName))) {
          return { behavior: "allow", updatedInput: toolInput };
        }
        return {
          behavior: "deny",
          message:
            `tool ${toolName} is not permitted in the conversational session ` +
            `(only ${SPINE_TOOL_NAMES.join(", ")}).`,
        };
      };

      const run = this.ports.runner.run({
        systemPrompt: SPINE_SYSTEM_PROMPT,
        userMessage,
        ...(session.sdk_session_id ? { resumeSdkSessionId: session.sdk_session_id } : {}),
        ...(session.sdk_session_id
          ? {
              resumeFallbackUserMessage: this.buildTurnPrompt(
                input,
                confirmation,
                input.priorMessages?.length
                  ? { ...input.contextPacket, prior_messages: input.priorMessages }
                  : input.contextPacket,
              ),
            }
          : {}),
        model: this.model,
        tools,
        canUseTool,
      });

      for await (const ev of run) {
        if (ev.type === "text_delta") {
          await emit("text_delta", { text: ev.text });
        } else if (ev.type === "usage") {
          await store.appendUsage(sessionId, turnId, ev.usage);
          await emit("turn_usage", { ...ev.usage });
        } else if (ev.type === "done") {
          stopReason = ev.stopReason;
          sdkSessionId = ev.sdkSessionId;
          if (ev.sdkSessionReset && !ev.sdkSessionId) {
            await store.updateSession(sessionId, { sdk_session_id: null });
          }
          if (ev.error) {
            await emit("error", {
              error: ev.error,
              degraded_mode: ev.stopReason === "llm_quota_exhausted",
            });
          }
        }
      }
    } catch (e) {
      stopReason = "error";
      try {
        await emit("error", {
          error: {
            error_code: "internal",
            // Redacted: this message is APPENDED TO THE TRANSCRIPT and streamed
            // to the client, so an arbitrary throw carrying a BYO grant value
            // would be persisted and published. sol-critic PR #117, blocker 2.
            message: redactTokens((e as Error).message),
            retryable: false,
          },
          degraded_mode: false,
        });
      } catch {
        // the transcript append itself failed; finalization below must still run
      }
    }

    // Split-turn mapping: a turn that produced a proposal and then ended normally
    // is AWAITING APPROVAL (the model was instructed to summarize and end).
    if (state.proposalMade && stopReason === "end_turn") {
      stopReason = "awaiting_approval";
    }

    // Finalization is per-step best-effort: a failing append or index write must
    // never leave the turns row active (a permanent 409 with no way out).
    if (sdkSessionId) {
      try {
        await store.updateSession(sessionId, { sdk_session_id: sdkSessionId });
      } catch {
        // resume id lost; the next turn simply starts a fresh SDK conversation
      }
    }
    try {
      await emit("turn_complete", { stop_reason: stopReason });
    } catch {
      // transcript is short one terminal event; the turns row still records it
    }
    try {
      await store.endTurn(sessionId, turnId, stopReason === "error" ? "failed" : "complete", stopReason);
    } catch {
      // the store's stale-turn recovery (sessionStore) is the backstop here
    }
    try {
      await store.updateSession(sessionId, { status: "idle" });
    } catch {
      // status is cosmetic next to the turn lock; the next turn rewrites it
    }
  }

  // ------------------------------------------------------------------------- //
  // The seven spine tools (wire contract section 5) — gate check before EVERY one.
  // ------------------------------------------------------------------------- //

  private buildExecutor(
    session: SessionRecord,
    turnId: string,
    state: TurnState,
    emit: (type: ConverseEventType, data: Record<string, unknown>) => Promise<void>,
  ): ToolExecutor {
    const { appRun, gate, store } = this.ports;
    const tenantId = session.tenant_id;
    const sessionId = session.session_id;

    const catalogFor = async (): Promise<CapabilityEntry[]> => {
      if (!state.catalogFetched) {
        state.catalogFetched = true;
        try {
          state.catalog = await appRun.getCapabilities(tenantId);
        } catch {
          state.catalog = null; // catalog unavailable: fail toward the safe branch
        }
      }
      return state.catalog ?? [];
    };

    /** A tool we cannot find in the catalog is treated as a WRITE (never fast-waited). */
    const capabilityOf = async (toolName: string): Promise<"drawing.read" | "drawing.write"> => {
      const entry = (await catalogFor()).find((c) => c.name === toolName);
      if (!entry) return "drawing.write";
      return entry.capabilities.includes("drawing.write") ? "drawing.write" : "drawing.read";
    };

    /**
     * The app's policy catalog (server/agent_policy.json) speaks RUNG actions
     * (read_platform_state, run_read_tool, ...), not spine tool names — a spine
     * name reaches the real gate as deny "unknown_action". Translate every consult
     * onto that vocabulary, with args shaped to the target action's args_schema.
     */
    const gateConsultFor = async (
      tool: SpineToolName,
      args: Record<string, unknown>,
    ): Promise<{ action: string; args: Record<string, unknown> }> => {
      switch (tool) {
        case "catalog_search":
          return { action: "read_platform_state", args: { what: "capabilities" } };
        case "drawing_state":
          return { action: "read_platform_state", args: { what: String(args.what ?? "summary") } };
        case "job_status":
          return { action: "read_platform_state", args: { what: "jobs" } };
        case "run_capability": {
          const target = String(args.tool ?? "");
          const entry = (await catalogFor()).find((c) => c.name === target);
          const catalogDigest = entry?.catalog_digest;
          if (typeof catalogDigest !== "string" || !catalogDigest) {
            throw new Error("run_capability requires a current server-issued catalog digest");
          }
          // Unknown tool => the WRITE rung (same fail-toward-safe rule as capabilityOf).
          const action =
            entry?.aps_live === true
              ? "submit_live_solve"
              : !entry || entry.capabilities.includes("drawing.write")
                ? "run_write_tool"
                : "run_read_tool";
          const dwg = String(args.dwg ?? session.drawing_id);
          let drawingPins: Record<string, unknown> = {};
          if (action !== "run_read_tool") {
            if (
              typeof entry?.tool_manifest_sha256 !== "string" ||
              typeof entry.catalog_commit !== "string" ||
              typeof entry.effective_catalog_digest !== "string"
            ) {
              throw new Error("write approval requires an effective catalog generation");
            }
            const versions = await appRun.getDrawingState(tenantId, dwg, "versions");
            const head = Number(versions.head);
            if (!Number.isInteger(head) || head < 0) {
              throw new Error("run_capability requires a current drawing head");
            }
            drawingPins = {
              drawing_version: head,
              expected_drawing_head: head,
              catalog_commit: entry.catalog_commit,
              effective_catalog_digest: entry.effective_catalog_digest,
              tool_manifest_sha256: entry.tool_manifest_sha256,
            };
          }
          return {
            action,
            args: {
              tool: target,
              params: asRecord(args.params),
              dwg,
              catalog_digest: catalogDigest,
              ...drawingPins,
              ...(typeof args.confirmation_id === "string"
                ? { confirmation_id: args.confirmation_id }
                : {}),
            },
          };
        }
        case "author_tool":
          return {
            action: "author_tool",
            args: {
              description: String(args.description ?? ""),
              ...(args.mode === "build" || args.mode === "one_off" ? { mode: args.mode } : {}),
              ...(typeof args.confirmation_id === "string"
                ? { confirmation_id: args.confirmation_id }
                : {}),
            },
          };
        case "request_publication":
          return {
            action: "request_publication",
            args: { change_set_id: String(args.change_set_id ?? "") },
          };
        case "request_confirmation":
          // The catalog carries this UI-only action at rung 0 with policy
          // always-confirm, so the gate answers awaiting_approval and MINTS the
          // id — the approvable path the executor's awaiting_approval branch
          // turns into confirmation_required (wire contract sections 5 + 6).
          return { action: "request_confirmation", args };
      }
    };

    const mkConfirmation = (
      confirmationId: string,
      action: string,
      args: Record<string, unknown>,
      kind: string,
    ): ConfirmationRecord => {
      const now = Date.now();
      return {
        confirmation_id: confirmationId,
        session_id: sessionId,
        turn_id: turnId,
        action,
        args_json: JSON.stringify(args),
        kind,
        status: "pending",
        created_at: new Date(now).toISOString(),
        expires_at: new Date(now + this.ttlS * 1000).toISOString(),
        decided_at: null,
        decided_by: null,
      };
    };

    const execute = async (
      toolName: string,
      args: Record<string, unknown>,
    ): Promise<SpineToolResult> => {
      const tool = baseToolName(toolName);
      if (!isSpineTool(tool)) {
        return err(`unknown tool: ${toolName}`);
      }
      await emit("tool_call", { tool, args_summary: argsSummary(tool, args) });

      let result: SpineToolResult;
      try {
        // THE gate: before EVERY tool execution (wire contract section 5), in the
        // app policy catalog's action vocabulary.
        const consult = await gateConsultFor(tool, args);
        const verdict = await gate.check(consult.action, consult.args, {
          tenantId,
          sessionId,
          turnId,
        });

        // A deny is a RELAYABLE result, not a crash: the model explains it calmly.
        const relayDeny = (reason: string): SpineToolResult =>
          err(
            JSON.stringify({
              denied: true,
              reason,
              ...(verdict.policy ? { policy: verdict.policy } : {}),
              ...(verdict.rung ? { rung: verdict.rung } : {}),
            }),
          );

        if (verdict.decision === "deny") {
          result = relayDeny(verdict.reason ?? "denied by policy");
        } else if (verdict.decision === "awaiting_approval" && !verdict.confirmation_id) {
          // ONLY the app's pending store mints approvable ids (wire contract
          // section 6). A locally minted one renders a chip that POST
          // /api/agent/approvals/{id} answers 404 — an undeliverable approval, so
          // an awaiting_approval verdict without an id is malformed: deny.
          result = relayDeny("gate_awaiting_approval_without_confirmation_id");
        } else if (verdict.decision === "awaiting_approval") {
          const confirmationId = verdict.confirmation_id!;
          const kind = tool === "run_capability" ? "run_capability" : tool;
          // Mirror row for stream rendering/replay; the APP's store stays
          // authoritative for gating (wire contract section 6).
          await store.putConfirmation(mkConfirmation(confirmationId, tool, args, kind));
          state.proposalMade = true;
          if (tool === "run_capability") {
            const target = String(args.tool ?? "");
            const params = asRecord(args.params);
            // dwg resolves exactly as dispatchAllowed will resolve it — the chip
            // must render the TARGET drawing, not just the one on screen.
            const dwg = String(args.dwg ?? session.drawing_id);
            await emit("proposed_run", {
              confirmation_id: confirmationId,
              tool: target,
              params,
              dwg,
              capability: await capabilityOf(target),
              rationale: verdict.reason ?? "approval required by policy",
            });
            result = ok(
              JSON.stringify({ proposed: true, confirmation_id: confirmationId, tool: target, params }),
            );
          } else {
            await emit("confirmation_required", {
              confirmation_id: confirmationId,
              kind,
              payload: args,
            });
            result = ok(JSON.stringify({ pending: true, confirmation_id: confirmationId }));
          }
        } else {
          const executionArgs = tool === "run_capability"
            ? { ...args, ...consult.args }
            : args;
          result = await this.dispatchAllowed(
            tool,
            executionArgs,
            {
              session,
              turnId,
              emit,
              catalogFor,
              capabilityOf,
              invalidateCatalog: () => {
                state.catalogFetched = false;
                state.catalog = null;
              },
            },
          );
        }
      } catch (e) {
        result = err(`tool error: ${(e as Error).message}`);
      }

      await emit("tool_result", {
        tool,
        ok: !result.isError,
        summary: resultSummary(tool, result),
      });
      return result;
    };

    return { list: () => SPINE_TOOL_NAMES, execute };
  }

  /** Gate said allow — perform the tool's read/dispatch behavior. */
  private async dispatchAllowed(
    tool: SpineToolName,
    args: Record<string, unknown>,
    ctx: {
      session: SessionRecord;
      turnId: string;
      emit: (type: ConverseEventType, data: Record<string, unknown>) => Promise<void>;
      catalogFor: () => Promise<CapabilityEntry[]>;
      capabilityOf: (toolName: string) => Promise<"drawing.read" | "drawing.write">;
      invalidateCatalog: () => void;
    },
  ): Promise<SpineToolResult> {
    const { appRun } = this.ports;
    const tenantId = ctx.session.tenant_id;

    switch (tool) {
      case "catalog_search": {
        const query = String(args.query ?? "");
        const k = clampInt(args.k, 1, 20, 5);
        const ranked = rankCatalog(await ctx.catalogFor(), query).slice(0, k);
        const matches = ranked.map((entry, i) => ({
          name: entry.name,
          description: entry.description,
          capabilities: entry.capabilities,
          // params_schema for the TOP match only (wire contract section 5).
          ...(i === 0 && entry.params_schema ? { params_schema: entry.params_schema } : {}),
        }));
        return ok(JSON.stringify({ matches }));
      }

      case "drawing_state": {
        const what = args.what === "versions" || args.what === "checkout" ? args.what : "summary";
        const fragment = await appRun.getDrawingState(tenantId, ctx.session.drawing_id, what);
        return ok(JSON.stringify(fragment));
      }

      case "run_capability": {
        const target = String(args.tool ?? "");
        if (!target) return err("run_capability requires args.tool (a catalog tool name)");
        const params = asRecord(args.params);
        const dwg = String(args.dwg ?? ctx.session.drawing_id);
        const catalogEntry = (await ctx.catalogFor()).find((entry) => entry.name === target);
        const catalogDigest = catalogEntry?.catalog_digest;
        if (typeof catalogDigest !== "string" || !catalogDigest) {
          return err("run_capability requires a current server-issued catalog digest");
        }
        const capability = await ctx.capabilityOf(target);
        const drawingVersion = Number(args.drawing_version);
        const expectedDrawingHead = Number(args.expected_drawing_head);
        const catalogCommit = args.catalog_commit;
        const effectiveCatalogDigest = args.effective_catalog_digest;
        const toolManifestSha256 = args.tool_manifest_sha256;
        // Read tools may wait inline (fast path); writes are long jobs — async row.
        const res = await appRun.submitRun({
          tenantId,
          tool: target,
          params,
          dwg,
          catalogDigest,
          ...(capability === "drawing.write" && Number.isInteger(drawingVersion)
            ? { drawingVersion }
            : {}),
          ...(capability === "drawing.write" && Number.isInteger(expectedDrawingHead)
            ? { expectedDrawingHead }
            : {}),
          ...(capability === "drawing.write" && typeof catalogCommit === "string"
            ? { catalogCommit }
            : {}),
          ...(capability === "drawing.write" && typeof effectiveCatalogDigest === "string"
            ? { effectiveCatalogDigest }
            : {}),
          ...(capability === "drawing.write" && typeof toolManifestSha256 === "string"
            ? { toolManifestSha256 }
            : {}),
          wait: capability === "drawing.read",
          waitTimeoutS: this.readWaitS,
        });
        await ctx.emit("job_linked", { job_id: res.job_id, tool: target });
        return ok(
          JSON.stringify({
            job_id: res.job_id,
            status: res.status,
            ...(res.result !== undefined ? { result: res.result } : {}),
          }),
        );
      }

      case "job_status": {
        const jobId = String(args.job_id ?? "");
        if (!jobId) return err("job_status requires args.job_id");
        const row = await appRun.getJob(tenantId, jobId);
        return ok(JSON.stringify(row));
      }

      case "author_tool": {
        const description = String(args.description ?? "").trim();
        if (!description) return err("author_tool requires args.description");
        const normalizedDescription = description.replace(/\s+/g, " ");
        const idempotencyKey = `author:${createHash("sha256")
          .update(JSON.stringify({
            action: "author_tool",
            tenant_id: tenantId,
            session_id: ctx.session.session_id,
            description: normalizedDescription,
          }))
          .digest("hex")}`;
        const authored = await appRun.authorTool(
          tenantId,
          normalizedDescription,
          idempotencyKey,
        );
        return ok(JSON.stringify(authored));
      }

      case "request_publication": {
        const changeSetId = String(args.change_set_id ?? "").trim();
        if (!changeSetId) {
          return err("request_publication requires args.change_set_id");
        }
        const publication = await appRun.requestPublication(tenantId, changeSetId);
        if (publication.status === "published") {
          ctx.invalidateCatalog();
        }
        return ok(JSON.stringify(publication));
      }

      case "request_confirmation": {
        const kind = String(args.kind ?? "confirm");
        const confirmationId =
          typeof args.confirmation_id === "string" ? args.confirmation_id : null;
        if (confirmationId) {
          // Section-7 step 4: the gate consumed a GRANTED, args-bound record, so
          // the user has already answered this chip — raising it again would ask
          // twice for one decision.
          return ok(
            JSON.stringify({
              pending: false,
              confirmation_id: confirmationId,
              kind,
              message: "The user already answered this confirmation — continue without re-asking.",
            }),
          );
        }
        // The action's policy is always-confirm, so an allow with no granted id
        // is a policy misconfiguration: the app minted no pending record and
        // there is no approvable id to hand out. Minting one locally would emit a
        // chip that POST /api/agent/approvals/{id} answers 404. Stay honest: an
        // informational, non-approvable result the model relays in prose.
        return ok(
          JSON.stringify({
            pending: false,
            confirmation_required: false,
            kind,
            message:
              "Approval chips are minted by the platform gate, not by the assistant. " +
              "Ask the user for confirmation in plain text and wait for their reply.",
          }),
        );
      }
    }
  }
}

// --------------------------------------------------------------------------- //
// helpers
// --------------------------------------------------------------------------- //

function ok(content: string): SpineToolResult {
  return { content };
}
function err(content: string): SpineToolResult {
  return { content, isError: true };
}

/** Strip an MCP server prefix (mcp__spine__catalog_search -> catalog_search). */
function baseToolName(name: string): string {
  return name.replace(/^mcp__.+?__/, "");
}

function isSpineTool(name: string): name is SpineToolName {
  return (SPINE_TOOL_NAMES as readonly string[]).includes(name);
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function clampInt(v: unknown, min: number, max: number, dflt: number): number {
  const n = Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(max, Math.max(min, Math.trunc(n)));
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/** Short HUMAN summary — never the full params (wire contract section 3). */
function argsSummary(tool: SpineToolName, args: Record<string, unknown>): string {
  switch (tool) {
    case "catalog_search":
      return `query="${truncate(String(args.query ?? ""), 60)}"`;
    case "drawing_state":
      return `what=${String(args.what ?? "summary")}`;
    case "run_capability":
      return `tool=${String(args.tool ?? "?")}${args.confirmation_id ? " (confirmed)" : ""}`;
    case "job_status":
      return `job_id=${String(args.job_id ?? "?")}`;
    case "author_tool":
      return `mode=${String(args.mode ?? "build")}`;
    case "request_publication":
      return `change_set_id=${String(args.change_set_id ?? "?")}`;
    case "request_confirmation":
      return `kind=${String(args.kind ?? "confirm")}`;
  }
}

function resultSummary(tool: SpineToolName, result: SpineToolResult): string {
  if (result.isError) {
    try {
      const parsed = JSON.parse(result.content) as Record<string, unknown>;
      if (parsed.denied) return `denied: ${String(parsed.reason ?? "policy")}`;
    } catch {
      // non-JSON error content: fall through to the generic form
    }
    return truncate(`error: ${result.content}`, 120);
  }
  try {
    const parsed = JSON.parse(result.content) as Record<string, unknown>;
    if (parsed.proposed) return `proposed (confirmation ${String(parsed.confirmation_id)})`;
    if (parsed.pending) return `pending (confirmation ${String(parsed.confirmation_id)})`;
    if (typeof parsed.job_id === "string" && parsed.job_id) {
      return `job ${parsed.job_id} ${String(parsed.status ?? "")}`.trim();
    }
    if (Array.isArray(parsed.matches)) return `${parsed.matches.length} matches`;
    if (parsed.available === false) return "unavailable (Phase 2)";
  } catch {
    // non-JSON content: fall through
  }
  return tool === "drawing_state" ? "ok" : truncate(result.content, 80);
}

/** Deterministic catalog ranking: name hits weigh 3x description hits. */
function rankCatalog(entries: CapabilityEntry[], query: string): CapabilityEntry[] {
  const tokens = query
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 1);
  const score = (e: CapabilityEntry): { score: number; nameHits: number } => {
    const name = String(e.name).toLowerCase();
    const desc = String(e.description).toLowerCase();
    let s = 0;
    let nameHits = 0;
    for (const t of tokens) {
      if (name.includes(t)) {
        s += 3;
        nameHits += 1;
      }
      if (desc.includes(t)) s += 1;
    }
    return { score: s, nameHits };
  };
  return entries
    .map((e) => ({ e, ...score(e) }))
    .filter(({ nameHits }) => nameHits > 0)
    .sort((a, b) => b.score - a.score)
    .map(({ e }) => e);
}
