/**
 * The author loop: NL prompt -> design-time Agent SDK session edits the tenant
 * repo -> validate -> (build only) register + commit. Plus the design-time-ONLY
 * run path, which dispatches a registered tool via the broker and NEVER touches
 * the AgentRunner / Agent SDK.
 *
 * This module owns the routing rule's behavior; server.ts is a thin HTTP shell
 * over it.
 */

import { createHash, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { isDeepStrictEqual } from "node:util";
import { AUTHOR_SYSTEM_PROMPT } from "./systemPrompt.js";
import { FsTenantRepo } from "./tools/fsTenantRepo.js";
import { makeApsTestRun } from "./tools/apsTestRun.js";
import { submitToolProposal } from "./tools/submitToolProposal.js";
import { validateTool } from "./tools/validateTool.js";
import {
  HARNESS_IDENTITY,
  findTool,
  registerTool,
  REGISTRY_FILE,
} from "../registry/registerTool.js";
import {
  GitRefConflictError,
  TenantChangeRepo,
  type TenantChangeSet,
} from "../ports/impl/tenantChangeRepo.js";
import { scrubSecrets } from "../ports/impl/envScrub.js";
import { redactCredentialShaped, redactSecrets, stripSecrets } from "../redact.js";
import { upstreamDedupeKey } from "../ports/impl/httpUpstreamSink.js";
import {
  failureCategory,
  heldSecretsAreVerifiable,
  intrinsics,
  invocationTaintReason,
  markUntrustedOrigin,
  snapshotUntrusted,
  stringValues,
  stripPrototypes,
  withFailureCategory,
  type FailureCategory,
} from "./captureGuards.js";
import type {
  AuthorResponse,
  AgentRunResult,
  AuthorToolset,
  GrantSettlement,
  HarnessPorts,
  ResultEnvelope,
  StagedCustomizationReceipt,
  TenantMutationFence,
  ToolPackage,
  ToolSourceReceipt,
  UpstreamCapture,
} from "../ports/index.js";

export interface StageCustomizationRequest {
  changeSetId: string;
  expectedBaseSha: string;
  platformRelease: string;
  workspaceContractDigest: string;
  idempotencyKey: string;
  targetToolName?: string;
}

export interface StageCustomizationResponse extends Partial<AuthorResponse> {
  receipt: StagedCustomizationReceipt;
}

export interface StageRemovalRequest extends StageCustomizationRequest {
  toolName: string;
  expectedCatalogDigest: string;
}

function removeExactToolRow(raw: string, toolName: string): string {
  const toolsValues: number[] = [];
  let depth = 0;
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    if (char === '"') {
      const start = index;
      let escaped = false;
      do {
        index += 1;
        const current = raw[index];
        if (escaped) escaped = false;
        else if (current === "\\") escaped = true;
        else if (current === '"') break;
      } while (index < raw.length);
      if (index >= raw.length) throw new AuthorLoopError("tenant registry is malformed", 422);
      if (depth === 1) {
        let after = index + 1;
        while (/\s/.test(raw[after] ?? "")) after += 1;
        if (raw[after] === ":") {
          let name: unknown;
          try { name = JSON.parse(raw.slice(start, index + 1)); }
          catch { throw new AuthorLoopError("tenant registry is malformed", 422); }
          if (name === "tools") {
            after += 1;
            while (/\s/.test(raw[after] ?? "")) after += 1;
            toolsValues.push(after);
          }
        }
      }
      continue;
    }
    if (char === "{" || char === "[") depth += 1;
    else if (char === "}" || char === "]") depth -= 1;
    if (depth < 0) throw new AuthorLoopError("tenant registry is malformed", 422);
  }
  if (depth !== 0 || toolsValues.length !== 1) {
    throw new AuthorLoopError("tenant registry is malformed", 422);
  }
  let cursor = toolsValues[0];
  if (raw[cursor] !== "[") throw new AuthorLoopError("tenant registry is malformed", 422);
  const open = cursor;
  cursor += 1;
  const spans: Array<{ start: number; end: number; value: unknown }> = [];
  while (cursor < raw.length) {
    while (/\s/.test(raw[cursor] ?? "")) cursor += 1;
    if (raw[cursor] === "]") break;
    const start = cursor;
    let depth = 0;
    let quoted = false;
    let escaped = false;
    while (cursor < raw.length) {
      const char = raw[cursor];
      if (quoted) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') quoted = false;
      } else if (char === '"') quoted = true;
      else if (char === "{" || char === "[") depth += 1;
      else if (char === "}" || char === "]") {
        if (depth === 0) break;
        depth -= 1;
      } else if (char === "," && depth === 0) break;
      cursor += 1;
    }
    let end = cursor;
    while (end > start && /\s/.test(raw[end - 1] ?? "")) end -= 1;
    let value: unknown;
    try { value = JSON.parse(raw.slice(start, end)); }
    catch { throw new AuthorLoopError("tenant registry is malformed", 422); }
    spans.push({ start, end, value });
    while (/\s/.test(raw[cursor] ?? "")) cursor += 1;
    if (raw[cursor] === ",") cursor += 1;
    else if (raw[cursor] !== "]") throw new AuthorLoopError("tenant registry is malformed", 422);
  }
  if (raw[cursor] !== "]" || open >= cursor) {
    throw new AuthorLoopError("tenant registry is malformed", 422);
  }
  const matches = spans
    .map((span, index) => ({ span, index }))
    .filter(({ span }) => typeof span.value === "object" && span.value !== null
      && (span.value as { name?: unknown }).name === toolName);
  if (matches.length !== 1) {
    throw new AuthorLoopError("removal target cardinality is not one", 409);
  }
  const { index, span } = matches[0];
  let removeStart = span.start;
  let removeEnd = span.end;
  if (spans.length > 1 && index < spans.length - 1) removeEnd = spans[index + 1].start;
  else if (spans.length > 1) removeStart = spans[index - 1].end;
  return raw.slice(0, removeStart) + raw.slice(removeEnd);
}

export class AuthorLoopError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly diagnostics?: string[],
  ) {
    super(message);
    this.name = "AuthorLoopError";
    // Brand the upstream failure category HERE, from the status this loop
    // chose, so every throw site is covered by construction and none can drift.
    // The category must never be inferred from message text: a pluggable
    // AgentRunner can throw the consumer's own description, which would let
    // untrusted prose pick what the operator's queue records (sol-critic PR #2
    // round 11).
    withFailureCategory(this, statusFailureCategory(status));
  }
}

/** The loop's OWN status vocabulary, which no caller-supplied text can reach. */
function statusFailureCategory(status: number): FailureCategory {
  if (status === 429) return "rate_limited";
  if (status === 402) return "quota_exhausted";
  if (status === 401 || status === 403) return "auth_failure";
  if (status === 503 || status === 504) return "unavailable";
  if (status >= 400 && status < 500) return "validation_failed";
  return "unknown";
}

