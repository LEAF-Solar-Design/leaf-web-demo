/**
 * Inspectable conversation state for bounded parallel requests.
 *
 * Consumers own durable storage. This manager owns live scheduling and projects
 * normalized records plus transcript messages into the standard browser wire
 * contract. Call recordInput() and appendMessage() while hydrating durable data
 * after a restart.
 */

import {
  ParallelRequestScheduler,
  type RequestSchedulerOptions,
  type RequestSnapshot,
  type RequestState,
} from "./requestScheduler.js";

export type ConversationState = RequestState | "interrupted";

export interface ConversationInput {
  id: string;
  prompt: string;
  createdAt?: number;
  kind?: string;
  cancellable?: boolean;
}

export interface ConversationMessage {
  conversationId: string;
  role: string;
  kind: string;
  text: string;
  timestamp: number;
}

export interface ScheduledConversation<Key, Result> extends ConversationInput {
  key: Key;
  run: (signal: AbortSignal) => Promise<Result> | Result;
}

export interface ConversationManagerOptions extends RequestSchedulerOptions {
  conversationHistoryLimit?: number;
  messageLimit?: number;
}

export interface ConversationMessageView {
  role: string;
  kind: string;
  text: string;
  ts: string;
}

export interface ConversationView {
  id: string;
  title: string;
  prompt: string;
  kind: string;
  state: ConversationState;
  active: boolean;
  cancellable: boolean;
  created_at: number;
  started_at?: number;
  finished_at?: number;
  duration_ms?: number;
  messages: ConversationMessageView[];
}

export interface ConversationStateView {
  conversations: ConversationView[];
  active: number;
  max_parallel: number;
}

export interface RequestStateView<Key> {
  requests: RequestSnapshot<Key>[];
  active: number;
  queued: number;
  max_parallel: number;
}

interface ConversationRecord {
  prompt: string;
  createdAt: number;
  kind: string;
  cancellable: boolean;
  scheduled: boolean;
  lastState?: RequestState;
  startedAt?: number;
  finishedAt?: number;
}

const DEFAULT_HISTORY_LIMIT = 100;
const DEFAULT_MESSAGE_LIMIT = 24;
const DEFAULT_MAX_CONCURRENCY = 4;

export class ParallelConversationManager<Key, Result> {
  private readonly scheduler: ParallelRequestScheduler<Key, Result>;
  private readonly maxParallel: number;
  private readonly conversationHistoryLimit: number;
  private readonly messageLimit: number;
  private readonly records = new Map<string, ConversationRecord>();
  private readonly messages = new Map<string, ConversationMessage[]>();
  private readonly cancellationRequested = new Set<string>();

  constructor(options: ConversationManagerOptions = {}) {
    this.maxParallel = positiveInteger(
      options.maxConcurrency ?? DEFAULT_MAX_CONCURRENCY,
      "maxConcurrency",
    );
    this.conversationHistoryLimit = nonNegativeInteger(
      options.conversationHistoryLimit ?? DEFAULT_HISTORY_LIMIT,
      "conversationHistoryLimit",
    );
    this.messageLimit = nonNegativeInteger(
      options.messageLimit ?? DEFAULT_MESSAGE_LIMIT,
      "messageLimit",
    );
    this.scheduler = new ParallelRequestScheduler(options);
  }

  schedule(conversation: ScheduledConversation<Key, Result>): Promise<Result> {
    const existing = this.records.get(conversation.id);
    if (existing?.scheduled || this.scheduler.snapshot(conversation.id)) {
      return Promise.reject(new Error(`Request ID already exists: ${conversation.id}`));
    }
    this.recordInput(conversation);
    const record = this.records.get(conversation.id)!;
    record.kind = conversation.kind ?? record.kind;
    record.cancellable = conversation.cancellable ?? record.cancellable;
    record.scheduled = true;
    const completion = this.scheduler.schedule({
      id: conversation.id,
      key: conversation.key,
      run: conversation.run,
    });
    void completion.then(
      () => this.captureTerminal(conversation.id, "succeeded"),
      () => this.captureTerminal(conversation.id, "failed"),
    );
    return completion;
  }

  enqueue(conversation: ScheduledConversation<Key, Result>): Promise<Result> {
    return this.schedule(conversation);
  }

  recordInput(input: ConversationInput): void {
    const prompt = String(input.prompt);
    const existing = this.records.get(input.id);
    if (existing) {
      if (!existing.prompt && prompt) existing.prompt = prompt;
      return;
    }
    this.records.set(input.id, {
      prompt,
      createdAt: finiteTimestamp(input.createdAt) ?? Date.now(),
      kind: input.kind ?? "conversation",
      cancellable: input.cancellable ?? true,
      scheduled: false,
    });
  }

  appendMessage(message: ConversationMessage): void {
    const normalized: ConversationMessage = {
      conversationId: message.conversationId,
      role: String(message.role),
      kind: String(message.kind),
      text: String(message.text),
      timestamp: finiteTimestamp(message.timestamp) ?? Date.now(),
    };
    const messages = this.messages.get(normalized.conversationId) ?? [];
    messages.push(normalized);
    while (messages.length > this.messageLimit) messages.shift();
    this.messages.set(normalized.conversationId, messages);
    this.trim();
  }

