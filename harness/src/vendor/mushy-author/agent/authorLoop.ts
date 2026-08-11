/**
 * The author loop: NL prompt -> design-time Agent SDK session edits the tenant
 * repo -> validate -> (build only) register + commit. Plus the design-time-ONLY
 * run path, which dispatches a registered tool via the broker and NEVER touches
 * the AgentRunner / Agent SDK.
 *
 * This module owns the routing rule's behavior; server.ts is a thin HTTP shell
 * over it.
 */

import { createHash } from "node:crypto";
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
import { redactSecrets, stripSecrets } from "../redact.js";
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
  }
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
    heldSecrets: readonly string[];
    run?: AgentRunResult;
    commitSha?: string;
    platformRelease?: string;
    failure?: unknown;
  }): void {
    const held = input.heldSecrets;
    const sink = this.ports.upstreamSink;
    if (!sink) return;
    try {
      const event: UpstreamCapture = {
        contract: "mushy.upstream-capture.v1",
        consumer: input.tenantId,
        platform: null, // sink impl fills its configured platform label
        route: input.route,
        // User content: literal-only scrub of secrets we actually held
        // (never TOKENISH — a 40-char git SHA in a prompt is legitimate).
        prompt: stripSecrets(input.description, held),
        authoring_status: input.failure === undefined ? "authored" : "failed",
        ...(input.run
          ? {
              tool_name: input.run.tool.name,
              tool_manifest: input.run.tool,
              tool_code: input.run.code,
              ...(input.run.telemetry ? { telemetry: input.run.telemetry } : {}),
            }
          : {}),
        ...(input.commitSha ? { commit_sha: input.commitSha } : {}),
        ...(input.platformRelease
          ? { platform_release: input.platformRelease }
          : {}),
        ...(input.failure !== undefined
          ? {
              // Failure text: literal scrub of held grants + TOKENISH backstop.
              error_message: redactSecrets(
                input.failure instanceof Error
                  ? input.failure.message
                  : String(input.failure),
                held,
              ),
            }
          : {}),
        captured_at: new Date().toISOString(),
      };
      void sink.capture(event).catch(() => {});
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
            throw new Error("tool revision must keep the bound target name");
          }
          const existing = findTool(repoDir, targetToolName);
          if (!existing || existing.provenance?.author !== "agent") {
            throw new Error("tool revision target is not an existing authored tool");
          }
          if (existing.kind !== "script" || existing.entry !== `tools/${targetToolName}/tool.py`) {
            throw new Error("tool revision target has an unsupported package identity");
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
            throw new Error("tool revision target manifest does not match its registry entry");
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
    const expectedFiles = [expectedEntry, expectedManifest].sort();
    if (run.files.length !== 2 || JSON.stringify([...run.files].sort()) !== JSON.stringify(expectedFiles)) {
      throw new AuthorLoopError("authored tool reported unexpected repository changes", 422);
    }

    const source = readFileSync(join(repoDir, expectedEntry), "utf8");
    const manifestBytes = readFileSync(join(repoDir, expectedManifest));
    if (source !== run.code) {
      throw new AuthorLoopError("authored source does not match the exact returned code", 422);
    }
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
    if (JSON.stringify(manifest) !== JSON.stringify(expectedPackageManifest)) {
      throw new AuthorLoopError("authored manifest does not match the validated tool", 422);
    }

    // The isolated author boundary can make Git observe a different owner for
    // this worktree. Reset inherited trust, then bind this command to only the
    // exact resolved checkout. Never use a wildcard safe.directory entry.
    const resolvedRepoDir = realpathSync(repoDir).replaceAll("\\", "/");
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
      ],
      { encoding: "utf8", env: scrubSecrets(process.env) },
    );
    const changed = status
      .split("\0")
      .filter(Boolean)
      .map((entry) => entry.slice(3).replaceAll("\\", "/"))
      .sort();
    if (JSON.stringify(changed) !== JSON.stringify(expectedFiles)) {
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
    scrubSink?: string[],
  ) {
    // Concern 2 grant ONLY (never the platform JWT). Use the same mounted-account
    // lease router as conversation turns, while retaining legacy single-grant
    // providers that expose only getGrant().
    const lease = this.ports.oauth.acquireGrant && this.ports.oauth.settleGrant
      ? await this.ports.oauth.acquireGrant(tenantId)
      : null;
    const grant = lease?.grant ?? await this.ports.oauth.getGrant(tenantId);
    // Record into the CALLER's invocation-owned scrub array (upstream capture
    // scrubs these literally; no cross-invocation state).
    if (scrubSink) {
      for (const value of [
        (grant as Record<string, unknown>)["oauthToken"],
        (grant as Record<string, unknown>)["apiKey"],
      ]) {
        if (typeof value === "string" && value.trim().length > 0) scrubSink.push(value);
      }
    }
    let run: AgentRunResult | undefined;
    let failure: unknown;
    try {
      const toolset = this.toolsetFor(repoDir, tenantId, targetToolName);
      run = await this.ports.agentRunner.run({
        description,
        systemPrompt: AUTHOR_SYSTEM_PROMPT,
        repoDir,
        grant,
        toolset,
      });

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

  private async author(tenantId: string, description: string, scrubSink?: string[]) {
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
    const held: string[] = []; // this invocation's scrub values, and only its own
    try {
      return await this.withTenantRepoLease(tenantId, async () => {
        const { repo, run } = await this.author(tenantId, description, held);
        const { commit } = await this.persistTool(repo, run.tool);
        this.captureUpstream({
          tenantId,
          route: "build",
          description,
          heldSecrets: held,
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
        heldSecrets: held,
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
    const held: string[] = []; // this invocation's scrub values, and only its own
    try {
      return await this.stageInner(tenantId, description, request, held);
    } catch (error) {
      this.captureUpstream({
        tenantId,
        route: "stage",
        description,
        heldSecrets: held,
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
    held: string[],
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
          return { receipt };
        }
        if (request.targetToolName) {
          const target = findTool(change.dir, request.targetToolName);
          if (!target || target.provenance?.author !== "agent") {
            throw new AuthorLoopError("tool revision target is not an existing authored tool", 422);
          }
        }
        const run = await this.authorInRepo(
          tenantId, description, change.dir, request.targetToolName, held,
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
          heldSecrets: held,
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
    const held: string[] = []; // this invocation's scrub values, and only its own
    try {
      return await this.withTenantRepoLease(tenantId, async () => {
        const { run } = await this.author(tenantId, description, held);
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
          heldSecrets: held,
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
        heldSecrets: held,
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