export function authorGrantSettlement(
  run: AgentRunResult | undefined,
  failure: unknown,
): GrantSettlement {
  const inputTokens = Math.max(0, Math.trunc(run?.telemetry?.input_tokens ?? 0));
  const outputTokens = Math.max(0, Math.trunc(run?.telemetry?.output_tokens ?? 0));
  if (failure === undefined) {
    return { usage: { cost_tokens: inputTokens + outputTokens }, stop_reason: "end_turn" };
  }

  const message = failure instanceof Error ? failure.message : String(failure);
  const rateLimit = message.match(/^Agent SDK rate limited(?: \(retry after ~?(\d+)s\)| \(retry horizon unknown\))?$/);
  if (rateLimit) {
    return {
      usage: { cost_tokens: inputTokens + outputTokens },
      stop_reason: "llm_rate_limited",
      ...(rateLimit[1] ? { retry_after_s: Number(rateLimit[1]) } : {}),
    };
  }
  if (message === "Agent SDK auth failure: billing_error") {
    return { usage: { cost_tokens: inputTokens + outputTokens }, stop_reason: "llm_quota_exhausted" };
  }
  return { usage: { cost_tokens: inputTokens + outputTokens }, stop_reason: "error" };
}

/**
 * One invocation's scrub state. `held` alone cannot distinguish "the grant
 * carried no extractable secret" from "we failed before ever asking for a
 * grant", and those need opposite capture behaviour, so the flag is explicit.
 */
interface ScrubState {
  held: string[];
  grantResolved: boolean;
  /**
   * One authoring invocation makes AT MOST ONE CAPTURE ATTEMPT, and the first
   * attempt wins. Not "exactly one row": this flag is set before the transport
   * runs, and capture is fire-and-forget. An unverifiable held secret or a
   * serialization refusal fails BEFORE anything is sent, so those yield zero
   * rows. A transport failure is different: the server may have committed the
   * row and then lost the response or connection, so delivery is UNKNOWN, not
   * zero -- the client just never learns the outcome. None of the three is
   * retried. What the flag rules out is the second, contradictory row.
   *
   * The success capture fires inside the tenant-repo lease, and that lease
   * rethrows a
   * post-commit cleanup failure -- so a tool that committed cleanly but hit a
   * locked file during resetWorkingTree turned into a rejection and fired a
   * SECOND capture saying "failed". The queue then held two contradictory
   * rows for one prompt, with different dedupe keys so nothing collapsed
   * them (opus-critic PR #2). First capture wins.
   */
  captured: boolean;
  /**
   * Identity of THIS invocation, minted once at the route entry point.
   *
   * The dedupe key exists to make a retried PUSH idempotent, but every other
   * component of it (platform, consumer, route, prompt, commit) is content, so
   * without this the key cannot tell "the same push again" from "a different
   * invocation that happened to look identical" -- and the receiving queue is
   * idempotent on the key through a unique index, so it silently keeps one row
   * and drops the rest. Three reachable collapses, all deterministic:
   *
   *   - two grant-unresolved failures from one tenant: the prompt is withheld
   *     and a failure carries no commit, so DIFFERENT prompts hash identically;
   *   - `oneOff` twice with the same prose: that route never passes a commit;
   *   - any repeat of an identical failed attempt.
   *
   * That destroys the frequency component of the demand signal, which is the
   * whole reason the sink exists. (sol-critic PR #2 round 8 and kimi-adversary,
   * independently, on the same tree; confirmed against the queue's own
   * `ingestCapture` unique index.)
   *
   * It lives on ScrubState, not on the capture call, precisely so a retry of
   * one invocation reuses it while a new invocation cannot.
   */
  invocationId: string;
}

/**
 * A copy of a caller-supplied string array that touches no method it defines.
 *
 * Spreading uses Symbol.iterator and `.sort()` is a property lookup, both of
 * which a hostile array can define, so neither is used here. Indexed reads are
 * not themselves unredirectable -- an index can be an accessor and a Proxy can
 * trap one -- but every array that reaches this helper has already passed
 * `snapshotUntrusted`, which refuses proxies and accessors outright. That
 * refusal is what makes the reads safe; the indexing only avoids re-adding the
 * two hooks the snapshot does not remove (sol-critic PR #2 round 31).
 * Rejecting a non-string element keeps the comparison total.
 */
function safeStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const source = value as unknown[];
  const out: string[] = [];
  for (let i = 0; i < source.length; i += 1) {
    const item = source[i];
    if (typeof item !== "string") return null;
    out[i] = item;
  }
  // PRISTINE sort. Array.prototype.sort is replaceable, and a runner that
  // swaps it can make three different file lists compare equal -- including the
  // guard that decides whether it touched files outside its package, right
  // before `git add -A` (sol-critic PR #2 rounds 22-23, probed).
  intrinsics.sortStrings(out);
  return out;
}

export class AuthorLoop {
  constructor(private readonly ports: HarnessPorts) {}

