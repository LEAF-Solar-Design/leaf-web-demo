import { readFileSync } from "node:fs";
import ts from "typescript";
import { describe, expect, it } from "vitest";

// Evaluate the actual service entrypoint's options without starting its process,
// opening a grant store, or running an SDK author turn.
const source = readFileSync(new URL("../scripts/serve.ts", import.meta.url), "utf8");
const file = ts.createSourceFile("serve.ts", source, ts.ScriptTarget.Latest, true);
let initializer: ts.Expression | undefined;
function visit(node: ts.Node): void {
  if (ts.isVariableDeclaration(node) && node.name.getText(file) === "serverOptions") {
    initializer = node.initializer;
  }
  ts.forEachChild(node, visit);
}
visit(file);
if (!initializer) throw new Error("Service entrypoint serverOptions is missing");
const compiled = ts.transpileModule(`return (${initializer.getText(file)});`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText;

class Editor {
  constructor(readonly limits: unknown) {}
}
class Inspector {}
class Portfolio {
  constructor(readonly dependencies: unknown) {}
}

function options(repository: object | undefined, privateStaticPreview?: object) {
  const oauth = { resolve: () => { throw new Error("Grant access is outside this test"); } };
  const evaluate = new Function("ports", "projectRepositoryEdits", "standardServicesApproval",
    "privateStaticPreview", "glugMushyAuthor", "PortfolioProposalService", "SdkRepoEditor", "SdkProjectVideoInspector", compiled);
  return { oauth, value: evaluate({ oauth }, repository, undefined, privateStaticPreview, undefined, Portfolio, Editor, Inspector) };
}

describe("project source author service entrypoint", () => {
  it("mounts source author dependencies alongside the repository coordinator", () => {
    const repository = {};
    const preview = { projectStaticBuilds: {}, projectStaticViews: {} };
    const { oauth, value } = options(repository, preview);
    expect(value.projectRepositoryEdits).toBe(repository);
    expect(value.projectSourceAuthoring).toEqual({
      oauth,
      editor: new Editor({ maxTurns: 24, maxWallTimeMs: 240000 }),
      videoInspector: new Inspector(),
    });
    expect(value.projectSourceAuthoring.oauth).toBe(oauth);
    expect(value.projectStaticBuilds).toBe(preview.projectStaticBuilds);
    expect(value.projectStaticViews).toBe(preview.projectStaticViews);
  });

  it("keeps both source services absent when repository coordination is unavailable", () => {
    const { value } = options(undefined);
    expect(value).not.toHaveProperty("projectRepositoryEdits");
    expect(value).not.toHaveProperty("projectSourceAuthoring");
    expect(value).not.toHaveProperty("projectStaticBuilds");
    expect(value).not.toHaveProperty("projectStaticViews");
  });

  it("the container startup invokes the tested service entrypoint", () => {
    const script = readFileSync(new URL("../scripts/start-harness.sh", import.meta.url), "utf8");
    expect(script).toContain("exec node dist/scripts/serve.js");
  });
});
