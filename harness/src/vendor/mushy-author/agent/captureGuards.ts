/**
 * INTERNAL capture guards. Deliberately NOT re-exported from the package
 * barrel: these are an implementation detail of the upstream-capture
 * boundary, and tests import this module directly rather than making them
 * public API (sol-critic PR #2 round 3, finding 4).
 */
import { types as nodeTypes } from "node:util";
import { containsCredentialShaped, isRedactableSecret } from "../redact.js";

/**
 * PRISTINE INTRINSICS, captured at module load.
 *
 * The capture path runs AFTER a pluggable AgentRunner has executed in this
 * realm, and that runner can rewrite `String.prototype.includes`,
 * `Array.prototype.sort`, and anything else these guards call. Round 22 showed
 * three such defeats; round 23 showed the escalation is real, because the
 * runner is handed NO sink reference and NO queue credential, so corrupting
 * these checks buys it authenticated reach to the operator's queue that it did
 * not otherwise have.
 *
 * This module is imported before any runner runs, so binding the methods here
 * captures them before they can be replaced.
 *
 * WHAT THIS IS NOT. It is not a hostile same-realm boundary and does not become
 * one by adding bindings: these are still invoked through
 * `Function.prototype.call`, which is itself replaceable, and a runner can
 * reach the sink object directly regardless (sol-critic PR #2 round 24). Nor
 * does every guard use them -- the capture path still resolves `filter`,
 * `some`, `includes` and `RegExp.prototype.test` at use time, deliberately,
 * because binding them would imply a completeness this cannot have.
 *
 * These bindings RAISE COST on specific demonstrated swaps. Process isolation
 * (the E2B runner) is the control that actually holds. See the boundary note on
 * HarnessPortsBase.
 */
const strIncludes = String.prototype.includes;
const strSplit = String.prototype.split;
const strReplace = String.prototype.replace;
const arrSort = Array.prototype.sort;

type SplitFn = (this: string, sep: string) => string[];
type ReplaceFn = (this: string, pattern: RegExp | string, out: string) => string;

export const intrinsics = {
  includes: (haystack: string, needle: string): boolean =>
    strIncludes.call(haystack, needle),
  split: (value: string, sep: string): string[] =>
    (strSplit as unknown as SplitFn).call(value, sep),
  replace: (value: string, pattern: RegExp | string, out: string): string =>
    (strReplace as unknown as ReplaceFn).call(value, pattern, out),
  sortStrings: (values: string[]): string[] => arrSort.call(values) as string[],
  /**
   * `git status -z` output -> paths, by index, avoiding the array and string
   * transformation helpers a same-realm runner can replace.
   *
   * Each record is `XY <path>` terminated by NUL. Walking the string with
   * charCodeAt and building the result by index avoids `split`, `filter`,
   * `map`, `slice` and `replaceAll` -- every one of which a same-realm runner
   * can replace, and one of which it did (sol-critic PR #2 round 24).
   *
   * STILL NOT DISPATCH-FREE: it calls `status.charCodeAt`, which is equally
   * replaceable. That sits inside the declared out-of-scope threat (a hostile
   * same-realm runner), so it is disclosed rather than chased.
   */
  parseNulSeparatedPaths: (status: string): string[] => {
    const out: string[] = [];
    let start = 0;
    const push = (from: number, to: number): void => {
      if (to <= from) return; // a trailing NUL, not a record
      // NEVER DROP A MALFORMED RECORD. My first version skipped any field of
      // three characters or fewer as "empty", which silently removed a
      // rename's short source name and let the guard see exactly the two
      // expected paths -- accepting a change the previous implementation had
      // rejected. A record we cannot parse must still COUNT, so it fails the
      // comparison rather than disappearing from it (sol-critic round 25).
      let path = "";
      for (let i = from + 3; i < to; i += 1) path += status[i];
      out[out.length] = path;
    };
    for (let i = 0; i < status.length; i += 1) {
      if (status.charCodeAt(i) === 0) {
        push(start, i);
        start = i + 1;
      }
    }
    push(start, status.length);
    return out;
  },
};

