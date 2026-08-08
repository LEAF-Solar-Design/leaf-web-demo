// Operator MODEL child env (contract/OPERATOR.md section 6, obligation O1 —
// model half). The operator loop's runner is injectable (operatorLoop.ts): a
// production Agent SDK runner is wired when an operator model grant is
// configured. That runner MUST build the model child's environment HERE, and
// only here — never from the vendored tenant scrub (buildScrubbedEnv /
// scrubSecrets) and never by passing process.env through.
//
// WHY an allowlist, not the tenant scrub: the tenant scrub is a credential-NAME
// DENYLIST. A production deploy credential under an unrecognised name
// (LEAF_LIVE_ACCESS, PROD_AUTHZ) matches none of its patterns and would ride
// into the model process. This builder mirrors the disposable worker
// (operatorWorker/workerManager.ts ENV_ALLOWLIST): it copies ONLY an explicit
// set of benign OS keys from the parent env and injects exactly ONE model
// credential from the grant, so no credential — whatever its name — crosses
// unless a human adds its key to the frozen allowlist and confirms it is safe.
//
// The single injected credential is the honest trusted residual: it is the
// operator model's own auth, provided by the deployment through the grant, NOT
// read from the parent env. No credential key is on the allowlist, so no
// unknown-named credential can ride in under a recognised alias.

/** The EXACT set of parent-env keys allowed to cross into the operator model
 * child. Every key is a benign OS path/locale key that holds no secret. This is
 * an ALLOWLIST-FREEZE: adding any key (recognised as a credential or not) is a
 * reviewed, pinned change (server gate O1 model half). A runner that needs a
 * further operational key adds it here under that review, never by widening the
 * env source. */
export const OPERATOR_MODEL_ENV_ALLOWLIST = [
  "PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
  "PATHEXT", "WINDIR", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
] as const;

/** The one credential the operator MODEL legitimately needs to authenticate.
 * It is provided by the deployment (the operator model grant), injected
 * explicitly, and NEVER read from the parent env — so no credential key is ever
 * allowlisted. */
export interface OperatorModelGrant {
  /** Canonical env key the model SDK reads for auth (e.g. the operator model
   * API key). */
  credentialKey: string;
  credentialValue: string;
}

/**
 * Build the operator model child's environment: allowlisted OS keys from the
 * parent env, the operator-model marker, and exactly the one grant credential.
 * Nothing else crosses. An unknown-named production deploy credential present
 * in `parentEnv` is stripped because its key is not on the allowlist.
 */
export function buildOperatorModelEnv(
  parentEnv: NodeJS.ProcessEnv,
  grant: OperatorModelGrant,
): Record<string, string> {
  const env: Record<string, string> = {};
  for (const key of OPERATOR_MODEL_ENV_ALLOWLIST) {
    const value = parentEnv[key];
    if (value !== undefined) env[key] = value;
  }
  // Policy-aware marker; carries no secret.
  env.LEAF_OPERATOR_MODEL = "1";
  // The single trusted injection. Because it comes from the grant and not from
  // the parent env, the allowlist above holds zero credential keys.
  env[grant.credentialKey] = grant.credentialValue;
  return env;
}
