import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FORGE_MATERIALIZATION_ERROR, materializeForge } from "../scripts/forgeMaterialize.js";
import { createProjectForgeAuthorityFromEnv, ProjectForgeAuthority } from "../src/ports/impl/projectForgeAuthority.js";

const authority = {
  tenantId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333",
  repoKey: "44444444-4444-4444-8444-444444444444",
};
const remoteUrl = "https://forge.leafdesign.ai/team/project.git";
const token = "synthetic_startup_secret";
const topology = { version: 1, repositories: [{ authority, remoteUrl }] };
const roots: string[] = [];
function fixture() {
  const parent = mkdtempSync(join(tmpdir(), "forge-materialize-test-"));
  roots.push(parent);
  return { parent, root: join(parent, "forge") };
}
function environment(credentials: unknown = { [authority.repoKey]: { token, username: "forge-user" } }): NodeJS.ProcessEnv {
  return { LEAF_FORGE_ORIGIN_MAP_JSON: JSON.stringify(topology),
    LEAF_FORGE_CREDENTIALS_JSON: JSON.stringify(credentials) };
}
function refused(env: NodeJS.ProcessEnv, root: string) {
  expect(() => materializeForge(env, root)).toThrow(new Error(FORGE_MATERIALIZATION_ERROR));
  expect(existsSync(join(root, "materialized"))).toBe(false);
  if (existsSync(root)) expect(readdirSync(root)).toEqual([]);
}
afterEach(() => {
  vi.restoreAllMocks();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("Forgejo startup materialization", () => {
  it("writes exact bound credentials and round-trips through the actual adapter", async () => {
    const { root, parent } = fixture();
    const stdout = vi.spyOn(process.stdout, "write");
    const stderr = vi.spyOn(process.stderr, "write");
    const paths = materializeForge(environment(), root)!;
    expect(stdout).not.toHaveBeenCalled();
    expect(stderr).not.toHaveBeenCalled();
    stdout.mockRestore();
    stderr.mockRestore();
    expect(JSON.parse(readFileSync(paths.LEAF_FORGE_ORIGIN_MAP, "utf8"))).toEqual(topology);
    const credential = join(paths.LEAF_FORGE_CREDENTIAL_DIR, `${authority.repoKey}.json`);
    expect(JSON.parse(readFileSync(credential, "utf8"))).toEqual({ authority, remoteUrl, token, username: "forge-user" });
    const forge = createProjectForgeAuthorityFromEnv(paths)!;
    expect(forge).toBeInstanceOf(ProjectForgeAuthority);
    // Valid credentials reach the missing local cache, without a remote call.
    await expect(forge.bind(authority, join(parent, "absent-cache")).refreshMain())
      .rejects.toMatchObject({ state: "unknown" });
    if (process.platform !== "win32") {
      for (const path of [root, dirname(paths.LEAF_FORGE_ORIGIN_MAP), paths.LEAF_FORGE_CREDENTIAL_DIR]) {
        expect(lstatSync(path).mode & 0o777).toBe(0o700);
        expect(lstatSync(path).uid).toBe(process.getuid!());
      }
      for (const path of [paths.LEAF_FORGE_ORIGIN_MAP, credential]) {
        expect(lstatSync(path).mode & 0o777).toBe(0o400);
        expect(lstatSync(path).uid).toBe(process.getuid!());
      }
    }
  });

  it.each([undefined, "{}"])("creates an empty credential directory for %s", async raw => {
    const { root } = fixture();
    const env = environment();
    env.LEAF_FORGE_CREDENTIALS_JSON = raw;
    const paths = materializeForge(env, root)!;
    expect(readdirSync(paths.LEAF_FORGE_CREDENTIAL_DIR)).toEqual([]);
    const forge = createProjectForgeAuthorityFromEnv(paths)!;
    await expect(forge.bind(authority, root).refreshMain()).rejects.toMatchObject({ state: "refused" });
  });

  it("leaves existing file-path mode untouched when both JSON names are absent", () => {
    const { root } = fixture();
    const env = { LEAF_FORGE_ORIGIN_MAP: "/existing/map", LEAF_FORGE_CREDENTIAL_DIR: "/existing/credentials" };
    expect(materializeForge(env, root)).toBeUndefined();
    expect(existsSync(root)).toBe(false);
    expect(env.LEAF_FORGE_ORIGIN_MAP).toBe("/existing/map");
  });

  it.each([
    { LEAF_FORGE_CREDENTIALS_JSON: "{}" },
    { ...environment(), LEAF_FORGE_ORIGIN_MAP: "" },
    { ...environment(), LEAF_FORGE_CREDENTIAL_DIR: "/existing" },
  ])("refuses partial and ambiguous modes", env => refused(env, fixture().root));

  it.each(["", "{", "null", "[]", "{}", JSON.stringify({ version: 1, repositories: [] }),
    JSON.stringify({ ...topology, extra: true }),
    JSON.stringify({ version: 1, repositories: [...topology.repositories, ...topology.repositories] }),
    JSON.stringify({ version: 1, repositories: [{ authority, remoteUrl: "https://evil.invalid/team/project.git" }] }),
    JSON.stringify({ version: 1, repositories: [{ authority: { ...authority, repoKey: "../escape" }, remoteUrl }] }),
    JSON.stringify({ version: 1, repositories: Array(101).fill(topology.repositories[0]) }),
    " ".repeat(65_537),
    '{"version":1,"repositories":[],"__proto__":{}}',
  ])("refuses invalid topology before publishing files", raw => {
    refused({ ...environment(), LEAF_FORGE_ORIGIN_MAP_JSON: raw }, fixture().root);
  });

  it.each([
    { unknown: { token } }, { "../escape": { token } },
    { [authority.repoKey]: { token: "bad:token" } },
    { [authority.repoKey]: { token: "" } },
    { [authority.repoKey]: { token, username: "-invalid" } },
    { [authority.repoKey]: { token, username: null } },
    { [authority.repoKey]: { token, authority } },
    { [authority.repoKey]: { token, remoteUrl } },
  ])("refuses unbound or invalid credential values", value => refused(environment(value), fixture().root));

  it.each(["", "{", "null", "[]", " ".repeat(65_537), '{"constructor":{}}',
    `{"${authority.repoKey}":{"token":"ok","prototype":{}}}`])("refuses invalid credential documents", raw => {
    refused({ ...environment(), LEAF_FORGE_CREDENTIALS_JSON: raw }, fixture().root);
  });

  it("is idempotent but refuses changed and stale credentials without rewriting", () => {
    const { root } = fixture();
    const paths = materializeForge(environment(), root)!;
    const file = join(paths.LEAF_FORGE_CREDENTIAL_DIR, `${authority.repoKey}.json`);
    const before = readFileSync(file, "utf8");
    const stat = lstatSync(file);
    expect(materializeForge(environment(), root)).toEqual(paths);
    expect(lstatSync(file).mtimeMs).toBe(stat.mtimeMs);
    for (const env of [environment({ [authority.repoKey]: { token: "changed" } }), environment({})]) {
      expect(() => materializeForge(env, root)).toThrow(FORGE_MATERIALIZATION_ERROR);
      expect(readFileSync(file, "utf8")).toBe(before);
      expect(readdirSync(root)).toEqual(["materialized"]);
    }
    writeFileSync(join(paths.LEAF_FORGE_CREDENTIAL_DIR, "stale.json"), "{}");
    expect(() => materializeForge(environment(), root)).toThrow(FORGE_MATERIALIZATION_ERROR);
  });

  it("refuses unexpected root entries and sanitizes filesystem failures", () => {
    const { root, parent } = fixture();
    mkdirSync(root, { mode: 0o700 });
    writeFileSync(join(root, "unexpected"), token);
    expect(() => materializeForge(environment(), root)).toThrow(new Error(FORGE_MATERIALIZATION_ERROR));
    const file = join(parent, token);
    writeFileSync(file, "not a directory");
    expect(() => materializeForge(environment(), file)).toThrow(new Error(FORGE_MATERIALIZATION_ERROR));
    expect(() => materializeForge(environment(), join(parent, "missing", "forge")))
      .toThrow(new Error(FORGE_MATERIALIZATION_ERROR));
    expect(() => materializeForge(environment(), "../escape")).toThrow(FORGE_MATERIALIZATION_ERROR);
  });

  it.skipIf(process.platform === "win32")("refuses root, ancestor, credential and map symlinks", () => {
    for (const kind of ["root", "ancestor", "credential", "map"]) {
      const { root, parent } = fixture();
      const target = join(parent, "target");
      mkdirSync(target, { mode: 0o700 });
      if (kind === "root" || kind === "ancestor") {
        symlinkSync(target, root, "dir");
        expect(() => materializeForge(environment(), kind === "root" ? root : join(root, "nested")))
          .toThrow(FORGE_MATERIALIZATION_ERROR);
      } else {
        const paths = materializeForge(environment(), root)!;
        const file = kind === "map" ? paths.LEAF_FORGE_ORIGIN_MAP
          : join(paths.LEAF_FORGE_CREDENTIAL_DIR, `${authority.repoKey}.json`);
        const replacement = join(target, "copy");
        writeFileSync(replacement, readFileSync(file), { mode: 0o400 });
        rmSync(file);
        symlinkSync(replacement, file);
        expect(() => materializeForge(environment(), root)).toThrow(FORGE_MATERIALIZATION_ERROR);
      }
    }
  });

  it.skipIf(process.platform === "win32")("refuses permissive existing modes", () => {
    const { root } = fixture();
    const paths = materializeForge(environment(), root)!;
    chmodSync(paths.LEAF_FORGE_ORIGIN_MAP, 0o644);
    expect(() => materializeForge(environment(), root)).toThrow(FORGE_MATERIALIZATION_ERROR);
  });
});

describe("container startup contract", () => {
  const repo = fileURLToPath(new URL("../../", import.meta.url));
  it("requires the helper before serve and unsets both raw JSON values before exec", () => {
    const script = readFileSync(join(repo, "harness/scripts/start-harness.sh"), "utf8");
    const helper = script.indexOf("  node dist/scripts/forgeMaterialize.js");
    const serve = script.indexOf("exec node dist/scripts/serve.js");
    expect(script).toContain("set -eu");
    expect(script).toContain('${LEAF_FORGE_ORIGIN_MAP_JSON+x}');
    expect(script).toContain('${LEAF_FORGE_CREDENTIALS_JSON+x}');
    expect(helper).toBeGreaterThan(script.indexOf("cleanup_raw_credentials\n"));
    for (const name of ["LEAF_FORGE_ORIGIN_MAP_JSON", "LEAF_FORGE_CREDENTIALS_JSON"]) {
      expect(script.indexOf(`unset ${name}`, helper)).toBeGreaterThan(helper);
      expect(script.indexOf(`unset ${name}`, helper)).toBeLessThan(serve);
    }
    expect(script).toContain('export LEAF_FORGE_ORIGIN_MAP="/app/run/forge/materialized/origins.json"');
    expect(script).toContain('export LEAF_FORGE_CREDENTIAL_DIR="/app/run/forge/materialized/credentials"');
  });
  it("compiles and guards the source and compiled helper as the runtime user", () => {
    const docker = readFileSync(join(repo, "deploy/Dockerfile.harness"), "utf8");
    const user = docker.indexOf("USER 10002:10002");
    expect(docker).toContain("RUN npx tsc -p tsconfig.build.json");
    expect(docker.indexOf("chmod 0700 /app/run/forge")).toBeLessThan(user);
    expect(docker).toContain("chown 10002:10002 /app/run/forge");
    const guard = docker.slice(user);
    for (const file of ["/app/scripts/forgeMaterialize.ts", "/app/dist/scripts/forgeMaterialize.js"]) {
      for (const flag of ["-f", "-s", "-r"]) expect(guard).toContain(`test ${flag} ${file}`);
    }
    expect(guard).toContain('RUN ["/bin/sh", "-c",');
  });
});