/** Native proxy detection: reading this does not run a trap. */
const isProxy = nodeTypes.isProxy;
import type { AgentRunResult } from "../ports/index.js";

/**
 * Normalize the ARTIFACT SUBSET into its scan representation.
 *
 * The scan must run over the SERIALIZED form's own structure, not the live
 * object graph: JSON.stringify consults toJSON (possibly inherited), emits
 * array indices regardless of enumerability, and drops undefined, functions
 * and symbols. A walk of the live graph therefore reports clean on payloads
 * whose serialization carries the secret anyway (sol-critic PR #2 round 3,
 * reproduced).
 *
 * SCAN-ONLY. This helper feeds the artifact-side taint scan and nothing else:
 * `captureUpstream` rebuilds and serializes the complete event separately. An
 * earlier version of this note said it normalized, scanned AND SENT the same
 * value, which described a design this code does not have.
 *
 * Throws on a cycle or a BigInt, which callers treat as unverifiable.
 */
export function normalizeToWire<T>(value: T): T {
  // STRIP THE PROTOTYPE CHAIN FIRST. This serializes RUNNER-SUPPLIED data
  // (`run.code`, `run.tool`) for the taint scan, so an inherited toJSON could
  // answer for it: with `Object.prototype.toJSON = () => ({})` installed, the
  // scan saw an empty object and reported an artifact clean while the real
  // artifact still carried the credential. Probed and confirmed.
  //
  // The GUARANTEE never depended on this -- the fail-closed sweep runs over the
  // actual transmitted bytes, which are stripped, plus a raw substring pass --
  // so a held credential could not reach the wire either way. What the hook
  // defeated is the output-side check this function feeds, which the doc below
  // correctly calls a bonus rather than the guarantee.
  //
  // Fixed anyway, because it is the SAME CLASS as rounds 14 and 20: an ordinary
  // object serialized before anything strips it. Fixing that class in one
  // function twice and leaving a third instance standing is how it kept coming
  // back.
  return JSON.parse(JSON.stringify(stripPrototypes(value))) as T;
}

/**
 * Every string VALUE reachable in a payload, walked rather than serialized.
 *
 * Serializing first and searching the JSON text is unsound: JSON.stringify
 * escapes a backslash, so a credential containing one is no longer present as
 * a literal in the serialized form and a substring search misses it, while a
 * consumer of the transmitted JSON parses the exact secret straight back out
 * (verified). Walking values compares the same bytes the receiver will see.
 *
 * Throws on a cycle, which callers treat as unverifiable rather than clean.
 */
export function stringValues(value: unknown, seen = new Set<object>()): string[] {
  if (typeof value === "string") return [value];
  if (value === null || typeof value !== "object") return [];
  if (seen.has(value as object)) {
    throw new Error("cyclic payload cannot be verified");
  }
  seen.add(value as object);
  const out: string[] = [];
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    // KEYS COUNT. Tool schemas permit arbitrary property names, so a
    // credential used as a property name is serialized onto the wire exactly
    // like a value. Walking Object.values() alone reported such a payload
    // clean while JSON.stringify sent the secret (sol-critic PR #2 round 2,
    // demonstrated by execution).
    if (!Array.isArray(value)) out.push(key);
    out.push(...stringValues(child, seen));
  }
  return out;
}

/**
 * Can we reason about the credentials this invocation held at all?
 *
 * redact.ts only scrubs values it considers redactable (long enough, printable
 * ASCII), but the grant provider accepts weaker tokens. A held value below
 * that bar can be neither scrubbed out of the prompt nor searched for in the
 * artifacts, so no field of the capture can be shown safe. The honest answer
 * is to send nothing rather than to send something unverified.
 */
export function heldSecretsAreVerifiable(held: readonly string[]): boolean {
  return held.every((value) => !value || isRedactableSecret(value));
}

