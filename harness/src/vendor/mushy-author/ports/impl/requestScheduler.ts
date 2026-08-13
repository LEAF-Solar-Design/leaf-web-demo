/** Bounded FIFO request scheduling with per-key concurrency limits. */

export type RequestState = "queued" | "running" | "cancelling" | "succeeded" | "failed" | "cancelled";

export interface RequestSchedulerOptions {
  maxConcurrency?: number;
  maxConcurrencyPerKey?: number;
  historyLimit?: number;
}

export interface ScheduledRequest<Key, Result> {
  id: string;
  key: Key;
  run: (signal: AbortSignal) => Promise<Result> | Result;
}

export interface RequestSnapshot<Key> {
  id: string;
  key: Key;
  state: RequestState;
  queuedAt: number;
  startedAt?: number;
  finishedAt?: number;
}

interface Record<Key, Result> {
  request: ScheduledRequest<Key, Result>;
  controller: AbortController;
  queuedAt: number;
  resolve: (value: Result) => void;
  reject: (reason: unknown) => void;
  state: RequestState;
  startedAt?: number;
  finishedAt?: number;
}

const DEFAULT_MAX_CONCURRENCY = 4;
const DEFAULT_MAX_CONCURRENCY_PER_KEY = 1;
const DEFAULT_HISTORY_LIMIT = 100;

/**
 * Schedules keyed async work with global and per-key bounds.
 *
 * A cancelled runner that resolves has succeeded: abort is cooperative and
 * the scheduler must not claim cancellation when the work actually completed.
 */
export class ParallelRequestScheduler<Key, Result> {
  private readonly maxConcurrency: number;
  private readonly maxConcurrencyPerKey: number;
  private readonly historyLimit: number;
  private readonly records = new Map<string, Record<Key, Result>>();
  private readonly queue: Record<Key, Result>[] = [];
  private readonly activeByKey = new Map<Key, number>();
  private readonly terminalIds: string[] = [];
  private activeCount = 0;

  constructor(options: RequestSchedulerOptions = {}) {
    this.maxConcurrency = positiveInteger(options.maxConcurrency ?? DEFAULT_MAX_CONCURRENCY, "maxConcurrency");
    this.maxConcurrencyPerKey = positiveInteger(
      options.maxConcurrencyPerKey ?? DEFAULT_MAX_CONCURRENCY_PER_KEY,
      "maxConcurrencyPerKey",
    );
    this.historyLimit = nonNegativeInteger(options.historyLimit ?? DEFAULT_HISTORY_LIMIT, "historyLimit");
  }

  schedule(request: ScheduledRequest<Key, Result>): Promise<Result> {
    if (this.records.has(request.id)) {
      return Promise.reject(new Error(`Request ID already exists: ${request.id}`));
    }
    let resolve!: (value: Result) => void;
    let reject!: (reason: unknown) => void;
    const completion = new Promise<Result>((resolvePromise, rejectPromise) => {
      resolve = resolvePromise;
      reject = rejectPromise;
    });
    const record: Record<Key, Result> = {
      request,
      controller: new AbortController(),
      queuedAt: Date.now(),
      resolve,
      reject,
      state: "queued",
    };
    this.records.set(request.id, record);
    this.queue.push(record);
    this.drain();
    return completion;
  }

  enqueue(request: ScheduledRequest<Key, Result>): Promise<Result> {
    return this.schedule(request);
  }

  /** Cancels queued work immediately, or asks a running runner to stop. */
  cancel(id: string, reason?: unknown): boolean {
    const record = this.records.get(id);
    if (!record || terminal(record.state)) return false;
    if (record.state === "queued") {
      const index = this.queue.indexOf(record);
      if (index !== -1) this.queue.splice(index, 1);
      record.controller.abort(reason);
      this.finishCancelled(record, reason);
      return true;
    }
    if (record.state === "running") {
      record.state = "cancelling";
      record.controller.abort(reason);
      return true;
    }
    return false;
  }

  snapshot(id: string): RequestSnapshot<Key> | undefined {
    const record = this.records.get(id);
    return record ? snapshot(record) : undefined;
  }

  getSnapshot(id: string): RequestSnapshot<Key> | undefined {
    return this.snapshot(id);
  }

  list(): RequestSnapshot<Key>[] {
    return [...this.records.values()].map(snapshot);
  }

  listSnapshots(): RequestSnapshot<Key>[] {
    return this.list();
  }

  private drain(): void {
    while (this.activeCount < this.maxConcurrency) {
      const index = this.queue.findIndex((record) => this.activeFor(record.request.key) < this.maxConcurrencyPerKey);
      if (index === -1) return;
      const [record] = this.queue.splice(index, 1);
      this.start(record);
    }
  }

  private start(record: Record<Key, Result>): void {
    record.state = "running";
    record.startedAt = Date.now();
    this.activeCount += 1;
    this.activeByKey.set(record.request.key, this.activeFor(record.request.key) + 1);
    void Promise.resolve()
      .then(() => record.request.run(record.controller.signal))
      .then((result) => this.finishSucceeded(record, result), (error: unknown) => this.finishRejected(record, error));
  }

  private finishSucceeded(record: Record<Key, Result>, result: Result): void {
    record.state = "succeeded";
    record.finishedAt = Date.now();
    this.release(record);
    record.resolve(result);
  }

  private finishRejected(record: Record<Key, Result>, error: unknown): void {
    record.state = record.controller.signal.aborted ? "cancelled" : "failed";
    record.finishedAt = Date.now();
    this.release(record);
    record.reject(error);
  }

  private finishCancelled(record: Record<Key, Result>, reason?: unknown): void {
    record.state = "cancelled";
    record.finishedAt = Date.now();
    this.retain(record);
    record.reject(abortError(reason));
  }

  private release(record: Record<Key, Result>): void {
    this.activeCount -= 1;
    const activeForKey = this.activeFor(record.request.key) - 1;
    if (activeForKey === 0) this.activeByKey.delete(record.request.key);
    else this.activeByKey.set(record.request.key, activeForKey);
    this.retain(record);
    this.drain();
  }

  private activeFor(key: Key): number {
    return this.activeByKey.get(key) ?? 0;
  }

  private retain(record: Record<Key, Result>): void {
    this.terminalIds.push(record.request.id);
    while (this.terminalIds.length > this.historyLimit) {
      const id = this.terminalIds.shift();
      if (id) this.records.delete(id);
    }
  }
}

function snapshot<Key, Result>(record: Record<Key, Result>): RequestSnapshot<Key> {
  return {
    id: record.request.id,
    key: record.request.key,
    state: record.state,
    queuedAt: record.queuedAt,
    ...(record.startedAt === undefined ? {} : { startedAt: record.startedAt }),
    ...(record.finishedAt === undefined ? {} : { finishedAt: record.finishedAt }),
  };
}

function terminal(state: RequestState): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function positiveInteger(value: number, name: string): number {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value <= 0) {
    throw new RangeError(`${name} must be a finite positive integer`);
  }
  return value;
}

function nonNegativeInteger(value: number, name: string): number {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 0) {
    throw new RangeError(`${name} must be a finite non-negative integer`);
  }
  return value;
}

function abortError(reason: unknown): Error {
  const error = new Error("Request was cancelled", { cause: reason });
  error.name = "AbortError";
  return error;
}
