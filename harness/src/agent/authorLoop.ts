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
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { AUTHOR_SYSTEM_PROMPT } from "./systemPrompt.js";
import { FsTenantRepo } from "./tools/fsTenantRepo.js";
import { makeApsTestRun } from "./tools/apsTestRun.js";
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
import type {
  AuthorResponse,
  AuthorToolset,
  HarnessPorts,
  ResultEnvelope,
  StagedCustomizationReceipt,
  ToolPackage,
} from "../ports/index.js";

export interface StageCustomizationRequest {
  changeSetId: string;
  expectedBaseSha: string;
  platformRelease: string;
  workspaceContractDigest: string;
  idempotencyKey: string;
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

export class AuthorLoop {
  constructor(private readonly ports: HarnessPorts) {}

  private withTenantRepoLease<T>(
    tenantId: string,
    action: () => Promise<T>,
  ): Promise<T> {
    const provider = this.ports.tenantRepo as HarnessPorts["tenantRepo"] & {
      withTenantLease?<R>(tenant: string, operation: () => Promise<R>): Promise<R>;
    };
    return provider.withTenantLease
      ? provider.withTenantLease(tenantId, action)
      : action();
  }

  private withTenantRepoReadLease<T>(
    tenantId: string,
    action: () => Promise<T>,
  ): Promise<T> {
    const provider = this.ports.tenantRepo as HarnessPorts["tenantRepo"] & {
      withTenantReadLease?<R>(tenant: string, operation: () => Promise<R>): Promise<R>;
    };
    return provider.withTenantReadLease
      ? provider.withTenantReadLease(tenantId, action)
      : action();
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
  private toolsetFor(repoDir: string, tenantId: string): AuthorToolset {
    return {
      fsTenantRepo: new FsTenantRepo(repoDir),
      validateTool,
      apsTestRun: makeApsTestRun(this.ports.broker, tenantId),
    };
  }

  /**
   * Shared authoring core (used by both build and one-off): spawn ONE design-time
   * session, get the authored tool package, then re-validate with the oracle.
   */
  private async authorInRepo(tenantId: string, description: string, repoDir: string) {
    // Concern 2 grant ONLY (never the platform JWT). Injected explicitly.
    const grant = await this.ports.oauth.getGrant(tenantId);
    const toolset = this.toolsetFor(repoDir, tenantId);

    const run = await this.ports.agentRunner.run({
      description,
      systemPrompt: AUTHOR_SYSTEM_PROMPT,
      repoDir,
      grant,
      toolset,
    });

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
    return this.withTenantRepoLease(tenantId, async () => {
    const { bare, coordination } = this.lifecyclePorts();
    const bareRepo = await bare.call(this.ports.tenantRepo, tenantId);
    const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
    const observedMainSha = changes.readRef("refs/heads/main");
    const existingChangeSha = changes.readChangeRef(request.changeSetId);
    if (existingChangeSha === null && observedMainSha !== request.expectedBaseSha) {
      throw new AuthorLoopError("staging base no longer matches main", 409);
    }
    const change = changes.createOrResume(request.changeSetId, request.expectedBaseSha);
    try {
      if (change.stagedSha !== null) {
        const catalogDigest = createHash("sha256")
          .update(readFileSync(join(change.dir, REGISTRY_FILE)))
          .digest("hex");
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
      const run = await this.authorInRepo(tenantId, description, change.dir);
      registerTool(change.dir, run.tool);
      const stagedCommit = changes.stageCommit(change, `stage tool: ${run.tool.name}`);
      const catalogDigest = createHash("sha256")
        .update(readFileSync(join(change.dir, REGISTRY_FILE)))
        .digest("hex");
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
      changes.cleanupWorktree(change);
    }
    });
  }

  /**
   * Trusted publish step. Durable approval and exact-receipt matching occur at
   * the coordination port before the Wave 1 adapter rechecks its private ref
   * and CAS-updates main.
   */
  async publish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<{ commit: string }> {
    return this.withTenantRepoLease(receipt.tenant_id, async () => {
    const { bare, coordination } = this.lifecyclePorts();
    const bareRepo = await bare.call(this.ports.tenantRepo, receipt.tenant_id);
    const changes = new TenantChangeRepo({ repoDir: bareRepo.dir, identity: HARNESS_IDENTITY });
    const ref = `refs/leaf/changes/${receipt.change_set_id.toLowerCase()}`;
    const observedRef = changes.readRef(ref);
    if (observedRef !== receipt.staged_commit) {
      throw new GitRefConflictError(ref, receipt.staged_commit, observedRef);
    }
    const observedMain = changes.readRef("refs/heads/main");
    if (observedMain === receipt.staged_commit) {
      await coordination.authorizePublish(receipt, expectedMainSha);
      return { commit: receipt.staged_commit };
    }
    if (observedMain !== expectedMainSha) {
      throw new GitRefConflictError("refs/heads/main", expectedMainSha, observedMain);
    }
    await coordination.authorizePublish(receipt, expectedMainSha);
    const change: TenantChangeSet = {
      id: receipt.change_set_id,
      ref,
      dir: "",
      expectedBaseSha: receipt.base_commit,
      stagedSha: receipt.staged_commit,
    };
    return { commit: changes.publishToMain(change, expectedMainSha) };
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
