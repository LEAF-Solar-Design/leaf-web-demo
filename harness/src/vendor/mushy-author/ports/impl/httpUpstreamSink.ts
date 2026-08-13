/**
 * HttpUpstreamSink — pushes captured authoring events to the operator's
 * upstream queue (ops-dashboard POST /api/upstream/ingest).
 *
 * Contract discipline:
 *   - Fire-and-forget: every failure path resolves; a sink outage can never
 *     fail, slow, or throw into the consumer's authoring path.
 *   - Own timeout (default 4s) via AbortController: a hung queue endpoint
 *     cannot hold the process open.
 *   - The bearer token is a machine credential for the OPERATOR's queue —
 *     never a tenant grant, never logged.
 */

import { createHash } from "node:crypto";
import type { UpstreamCapture, UpstreamSink } from "../index.js";

export interface HttpUpstreamSinkOptions {
  /** Full ingest URL, e.g. https://ops.example.com/api/upstream/ingest */
  url: string;
  /** UPSTREAM_API_KEY value on the queue side. */
  token: string;
  /** Label for the host platform this instance is bolted onto. */
  platform?: string;
  timeoutMs?: number;
  /** Test seam: injectable fetch. */
  fetchImpl?: typeof fetch;
}

/**
 * Idempotency key for one authoring INVOCATION.
 *
 * Stable across a retried push of the same invocation, distinct across
 * different invocations -- both halves matter, because the receiving queue is
 * idempotent on this key through a unique index, so anything two invocations
 * share is a row the operator never sees.
 */
export function upstreamDedupeKey(event: UpstreamCapture): string {
  // REFUSE AN ABSENT IDENTITY RATHER THAN SUBSTITUTING ONE. `?? ""` made the
  // key silently fall back to a pure content hash -- exactly the round 8 defect
  // -- for any caller that omitted the field, and the optional type let one
  // compile (sol-critic PR #2 round 18). Every production route mints a UUID,
  // so this throws only for a caller that was already producing colliding keys.
  if (!event.invocation_id) {
    throw new Error("upstreamDedupeKey requires a non-empty invocation_id");
  }
  // NO JSON, AND NO ARRAY THE PROTOTYPE CHAIN CAN TOUCH. This hashed
  // `JSON.stringify([...])` over an ORDINARY array, which inherits from
  // Array.prototype and so from Object.prototype -- so
  // `Object.prototype.toJSON = () => ["forced"]` made two different invocation
  // ids hash identically and the queue collapsed distinct events
  // (sol-critic PR #2 round 20, reproduced). That is the round 14 array defect
  // again, in the one place the capture's null-prototype rebuild does not
  // reach: the key is computed BEFORE that rebuild.
  //
  // LENGTH-PREFIXED FIELDS instead. No stringify means no hook to install, and
  // an explicit length before each field gives a stronger boundary than JSON
  // did -- ["a b","c"] and ["a","b c"] differ by construction rather than by
  // quoting, so the round 9 forgery stays closed.
  // NO ARRAY, NO ITERATOR, NO DISPATCH. `for...of` goes through
  // Array.prototype[Symbol.iterator], which is mutable: replacing it made two
  // different invocation ids hash to the same key (sol-critic PR #2 round 22,
  // probed). Six explicit calls cannot be redirected.
  const hash = createHash("sha256");
  const field = (value: string): void => {
    hash.update(String(value.length));
    hash.update(" ");
    hash.update(value);
  };
  field(event.invocation_id);
  field(event.platform ?? "");
  field(event.consumer);
  field(event.route);
  field(event.prompt ?? "");
  field(event.commit_sha ?? "");
  return hash.digest("hex");
}

export class HttpUpstreamSink implements UpstreamSink {
  // A REAL PRIVATE FIELD, not TypeScript `private`. `private` is erased at
  // compile time, so a same-realm runner that replaced
  // HttpUpstreamSink.prototype.capture received the live sink as `this` and
  // read `this.options.token` straight out -- recovering the operator's queue
  // credential with no guard corruption at all (sol-critic PR #2 round 24,
  // probed). `#options` is enforced by the runtime.
  //
  // AND `capture` IS AN OWN, NON-WRITABLE, BOUND PROPERTY, so replacing it on
  // the prototype no longer intercepts anything: the own property shadows the
  // prototype and cannot be reassigned.
  //
  // THIS REDUCES EXPOSURE. IT DOES NOT MAKE SAME-REALM HOSTILE CODE SAFE, and
  // nothing in this file can. See the boundary note on HarnessPortsBase.
  readonly #options: HttpUpstreamSinkOptions;

  constructor(options: HttpUpstreamSinkOptions) {
    this.#options = options;
    const bound = this.#capture.bind(this);
    Object.defineProperty(this, "capture", {
      value: bound,
      writable: false,
      configurable: false,
      enumerable: false,
    });
  }

  /** The label the caller stamps on events before serializing. */
  get platform(): string | null {
    return this.#options.platform ?? null;
  }

  /**
   * Transmit EXACTLY these bytes. No parse, no spread, no re-stringify: the
   * caller already serialized once and scanned the parsed form of this exact
   * string, and a second serialization here would let a stateful toJSON
   * return clean during the scan and leak on the wire.
   */
  declare readonly capture: (body: string) => Promise<void>;

  async #capture(body: string): Promise<void> {
    const timeoutMs = this.#options.timeoutMs ?? 4000;
    const doFetch = this.#options.fetchImpl ?? fetch;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    // Node keeps the process alive for a pending timer; the sink must never
    // extend the consumer's lifetime.
    if (typeof (timer as { unref?: () => void }).unref === "function") {
      (timer as unknown as { unref(): void }).unref();
    }
    try {
      const response = await doFetch(this.#options.url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.#options.token}`,
        },
        body,
        signal: controller.signal,
      });
      // fetch() resolves as soon as HEADERS arrive, so returning here would
      // leave the RESPONSE BODY ungoverned: a queue that sends headers and
      // then never finishes the body holds a socket open indefinitely, and
      // repeated captures accumulate handles.
      //
      // Tear it down with abort() rather than `await body.cancel()`. cancel()
      // is a promise the STREAM controls, and a conforming implementation may
      // return one that never settles (verified) -- awaiting it would hang
      // capture() forever and leak the very socket this is releasing.
      // abort() is synchronous, unconditional, and owned by us. Nothing here
      // reads the response: capture is best-effort and the reply is unused.
      void response;
      controller.abort();
    } catch {
      // Swallowed by contract: capture is best-effort telemetry, and the
      // authoring path must be unobservable to sink failures.
    } finally {
      clearTimeout(timer);
    }
  }
}

/**
 * Env-configured construction: returns the sink when UPSTREAM_SINK_URL and
 * UPSTREAM_SINK_TOKEN are both present, undefined otherwise (port stays
 * unwired, authoring identical).
 */
export function upstreamSinkFromEnv(
  env: Record<string, string | undefined> = process.env,
): HttpUpstreamSink | undefined {
  const url = env.UPSTREAM_SINK_URL?.trim();
  const token = env.UPSTREAM_SINK_TOKEN?.trim();
  if (!url || !token) return undefined;
  return new HttpUpstreamSink({
    url,
    token,
    platform: env.UPSTREAM_SINK_PLATFORM?.trim() || undefined,
  });
}
