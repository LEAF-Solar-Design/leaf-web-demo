/**
 * Durable request and conversation journal.
 *
 * The scheduler owns live concurrency. This journal owns restart-safe identity,
 * idempotent admission, explicit state transitions, full prompts/results, and
 * the ordered event feed used by conversation and history projections.
 */

import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeSync,
} from "node:fs";
import { dirname, join } from "node:path";

export type DurableRequestState =
  | "accepted"
  | "queued"
  | "running"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "conflicted";

export type RequestJournalEventType = "state" | "message" | "activity";

export interface RequestIdentity {
  principalId: string;
  conversationId: string;
  requestId: string;
  parentTurnId?: string;
  runnerSessionId?: string;
}

export interface RequestJournalEvent {
  schema: "mushy.request-event.v1";
  seq: number;
  eventId: string;
  recordedAt: string;
  eventType: RequestJournalEventType;
  identity: RequestIdentity;
  attempt: number;
  state?: DurableRequestState;
  payloadSha256?: string;
  prompt?: string;
  role?: string;
  messageKind?: string;
  text?: string;
  result?: unknown;
  error?: string;
  metadata?: Record<string, unknown>;
}

export type RequestJournalEventInput = Omit<
  RequestJournalEvent,
  "schema" | "seq" | "eventId" | "recordedAt"
>;

export interface RequestJournalStore {
  readAll(): Promise<RequestJournalEvent[]>;
  append(event: RequestJournalEventInput): Promise<RequestJournalEvent>;
}

export interface DurableRequestRecord {
  identity: RequestIdentity;
  attempt: number;
  state: DurableRequestState;
  payloadSha256: string;
  prompt: string;
  acceptedAt: string;
  updatedAt: string;
  /** When the first `running` transition was recorded. Together with
   *  finishedAt this makes completed parallel OVERLAP durable, provable
   *  evidence: two requests overlapped iff each started before the other
   *  finished. Absent when the request never ran (e.g. cancelled queued). */
  startedAt?: string;
  /** When the terminal transition was recorded. */
  finishedAt?: string;
  result?: unknown;
  error?: string;
}

export type RequestAdmission =
  | { status: "new"; record: DurableRequestRecord }
  | { status: "active"; record: DurableRequestRecord }
  | { status: "terminal"; record: DurableRequestRecord }
  | { status: "conflict"; reason: "principal" | "payload" | "conversation" };

export interface AdmitRequestInput {
  principalId: string;
  requestId: string;
  prompt: string;
  conversationId?: string;
  parentTurnId?: string;
  runnerSessionId?: string;
}

const TERMINAL = new Set<DurableRequestState>([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "conflicted",
]);
const STATES = new Set<DurableRequestState>([
  "accepted",
  "queued",
  "running",
  "cancelling",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "conflicted",
]);

const NEXT: Record<DurableRequestState, ReadonlySet<DurableRequestState>> = {
  accepted: new Set(["queued", "running", "failed", "cancelled", "interrupted", "conflicted"]),
  queued: new Set(["running", "cancelled", "interrupted", "conflicted"]),
  running: new Set(["cancelling", "succeeded", "failed", "cancelled", "interrupted", "conflicted"]),
  cancelling: new Set(["cancelled", "failed", "interrupted"]),
  succeeded: new Set(),
  failed: new Set(),
  cancelled: new Set(),
  interrupted: new Set(),
  conflicted: new Set(),
};

function payloadHash(prompt: string): string {
  return createHash("sha256").update(prompt, "utf8").digest("hex");
}

function cloneIdentity(identity: RequestIdentity): RequestIdentity {
  return {
    principalId: identity.principalId,
    conversationId: identity.conversationId,
    requestId: identity.requestId,
    ...(identity.parentTurnId ? { parentTurnId: identity.parentTurnId } : {}),
    ...(identity.runnerSessionId ? { runnerSessionId: identity.runnerSessionId } : {}),
  };
}

function assertText(value: string, name: string): string {
  const text = String(value).trim();
  if (!text) throw new Error(`${name} is required`);
  return text;
}

function isTerminal(state: DurableRequestState): boolean {
  return TERMINAL.has(state);
}

function isJournalEvent(value: unknown): value is RequestJournalEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<RequestJournalEvent>;
  return event.schema === "mushy.request-event.v1" &&
    Number.isInteger(event.seq) &&
    typeof event.eventId === "string" &&
    typeof event.recordedAt === "string" &&
    (event.eventType === "state" || event.eventType === "message" || event.eventType === "activity") &&
    !!event.identity &&
    typeof event.identity.requestId === "string" &&
    typeof event.identity.conversationId === "string" &&
    typeof event.identity.principalId === "string" &&
    Number.isInteger(event.attempt) &&
    event.attempt! > 0 &&
    (event.state === undefined || STATES.has(event.state));
}