  /**
   * Best-effort push of one authoring event (prompt + what the consumer
   * authored for themselves, or the failure) to the operator's upstream
   * queue. Fire-and-forget by construction: no await, every error swallowed,
   * so the authoring path is unobservable to sink presence or health.
   */
  private captureUpstream(input: {
    tenantId: string;
    route: UpstreamCapture["route"];
    description: string;
    /**
     * Grant values THIS invocation held, for literal scrubbing (sol-critic
     * PR #1 rounds 1-3: TOKENISH misses short credentials, a process-lived
     * map retains them, and a tenant-keyed map races across concurrent
     * invocations — so each invocation owns its own array, created at the
     * route entry point and garbage-collected with it).
     */
    scrub: ScrubState;
    run?: AgentRunResult;
    commitSha?: string;
    platformRelease?: string;
    failure?: unknown;
  }): void {
    const held = input.scrub.held;
    const sink = this.ports.upstreamSink;
    if (!sink) return;
    // At most one capture ATTEMPT per invocation, first wins; the attempt can
    // still yield no row. See ScrubState.captured.
    if (input.scrub.captured) return;
    input.scrub.captured = true;
    try {
      // Nothing about this capture can be shown safe if we cannot even reason
      // about the credentials we held, so send nothing at all.
      if (!heldSecretsAreVerifiable(held)) return;
      // AND an EMPTY held list is not the same as "nothing to scrub". Every
      // failure BEFORE the grant is acquired leaves it empty, at which point
      // heldSecretsAreVerifiable() is vacuously true, stripSecrets() is the
      // identity, and the fail-closed sweep below has no candidates -- so the
      // raw prompt would ship. The reachable case is the worst one: an
      // unlinked tenant fails grant acquisition on EVERY attempt, which is
      // exactly when a consumer pastes their key in to "set it up".
      // (opus-critic PR #2.) Without a resolved grant we cannot know what to
      // scrub, so the prose does not go at all; consumer, route, status and
      // category still carry the demand signal.
      const promptIsScrubbable = input.scrub.grantResolved === true;
      // Artifacts are the model's OUTPUT and are opt-in (see
      // HarnessPorts.dangerouslyExportModelOutput). The model reads tenant files
      // and broker results as well as the prompt, so a credential can reach
      // its output in an encoding no literal scan will match; the safe
      // default is to send no model output at all. When an operator does opt
      // in, the taint and scan checks below still apply as a best-effort
      // layer -- best effort, not a guarantee, and deliberately labelled so.
      const artifactsAllowed = this.ports.dangerouslyExportModelOutput === true;
      // THE REASON, not just the fact. One label for four causes told the
      // operator "held-secret-present" for an artifact withheld because the
      // PROMPT looked credential-shaped -- naming a credential this loop never
      // held (sol-critic PR #2 round 19).
      const taintReason = artifactsAllowed
        ? invocationTaintReason(input.description, input.run, held)
        : null;
      const tainted = !artifactsAllowed || taintReason !== null;
      const event: UpstreamCapture = {
        contract: "mushy.upstream-capture.v1",
        consumer: input.tenantId,
        platform: null, // caller fills this from sink.platform in the payload rebuild below
        route: input.route,
        // USER PROSE. Literal scrub of what we held, PLUS the TOKENISH
        // pattern. The "never TOKENISH on user content" rule exists because
        // corrupting the PROMPT changes what the model is asked -- but this
        // is a telemetry copy that no model ever reads, so a [REDACTED] git
        // SHA costs nothing here and catches a pasted key we never held.
        // On grant-unresolved the prompt is NOT omitted: the withheld branch
        // below (promptIsScrubbable ? scrubbed : sentinel) still builds this
        // event and sends a fixed sentinel string, because the receiving queue
        // requires the field.
        // WITHHELD STILL NEEDS A PROMPT FIELD, and this is a cross-repo
        // contract, not a style choice. The receiving queue declares
        // `prompt TEXT NOT NULL` and its ingest route answers 400 "prompt is
        // required" for an absent or empty one (ops-dashboard
        // src/app/api/upstream/ingest/route.ts). The sink swallows transport
        // errors by design, so omitting the field entirely made the queue
        // DISCARD exactly the captures this branch exists to report -- the
        // unlinked-tenant setup loop, the most common failure there is -- and
        // nothing anywhere said so. A fixed sentinel carries no consumer
        // bytes, satisfies the column, and reads on the operator's panel as
        // what it is; `prompt_withheld` still states the reason in full.
        ...(promptIsScrubbable
          ? {
              // THREE LAYERS, and the third is the one that was missing.
              // stripSecrets removes the grant we HOLD, exactly. redactSecrets
              // adds the TOKENISH log heuristic. Neither covers a DIFFERENT
              // credential the consumer pasted in ("rotate to <key>"): it is
              // not held, so the literal strip and the fail-closed sweep both
              // ignore it, and TOKENISH needs 40 characters or an sk-ant-
              // prefix while the system ACCEPTS 24. It reached the queue
              // verbatim (kimi-adversary round 8 as a MINOR I under-rated;
              // sol-critic round 13 as a BLOCKER, reproduced).
              //
              // redactCredentialShaped is the acceptance grammar itself, so
              // nothing the system would take as a credential survives. Safe
              // here and nowhere else: this is a telemetry copy no model reads,
              // so rewriting a long Git SHA costs nothing.
              prompt: redactCredentialShaped(
                redactSecrets(stripSecrets(input.description, held), held),
              ),
            }
          : {
              prompt: "[prompt withheld: no grant resolved, nothing to scrub against]",
              prompt_withheld: "grant-unresolved" as const,
            }),
        authoring_status: input.failure === undefined ? "authored" : "failed",
        // AUTHORED ARTIFACTS, dropped WHOLE when the invocation is tainted.
        // Rewriting them in place would be worse than dropping them: generated
        // code is consumed as an exact artifact downstream, so editing bytes
        // inside it yields a capture that misrepresents what the consumer
        // authored. A capture missing its code is a smaller loss than a leaked
        // grant, and the prompt + status still carry the platform signal.
        ...(input.run && !tainted
          ? {
              tool_name: input.run.tool.name,
              tool_manifest: input.run.tool,
              tool_code: input.run.code,
              ...(input.run.telemetry ? { telemetry: input.run.telemetry } : {}),
            }
          : {}),
        ...(input.run && tainted
          ? {
              artifacts_withheld: artifactsAllowed
                ? (taintReason ?? "unverifiable-artifact")
                : ("not-opted-in" as const),
            }
          : {}),
        ...(input.commitSha ? { commit_sha: input.commitSha } : {}),
        ...(input.platformRelease
          ? { platform_release: input.platformRelease }
          : {}),
        // FAILURE. The raw message is MODEL-REACHABLE output -- any runner can
        // throw model text -- so it rides the same opt-in as the artifacts.
        // The default carries one of six ALLOWLISTED categories, which is the
        // property that matters: no input bytes reach it, whichever trusted
        // first-party site branded the error.
        ...(input.failure !== undefined
          ? (() => {
              const category = failureCategory(input.failure);
              return {
                failure_category: category,
                // error_message ALSO CARRIES THE CATEGORY BY DEFAULT, and that
                // is a receiver constraint rather than redundancy. The queue
                // stores `error_message` and has no `failure_category` column
                // (ops-dashboard ingestCapture), so with the raw message
                // correctly withheld a failed authoring was arriving with
                // error_message NULL -- the operator could not tell an auth
                // failure from a quota exhaustion from a 503 (sol-critic PR #2
                // round 10). Writing the category here is safe by
                // construction: failureCategory validates every result against
                // a six-value allowlist and returns a fixed string, so no byte
                // of any input reaches the field. The brands themselves come
                // from several trusted producers -- this loop, the SDK runner,
                // the grant provider, the tenant repository, the proposal
                // tools -- and the allowlist, not their provenance, is what
                // makes the field safe. When the receiver
                // grows a real column this line becomes redundant rather than
                // wrong.
                // EVEN THE OPT-IN RAW MESSAGE GETS THE ACCEPTANCE GRAMMAR.
                // redactSecrets only removes credentials we HELD plus the
                // TOKENISH log heuristic, so a different key the consumer
                // pasted survived both and was exported verbatim under the
                // flag (sol-critic PR #2 round 15). The flag authorises
                // exporting model OUTPUT; it was never meant to authorise
                // exporting a live credential.
                error_message: artifactsAllowed
                  ? redactCredentialShaped(
                      redactSecrets(
                        input.failure instanceof Error
                          ? input.failure.message
                          : String(input.failure),
                        held,
                      ),
                    )
                  : category,
              };
            })()
          : {}),
        captured_at: new Date().toISOString(),
        invocation_id: input.scrub.invocationId,
      };
      // FAIL-CLOSED BACKSTOP over the assembled event, covering every field
      // including ones added later (platform_release, telemetry, anything a
      // future contract version introduces). If a held credential is still
      // literally present anywhere on the wire, send nothing.
      //
      // SERIALIZE EXACTLY ONCE, scan the parsed form of THOSE BYTES, and send
      // THOSE BYTES. Anything that serializes a second time reopens the hole:
      // a stateful toJSON -- installable on Object.prototype by anything
      // sharing this process, including a runner that holds the tenant's
      // grant -- can return a clean object on the first serialization and the
      // credential on the second (sol-critic PR #2 round 4, reproduced). The
      // payload is therefore COMPLETE here, platform and dedupe key included,
      // so the sink has nothing left to add.
      let body: string;
      let wireEvent: UpstreamCapture;
      try {
        const withPlatform: UpstreamCapture = {
          ...event,
          platform: sink.platform ?? null,
        };
        // Keyed off the platform-bearing event, so two instances serving the
        // same tenant do not collapse into one queue row.
        const complete: UpstreamCapture = {
          ...withPlatform,
          dedupe_key: upstreamDedupeKey(withPlatform),
        };
        // STRIP THE PROTOTYPE CHAIN BEFORE THE ONE SERIALIZATION. Anything
        // sharing this process can install Object.prototype.toJSON and have it
        // inject text during that stringify; the sweep below only searches for
        // credentials the loop HELD, so an injected value it never held -- a
        // second key the consumer pasted -- went out on the wire
        // (sol-critic PR #2 round 14, reproduced). A null-prototype rebuild
        // leaves no chain to consult, which ends that class rather than adding
        // another scan to it.
        body = JSON.stringify(stripPrototypes(complete));
        wireEvent = JSON.parse(body) as UpstreamCapture;
      } catch {
        return; // cyclic, BigInt, or otherwise unserializable: send nothing
      }
      const candidates = held.filter((value) => value && value.length >= 24);
      if (candidates.length > 0) {
        let values: string[];
        try {
          values = stringValues(wireEvent);
        } catch {
          return; // unverifiable payload
        }
        if (
          candidates.some((secret) =>
            values.some((value) => value.includes(secret)),
          )
        ) {
          return;
        }
        // Belt and braces on the exact bytes, in case a value escaped the
        // structural walk: the string that goes out must not contain them.
        if (candidates.some((secret) => body.includes(secret))) return;
      }
      void sink.capture(body).catch(() => {});
    } catch {
      // Capture must never fail authoring.
    }
  }

