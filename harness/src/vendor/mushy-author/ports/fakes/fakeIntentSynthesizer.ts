/**
 * Scripted IntentSynthesizer — no model, no network. Tests set the verdict (or
 * make it fail) and assert what the turn prompt carried.
 */

import type { IntentSynthesizer, TurnIntent } from "../index.js";

export class FakeIntentSynthesizer implements IntentSynthesizer {
  /** Every message it was asked to classify, for assertions. */
  readonly calls: string[] = [];
  /** Verdict to return. `null` models the fail-open path. */
  next: TurnIntent | null = null;
  /** When set, synthesize REJECTS — the loop must swallow it and continue. */
  throwWith: Error | null = null;

  async synthesize(text: string): Promise<TurnIntent | null> {
    this.calls.push(text);
    if (this.throwWith) throw this.throwWith;
    return this.next;
  }
}
