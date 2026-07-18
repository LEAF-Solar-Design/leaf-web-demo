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
    const { repo, run } = await this.author(tenantId, description);
    registerTool(repo.dir, run.tool);
    await repo.commit(`author tool: ${run.tool.name}`, HARNESS_IDENTITY);
    return { tool: run.tool, code: run.code, preview: run.preview };
  }

  /** one-off route: author + (optionally) test-run once, but DO NOT persist. */
  async oneOff(
    tenantId: string,
    description: string,
  ): Promise<AuthorResponse & { run: ResultEnvelope }> {
    const { run } = await this.author(tenantId, description);
    const envelope = await this.ports.broker.runTool({
      tenantId,
      tool: run.tool,
      params: {},
      dwg: "rooftop_demo",
      apsLive: false,
    });
    return { tool: run.tool, code: run.code, preview: run.preview, run: envelope };
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
    const repo = await this.ports.tenantRepo.checkout(tenantId);
    const tool: ToolPackage | undefined = findTool(repo.dir, toolName);
    if (!tool) {
      throw new AuthorLoopError(`unknown registered tool: ${toolName}`, 404);
    }
    return this.ports.broker.runTool({ tenantId, tool, params, dwg, apsLive });
  }
}
