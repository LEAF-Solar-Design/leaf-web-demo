/**
 * Token redaction shared by EVERY harness logging site (sol-critic F5 + R5,
 * 2026-07-22): serve.ts's log wrapper, server.ts's request-error line, and the
 * tenant repo provider's worker diagnostics all route through this one pattern,
 * so a token-shaped string never reaches stderr no matter which site renders an
 * arbitrary error.
 */
const rawSplit = String.prototype.split;
const rawJoin = Array.prototype.join;
const rawReplace = String.prototype.replace;
const pristineSplit = (v: string, sep: string): string[] =>
  (rawSplit as unknown as (this: string, sep: string) => string[]).call(v, sep);
const pristineJoin = (v: string[], sep: string): string =>
  (rawJoin as unknown as (this: string[], sep: string) => string).call(v, sep);
const pristineReplace = (v: string, p: RegExp, out: string): string =>
  (rawReplace as unknown as (this: string, p: RegExp, out: string) => string).call(v, p, out);

export const TOKENISH = /\b(sk-ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{40,})\b/g;

export function redactTokens(s: string): string {
  return pristineReplace(s, TOKENISH, "[REDACTED]");
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
  return redactTokens(stripSecrets(s, secrets));
}

/**
 * Literal-only removal of known secrets — NO pattern pass.
 *
 * Use this on USER CONTENT. `redactSecrets` finishes with TOKENISH, which
 * matches any 40+ character alphanumeric run, so a perfectly ordinary prompt
 * mentioning a 40-char Git SHA would have that SHA rewritten to [REDACTED]
 * before the model ever saw it. Corrupting a log line that way is harmless;
 * corrupting the prompt changes what the model is asked. Content therefore gets
 * exact literal removal of values we actually hold, and nothing heuristic.
 * (sol-critic PR #123 round 7, blocker 2.)
 */
export function stripSecrets(s: string, secrets: readonly (string | undefined)[]): string {
  // PRISTINE `split`/`join`, bound at module load. A pluggable AgentRunner runs
  // in this realm and can replace String.prototype.split so this returns the
  // input unchanged while every downstream check reports it clean
  // (sol-critic PR #2 round 22, probed). Binding before any runner executes
  // raises the cost of that specific swap. It does NOT close the class: the
  // binding is still invoked through Function.prototype.call, which is equally
  // replaceable. Process isolation is the control that holds.
  let out = s;
  for (const secret of secrets) {
    if (!isRedactableSecret(secret)) continue;
    out = pristineJoin(pristineSplit(out, secret), "[REDACTED]");
  }
  return out;
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

/**
 * PRINTABLE ASCII, no space. Deliberately the same rule as the server's
 * _CREDENTIAL_CHARS, and deliberately NOT "not whitespace": Python's
 * str.isspace() and JavaScript's \s disagree (U+FEFF is whitespace to \s but
 * not to isspace()), so a credential containing it was ACCEPTED by the app yet
 * treated as unredactable here — accepted but unstrippable is the worst of both.
 * An explicit shared charset removes the ambiguity, and real Agent SDK
 * credentials are long ASCII strings. (sol-critic PR #123 rounds 6-8.)
 */
const PRINTABLE_ASCII = /^[\x21-\x7E]+$/;

/**
 * Any span the system would ACCEPT as a credential, for a telemetry copy only.
 *
 * TOKENISH is not a credential grammar, it is a log heuristic: it needs
 * `sk-ant-` or a 40-character run. The system's own acceptance floor is 24
 * printable-ASCII characters (MIN_REDACTABLE_SECRET_LEN, matching the app's
 * _MIN_CREDENTIAL_LEN), so a 24-to-39 character credential is ACCEPTED
 * everywhere and matched by nothing here.
 *
 * That gap is reachable and it leaked. With a grant resolved, `stripSecrets`
 * removes the value we HOLD; a consumer who pastes a DIFFERENT credential into
 * the prompt ("rotate to <key>") has it survive both the literal strip and
 * TOKENISH, and the fail-closed sweep only searches for held values -- so it
 * reached the operator's queue verbatim. (kimi-adversary PR #2 round 8 as a
 * MINOR, which I under-rated; sol-critic round 13 as a BLOCKER, reproduced.)
 *
 * This pattern is the acceptance grammar itself, so nothing the system would
 * take as a credential can survive it.
 *
 * ONLY EVER FOR THE UPSTREAM TELEMETRY COPY. Applying it to the prompt the
 * model actually reads would rewrite ordinary long tokens -- a 40-character Git
 * SHA, a URL, a base64 blob -- and change what the consumer asked. Corrupting a
 * telemetry field costs nothing; corrupting the live prompt changes the work.
 * See the note on `stripSecrets` for why user content gets literal removal only.
 */
const CREDENTIAL_SHAPED = new RegExp(`[\\x21-\\x7E]{${MIN_REDACTABLE_SECRET_LEN},}`, "g");

/** Does this text contain anything the system would accept as a credential? */
export function containsCredentialShaped(s: string): boolean {
  // A FRESH REGEX PER CALL. CREDENTIAL_SHAPED carries /g, so it is stateful:
  // reusing it across .test() calls advances lastIndex and makes alternate
  // calls return false on identical input. A taint check that says "clean"
  // every other time is worse than no check.
  return new RegExp(CREDENTIAL_SHAPED.source).test(s);
}

export function redactCredentialShaped(s: string): string {
  return pristineReplace(s, CREDENTIAL_SHAPED, "[REDACTED]");
}

export function isRedactableSecret(secret: string | undefined): secret is string {
  return (
    typeof secret === "string" &&
    secret.length >= MIN_REDACTABLE_SECRET_LEN &&
    PRINTABLE_ASCII.test(secret)
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
