/**
 * Durable escalation approval outbox.
 *
 * The outbox is deliberately separate from the request journal. It binds an
 * approved fleet dispatch to immutable request intent and persists every
 * state change as an append-only JSONL snapshot.
 */

import { randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  writeSync,
} from "node:fs";
import { dirname, join } from "node:path";

export type CapDeathKind = "turn_cap" | "wall_cap";

export interface CapDeathEvidence {
  phrase: string;
  source: "error_text" | "telemetry";
}

/** Classifies only the two editor failure signatures that authorize an offer. */
export function capDeathClassifier(
  errorText: string,
  telemetry?: Record<string, unknown>,
): { kind: CapDeathKind | null; evidence: CapDeathEvidence | null } {
  const text = String(errorText);
  const terminalKind = telemetry?.terminal_kind;
  if (terminalKind === "turn_cap") {
    return { kind: "turn_cap", evidence: { phrase: "turn_cap", source: "telemetry" } };
  }
  if (terminalKind === "wall_cap") {
    return { kind: "wall_cap", evidence: { phrase: "wall_cap", source: "telemetry" } };
  }
  const turnCap = "Claude Code returned an error result: Reached maximum number of turns (40)";
  if (text.includes(turnCap)) {
    return { kind: "turn_cap", evidence: { phrase: turnCap, source: "error_text" } };
  }
  const wallCap = "wall-time cap";
  if (text.includes(wallCap)) {
    return { kind: "wall_cap", evidence: { phrase: wallCap, source: "error_text" } };
  }
  return { kind: null, evidence: null };
}

export type EscalationState =
  | "offered"
  | "approved"
  | "denied"
  | "expired"
  | "dispatch_claimed"
  | "submitted"
  | "result_ready"
  | "merge_pending"
  | "closed_succeeded"
  | "closed_failed";

export interface EscalationEnvelope {
  schema: "mushy.escalation-envelope.v1";
  request_id: string;
  escalation_id: string;
  principal_id: string;
  instruction_sha256: string;
  base_commit: string;
  preserved_ref: string;
  cost_estimate: { low_usd: number; high_usd: number; basis: string };
  budget_usd: number;
  expires_at: string;
  nonce: string;
  created_at: string;
  state: EscalationState;
}

export interface OfferEscalationInput {
  request_id: string;
  principal_id: string;
  instruction_sha256: string;
  base_commit: string;
  preserved_ref: string;
  cost_estimate: EscalationEnvelope["cost_estimate"];
  budget_usd: number;
  expires_at: string;
}

export type ApproveResult =
  | { status: "approved"; envelope: EscalationEnvelope }
  | { status: "reoffered"; envelope: EscalationEnvelope }
  | { status: "unchanged"; envelope: EscalationEnvelope };

interface EscalationEvent {
  schema: "mushy.escalation-event.v1";
  seq: number;
  event_id: string;
  recorded_at: string;
  envelope: EscalationEnvelope;
  claimant?: string;
  actor?: string;
}

interface StoredEscalation {
  envelope: EscalationEnvelope;
  claimant?: string;
}

const ACTIVE_STATES = new Set<EscalationState>([
  "offered", "approved", "dispatch_claimed", "submitted", "result_ready", "merge_pending",
]);

const NEXT: Record<EscalationState, ReadonlySet<EscalationState>> = {
  offered: new Set(["approved", "denied", "expired"]),
  approved: new Set(["dispatch_claimed"]),
  denied: new Set(),
  expired: new Set(),
  dispatch_claimed: new Set(["submitted"]),
  submitted: new Set(["result_ready"]),
  result_ready: new Set(["merge_pending"]),
  merge_pending: new Set(["closed_succeeded", "closed_failed"]),
  closed_succeeded: new Set(),
  closed_failed: new Set(),
};

function assertText(value: string, name: string): string {
  const text = String(value).trim();
  if (!text) throw new Error(`${name} is required`);
  return text;
}

function assertUsd(value: number, name: string): number {
  if (!Number.isFinite(value) || value < 0) throw new Error(`${name} must be a non-negative finite number`);
  return value;
}

function cloneEnvelope(envelope: EscalationEnvelope): EscalationEnvelope {
  return structuredClone(envelope);
}

function isState(value: unknown): value is EscalationState {
  return typeof value === "string" && value in NEXT;
}

function isEvent(value: unknown): value is EscalationEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<EscalationEvent>;
  const envelope = event.envelope;
  return event.schema === "mushy.escalation-event.v1" &&
    Number.isInteger(event.seq) && event.seq! > 0 &&
    typeof event.event_id === "string" && typeof event.recorded_at === "string" &&
    !!envelope && envelope.schema === "mushy.escalation-envelope.v1" &&
    typeof envelope.escalation_id === "string" && typeof envelope.request_id === "string" &&
    typeof envelope.principal_id === "string" && typeof envelope.instruction_sha256 === "string" &&
    typeof envelope.base_commit === "string" && typeof envelope.preserved_ref === "string" &&
    typeof envelope.budget_usd === "number" && typeof envelope.nonce === "string" &&
    typeof envelope.created_at === "string" && typeof envelope.expires_at === "string" &&
    isState(envelope.state);
}