  cancel(id: string, reason?: unknown): boolean {
    const record = this.records.get(id);
    if (record && !record.cancellable) return false;
    const accepted = this.scheduler.cancel(id, reason);
    if (accepted) this.cancellationRequested.add(id);
    return accepted;
  }

  snapshotRequest(id: string): RequestSnapshot<Key> | undefined {
    return this.scheduler.snapshot(id);
  }

  listRequests(): RequestSnapshot<Key>[] {
    return this.scheduler.list();
  }

  requestState(): RequestStateView<Key> {
    const requests = this.listRequests();
    return {
      requests,
      active: requests.filter((request) =>
        request.state === "running" || request.state === "cancelling").length,
      queued: requests.filter((request) => request.state === "queued").length,
      max_parallel: this.maxParallel,
    };
  }

  conversationState(): ConversationStateView {
    const snapshots = this.listRequests();
    const byId = new Map(snapshots.map((request) => [request.id, request]));
    const ids = new Set([...this.records.keys(), ...this.messages.keys(), ...byId.keys()]);
    const conversations = [...ids].map((id) => this.project(id, byId.get(id)));
    conversations.sort((left, right) =>
      activeRank(left.state) - activeRank(right.state) ||
      right.created_at - left.created_at);
    return {
      conversations: conversations.slice(0, this.conversationHistoryLimit),
      active: conversations.filter((conversation) => conversation.active).length,
      max_parallel: this.maxParallel,
    };
  }

  private captureTerminal(id: string, fallbackState: RequestState): void {
    const snapshot = this.scheduler.snapshot(id);
    const record = this.records.get(id);
    if (!record) return;
    record.lastState = snapshot?.state ??
      (fallbackState === "failed" && this.cancellationRequested.has(id)
        ? "cancelled" : fallbackState);
    record.startedAt = snapshot?.startedAt ?? record.startedAt;
    record.finishedAt = snapshot?.finishedAt ?? Date.now();
    this.cancellationRequested.delete(id);
    this.trim();
  }

  private project(id: string, request?: RequestSnapshot<Key>): ConversationView {
    const messages = this.messages.get(id) ?? [];
    const stored = this.records.get(id);
    const firstOperator = messages.find((message) => message.role === "operator");
    const prompt = stored?.prompt || firstOperator?.text || "Conversation";
    const createdAt = request?.queuedAt ?? stored?.createdAt ??
      firstOperator?.timestamp ?? messages[0]?.timestamp ?? Date.now();
    const finishedMessage = [...messages].reverse()
      .find((message) => message.role !== "operator");
    const inferredState: ConversationState = finishedMessage
      ? (finishedMessage.kind === "error" ? "failed" : "succeeded")
      : "interrupted";
    const state = request?.state ?? stored?.lastState ?? inferredState;
    const active = state === "queued" || state === "running" || state === "cancelling";
    const startedAt = request?.startedAt ?? stored?.startedAt;
    const finishedAt = request?.finishedAt ?? stored?.finishedAt ??
      (finishedMessage ? finishedMessage.timestamp : undefined);
    return {
      id,
      title: prompt.replace(/\s+/g, " ").trim().slice(0, 120) || "Conversation",
      prompt: prompt.slice(0, 2000),
      kind: stored?.kind ?? firstOperator?.kind ?? "conversation",
      state,
      active,
      cancellable: active && (stored?.cancellable ?? true),
      created_at: createdAt,
      ...(startedAt === undefined ? {} : { started_at: startedAt }),
      ...(finishedAt === undefined ? {} : {
        finished_at: finishedAt,
        duration_ms: Math.max(0, finishedAt - (startedAt ?? createdAt)),
      }),
      messages: messages.slice(-this.messageLimit).map((message) => ({
        role: message.role,
        kind: message.kind,
        text: message.text,
        ts: new Date(message.timestamp).toISOString(),
      })),
    };
  }

  private trim(): void {
    if (this.conversationHistoryLimit === 0) {
      for (const id of this.records.keys()) {
        if (!this.scheduler.snapshot(id)) {
          this.records.delete(id);
          this.messages.delete(id);
        }
      }
      return;
    }
    const candidates = [...new Set([...this.records.keys(), ...this.messages.keys()])]
      .filter((id) => !this.scheduler.snapshot(id))
      .sort((left, right) => this.createdAt(left) - this.createdAt(right));
    let retained = new Set([...this.records.keys(), ...this.messages.keys()]).size;
    while (retained > this.conversationHistoryLimit && candidates.length) {
      const id = candidates.shift()!;
      this.records.delete(id);
      this.messages.delete(id);
      retained -= 1;
    }
  }

  private createdAt(id: string): number {
    return this.records.get(id)?.createdAt ?? this.messages.get(id)?.[0]?.timestamp ?? 0;
  }
}

function activeRank(state: ConversationState): number {
  if (state === "running" || state === "cancelling") return 0;
  if (state === "queued") return 1;
  return 2;
}

function finiteTimestamp(value: number | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function positiveInteger(value: number, name: string): number {
  if (!Number.isInteger(value) || !Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be a finite positive integer`);
  }
  return value;
}

function nonNegativeInteger(value: number, name: string): number {
  if (!Number.isInteger(value) || !Number.isFinite(value) || value < 0) {
    throw new RangeError(`${name} must be a finite non-negative integer`);
  }
  return value;
}