  private withTenantRepoLease<T>(
    tenantId: string,
    action: (runFenced: TenantMutationFence) => Promise<T>,
  ): Promise<T> {
    const provider = this.ports.tenantRepo;
    if (!provider.withTenantLease) {
      return Promise.reject(new AuthorLoopError("tenant repository writer lease is required", 503));
    }
    return provider.withTenantLease(tenantId, action);
  }

  private withTenantRepoReadLease<T>(
    tenantId: string,
    action: () => Promise<T>,
  ): Promise<T> {
    const provider = this.ports.tenantRepo;
    if (!provider.withTenantReadLease) {
      return Promise.reject(new AuthorLoopError("tenant repository read lease is required", 503));
    }
    return provider.withTenantReadLease(tenantId, action);
  }

  private persistTool(
    repo: Awaited<ReturnType<HarnessPorts["tenantRepo"]["checkout"]>>,
    tool: ToolPackage,
  ): Promise<{ commit: string }> {
    const leaseAwareRepo = repo as typeof repo & {
      mutateAndCommit?(
        mutation: () => void,
        message: string,
        identity: typeof HARNESS_IDENTITY,
      ): Promise<{ commit: string }>;
    };
    const message = `author tool: ${tool.name}`;
    if (leaseAwareRepo.mutateAndCommit) {
      return leaseAwareRepo.mutateAndCommit(
        () => registerTool(repo.dir, tool),
        message,
        HARNESS_IDENTITY,
      );
    }
    registerTool(repo.dir, tool);
    return repo.commit(message, HARNESS_IDENTITY);
  }

  /** Build the exactly-three-tool toolset the author session is granted. */
  private toolsetFor(repoDir: string, tenantId: string, targetToolName?: string): AuthorToolset {
    let previousReceipt: ToolSourceReceipt | undefined;
    return {
      fsTenantRepo: new FsTenantRepo(repoDir),
      submitTool: (proposal) => {
        if (targetToolName && !previousReceipt) {
          if (proposal.name !== targetToolName) {
            throw withFailureCategory(new Error("tool revision must keep the bound target name"), "validation_failed");
          }
          const existing = findTool(repoDir, targetToolName);
          if (!existing || existing.provenance?.author !== "agent") {
            throw withFailureCategory(new Error("tool revision target is not an existing authored tool"), "validation_failed");
          }
          if (existing.kind !== "script" || existing.entry !== `tools/${targetToolName}/tool.py`) {
            throw withFailureCategory(new Error("tool revision target has an unsupported package identity"), "validation_failed");
          }
          for (const key of ["engine_op", "capabilities", "params", "returns"] as const) {
            if (!isDeepStrictEqual(proposal[key], existing[key])) {
              throw new Error(`tool revision cannot change ${key}`);
            }
          }
          const entry = `tools/${targetToolName}/tool.py`;
          const manifest = `tools/${targetToolName}/tool.json`;
          const sourceBytes = readFileSync(join(repoDir, entry));
          const manifestBytes = readFileSync(join(repoDir, manifest));
          const packageManifest = JSON.parse(manifestBytes.toString("utf8")) as ToolPackage;
          if (!isDeepStrictEqual(packageManifest, { ...existing, entry: "tool.py" })) {
            throw withFailureCategory(new Error("tool revision target manifest does not match its registry entry"), "validation_failed");
          }
          previousReceipt = {
            contract: "leaf.tool-source.v1",
            source_sha256: createHash("sha256").update(sourceBytes).digest("hex"),
            manifest_sha256: createHash("sha256").update(manifestBytes).digest("hex"),
            source_bytes: sourceBytes.byteLength,
            manifest_bytes: manifestBytes.byteLength,
            entry,
            manifest,
          };
        }
        const submitted = submitToolProposal(repoDir, proposal, new Date(), previousReceipt);
        previousReceipt = submitted.receipt;
        return submitted;
      },
      apsTestRun: makeApsTestRun(this.ports.broker, tenantId),
    };
  }

