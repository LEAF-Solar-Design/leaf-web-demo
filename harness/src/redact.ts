/**
 * Token redaction shared by EVERY harness logging site (sol-critic F5 + R5,
 * 2026-07-22): serve.ts's log wrapper, server.ts's request-error line, and the
 * tenant repo provider's worker diagnostics all route through this one pattern,
 * so a token-shaped string never reaches stderr no matter which site renders an
 * arbitrary error.
 */
export const TOKENISH = /\b(sk-ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{40,})\b/g;

export function redactTokens(s: string): string {
  return s.replace(TOKENISH, "[REDACTED]");
}

/**
 * Scrub a KNOWN secret value by literal match, then apply the pattern pass.
 *
 * The pattern alone is not sufficient for bring-your-own credentials. TOKENISH
 * only recognizes `sk-ant-*` or a 40+ character run, so a shorter credential —
 * the app's floor is 24 (server/routers/sessions.py _MIN_CREDENTIAL_LEN) — can
 * clear validation and still slip past the regex. Wherever the caller actually
 * HOLDS the secret, matching it literally is exact instead of heuristic.
 * (sol-critic PR #117 round 2, blocker 1.)
 *
 * Empty/whitespace-only secrets are ignored so we never replace every empty
 * string in the message. Callers pass the pattern pass as a backstop for
 * secrets they do not hold (e.g. a tenant grant minted elsewhere).
 */
export function redactSecrets(s: string, secrets: readonly (string | undefined)[]): string {
  let out = s;
  for (const secret of secrets) {
    if (!isRedactableSecret(secret)) continue;
    out = out.split(secret).join("[REDACTED]");
  }
  return redactTokens(out);
}

/**
 * Only long, whitespace-free values are safe to strip by literal match.
 *
 * A short or common secret ("a", "error") would rewrite ordinary prose and even
 * protocol values — turning a leak into silent transcript corruption. The app
 * already refuses such credentials (server/routers/sessions.py
 * _MIN_CREDENTIAL_LEN), so this is the same floor enforced independently on this
 * side of the wire: a value that slips through some other path is left to the
 * TOKENISH pattern pass rather than being blasted through the transcript.
 * Keep the length in step with the server's floor.
 * (sol-critic PR #117 round 4, blocker 3.)
 */
export const MIN_REDACTABLE_SECRET_LEN = 24;

export function isRedactableSecret(secret: string | undefined): secret is string {
  return (
    typeof secret === "string" &&
    secret.length >= MIN_REDACTABLE_SECRET_LEN &&
    !/\s/.test(secret)
  );
}

/*
 * WHY THERE IS NO DEEP "scrub the whole event graph" HELPER HERE.
 *
 * Two different properties are at stake, and only one of them needs redaction.
 *
 * 1. The credential THIS SYSTEM IS ENTRUSTED WITH must never be logged, echoed,
 *    or persisted. That is structural, not a scrubbing problem: the app
 *    validates the grant and forwards it, the harness maps it into a scrubbed
 *    child env (buildScrubbedEnv), and neither writes it to a transcript, a
 *    database, or a log. The two places it could genuinely escape are covered —
 *    the 422 validation body (server/envelopes.py) and an SDK/Node throw quoting
 *    the env value (converseSdkRunner's run(), which uses redactSecrets above).
 *
 * 2. A credential the USER PASTED INTO THEIR OWN PROMPT is content. That IS
 *    scrubbed, but at the INPUT and nowhere else:
 *      - server/turn_runner.py start_turn, before the transcript append (the app
 *        persists the prompt before the harness is ever called, so this is the
 *        only place that can keep it out of app storage);
 *      - spineTurnAdapter.runTurn, for the tenant's linked grant, whose value
 *        the app never sees.
 *
 * Rounds 2-5 of the PR #117 review tried to do (2) by scrubbing every downstream
 * SINK instead — transcript events, confirmations, the app gate's pending
 * record, usage rows. Each patch created a worse defect than the leak it closed:
 *
 *   - rebuilding objects with redacted KEYS silently dropped a field on
 *     collision, and `__proto__` mutated the rebuilt object's prototype — a
 *     prototype-pollution vector introduced BY the fix;
 *   - scrubbing the confirmation but not the app gate's copy made the two
 *     disagree, so approval replay failed the args hash as `args_mismatch` —
 *     the fix broke the approval flow outright.
 *
 * Scrubbing the source has neither failure mode: every downstream copy derives
 * from the same scrubbed text, so they agree by construction and no hash can
 * mismatch, and only `text` strings are touched, never a key or a structure. Do
 * not reintroduce a sink-side deep scrub.
 */

/** The secret values carried by a grant, for redactSecrets(). */
export function grantSecrets(
  grant: { kind: "api_key"; apiKey: string } | { kind: "oauth"; oauthToken: string } | null | undefined,
): string[] {
  if (!grant) return [];
  return grant.kind === "api_key" ? [grant.apiKey] : [grant.oauthToken];
}
