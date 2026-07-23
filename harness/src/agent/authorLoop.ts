/**
 * The author loop: NL prompt -> design-time Agent SDK session edits the tenant
 * repo -> validate -> (build only) register + commit. Plus the design-time-ONLY
 * run path, which dispatches a registered tool via the broker and NEVER touches
 * the AgentRunner / Agent SDK.
 *
 * This module owns the routing rule's behavior; server.ts is a thin HTTP shell
 * over it.
 */

import { AUTHOR_SYSTEM_PROMPT } from "./systemPrompt.js";
import { FsTenantRepo } from "./tools/fsTenantRepo.js";
import { makeApsTestRun } from "./tools/apsTestRun.js";
import { validateTool } from "./tools/validateTool.js";
import {
  HARNESS_IDENTITY,
  findTool,
  registerTool,
} from "../registry/registerTool.js";
import type {
  AuthorResponse,
  AuthorToolset,
  HarnessPorts,
  ResultEnvelope,
  ToolPackage,
} from "../ports/index.js";

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
  private async author(tenantId: string, description: string) {
    // Concern 2 grant ONLY (never the platform JWT). Injected explicitly.
    const grant = await this.ports.oauth.getGrant(tenantId);
    const repo = await this.ports.tenantRepo.checkout(tenantId);
    const toolset = this.toolsetFor(repo.dir, tenantId);

    const run = await this.ports.agentRunner.run({
      description,
      systemPrompt: AUTHOR_SYSTEM_PROMPT,
      repoDir: repo.dir,
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
    return { repo, run };
  }

  /** build route: author + register + exactly one harness commit. */
  async build(tenantId: string, description: string): Promise<AuthorResponse> {
    return this.withTenantRepoLease(tenantId, async () => {
      const { repo, run } = await this.author(tenantId, description);
      await this.persistTool(repo, run.tool);
      // A1: thread the runner's authoring telemetry (turns/tokens/cost/models) through
      // to /author, ADDITIVELY. A runner that did not meter leaves it undefined.
      return {
        tool: run.tool,
        code: run.code,
        preview: run.preview,
        ...(run.telemetry ? { telemetry: run.telemetry } : {}),
      };
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