  /**
   * Defense in depth after the runner returns. The structured-submit receipt,
   * returned source, package manifest, and actual Git changes must all bind to
   * the same two exact files. A runner cannot smuggle an unrelated repo edit
   * into the later harness commit.
   */
  private verifySubmittedTool(tenantId: string, repoDir: string, run: AgentRunResult): void {
    const expectedEntry = `tools/${run.tool.name}/tool.py`;
    const expectedManifest = `tools/${run.tool.name}/tool.json`;
    if (run.tool.entry !== expectedEntry) {
      throw new AuthorLoopError("authored tool entry does not match its package path", 422);
    }
    if (!run.sourceReceipt || run.sourceReceipt.contract !== "leaf.tool-source.v1") {
      throw new AuthorLoopError("authored tool is missing an exact source receipt", 422);
    }
    if (
      run.sourceReceipt.entry !== expectedEntry ||
      run.sourceReceipt.manifest !== expectedManifest
    ) {
      throw new AuthorLoopError("authored tool source receipt path mismatch", 422);
    }
    const expectedFiles = intrinsics.sortStrings([expectedEntry, expectedManifest]);
    // NOT JSON EQUALITY. Both sides went through JSON.stringify, so an
    // inherited toJSON made ANY two values compare equal and this guard --
    // which exists to reject a runner that touched files outside its package --
    // accepted everything (sol-critic PR #2 round 21, probed).
    const reportedFiles = safeStringArray(run.files);
    if (
      run.files.length !== 2 ||
      reportedFiles === null ||
      !isDeepStrictEqual(reportedFiles, expectedFiles)
    ) {
      throw new AuthorLoopError("authored tool reported unexpected repository changes", 422);
    }

    const source = readFileSync(join(repoDir, expectedEntry), "utf8");
    const manifestBytes = readFileSync(join(repoDir, expectedManifest));
    if (source !== run.code) {
      throw new AuthorLoopError("authored source does not match the exact returned code", 422);
    }
    // WHAT GIT STORES CAN STILL DIFFER FROM WHAT WE VERIFY, and this PR does
    // NOT close that. Everything above checks WORKTREE bytes; both commit
    // paths then run `git add -A`, and a clean filter (core.autocrlf, a tenant
    // `.gitattributes`, a custom filter) rewrites content on the way into the
    // index. Reproduced: 14 worktree bytes became 12 committed bytes with a
    // different sha256, so the source receipt can attest content that is not
    // in the committed artifact.
    //
    // I TRIED TO CLOSE IT HERE AND THE PLACEMENT CANNOT WORK. Comparing
    // `hash-object --path` against `--no-filters` runs the filter a SECOND,
    // SEPARATE time from the one staging will run, and git only RECOMMENDS
    // that filters be idempotent -- it does not require it. A stateful or
    // context-sensitive filter returns clean bytes to the check and different
    // bytes to `git add -A` (sol-critic PR #2 rounds 26-27). A pre-stage check
    // cannot prove a post-stage fact.
    //
    // The real fix is to stage ONCE, verify the resulting INDEX blobs against
    // the receipt, and commit inside the same fenced operation -- which needs
    // the receipt at commit time and therefore a change to the TenantRepo /
    // TenantChangeRepo contracts. That is repository-provenance architecture,
    // it is PRE-EXISTING (the receipt was always verified pre-stage), and it is
    // not the upstream-capture boundary this PR exists to harden. Filed as its
    // own change rather than grown into this one.
    //
    // What DOES reduce it here: tenant repos are provisioned with
    // core.autocrlf=false / core.eol=lf, which removes the conversion driven
    // solely by core.autocrlf. NOT "by construction" -- attribute-driven EOL
    // normalization (`text` / `text=auto`) and custom clean filters both
    // survive those settings, so the gap narrows rather than closes.
    if (
      createHash("sha256").update(source).digest("hex") !== run.sourceReceipt.source_sha256 ||
      createHash("sha256").update(manifestBytes).digest("hex") !== run.sourceReceipt.manifest_sha256
    ) {
      throw new AuthorLoopError("authored source receipt digest mismatch", 422);
    }
    if (
      Buffer.byteLength(source, "utf8") !== run.sourceReceipt.source_bytes ||
      manifestBytes.byteLength !== run.sourceReceipt.manifest_bytes
    ) {
      throw new AuthorLoopError("authored source receipt byte-count mismatch", 422);
    }
    if (
      run.executionReceipt &&
      (
        run.executionReceipt.source_sha256 !== run.sourceReceipt.source_sha256 ||
        run.executionReceipt.tenant_hash !==
          createHash("sha256").update(tenantId).digest("hex")
      )
    ) {
      throw new AuthorLoopError(
        "broker execution receipt does not match the submitted source and tenant",
        422,
      );
    }

    const manifest = JSON.parse(manifestBytes.toString("utf8")) as ToolPackage;
    const expectedPackageManifest = { ...run.tool, entry: "tool.py" };
    // isDeepStrictEqual does not consult toJSON; JSON equality did.
    if (!isDeepStrictEqual(manifest, expectedPackageManifest)) {
      throw new AuthorLoopError("authored manifest does not match the validated tool", 422);
    }

    // The isolated author boundary can make Git observe a different owner for
    // this worktree. Reset inherited trust, then bind this command to only the
    // exact resolved checkout. Never use a wildcard safe.directory entry.
    const resolvedRepoDir = realpathSync(repoDir).replaceAll("\\", "/");
    // --no-renames IS LOAD-BEARING. With rename detection on, git emits a
    // rename as "XY <dest>\0<source>\0" and the SOURCE field carries NO XY
    // prefix, so any parser assuming one prefixed record per NUL mis-reads it.
    // A short source name vanished entirely, leaving exactly the two expected
    // paths, and this guard ACCEPTED the change before `git add -A`
    // (sol-critic PR #2 round 25). --no-renames makes git report the pair as
    // separate prefixed D and A records, so the grammar the parser assumes is
    // the grammar git produces.
    //
    // The flags stay compact and adjacent to the env scrub on purpose:
    // authorRepoSafety.test.ts pins them within one window to prove THIS git
    // child receives scrubbed credentials. Explanation lives here, not inline.
    const status = execFileSync(
      "git",
      [
        "-c", "safe.directory=",
        "-c", `safe.directory=${resolvedRepoDir}`,
        "-C",
        resolvedRepoDir,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
      ],
      { encoding: "utf8", env: scrubSecrets(process.env) },
    );
    // PARSED WITHOUT THE HELPERS THAT WERE DEMONSTRATED TO BREAK IT. This is
    // the guard that decides whether the runner touched files outside its
    // submitted package, and `git add -A` follows it -- so a replaced
    // `String.prototype.split` made it see only the two expected paths while an
    // unrelated file rode along into the tenant's commit (sol-critic PR #2
    // round 24, probed). The parse avoids `split`, `filter`, `map` and
    // `replaceAll` for that reason.
    //
    // It is NOT free of mutable dispatch and does not claim to be:
    // `parseNulSeparatedPaths` reads `status.charCodeAt` and the sort reaches
    // `Function.prototype.call`, both replaceable. Under the excluded
    // same-realm threat model this guard falls like everything else in the
    // realm. What the rewrite buys is the ordinary case -- a replaced
    // prototype helper, which is far cheaper to hit than a patched
    // `Function.prototype` -- on the one path that precedes a commit into the
    // tenant's own repository (sol-critic PR #2 round 31).
    const changed = intrinsics.parseNulSeparatedPaths(status);
    intrinsics.sortStrings(changed);
    // THE MOST DANGEROUS OF THE THREE. This is the guard that decides whether
    // the runner touched files outside its package, and the commit below is
    // `git add -A` -- so under the hook it accepted unrelated changes and then
    // committed them into the tenant's repository.
    if (!isDeepStrictEqual(changed, expectedFiles)) {
      throw new AuthorLoopError("author runner changed files outside the submitted tool package", 422);
    }
  }