/**
 * Is this invocation's output tainted by a credential it held?
 *
 * TAINT IS DECIDED ON THE INPUT, and that is the point. The prompt reaches the
 * model unchanged, so a consumer who pastes their own grant can have the model
 * echo it back base64-encoded, case-folded, line-wrapped, or split across
 * string literals. No amount of scanning the OUTPUT can catch every such
 * transformation -- that is an arms race with a generative model, and losing
 * it once means shipping a live credential to the operator's queue. So if a
 * held secret appeared in what we fed the model, the model's output is treated
 * as tainted no matter what it looks like.
 *
 * The output is scanned too, for the narrower case where a credential reaches
 * an artifact without passing through the prompt (e.g. read out of the tenant
 * repo). That check is a bonus, not the guarantee.
 *
 * WHAT THIS DOES NOT COVER, stated rather than implied. A credential sitting in
 * the TENANT'S OWN REPOSITORY -- committed by an earlier authoring, or by the
 * tenant -- can be read by the model through `fs_tenant_repo` and preserved in
 * generated code by an innocuous revision like "add logging". It never appears
 * in the description and was never held, so neither check above sees it. There
 * is no heuristic fix: a credential-shaped scan of the output fires on ordinary
 * generated code, and rewriting inside an artifact would misrepresent what the
 * consumer authored (sol-critic PR #2 rounds 15-16, and I reached the same path
 * independently).
 *
 * That gap is INSIDE the contract of `dangerouslyExportModelOutput`, which
 * already states it exports a derivative of every piece of tenant data the
 * model can see to whoever owns the queue. The honest reading is that the flag
 * is safe only where the queue owner is already authorized to read all
 * model-visible tenant data for every tenant served, credentials included. With
 * the flag off -- the default, and what a multi-tenant deployment must use --
 * no artifact is exported at all and the gap is unreachable.
 */
/**
 * Why an invocation's artifacts cannot be exported, or null if they can.
 *
 * A REASON, not a boolean, because the capture reports it to the operator and
 * one label for four different causes is a false statement: an artifact
 * withheld for a credential-shaped prompt was being reported as
 * "held-secret-present", which names a credential the loop never held
 * (sol-critic PR #2 round 19).
 */
export type TaintReason =
  | "held-secret-present"
  | "credential-shaped-input"
  | "unverifiable-artifact";

export function invocationTaintReason(
  description: string,
  run: AgentRunResult | undefined,
  held: readonly string[],
): TaintReason | null {
  // A CREDENTIAL WE NEVER HELD IS STILL A CREDENTIAL. Taint used to be decided
  // purely against the held grant, so with the dangerous opt-in enabled a
  // consumer who pasted a SECOND key had it exported inside the artifacts: not
  // held, so nothing searched for it (sol-critic PR #2 round 15).
  //
  // ON THE INPUT ONLY, and that restriction is load-bearing. A credential the
  // consumer pasted arrives in the DESCRIPTION, so checking it there catches
  // the case and taints the artifacts derived from it.
  //
  // The same grammar must NOT be run over the model's OUTPUT. Generated code is
  // full of 24-plus-character printable runs -- identifiers, URLs, hashes,
  // base64 -- so every successful authoring would taint and the opt-in would
  // never export anything. I tried it and it failed six tests, which is the
  // feature working as designed rather than a test problem. A check that fires
  // on everything protects nothing and costs the whole signal.
  // THE SPECIFIC CAUSE FIRST. A held grant pasted into the prompt is also
  // credential-shaped, so testing the generic rule first labelled a known
  // credential with the vaguer reason -- accurate in the sense of "something
  // credential-shaped was there", useless for telling the operator WHICH rule
  // fired. Name the one we can identify.
  const candidates = held.filter(isRedactableSecret);
  if (candidates.some((secret) => intrinsics.includes(description, secret)))
    return "held-secret-present";
  if (containsCredentialShaped(description)) return "credential-shaped-input";
  if (!run) return null;
  try {
    // Scan the run as the wire would carry it, so a toJSON or a
    // non-enumerable index cannot smuggle the secret past this check.
    const values = stringValues(normalizeToWire({ code: run.code, tool: run.tool }));
    return candidates.some((secret) =>
      values.some((value) => intrinsics.includes(value, secret)),
    )
      ? "held-secret-present"
      : null;
  } catch {
    // Unserializable or cyclic: cannot be proven clean, so treat as tainted.
    return "unverifiable-artifact";
  }
}

