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
import { readFileSync, realpathSync } from "node:fs";
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
  ) {
    // Concern 2 grant ONLY (never the platform JWT). Use the same mounted-account
    // lease router as conversation turns, while retaining legacy single-grant
    // providers that expose only getGrant().
    const lease = this.ports.oauth.acquireGrant && this.ports.oauth.settleGrant
      ? await this.ports.oauth.acquireGrant(tenantId)
      : null;
    const grant = lease?.grant ?? await this.ports.oauth.getGrant(tenantId);
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

  private async author(tenantId: string, description: string) {
    const repo = await this.ports.tenantRepo.checkout(tenantId);
    const run = await this.authorInRepo(tenantId, description, repo.dir);
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
    return this.withTenantRepoLease(tenantId, async () => {
      const { repo, run } = await this.author(tenantId, description);
      await this.persistTool(repo, run.tool);
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
          tenantId, description, change.dir, request.targetToolName,
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
    return this.withTenantRepoLease(tenantId, async () => {
      const { run } = await this.author(tenantId, description);
      const envelope = await this.ports.broker.runTool({
        tenantId,
        tool: run.tool,
        params: {},
        dwg: "rooftop_demo",
        apsLive: false,
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