function readEvents(path: string): { events: EscalationEvent[]; repairedText?: string } {
  if (!existsSync(path)) return { events: [] };
  const source = readFileSync(path, "utf8");
  if (!source) return { events: [] };
  const events: EscalationEvent[] = [];
  const validLines: string[] = [];
  let expectedSeq = 1;
  const lines = source.split("\n");
  let repaired = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    if (!line.trim()) continue;
    try {
      const parsed: unknown = JSON.parse(line);
      if (!isEvent(parsed)) throw new Error("invalid escalation outbox event");
      if (parsed.seq !== expectedSeq) throw new Error("escalation outbox sequence mismatch");
      events.push(parsed);
      validLines.push(line);
      expectedSeq += 1;
    } catch (error) {
      const isFinal = lines.slice(index + 1).every((candidate) => !candidate.trim());
      if (!isFinal) throw error;
      repaired = true;
    }
  }
  return { events, ...(repaired ? { repairedText: validLines.length ? `${validLines.join("\n")}\n` : "" } : {}) };
}

function appendLine(path: string, line: string): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const fd = openSync(path, "a", 0o600);
  try {
    writeSync(fd, line, undefined, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export class EscalationOutbox {
  private readonly records = new Map<string, StoredEscalation>();
  private tail: Promise<void> = Promise.resolve();
  private seq = 0;

  private constructor(
    private readonly path: string,
    private readonly now: () => Date,
    private readonly id: () => string,
  ) {}

  static async open(options: {
    dir: string;
    now?: () => Date;
    id?: () => string;
  }): Promise<EscalationOutbox> {
    const outbox = new EscalationOutbox(
      join(options.dir, "escalation-outbox.jsonl"), options.now ?? (() => new Date()), options.id ?? randomUUID,
    );
    const { events, repairedText } = readEvents(outbox.path);
    if (repairedText !== undefined) {
      const fd = openSync(outbox.path, "w", 0o600);
      try {
        writeSync(fd, repairedText, undefined, "utf8");
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
    }
    for (const event of events) outbox.fold(event);
    outbox.seq = events.length;
    return outbox;
  }

  private fold(event: EscalationEvent): void {
    const previous = this.records.get(event.envelope.escalation_id);
    if (previous && !NEXT[previous.envelope.state].has(event.envelope.state) &&
      previous.envelope.state !== event.envelope.state) {
      throw new Error(`invalid escalation transition ${previous.envelope.state} -> ${event.envelope.state}`);
    }
    this.records.set(event.envelope.escalation_id, {
      envelope: cloneEnvelope(event.envelope),
      ...(event.claimant ? { claimant: event.claimant } : {}),
    });
  }

  private mutate<T>(operation: () => T): Promise<T> {
    const result = this.tail.then(operation);
    this.tail = result.then(() => undefined, () => undefined);
    return result;
  }

  private persist(envelope: EscalationEnvelope, detail: { claimant?: string; actor?: string } = {}): void {
    const event: EscalationEvent = {
      schema: "mushy.escalation-event.v1",
      seq: this.seq + 1,
      event_id: this.id(),
      recorded_at: this.now().toISOString(),
      envelope: cloneEnvelope(envelope),
      ...detail,
    };
    appendLine(this.path, `${JSON.stringify(event)}\n`);
    this.seq = event.seq;
    this.fold(event);
  }

  private isExpired(envelope: EscalationEnvelope): boolean {
    return Date.parse(envelope.expires_at) <= this.now().getTime();
  }

  private createOffer(input: OfferEscalationInput): EscalationEnvelope {
    assertText(input.request_id, "request_id");
    assertText(input.principal_id, "principal_id");
    assertText(input.instruction_sha256, "instruction_sha256");
    assertText(input.base_commit, "base_commit");
    assertText(input.preserved_ref, "preserved_ref");
    assertText(input.cost_estimate.basis, "cost_estimate.basis");
    assertUsd(input.cost_estimate.low_usd, "cost_estimate.low_usd");
    assertUsd(input.cost_estimate.high_usd, "cost_estimate.high_usd");
    if (input.cost_estimate.low_usd > input.cost_estimate.high_usd) {
      throw new Error("cost_estimate low_usd cannot exceed high_usd");
    }
    assertUsd(input.budget_usd, "budget_usd");
    if (!Number.isFinite(Date.parse(input.expires_at))) throw new Error("expires_at must be an ISO date");
    return {
      schema: "mushy.escalation-envelope.v1",
      request_id: input.request_id,
      escalation_id: this.id(),
      principal_id: input.principal_id,
      instruction_sha256: input.instruction_sha256,
      base_commit: input.base_commit,
      preserved_ref: input.preserved_ref,
      cost_estimate: structuredClone(input.cost_estimate),
      budget_usd: input.budget_usd,
      expires_at: input.expires_at,
      nonce: this.id(),
      created_at: this.now().toISOString(),
      state: "offered",
    };
  }

  offer(input: OfferEscalationInput): Promise<EscalationEnvelope> {
    return this.mutate(() => {
      const existing = [...this.records.values()].find(({ envelope }) =>
        ACTIVE_STATES.has(envelope.state) && envelope.request_id === input.request_id &&
        envelope.principal_id === input.principal_id &&
        envelope.instruction_sha256 === input.instruction_sha256 && envelope.base_commit === input.base_commit &&
        envelope.preserved_ref === input.preserved_ref && envelope.budget_usd === input.budget_usd);
      if (existing) return cloneEnvelope(existing.envelope);
      const envelope = this.createOffer(input);
      this.persist(envelope);
      return cloneEnvelope(envelope);
    });
  }

  get(escalationId: string): EscalationEnvelope | undefined {
    const stored = this.records.get(assertText(escalationId, "escalation_id"));
    return stored ? cloneEnvelope(stored.envelope) : undefined;
  }

  async approve(escalationId: string, input: { budget_usd: number; nonce: string }): Promise<ApproveResult> {
    return this.mutate(() => {
      const stored = this.require(escalationId);
      const current = stored.envelope;
      if (current.state === "approved" && current.nonce === input.nonce && current.budget_usd === input.budget_usd) {
        return { status: "unchanged", envelope: cloneEnvelope(current) };
      }
      if (current.state !== "offered") throw new Error(`escalation is not offered: ${current.state}`);
      if (this.isExpired(current)) {
        this.persist({ ...current, state: "expired" }, { actor: "system" });
        throw new Error("escalation approval has expired");
      }
      if (current.nonce !== input.nonce) throw new Error("escalation nonce does not match");
      assertUsd(input.budget_usd, "budget_usd");
      if (current.budget_usd !== input.budget_usd) {
        const reoffered = this.createOffer({
          request_id: current.request_id,
          principal_id: current.principal_id,
          instruction_sha256: current.instruction_sha256,
          base_commit: current.base_commit,
          preserved_ref: current.preserved_ref,
          cost_estimate: current.cost_estimate,
          budget_usd: input.budget_usd,
          expires_at: current.expires_at,
        });
        this.persist(reoffered);
        return { status: "reoffered", envelope: cloneEnvelope(reoffered) };
      }
      const approved = { ...current, state: "approved" as const };
      this.persist(approved);
      return { status: "approved", envelope: cloneEnvelope(approved) };
    });
  }

  deny(escalationId: string, actor: string): Promise<EscalationEnvelope> {
    return this.transition(escalationId, "denied", { actor: assertText(actor, "actor") });
  }

  expire(escalationId: string, actor: string): Promise<EscalationEnvelope> {
    return this.transition(escalationId, "expired", { actor: assertText(actor, "actor") });
  }

  claimForDispatch(escalationId: string, claimant: string): Promise<{ envelope: EscalationEnvelope; claimant: string }> {
    return this.mutate(() => {
      const stored = this.require(escalationId);
      const current = stored.envelope;
      if (current.state === "dispatch_claimed") {
        return { envelope: cloneEnvelope(current), claimant: stored.claimant! };
      }
      if (this.isExpired(current) && current.state === "offered") {
        this.persist({ ...current, state: "expired" }, { actor: "system" });
        throw new Error("escalation dispatch has expired");
      }
      if (current.state !== "approved") throw new Error(`escalation cannot dispatch from ${current.state}`);
      if (this.isExpired(current)) throw new Error("escalation dispatch has expired");
      const claimed = { ...current, state: "dispatch_claimed" as const };
      const winningClaimant = assertText(claimant, "claimant");
      this.persist(claimed, { claimant: winningClaimant });
      return { envelope: cloneEnvelope(claimed), claimant: winningClaimant };
    });
  }

  markSubmitted(escalationId: string): Promise<EscalationEnvelope> {
    return this.transition(escalationId, "submitted");
  }

  markResultReady(escalationId: string): Promise<EscalationEnvelope> {
    return this.transition(escalationId, "result_ready");
  }

  markMergePending(escalationId: string): Promise<EscalationEnvelope> {
    return this.transition(escalationId, "merge_pending");
  }

  close(escalationId: string, succeeded: boolean): Promise<EscalationEnvelope> {
    return this.transition(escalationId, succeeded ? "closed_succeeded" : "closed_failed");
  }

  private require(escalationId: string): StoredEscalation {
    const stored = this.records.get(assertText(escalationId, "escalation_id"));
    if (!stored) throw new Error(`escalation not found: ${escalationId}`);
    return stored;
  }

  private transition(
    escalationId: string,
    state: EscalationState,
    detail: { actor?: string } = {},
  ): Promise<EscalationEnvelope> {
    return this.mutate(() => {
      const stored = this.require(escalationId);
      const current = stored.envelope;
      if (current.state === state) return cloneEnvelope(current);
      if (!NEXT[current.state].has(state)) throw new Error(`invalid escalation transition ${current.state} -> ${state}`);
      const updated = { ...current, state };
      this.persist(updated, { ...(stored.claimant ? { claimant: stored.claimant } : {}), ...detail });
      return cloneEnvelope(updated);
    });
  }
}
