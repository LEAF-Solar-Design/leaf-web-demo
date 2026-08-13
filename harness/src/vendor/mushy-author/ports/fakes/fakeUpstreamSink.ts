/**
 * FakeUpstreamSink — records captures in memory for hermetic tests.
 * Optional failure mode proves the loop is unobservable to sink errors.
 *
 * capture() receives the exact bytes the real sink would transmit. The fake
 * parses them for convenient assertions, and keeps the raw string so a test
 * can assert on what actually went over the wire.
 */

import type { UpstreamCapture, UpstreamSink } from "../index.js";

export class FakeUpstreamSink implements UpstreamSink {
  readonly captures: UpstreamCapture[] = [];
  readonly bodies: string[] = [];
  failWith: Error | null = null;

  constructor(readonly platform: string | null = null) {}

  async capture(body: string): Promise<void> {
    if (this.failWith) throw this.failWith;
    this.bodies.push(body);
    this.captures.push(JSON.parse(body) as UpstreamCapture);
  }

  /** Await this after an author call: capture is fired without being awaited. */
  async settled(): Promise<void> {
    // Two microtask turns cover the fire-and-forget promise chain.
    await Promise.resolve();
    await Promise.resolve();
  }
}