  /**
   * Shared authoring core (used by both build and one-off): spawn ONE design-time
   * session, get the authored tool package, then re-validate with the oracle.
   */
  private async authorInRepo(
    tenantId: string,
    description: string,
    repoDir: string,
    targetToolName?: string,
    scrubSink?: ScrubState,
  ) {
    // Concern 2 grant ONLY (never the platform JWT). Use the same mounted-account
    // lease router as conversation turns, while retaining legacy single-grant
    // providers that expose only getGrant().
    const lease = this.ports.oauth.acquireGrant && this.ports.oauth.settleGrant
      ? await this.ports.oauth.acquireGrant(tenantId)
      : null;
    const grant = lease?.grant ?? await this.ports.oauth.getGrant(tenantId);
    // Past this line a grant EXISTS, so an empty scrub list genuinely means
    // "this grant carried nothing to scrub" rather than "we never looked".
    if (scrubSink) scrubSink.grantResolved = true;
    // Record into the CALLER's invocation-owned scrub array (upstream capture
    // scrubs these literally; no cross-invocation state).
    if (scrubSink) {
      for (const value of [
        (grant as Record<string, unknown>)["oauthToken"],
        (grant as Record<string, unknown>)["apiKey"],
      ]) {
        if (typeof value === "string" && value.trim().length > 0) {
          scrubSink.held.push(value);
        }
      }
    }
    let run: AgentRunResult | undefined;
    let failure: unknown;
    try {
      const toolset = this.toolsetFor(repoDir, tenantId, targetToolName);
      // THE RUNNER IS THE UNTRUSTED BOUNDARY, so anything it throws is marked
      // here rather than trusted for what it claims about itself. A private
      // symbol is not a capability in-process: the runner can import
      // `withFailureCategory` from the guards module, or construct a public
      // AuthorLoopError, and brand its own throw with any category it likes
      // (sol-critic PR #2 round 15). Origin is something it cannot forge --
      // this mark is applied by the loop, at the one place the boundary is
      // crossed -- and `failureCategory` checks it before the brand.
      try {
        run = await this.ports.agentRunner.run({
          description,
          systemPrompt: AUTHOR_SYSTEM_PROMPT,
          repoDir,
          grant,
          toolset,
        });
      } catch (error) {
        // try/await/catch, NOT .catch(). `run()` can throw SYNCHRONOUSLY before
        // it ever returns a Promise, and a .catch() attached to the result then
        // never runs -- the mark was skipped entirely and a forged brand was
        // believed (sol-critic PR #2 round 16, probed).
        //
        // Marked only when the operator has NOT declared this runner trusted.
        // Marking unconditionally erased the bundled AgentSdkRunner's real
        // auth, quota and rate-limit categories -- the exact signals this
        // contract exists to carry -- which is the same mistake as round 12,
        // where a safety tightening quietly downgraded real signal.
        if (!this.ports.trustRunnerFailureCategories) markUntrustedOrigin(error);
        throw error;
      }
      // SNAPSHOT THE RESULT ONCE, HERE, at the boundary it crossed. Every
      // later consumer -- validation, registration, the HTTP response and the
      // capture -- reads the snapshot, never the runner's object. Reading the
      // same property twice was the defect: a getter answered clean for the
      // taint scan and returned an unheld credential for the payload, and a
      // non-enumerable toJSON survived a spread to be invoked later by
      // registerTool (sol-critic PR #2 round 22, both probed).
      //
      // A graph that cannot be snapshotted -- an accessor, a function where
      // data belongs, a cycle -- is REFUSED rather than coerced. Authoring
      // fails loudly instead of proceeding on a value nobody can pin down.
      //
      // OUTSIDE the runner's catch, deliberately. Inside it, this 422 -- which
      // the loop itself raises and brands `validation_failed` -- was marked
      // untrusted on the way out, and `failureCategory` then discarded the
      // brand and recorded `unknown`. The mark exists to distrust what the
      // RUNNER threw, not what the loop concluded about it (sol-critic PR #2
      // round 31). The catch now covers exactly `run()`.
      const snapshot = snapshotUntrusted(run) as AgentRunResult | null;
      if (snapshot === null) {
        throw new AuthorLoopError(
          "author runner returned a result that cannot be safely snapshotted",
          422,
        );
      }
      run = snapshot;

      this.verifySubmittedTool(tenantId, repoDir, run);

      // Defense in depth: re-run the CONTRACT section 2 oracle on the result.
      const vr = validateTool(run.tool);
      if (!vr.ok) {
        throw new AuthorLoopError(
          `authored tool failed CONTRACT section 2 validation`,
          422,
          vr.diagnostics,
        );
      }
      return run;
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      if (lease && this.ports.oauth.settleGrant) {
        try {
          await this.ports.oauth.settleGrant(
            tenantId,
            lease.lease_id,
            authorGrantSettlement(run, failure),
          );
        } catch {
          // Routing telemetry must never replace the author result or root error.
        }
      }
    }
  }

  private async author(tenantId: string, description: string, scrubSink?: ScrubState) {
    const repo = await this.ports.tenantRepo.checkout(tenantId);
    const run = await this.authorInRepo(tenantId, description, repo.dir, undefined, scrubSink);
    return { repo, run };
  }

  private lifecyclePorts() {
    const bare = this.ports.tenantRepo.bare;
    const coordination = this.ports.customizationCoordination;
    if (!bare || !coordination) {
      throw new AuthorLoopError("customization lifecycle is not configured", 503);
    }
    return { bare, coordination };
  }

