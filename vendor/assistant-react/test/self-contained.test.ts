import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const contractSrc = path.resolve(packageRoot, "../assistant-contract/src");

function isInside(root: string, file: string): boolean {
  const relative = path.relative(root, file);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? sourceFiles(file) : /\.tsx?$/.test(entry.name) ? [file] : [];
  });
}

describe("self-contained vendoring", () => {
  it("keeps the local tokens equal to the contract as text and JSON", () => {
    const local = readFileSync(path.join(packageRoot, "src/leaf-tokens.v1.json"), "utf8");
    const canonical = readFileSync(path.resolve(packageRoot, "../../contract/leaf-tokens.v1.json"), "utf8");
    expect(local.replace(/\r\n/g, "\n")).toBe(canonical.replace(/\r\n/g, "\n"));
    expect(JSON.parse(local)).toEqual(JSON.parse(canonical));
  });

  it("keeps relative imports and exports inside the package or sibling contract source", () => {
    const files = sourceFiles(path.join(packageRoot, "src"));
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      const specifiers = /\b(?:from\s*|import\s*\(\s*|import\s*)(["'])(\.[^"']*)\1/g;
      for (const match of source.matchAll(specifiers)) {
        const specifier = match[2];
        const resolved = path.resolve(path.dirname(file), specifier);
        expect(
          isInside(packageRoot, resolved) || isInside(contractSrc, resolved),
          `${path.relative(packageRoot, file)} imports or exports outside the vendored packages: ${specifier}`,
        ).toBe(true);
      }
    }
  });

  it("admits both React 18.3 and React 19 peers", () => {
    const manifest = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
    for (const name of ["react", "react-dom"]) {
      expect(manifest.peerDependencies[name]).toContain("^18.3.1");
      expect(manifest.peerDependencies[name]).toContain("^19.0.0");
    }
  });
});
