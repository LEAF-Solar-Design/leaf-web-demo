/**
 * FakeUpstreamSink — records captures in memory for hermetic tests.
 * Optional failure mode proves the loop is unobservable to sink errors.
 */

import type { UpstreamCapture, UpstreamSink } from "../index.js";

export class FakeUpstreamSink implements UpstreamSink {
  readonly captures: UpstreamCapture[] = [];
  failWith: Error | null = null;

  async capture(event: UpstreamCapture): Promise<void> {
    if (this.failWith) throw this.failWith;
    this.captures.push(event);
  }

  /** Await this after an author call: capture is fired without being awaited. */
  async settled(): Promise<void> {
    // Two microtask turns cover the fire-and-forget promise chain.
    await Promise.resolve();
    await Promise.resolve();
  }
}