  /**
   * Materialize the canonical bare repository for a first-time tenant without
   * opening a change set or running the author model. The app calls this before
   * it mints the lifecycle base commit, then verifies the same ref from the
   * shared repository mount.
   */
  async ensureRepository(tenantId: string): Promise<{ tenant_id: string; base_commit: string }> {
    return this.withTenantRepoLease(tenantId, async (runFenced) => {
      const { bare } = this.lifecyclePorts();
      const bareRepo = await runFenced(() => bare.call(this.ports.tenantRepo, tenantId));
      const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
      const baseCommit = changes.readRef("refs/heads/main");
      if (!baseCommit || !/^[0-9a-f]{40}$/i.test(baseCommit)) {
        throw new AuthorLoopError("tenant repository main ref is unavailable", 503);
      }
      return { tenant_id: tenantId, base_commit: baseCommit.toLowerCase() };
    });
  }

  /** Legacy auth-off compatibility: author + register + direct commit. */
  async buildLegacyAuthOff(tenantId: string, description: string): Promise<AuthorResponse> {
    const scrub: ScrubState = {
      held: [],
      grantResolved: false,
      captured: false,
      invocationId: randomUUID(),
    };
    try {
      return await this.withTenantRepoLease(tenantId, async () => {
        const { repo, run } = await this.author(tenantId, description, scrub);
        const { commit } = await this.persistTool(repo, run.tool);
        this.captureUpstream({
          tenantId,
          route: "build",
          description,
          scrub,
          run,
          commitSha: commit,
        });
      // A1: thread the runner's authoring telemetry (turns/tokens/cost/models) through
      // to /author, ADDITIVELY. A runner that did not meter (the fake) leaves it undefined,
      // so the response stays exactly {tool, code, preview} — the frozen shape is preserved.
        return {
          tool: run.tool,
          code: run.code,
          preview: run.preview,
          ...(run.telemetry ? { telemetry: run.telemetry } : {}),
        };
      });
    } catch (error) {
      this.captureUpstream({
        tenantId,
        route: "build",
        description,
        scrub,
        failure: error,
      });
      throw error;
    }
  }

  /** @deprecated Live authenticated customization must use stage() then publish(). */
  async build(tenantId: string, description: string): Promise<AuthorResponse> {
    return this.buildLegacyAuthOff(tenantId, description);
  }

  /**
   * Stage a proposed catalog change in an isolated worktree and private change
   * ref. This never updates main or an effective catalog pointer.
   */
  async stage(
    tenantId: string,
    description: string,
    request: StageCustomizationRequest,
  ): Promise<StageCustomizationResponse> {
    const scrub: ScrubState = {
      held: [],
      grantResolved: false,
      captured: false,
      invocationId: randomUUID(),
    };
    try {
      return await this.stageInner(tenantId, description, request, scrub);
    } catch (error) {
      this.captureUpstream({
        tenantId,
        route: "stage",
        description,
        scrub,
        platformRelease: request.platformRelease,
        failure: error,
      });
      throw error;
    }
  }

  private async stageInner(
    tenantId: string,
    description: string,
    request: StageCustomizationRequest,
    scrub: ScrubState,
  ): Promise<StageCustomizationResponse> {
    return this.withTenantRepoLease(tenantId, async (runFenced) => {
      const { bare, coordination } = this.lifecyclePorts();
      const bareRepo = await runFenced(() => bare.call(this.ports.tenantRepo, tenantId));
      const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
      const { observedMainSha, existingChangeSha } = await runFenced(() => ({
        observedMainSha: changes.readRef("refs/heads/main"),
        existingChangeSha: changes.readChangeRef(request.changeSetId),
      }));
      if (existingChangeSha === null && observedMainSha !== request.expectedBaseSha) {
        throw new AuthorLoopError("staging base no longer matches main", 409);
      }
      const change = await runFenced(() =>
        changes.createOrResume(request.changeSetId, request.expectedBaseSha));
      try {
        if (change.stagedSha !== null) {
          const catalogDigest = await runFenced(() => createHash("sha256")
            .update(readFileSync(join(change.dir, REGISTRY_FILE)))
            .digest("hex"));
        const receipt = Object.freeze({
          contract: "leaf.customization.v1" as const,
          tenant_id: tenantId,
          change_set_id: request.changeSetId,
          state: "staged" as const,
          base_commit: request.expectedBaseSha,
          staged_commit: change.stagedSha,
          catalog_digest: catalogDigest,
          platform_release: request.platformRelease,
          workspace_contract_digest: request.workspaceContractDigest,
          idempotency_key: request.idempotencyKey,
        });
          await coordination.recordStaged(receipt);
          // A RESUMED STAGE IS STILL AN INVOCATION, and it was producing zero
          // queue rows. This path returns the existing receipt without ever
          // reaching the author loop, so no capture was even ATTEMPTED -- an
          // eligible invocation that never tried, which is the direction that
          // loses the signal entirely rather than the one that duplicates it.
          // A consumer
          // retrying a stage is demand, and the operator saw none of it.
          // (kimi-adversary round 8 and sol-critic rounds 12-13 both noted the
          // resume path never re-captures; round 13 called it a defect.)
          //
          // No grant resolves on this path, so `scrub.grantResolved` is false
          // and the capture carries the withheld-prompt sentinel by the same
          // rule as every other pre-grant case. The staged commit and platform
          // release still identify what was resumed.
          this.captureUpstream({
            tenantId,
            route: "stage",
            description,
            scrub,
            commitSha: change.stagedSha,
            platformRelease: request.platformRelease,
          });
          return { receipt };
        }
        if (request.targetToolName) {
          const target = findTool(change.dir, request.targetToolName);
          if (!target || target.provenance?.author !== "agent") {
            throw new AuthorLoopError("tool revision target is not an existing authored tool", 422);
          }
        }
        const run = await this.authorInRepo(
          tenantId, description, change.dir, request.targetToolName, scrub,
        );
        const { stagedCommit, catalogDigest } = await runFenced(() => {
          registerTool(change.dir, run.tool, {
            replaceExisting: request.targetToolName !== undefined,
          });
          const stagedCommit = changes.stageCommit(change, `stage tool: ${run.tool.name}`);
          const catalogDigest = createHash("sha256")
            .update(readFileSync(join(change.dir, REGISTRY_FILE)))
            .digest("hex");
          return { stagedCommit, catalogDigest };
        });
      const receipt = Object.freeze({
        contract: "leaf.customization.v1" as const,
        tenant_id: tenantId,
        change_set_id: request.changeSetId,
        state: "staged" as const,
        base_commit: request.expectedBaseSha,
        staged_commit: stagedCommit,
        catalog_digest: catalogDigest,
        platform_release: request.platformRelease,
        workspace_contract_digest: request.workspaceContractDigest,
        idempotency_key: request.idempotencyKey,
      });
        await coordination.recordStaged(receipt);
        this.captureUpstream({
          tenantId,
          route: "stage",
          description,
          scrub,
          run,
          commitSha: stagedCommit,
          platformRelease: request.platformRelease,
        });
        return {
          tool: run.tool,
          code: run.code,
          preview: run.preview,
          receipt,
          ...(run.telemetry ? { telemetry: run.telemetry } : {}),
        };
      } finally {
        await runFenced(() => changes.cleanupWorktree(change));
      }
    });
  }