function writeAtomic(path: string, text: string): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  const fd = openSync(tmp, "wx", 0o600);
  try {
    writeSync(fd, text, undefined, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
}

function readJournal(path: string): { events: RequestJournalEvent[]; repairedText?: string } {
  if (!existsSync(path)) return { events: [] };
  const source = readFileSync(path, "utf8");
  if (!source) return { events: [] };
  const lines = source.split("\n");
  const events: RequestJournalEvent[] = [];
  const validLines: string[] = [];
  let repaired = false;
  let expectedSeq = 1;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    if (!line.trim()) continue;
    try {
      const parsed: unknown = JSON.parse(line);
      if (!isJournalEvent(parsed)) throw new Error("invalid request journal event");
      if (parsed.seq !== expectedSeq) {
        throw new Error(`request journal sequence mismatch: expected ${expectedSeq}, got ${parsed.seq}`);
      }
      events.push(parsed);
      validLines.push(line);
      expectedSeq += 1;
    } catch (error) {
      const isFinal = lines.slice(index + 1).every((candidate) => !candidate.trim());
      const isUnterminated = index === lines.length - 1;
      if (!isFinal || !isUnterminated) throw error;
      repaired = true;
    }
  }
  return {
    events,
    ...(repaired ? { repairedText: validLines.length ? `${validLines.join("\n")}\n` : "" } : {}),
  };
}

/**
 * Zero-dependency durable store for small and reference deployments.
 *
 * A single server process is the writer. The service lifecycle lease enforces
 * that boundary; this class serializes all writes inside that process.
 */
export class FileRequestJournalStore implements RequestJournalStore {
  private readonly path: string;
  private readonly sync: (fd: number) => void;
  private tail: Promise<void> = Promise.resolve();
  private failure: Error | null = null;
  private seq = 0;

  constructor(options: {
    dir: string;
    filename?: string;
    /** Test seam for durability faults. Production uses node:fs fsyncSync. */
    fsync?: (fd: number) => void;
  }) {
    mkdirSync(options.dir, { recursive: true, mode: 0o700 });
    this.path = join(options.dir, options.filename ?? "request-events.jsonl");
    this.sync = options.fsync ?? fsyncSync;
    const loaded = readJournal(this.path);
    this.seq = loaded.events.at(-1)?.seq ?? 0;
    if (loaded.repairedText !== undefined) writeAtomic(this.path, loaded.repairedText);
  }

  async readAll(): Promise<RequestJournalEvent[]> {
    await this.tail;
    if (this.failure) throw this.failure;
    return readJournal(this.path).events;
  }

  append(input: RequestJournalEventInput): Promise<RequestJournalEvent> {
    const operation = this.tail.then(() => {
      if (this.failure) throw this.failure;
      const event = JSON.parse(JSON.stringify({
        schema: "mushy.request-event.v1",
        seq: this.seq + 1,
        eventId: randomUUID(),
        recordedAt: new Date().toISOString(),
        ...input,
        identity: cloneIdentity(input.identity),
      })) as RequestJournalEvent;
      const fd = openSync(this.path, "a", 0o600);
      try {
        writeSync(fd, `${JSON.stringify(event)}\n`, undefined, "utf8");
        this.sync(fd);
      } finally {
        closeSync(fd);
      }
      this.seq = event.seq;
      return event;
    });
    this.tail = operation.then(
      () => undefined,
      (error: unknown) => {
        this.failure = error instanceof Error ? error : new Error(String(error));
      },
    );
    return operation;
  }
}

export class DurableRequestJournal {
  private readonly records = new Map<string, DurableRequestRecord>();
  private events: RequestJournalEvent[] = [];
  private mutationTail: Promise<void> = Promise.resolve();

  private constructor(private readonly store: RequestJournalStore) {}

  static async open(store: RequestJournalStore): Promise<DurableRequestJournal> {
    const journal = new DurableRequestJournal(store);
    journal.events = await store.readAll();
    journal.hydrate();
    return journal;
  }