/** Boolean view, for callers that only need to know whether to withhold. */
export function invocationIsTainted(
  description: string,
  run: AgentRunResult | undefined,
  held: readonly string[],
): boolean {
  return invocationTaintReason(description, run, held) !== null;
}


/**
 * A stable, allowlisted category for a failure, for the DEFAULT capture.
 *
 * `Error.message` is not safe to export by default: any AgentRunner may throw
 * model-generated text, and the model sees tenant files and broker results as
 * well as the prompt, so a thrown message can carry a transformed credential
 * that no literal scan will match (sol-critic PR #2 round 3, finding 1). The
 * raw message is therefore gated behind the same opt-in as the other model
 * output, and the default carries only one of these fixed strings.
 *
 * The category is carried by the THROWER, never inferred from message text.
 */
export type FailureCategory =
  | "rate_limited"
  | "quota_exhausted"
  | "auth_failure"
  | "validation_failed"
  | "unavailable"
  | "unknown";

const CATEGORIES: readonly FailureCategory[] = [
  "rate_limited",
  "quota_exhausted",
  "auth_failure",
  "validation_failed",
  "unavailable",
  "unknown",
];

/**
 * Brand a first-party error with the category it means.
 *
 * A symbol rather than a string key or a message convention, so the brand
 * cannot be produced by text alone. It is NOT a capability boundary: this
 * symbol and this function are exported, and `AuthorLoopError` is public, so
 * anything able to import the module can brand an error. Trust is decided by
 * ORIGIN instead -- see `failureCategory`.
 */
// A module-private symbol rather than Symbol.for, which keeps it off the global
// registry. That is TIDINESS, NOT A CAPABILITY BOUNDARY: this symbol and
// `withFailureCategory` are both exported, so anything that can import this
// module can brand an error. An earlier version of this note claimed the
// symbol made branding unreachable from outside; it does not, and the retraction
// below is the accurate account.
//
// WHAT ACTUALLY DECIDES TRUST is where the error CAME FROM. Errors crossing the
// AgentRunner boundary are marked untrusted and read as "unknown" regardless of
// any brand they carry -- UNLESS the composition set
// `trustRunnerFailureCategories`, which is an explicit operator statement that
// the wired runner is first-party.
export const FAILURE_CATEGORY = Symbol("mushy.upstream.failure-category");

/**
 * Rebuild a payload with null-prototype objects, dropping any `toJSON`.
 *
 * JSON.stringify consults `toJSON` through the PROTOTYPE CHAIN, so anything
 * sharing this process -- including a pluggable AgentRunner holding the
 * tenant's grant -- can install `Object.prototype.toJSON` and have it inject
 * arbitrary text into a field during the single serialization. The fail-closed
 * sweep only searches for HELD credentials, so an injected value the loop never
 * held (a second key the consumer pasted) passed straight through onto the wire
 * (sol-critic PR #2 round 14, reproduced).
 *
 * Rebuilding on a null prototype means there is no chain to consult. An own
 * `toJSON` is dropped too: on a model-supplied manifest it is not content we
 * owe the operator, and keeping it would hand the hook straight back.
 *
 * Structural, not another scan. Enumerating what a hook might inject is the
 * arms race this boundary keeps losing; removing the hook ends it.
 */
