/**
 * The allowed per-session model ids for the "mount your LLM" lane.
 *
 * A session may choose its model (converse.ts ConverseTurnInput.model), but only
 * from this fixed Claude family — the runner is the Anthropic Claude Agent SDK
 * ONLY (no OpenAI/Gemini provider abstraction here; that is an explicit, separate
 * follow-up). Validation is enforced at the app entry (server/routers/sessions.py)
 * and DEFENSIVELY again on the wire (server.ts validateConverseTurnInput) so a
 * stray/unknown model id never reaches sdk.query().
 *
 * Keep in lockstep with server/turn_runner.py ALLOWED_MODELS.
 */
export const ALLOWED_MODELS = [
  "claude-sonnet-5",
  "claude-opus-4-8",
  "claude-haiku-4-5",
  "claude-fable-5",
] as const;

export type AllowedModel = (typeof ALLOWED_MODELS)[number];

/** True iff `m` is one of the allowed Claude family model ids. */
export function isAllowedModel(m: unknown): m is AllowedModel {
  return typeof m === "string" && (ALLOWED_MODELS as readonly string[]).includes(m);
}
