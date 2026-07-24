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
 * The pattern alone is not sufficient for bring-your-own credentials: the app
 * accepts ANY non-empty string as api_key/oauth_token
 * (server/routers/sessions.py _validate_credential_grant), so a short or
 * unusually-shaped token — "short-key!", "hunter2" — does not match TOKENISH and
 * would survive. Wherever the caller actually HOLDS the secret, matching it
 * literally is exact instead of heuristic. (sol-critic PR #117 round 2,
 * blocker 1.)
 *
 * Empty/whitespace-only secrets are ignored so we never replace every empty
 * string in the message. Callers pass the pattern pass as a backstop for
 * secrets they do not hold (e.g. a tenant grant minted elsewhere).
 */
export function redactSecrets(s: string, secrets: readonly (string | undefined)[]): string {
  let out = s;
  for (const secret of secrets) {
    if (!secret || !secret.trim()) continue;
    out = out.split(secret).join("[REDACTED]");
  }
  return redactTokens(out);
}

/**
 * Recursively scrub every STRING LEAF of a structure, leaving shape intact.
 *
 * Do NOT do this by serializing, replacing, and re-parsing: a credential is
 * accepted as any non-empty string, so a secret of `"` would replace every JSON
 * delimiter and make the result unparseable — which silently kills the turn
 * rather than leaking it. Walking the values never touches delimiters and is
 * correct for a secret containing quotes, backslashes, or newlines.
 * (sol-critic PR #117 round 3, blocker 2.)
 */
export function scrubDeep<T>(value: T, secrets: readonly string[]): T {
  if (!secrets.length) return value;
  if (typeof value === "string") return redactSecrets(value, secrets) as unknown as T;
  if (Array.isArray(value)) {
    return value.map((v) => scrubDeep(v, secrets)) as unknown as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = scrubDeep(v, secrets);
    }
    return out as unknown as T;
  }
  return value;
}

/** The secret values carried by a grant, for redactSecrets(). */
export function grantSecrets(
  grant: { kind: "api_key"; apiKey: string } | { kind: "oauth"; oauthToken: string } | null | undefined,
): string[] {
  if (!grant) return [];
  return grant.kind === "api_key" ? [grant.apiKey] : [grant.oauthToken];
}