/**
 * One immutable, accessor-free snapshot of a value the runner supplied.
 *
 * `stripPrototypes` copies, but it copies by READING -- `source[i]` and
 * `Object.entries` both invoke getters. A getter-backed graph therefore
 * answered differently on the taint rebuild and the wire rebuild: clean the
 * first time, an unheld credential the second (sol-critic PR #2 round 22, and
 * I reproduced the same split independently before the report arrived).
 *
 * Reading twice is the defect, so this reads ONCE and every later consumer uses
 * the snapshot. Accessors are refused rather than invoked: a property that
 * computes its own value is not data, and the honest response to
 * "this graph cannot be snapshotted" is to fail closed.
 *
 * Returns null for an accessor, a proxy, a boxed primitive, or a cycle.
 *
 * FUNCTIONS ARE HANDLED ASYMMETRICALLY, deliberately. A function-valued OBJECT
 * PROPERTY is dropped and the rest of the object survives -- that is what makes
 * an own `toJSON` disappear while the manifest's real data is kept. A function
 * at the ROOT, or inside an ARRAY, yields null and rejects the WHOLE snapshot
 * -- deliberately conservative, and it does discard meaningful sibling data in
 * that array rather than there being none to keep. Documented rather than
 * unified: rejecting object properties too
 * would refuse every manifest carrying a `toJSON`, which is the case this
 * exists to sanitise rather than refuse.
 */
export function snapshotUntrusted(value: unknown, depth = 0): unknown {
  if (depth > 64) return null;
  if (value === null || typeof value !== "object") {
    return typeof value === "function" ? null : value;
  }
  // A PROXY DEFEATS REFLECTION ITSELF. This function refuses accessors so that
  // no property can execute during snapshotting -- but `getOwnPropertyNames`
  // and `getOwnPropertyDescriptor` INVOKE PROXY TRAPS, so a Proxy-wrapped
  // result ran attacker code inside the very call that was supposed to make
  // execution impossible, and could patch the intrinsics the later redaction
  // uses (sol-critic PR #2 round 23). The claim this function made about itself
  // was false, which is worse than the gap.
  //
  // `util.types.isProxy` is a native check that does not trigger a trap.
  if (isProxy(value)) return null;
  // A BOXED PRIMITIVE IS NOT PLAIN DATA. `new String("x")` has own index
  // properties, so the walk below turned it into {"0":"x","length":1} -- the
  // snapshot MANGLING content, which is exactly what it claims not to do
  // (found by probing my own function, not reported). Refusing is consistent
  // with how accessors and proxies are handled: a shape that cannot be copied
  // faithfully is rejected rather than approximated.
  if (nodeTypes.isBoxedPrimitive(value)) return null;
  if (Array.isArray(value)) {
    const source = value as unknown[];
    const out: unknown[] = [];
    const length = Object.getOwnPropertyDescriptor(source, "length");
    const count = typeof length?.value === "number" ? length.value : 0;
    for (let i = 0; i < count; i += 1) {
      const d = Object.getOwnPropertyDescriptor(source, String(i));
      if (d === undefined) { out[i] = undefined; continue; }
      if (d.get !== undefined || d.set !== undefined) return null;
      const child = snapshotUntrusted(d.value, depth + 1);
      if (child === null && d.value !== null) return null;
      out[i] = child;
    }
    return Object.freeze(out);
  }
  // ORDINARY PROTOTYPES ON PURPOSE. This function has ONE job -- read the graph
  // exactly once and refuse anything that is not plain data -- and stripping
  // prototypes here would be a second, different job that breaks legitimate
  // consumers: `isDeepStrictEqual` compares prototypes, so a null-prototype
  // snapshot stopped matching a manifest parsed normally from disk and failed
  // 14 tests. Hook removal belongs at serialization, where `stripPrototypes`
  // already does it. Own function properties are still dropped below, which is
  // what closes the non-enumerable-toJSON path.
  // ONLY A PLAIN OBJECT IS PLAIN DATA. A `Date`, `Map`, `Set` or `RegExp`
  // carries its whole value in internal slots that no own property exposes, so
  // walking own properties reduced every one of them to `{}` -- silently, and
  // AFTER the snapshot was declared authoritative, so the empty object was
  // what the capture and the HTTP response then reported. Refusing is the same
  // answer already given to a boxed primitive above, for the same reason: this
  // function's contract is refuse-or-copy-faithfully, never approximate
  // (sol-critic PR #2 round 31).
  const proto = Object.getPrototypeOf(value);
  if (proto !== Object.prototype && proto !== null) return null;
  const out = {} as Record<string, unknown>;
  for (const key of Object.getOwnPropertyNames(value)) {
    const d = Object.getOwnPropertyDescriptor(value, key);
    if (d === undefined) continue;
    // AN ACCESSOR IS NOT DATA. Invoking it is what let the graph answer twice.
    if (d.get !== undefined || d.set !== undefined) return null;
    if (typeof d.value === "function") continue;
    const child = snapshotUntrusted(d.value, depth + 1);
    if (child === null && d.value !== null) return null;
    out[key] = child;
  }
  return Object.freeze(out);
}

