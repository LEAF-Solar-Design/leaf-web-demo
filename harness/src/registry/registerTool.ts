/**
 * registerTool - persist a validated tool package into the TENANT repo's
 * registry.json (NOT the demo's engine/registry.json). This is the ONLY step on
 * the "build" route that mutates the registry; the "one-off" route skips it.
 *
 * It does NOT commit - the author loop owns the single harness-authored commit
 * (via TenantRepoProvider.commit) so the commit identity + message stay in one
 * place.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { HarnessIdentity, Registry, ToolPackage } from "../ports/index.js";

/** The git identity every harness commit is authored/committed as. */
export const HARNESS_IDENTITY: HarnessIdentity = {
  name: "Leaf Tool Harness",
  email: "harness@leafdesign.ai",
};

export const REGISTRY_FILE = "registry.json";

export function readRegistry(repoDir: string): Registry {
  const path = join(repoDir, REGISTRY_FILE);
  const raw = readFileSync(path, "utf8");
  const parsed = JSON.parse(raw) as Registry;
  if (!parsed || !Array.isArray(parsed.tools)) {
    throw new Error(`registry.json malformed at ${path} (missing tools[])`);
  }
  return parsed;
}

export function findTool(repoDir: string, name: string): ToolPackage | undefined {
  return readRegistry(repoDir).tools.find((t) => t.name === name);
}

/**
 * Append a tool package to the tenant registry. Rejects a duplicate name so a
 * "build" never silently produces two entries with the same name (the acceptance
 * gate asserts EXACTLY one new entry).
 */
export function registerTool(repoDir: string, tool: ToolPackage): Registry {
  const registry = readRegistry(repoDir);
  if (registry.tools.some((t) => t.name === tool.name)) {
    throw new Error(`registerTool: a tool named ${JSON.stringify(tool.name)} already exists`);
  }
  registry.tools.push(tool);
  const path = join(repoDir, REGISTRY_FILE);
  writeFileSync(path, JSON.stringify(registry, null, 2) + "\n", "utf8");
  return registry;
}