  private hydrate(): void {
    this.records.clear();
    for (const event of this.events) {
      if (event.eventType !== "state" || !event.state) continue;
      const previous = this.records.get(event.identity.requestId);
      if (!previous) {
        if (event.state !== "accepted" || !event.payloadSha256 || event.prompt === undefined) {
          throw new Error(`request journal starts without accepted event: ${event.identity.requestId}`);
        }
        this.records.set(event.identity.requestId, {
          identity: cloneIdentity(event.identity),
          attempt: event.attempt,
          state: event.state,
          payloadSha256: event.payloadSha256,
          prompt: event.prompt,
          acceptedAt: event.recordedAt,
          updatedAt: event.recordedAt,
          ...(event.result === undefined ? {} : { result: event.result }),
          ...(event.error === undefined ? {} : { error: event.error }),
        });
        continue;
      }
      if (!NEXT[previous.state].has(event.state)) {
        throw new Error(
          `invalid durable request transition ${previous.state} -> ${event.state}: ` +
          event.identity.requestId,
        );
      }
      this.records.set(event.identity.requestId, {
        ...previous,
        identity: cloneIdentity(event.identity),
        attempt: event.attempt,
        state: event.state,
        updatedAt: event.recordedAt,
        ...(event.state === "running" && previous.startedAt === undefined
          ? { startedAt: event.recordedAt } : {}),
        ...(TERMINAL.has(event.state) && previous.finishedAt === undefined
          ? { finishedAt: event.recordedAt } : {}),
        ...(event.result === undefined ? {} : { result: event.result }),
        ...(event.error === undefined ? {} : { error: event.error }),
      });
    }
  }

  admit(input: AdmitRequestInput): Promise<RequestAdmission> {
    return this.enqueueMutation(() => this.admitSerial(input));
  }

  private enqueueMutation<T>(action: () => Promise<T>): Promise<T> {
    const operation = this.mutationTail.then(action);
    this.mutationTail = operation.then(() => undefined, () => undefined);
    return operation;
  }

  private async admitSerial(input: AdmitRequestInput): Promise<RequestAdmission> {
    const principalId = assertText(input.principalId, "principalId");
    const requestId = assertText(input.requestId, "requestId");
    const prompt = String(input.prompt);
    if (!prompt.trim()) throw new Error("prompt is required");
    const conversationId = assertText(input.conversationId ?? requestId, "conversationId");
    const hash = payloadHash(prompt);
    const existing = this.records.get(requestId);
    if (existing) {
      if (existing.identity.principalId !== principalId) {
        return { status: "conflict", reason: "principal" };
      }
      if (existing.payloadSha256 !== hash) {
        return { status: "conflict", reason: "payload" };
      }
      if (existing.identity.conversationId !== conversationId) {
        return { status: "conflict", reason: "conversation" };
      }
      return {
        status: isTerminal(existing.state) ? "terminal" : "active",
        record: existing,
      };
    }
    const identity: RequestIdentity = {
      principalId,
      conversationId,
      requestId,
      ...(input.parentTurnId ? { parentTurnId: input.parentTurnId } : {}),
      ...(input.runnerSessionId ? { runnerSessionId: input.runnerSessionId } : {}),
    };
    const event = await this.store.append({
      eventType: "state",
      identity,
      attempt: 1,
      state: "accepted",
      payloadSha256: hash,
      prompt,
    });
    this.events.push(event);
    const record: DurableRequestRecord = {
      identity,
      attempt: 1,
      state: "accepted",
      payloadSha256: hash,
      prompt,
      acceptedAt: event.recordedAt,
      updatedAt: event.recordedAt,
    };
    this.records.set(requestId, record);
    return { status: "new", record };
  }

  transition(
    requestId: string,
    state: DurableRequestState,
    detail: { result?: unknown; error?: string; metadata?: Record<string, unknown> } = {},
  ): Promise<DurableRequestRecord> {
    return this.enqueueMutation(() => this.transitionSerial(requestId, state, detail));
  }

  private async transitionSerial(
    requestId: string,
    state: DurableRequestState,
    detail: { result?: unknown; error?: string; metadata?: Record<string, unknown> } = {},
  ): Promise<DurableRequestRecord> {
    const current = this.records.get(requestId);
    if (!current) throw new Error(`request not found: ${requestId}`);
    if (current.state === state && isTerminal(state)) return current;
    if (!NEXT[current.state].has(state)) {
      throw new Error(`invalid request transition ${current.state} -> ${state}: ${requestId}`);
    }
    if (state === "succeeded" && detail.result === undefined) {
      throw new Error("succeeded request requires a durable result");
    }
    const event = await this.store.append({
      eventType: "state",
      identity: current.identity,
      attempt: current.attempt,
      state,
      ...(detail.result === undefined ? {} : { result: detail.result }),
      ...(detail.error === undefined ? {} : { error: detail.error }),
      ...(detail.metadata === undefined ? {} : { metadata: detail.metadata }),
    });
    this.events.push(event);
    const updated: DurableRequestRecord = {
      ...current,
      state,
      updatedAt: event.recordedAt,
      ...(state === "running" && current.startedAt === undefined
        ? { startedAt: event.recordedAt } : {}),
      ...(TERMINAL.has(state) && current.finishedAt === undefined
        ? { finishedAt: event.recordedAt } : {}),
      ...(detail.result === undefined ? {} : { result: detail.result }),
      ...(detail.error === undefined ? {} : { error: detail.error }),
    };
    this.records.set(requestId, updated);
    return updated;
  }