export function stripPrototypes(value: unknown, path = new Set<object>()): unknown {
  if (value === null || typeof value !== "object") {
    return typeof value === "function" ? undefined : value;
  }
  // THE CURRENT PATH, NOT EVERYTHING SEEN. A set of every visited object
  // rejects a DAG -- the same subobject referenced twice, which a tool manifest
  // does routinely -- as if it were a cycle, and the whole capture is then
  // suppressed. Deleting on the way out means only a true ancestor cycle
  // throws. (kimi-adversary flagged this shape in `stringValues` in round 8;
  // I reproduced it here in the fix for round 14 and caught it by probe.)
  if (path.has(value as object)) throw new Error("cyclic payload cannot be verified");
  path.add(value as object);
  try {
    if (Array.isArray(value)) {
      // NEVER CALL A METHOD OFF THE SUPPLIED VALUE. This used `value.map(...)`,
      // and `map` is a property of the runner's own array: a hostile one
      // overrides it and returns an array carrying its OWN toJSON, so the
      // rebuild handed back exactly the hooked object it was meant to
      // neutralise -- after the code believed the chain was gone. Worse than
      // the original defect, because it looked fixed. The probe produced a wire
      // body carrying an UNHELD secret, which the final sweep does not search
      // for (sol-critic PR #2 round 21).
      //
      // An indexed loop into a fresh array touches no method the caller can
      // define. `length` is still a property read: a plain array's own `length`
      // cannot be a getter, but a Proxy around an array passes `Array.isArray`
      // and can overstate `length` and synthesize indexed values. So this loop
      // is safe only DOWNSTREAM of snapshotUntrusted, which refuses Proxies
      // before any value reaches here; on its own it does not bound what a
      // hostile `length` reports.
      const source = value as unknown[];
      const rebuilt: unknown[] = [];
      for (let i = 0; i < source.length; i += 1) {
        rebuilt[i] = stripPrototypes(source[i], path);
      }
      // AN ARRAY NEEDS ITS PROTOTYPE REMOVED TOO. `map` returns an ordinary
      // Array, which inherits from Array.prototype and therefore from
      // Object.prototype -- so a hook installed there is still consulted for
      // the array itself, and the rebuild leaked exactly the value it was
      // meant to strip. Array.isArray does not read the prototype, so the
      // value still serializes as an array.
      Object.setPrototypeOf(rebuilt, null);
      return rebuilt;
    }
    const out = Object.create(null) as Record<string, unknown>;
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      // A `toJSON` KEY IS ONLY DANGEROUS IF IT IS CALLABLE. Dropping the name
      // outright corrupted legitimate content: a JSON Schema may perfectly well
      // declare `properties.toJSON`, and removing it while `required` still
      // names it hands the operator a manifest that differs from the artifact
      // the consumer actually authored (sol-critic PR #2 round 15). Only a
      // function can be invoked by JSON.stringify, so only a function is
      // dropped -- ordinary data under that name survives intact.
      if (key === "toJSON" && typeof child === "function") continue;
      out[key] = stripPrototypes(child, path);
    }
    return out;
  } finally {
    path.delete(value as object);
  }
}

export function withFailureCategory<E extends object>(
  error: E,
  category: FailureCategory,
): E {
  Object.defineProperty(error, FAILURE_CATEGORY, {
    value: category,
    enumerable: false,
    configurable: false,
    writable: false,
  });
  return error;
}