  /** Stage removal of one exact tenant registry row on a private change ref. */
  async stageRemoval(
    tenantId: string,
    request: StageRemovalRequest,
  ): Promise<StageCustomizationResponse> {
    return this.withTenantRepoLease(tenantId, async (runFenced) => {
      const { bare, coordination } = this.lifecyclePorts();
      const bareRepo = await runFenced(() => bare.call(this.ports.tenantRepo, tenantId));
      const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
      const observedMainSha = await runFenced(() => changes.readRef("refs/heads/main"));
      if (observedMainSha !== request.expectedBaseSha) {
        throw new AuthorLoopError("removal base no longer matches main", 409);
      }
      const change = await runFenced(() =>
        changes.createOrResume(request.changeSetId, request.expectedBaseSha));
      try {
        if (change.stagedSha === null) {
          const registryPath = join(change.dir, REGISTRY_FILE);
          const raw = readFileSync(registryPath);
          const observedDigest = createHash("sha256").update(raw).digest("hex");
          if (observedDigest !== request.expectedCatalogDigest) {
            throw new AuthorLoopError("effective catalog digest changed", 409);
          }
          const registry = JSON.parse(raw.toString("utf8")) as { tools?: unknown };
          if (!Array.isArray(registry.tools)) {
            throw new AuthorLoopError("tenant registry is malformed", 422);
          }
          const matches = registry.tools.filter((row) =>
            typeof row === "object" && row !== null
            && (row as { name?: unknown }).name === request.toolName);
          if (matches.length !== 1) {
            throw new AuthorLoopError("removal target cardinality is not one", 409);
          }
          writeFileSync(
            registryPath, removeExactToolRow(raw.toString("utf8"), request.toolName), "utf8",
          );
          await runFenced(() => changes.stageCommit(
            change, `remove tenant tool: ${request.toolName}`));
        }
        const stagedCommit = change.stagedSha;
        if (!stagedCommit) throw new AuthorLoopError("removal did not stage", 500);
        const catalogDigest = await runFenced(() => createHash("sha256")
          .update(readFileSync(join(change.dir, REGISTRY_FILE))).digest("hex"));
        const receipt = Object.freeze({
          contract: "leaf.customization.v1" as const,
          tenant_id: tenantId,
          change_set_id: request.changeSetId,
          state: "staged" as const,
          base_commit: request.expectedBaseSha,
          staged_commit: stagedCommit,
          catalog_digest: catalogDigest,
          platform_release: request.platformRelease,
          workspace_contract_digest: request.workspaceContractDigest,
          idempotency_key: request.idempotencyKey,
        });
        await coordination.recordStaged(receipt);
        return { receipt };
      } finally {
        await runFenced(() => changes.cleanupWorktree(change));
      }
    });
  }

  /**
   * Trusted publish step. Durable approval and exact-receipt matching occur at
   * the coordination port before the Wave 1 adapter rechecks its private ref
   * and CAS-updates main.
   */
  async publish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<{ commit: string }> {
    return this.withTenantRepoLease(receipt.tenant_id, async (runFenced) => {
      const { bare, coordination } = this.lifecyclePorts();
      const bareRepo = await runFenced(() => bare.call(this.ports.tenantRepo, receipt.tenant_id));
      const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
      const ref = `refs/leaf/changes/${receipt.change_set_id.toLowerCase()}`;
      await coordination.authorizePublish(receipt, expectedMainSha);
      return runFenced(() => {
        const observedRef = changes.readRef(ref);
        if (observedRef !== receipt.staged_commit) {
          throw new GitRefConflictError(ref, receipt.staged_commit, observedRef);
        }
        const observedMain = changes.readRef("refs/heads/main");
        if (observedMain === receipt.staged_commit) {
          return { commit: receipt.staged_commit };
        }
        if (observedMain !== expectedMainSha) {
          throw new GitRefConflictError("refs/heads/main", expectedMainSha, observedMain);
        }
        const change: TenantChangeSet = {
          id: receipt.change_set_id,
          ref,
          dir: "",
          expectedBaseSha: receipt.base_commit,
          stagedSha: receipt.staged_commit,
        };
        return { commit: changes.publishToMain(change, expectedMainSha) };
      });
    });
  }

  /** one-off route: author + (optionally) test-run once, but DO NOT persist. */
  async oneOff(
    tenantId: string,
    description: string,
  ): Promise<AuthorResponse & { run: ResultEnvelope }> {
    const scrub: ScrubState = {
      held: [],
      grantResolved: false,
      captured: false,
      invocationId: randomUUID(),
    };
    try {
      return await this.withTenantRepoLease(tenantId, async () => {
        const { run } = await this.author(tenantId, description, scrub);
        const envelope = await this.ports.broker.runTool({
          tenantId,
          tool: run.tool,
          params: {},
          dwg: "rooftop_demo",
          apsLive: false,
        });
        this.captureUpstream({
          tenantId,
          route: "one-off",
          description,
          scrub,
          run,
        });
        // A1: same additive, absent-safe telemetry threading as build().
        return {
          tool: run.tool,
          code: run.code,
          preview: run.preview,
          run: envelope,
          ...(run.telemetry ? { telemetry: run.telemetry } : {}),
        };
      });
    } catch (error) {
      this.captureUpstream({
        tenantId,
        route: "one-off",
        description,
        scrub,
        failure: error,
      });
      throw error;
    }
  }

  /**
   * run route (design-time-ONLY invariant): dispatch a registered tool through
   * the broker. This method NEVER references the AgentRunner / Agent SDK.
   */
  async run(
    tenantId: string,
    toolName: string,
    params: Record<string, unknown> = {},
    dwg = "rooftop_demo",
    apsLive = false,
  ): Promise<ResultEnvelope> {
    return this.withTenantRepoReadLease(tenantId, async () => {
      const repo = await this.ports.tenantRepo.checkout(tenantId);
      const tool: ToolPackage | undefined = findTool(repo.dir, toolName);
      if (!tool) {
        throw new AuthorLoopError(`unknown registered tool: ${toolName}`, 404);
      }
      return this.ports.broker.runTool({ tenantId, tool, params, dwg, apsLive });
    });
  }
}