  appendMessage(
    requestId: string,
    message: { role: string; kind: string; text: string },
  ): Promise<RequestJournalEvent> {
    return this.enqueueMutation(() => this.appendMessageSerial(requestId, message));
  }

  private async appendMessageSerial(
    requestId: string,
    message: { role: string; kind: string; text: string },
  ): Promise<RequestJournalEvent> {
    const current = this.records.get(requestId);
    if (!current) throw new Error(`request not found: ${requestId}`);
    const event = await this.store.append({
      eventType: "message",
      identity: current.identity,
      attempt: current.attempt,
      role: assertText(message.role, "role"),
      messageKind: assertText(message.kind, "kind"),
      text: String(message.text),
    });
    this.events.push(event);
    return event;
  }

  appendActivity(
    requestId: string,
    metadata: Record<string, unknown>,
  ): Promise<RequestJournalEvent> {
    return this.enqueueMutation(() => this.appendActivitySerial(requestId, metadata));
  }

  private async appendActivitySerial(
    requestId: string,
    metadata: Record<string, unknown>,
  ): Promise<RequestJournalEvent> {
    const current = this.records.get(requestId);
    if (!current) throw new Error(`request not found: ${requestId}`);
    const event = await this.store.append({
      eventType: "activity",
      identity: current.identity,
      attempt: current.attempt,
      metadata,
    });
    this.events.push(event);
    return event;
  }

  async interruptActive(reason = "process restarted before completion"): Promise<number> {
    const active = [...this.records.values()].filter((record) => !isTerminal(record.state));
    for (const record of active) {
      await this.transition(record.identity.requestId, "interrupted", { error: reason });
    }
    return active.length;
  }

  /** @deprecated Request-facing code must use getForPrincipal. */
  get(requestId: string): DurableRequestRecord | undefined {
    const record = this.records.get(requestId);
    return record ? structuredClone(record) : undefined;
  }

  getForPrincipal(principalId: string, requestId: string): DurableRequestRecord | undefined {
    const principal = assertText(principalId, "principalId");
    const record = this.records.get(assertText(requestId, "requestId"));
    return record?.identity.principalId === principal ? structuredClone(record) : undefined;
  }

  /** @deprecated Request-facing code must use listForPrincipal. */
  list(): DurableRequestRecord[] {
    return [...this.records.values()].map((record) => structuredClone(record))
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  }

  listForPrincipal(principalId: string): DurableRequestRecord[] {
    const principal = assertText(principalId, "principalId");
    return [...this.records.values()]
      .filter((record) => record.identity.principalId === principal)
      .map((record) => structuredClone(record))
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  }

  /** @deprecated Request-facing code must use messagesForPrincipal. */
  messages(conversationId: string): RequestJournalEvent[] {
    return this.events.filter((event) =>
      event.eventType === "message" && event.identity.conversationId === conversationId)
      .map((event) => structuredClone(event));
  }

  messagesForPrincipal(principalId: string, conversationId: string): RequestJournalEvent[] {
    const principal = assertText(principalId, "principalId");
    const conversation = assertText(conversationId, "conversationId");
    return this.events.filter((event) =>
      event.eventType === "message" &&
      event.identity.principalId === principal &&
      event.identity.conversationId === conversation)
      .map((event) => structuredClone(event));
  }

  /** @deprecated Request-facing code must use pageForPrincipal. */
  page(afterSeq = 0, limit = 100): { events: RequestJournalEvent[]; nextCursor: number | null } {
    if (!Number.isInteger(afterSeq) || afterSeq < 0) throw new RangeError("afterSeq is invalid");
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError("limit must be between 1 and 500");
    }
    const events = this.events.filter((event) => event.seq > afterSeq).slice(0, limit);
    return {
      events: events.map((event) => structuredClone(event)),
      nextCursor: events.length === limit ? events.at(-1)!.seq : null,
    };
  }

  pageForPrincipal(
    principalId: string,
    afterSeq = 0,
    limit = 100,
  ): { events: RequestJournalEvent[]; nextCursor: number | null } {
    const principal = assertText(principalId, "principalId");
    if (!Number.isInteger(afterSeq) || afterSeq < 0) throw new RangeError("afterSeq is invalid");
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError("limit must be between 1 and 500");
    }
    const events = this.events.filter((event) =>
      event.seq > afterSeq && event.identity.principalId === principal).slice(0, limit);
    return {
      events: events.map((event) => structuredClone(event)),
      nextCursor: events.length === limit ? events.at(-1)!.seq : null,
    };
  }
}