/**
 * The category of a failure, read from the thrower's own brand.
 *
 * THIS USED TO CLASSIFY BY MATCHING THE MESSAGE TEXT, and that was a real
 * side channel rather than an ugliness. `AgentRunner` is a pluggable port:
 * `buildLegacyAuthOff` hands it the consumer's description, and a runner may
 * throw `new Error(description)`. So a prompt containing "quota", "credential"
 * or "invalid" chose its own category, and since the default capture now sends
 * the category in `error_message`, untrusted text was selecting what the
 * operator's queue recorded. The six outputs were fixed; WHICH one was not.
 * (sol-critic PR #2 round 11, by execution. kimi-adversary flagged the same
 * root cause in round 8 as a doc-comment inaccuracy and I under-rated it.)
 *
 * An unbranded error is `unknown`. So is a BRANDED one that crossed the runner
 * boundary -- unless the composition set `trustRunnerFailureCategories`, which
 * is an explicit operator statement that the wired runner is first-party; then
 * its brand is honoured. The brand is validated against the allowlist on the
 * way out, so even a forged symbol cannot inject free text.
 */
/**
 * Marks an error as having crossed the pluggable AgentRunner boundary.
 *
 * A module-private symbol is not a capability boundary on its own: the runner
 * runs in this process and can import `withFailureCategory` from this very
 * module, or construct a public `AuthorLoopError`, and brand its own throw with
 * whichever category it likes (sol-critic PR #2 round 15). Making the helper
 * unreachable is not possible in-process, so the trust decision moves to WHERE
 * THE ERROR CAME FROM instead of what it claims about itself.
 */
/**
 * Errors that crossed the pluggable AgentRunner boundary.
 *
 * A WEAKSET OWNED BY THIS MODULE, not a property on the error. Storing trust
 * state on the thrown object puts it inside the attacker's reach: a runner can
 * throw a Proxy whose `defineProperty` trap reports success without storing
 * anything and whose `get` trap answers whatever it likes, so the mark simply
 * did not stick and the forged brand was believed (sol-critic PR #2 round 16,
 * probed). Identity cannot be spoofed by a trap.
 */
const untrustedErrors = new WeakSet<object>();

/** Everything thrown by an untrusted runner is untrusted, whatever it says. */
export function markUntrustedOrigin<E>(error: E): E {
  if (typeof error === "object" && error !== null) untrustedErrors.add(error);
  return error;
}

/**
 * The brand an error actually carries, or null when it carries none.
 *
 * FOR REWRAPPING INSIDE A RUNNER, and only there. `failureCategory` answers the
 * question the CAPTURE asks -- "what may I record about this?" -- so it folds an
 * unbranded error, an untrusted origin and a forged brand all into "unknown".
 * That is the right answer downstream and the wrong one for a wrapper, which
 * needs to tell "carried no brand" apart from "carried a brand meaning unknown".
 *
 * Getting that distinction wrong is not cosmetic. `withFailureCategory` defines
 * the property `configurable: false`, so a wrapper that branded every error with
 * `failureCategory(e)` would stamp "unknown" onto errors that carried nothing
 * and PERMANENTLY prevent a later, better-informed brand -- a fresh defineProperty
 * throws. Returning null keeps an unbranded error unbranded.
 *
 * Origin is deliberately NOT consulted here. This is read on the runner's side of
 * the AgentRunner boundary, before `authorLoop` decides trust; the wrapped error
 * that leaves the runner is the object the loop marks, so trust is decided on the
 * same object it has always been decided on.
 */
export function carriedFailureCategory(failure: unknown): FailureCategory | null {
  if (typeof failure !== "object" || failure === null) return null;
  // REFUSE A PROXY BEFORE ANY REFLECTION. `Object.getOwnPropertyDescriptor`
  // INVOKES a Proxy's getOwnPropertyDescriptor trap, so a hostile thrown Proxy
  // runs attacker code inside the very call meant to avoid its getter. The
  // try/catch below is NOT enough: the trap need not throw. It can return a
  // valid "auth_failure" data descriptor while its SIDE EFFECT swaps a global
  // intrinsic -- e.g. `Object.defineProperty` for a leaking stub -- which the
  // `withFailureCategory(wrapped, carried)` the runner then calls executes to
  // re-leak the grant in message and stack (sol-critic PR #9 round 3). Same
  // class as the `snapshotUntrusted` refusal, and fixed the same way: the
  // native `isProxy` check runs no trap (sol-critic PR #2 round 23).
  if (isProxy(failure)) return null;
  // READ THE DESCRIPTOR, NEVER THE PROPERTY. This runs on the runner's raw
  // thrown error, before trust is decided and AFTER redaction, so a bracket
  // read `failure[FAILURE_CATEGORY]` would INVOKE an attacker-planted getter --
  // which can throw to re-leak the grant the redaction just closed over (both
  // message and stack). `withFailureCategory` always defines a plain,
  // non-configurable DATA property, so anything that is not an own data
  // property is not our brand. Fail closed if a descriptor read on a non-Proxy
  // exotic object still throws.
  let branded: unknown;
  try {
    const desc = Object.getOwnPropertyDescriptor(failure, FAILURE_CATEGORY);
    if (!desc || !("value" in desc)) return null;
    branded = desc.value;
  } catch {
    return null;
  }
  return CATEGORIES.includes(branded as FailureCategory)
    ? (branded as FailureCategory)
    : null;
}

export function failureCategory(failure: unknown): FailureCategory {
  if (typeof failure !== "object" || failure === null) return "unknown";
  // ORIGIN BEATS BRAND. Checked first and unconditionally, against a set this
  // module owns, so a runner that brands its own error still lands on
  // "unknown" and a Proxy cannot answer its way out.
  if (untrustedErrors.has(failure as object)) return "unknown";
  return carriedFailureCategory(failure) ?? "unknown";
}

/**
 * The message of a thrown value, read WITHOUT running attacker code.
 *
 * The grant-redaction wrappers on `AgentSdkRunner.run` and `SdkRepoEditor.edit`
 * feed this to `redactSecrets` before anything the thrower controls escapes.
 * The obvious `e instanceof Error ? e.message : String(e)` is UNSAFE on a
 * hostile throw: `AgentRunner` is a pluggable same-realm port, so `e` can be a
 * Proxy whose `get` trap -- or an accessor `message` -- runs during the read
 * and throws a fresh error quoting the grant. That fresh throw escapes the
 * catch BEFORE `redactSecrets` ever sees the text, re-leaking the credential in
 * its message and stack. It is the exact hole `carriedFailureCategory` was
 * hardened against (read the descriptor, never the property), sitting on the
 * line directly above it; `String(e)` is the same hole through `toString` /
 * `Symbol.toPrimitive` (sol-critic PR #9).
 *
 * So: a string passes through; a primitive stringifies (no user code); a Proxy
 * is refused via the native `isProxy` check, which runs no trap; and for an
 * ordinary object we read an OWN DATA-property descriptor for `message`,
 * accepting it only when its value is a string and never invoking a getter.
 * Anything we cannot read as plain data yields "" -- a benign message, with the
 * real category still carried separately by `carriedFailureCategory`. Fail
 * closed on any throw.
 */
export function safeErrorMessage(failure: unknown): string {
  if (typeof failure === "string") return failure;
  if (typeof failure !== "object" || failure === null) {
    try {
      return String(failure);
    } catch {
      return "";
    }
  }
  // A Proxy's get trap / getOwnPropertyDescriptor trap both run attacker code;
  // the native isProxy check runs neither, so refuse before touching it.
  if (isProxy(failure)) return "";
  try {
    const desc = Object.getOwnPropertyDescriptor(failure, "message");
    if (desc && "value" in desc && typeof desc.value === "string") {
      return desc.value;
    }
  } catch {
    return "";
  }
  return "";
}
