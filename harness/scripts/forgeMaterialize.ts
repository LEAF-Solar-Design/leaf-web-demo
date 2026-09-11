/** Task-local startup files for the Mushy platform's Forgejo authority adapter. */
import { chmodSync, lstatSync, mkdirSync, readFileSync, readdirSync, renameSync,
  rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createProjectForgeAuthorityFromEnv } from "../src/ports/impl/projectForgeAuthority.js";

export const FORGE_ROOT = "/app/run/forge";
export const FORGE_MATERIALIZATION_ERROR = "project Forge startup refused";
type Paths = { LEAF_FORGE_ORIGIN_MAP: string; LEAF_FORGE_CREDENTIAL_DIR: string };
function refuse(): never { throw new Error(FORGE_MATERIALIZATION_ERROR); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) refuse();
  return value as Record<string, unknown>;
}
function parse(raw: string): unknown {
  if (Buffer.byteLength(raw, "utf8") > 65_536) refuse();
  return JSON.parse(raw, (key, value: unknown) => {
    if (["__proto__", "prototype", "constructor"].includes(key)) refuse();
    return value;
  });
}
function present(path: string): boolean {
  try { lstatSync(path); return true; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}
function check(path: string, directory: boolean): void {
  const stat = lstatSync(path);
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile())) refuse();
  if (process.platform !== "win32" &&
      ((stat.mode & 0o777) !== (directory ? 0o700 : 0o400) || stat.uid !== process.getuid!() ||
       (!directory && stat.nlink !== 1))) refuse();
}
function directory(path: string): void {
  mkdirSync(path, { mode: 0o700 });
  chmodSync(path, 0o700);
  check(path, true);
}
function paths(root: string): Paths {
  return { LEAF_FORGE_ORIGIN_MAP: join(root, "origins.json"),
    LEAF_FORGE_CREDENTIAL_DIR: join(root, "credentials") };
}
function entries(path: string, expected: string[]): void {
  if (JSON.stringify(readdirSync(path).sort()) !== JSON.stringify([...expected].sort())) refuse();
}

/** The injected root is for filesystem fixtures only; the CLI always uses FORGE_ROOT.
 * Rotation requires a new task. No existing credential is rewritten or removed.
 */
export function materializeForge(env: NodeJS.ProcessEnv = process.env, root = FORGE_ROOT): Paths | undefined {
  let stage: string | undefined;
  const createdFiles: string[] = [];
  let credentialStage: string | undefined;
  try {
    const rawMap = env.LEAF_FORGE_ORIGIN_MAP_JSON;
    const rawCredentials = env.LEAF_FORGE_CREDENTIALS_JSON;
    if (rawMap === undefined && rawCredentials === undefined) return undefined;
    if (rawMap === undefined || env.LEAF_FORGE_ORIGIN_MAP !== undefined ||
        env.LEAF_FORGE_CREDENTIAL_DIR !== undefined) refuse();
    const map = object(parse(rawMap));
    if (!Array.isArray(map.repositories) || map.repositories.length === 0 || map.repositories.length > 100) refuse();
    const credentials = rawCredentials === undefined ? {} : object(parse(rawCredentials));
    for (const value of Object.values(credentials)) {
      const credential = object(value);
      const keys = Object.keys(credential).sort().join(",");
      if ((keys !== "token" && keys !== "token,username") ||
          typeof credential.token !== "string" || !/^[A-Za-z0-9_-]+$/.test(credential.token) ||
          ("username" in credential && (typeof credential.username !== "string" ||
            !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(credential.username)))) refuse();
    }
    if (!isAbsolute(root) || resolve(root) !== root) refuse();
    // Refuse links in every ancestor, including a linked runtime root.
    for (let ancestor = root; ; ancestor = dirname(ancestor)) {
      if (present(ancestor)) {
        const stat = lstatSync(ancestor);
        if (stat.isSymbolicLink() || !stat.isDirectory()) refuse();
      }
      if (dirname(ancestor) === ancestor) break;
    }
    if (!present(root)) directory(root);
    check(root, true);
    const final = join(root, "materialized");
    entries(root, present(final) ? ["materialized"] : []);
    const candidate = join(root, ".staging");
    directory(candidate);
    stage = candidate;
    credentialStage = join(stage, "credentials");
    directory(credentialStage);
    const stagedPaths = paths(stage);
    const contents = new Map<string, string>([["origins.json", JSON.stringify(map)]]);
    function write(path: string, text: string): void {
      writeFileSync(path, text, { flag: "wx", mode: 0o400 });
      createdFiles.push(path);
      chmodSync(path, 0o400);
      check(path, false);
    }
    write(stagedPaths.LEAF_FORGE_ORIGIN_MAP, contents.get("origins.json")!);
    // This is the actual adapter, retaining its UUID, host and duplicate checks.
    if (!createProjectForgeAuthorityFromEnv(stagedPaths)) refuse();
    const bindings = new Map(map.repositories.map(value => {
      const entry = object(value);
      return [object(entry.authority).repoKey as string, entry] as const;
    }));
    for (const [repoKey, value] of Object.entries(credentials)) {
      const binding = bindings.get(repoKey);
      if (!binding) refuse();
      const text = JSON.stringify({ authority: binding.authority, remoteUrl: binding.remoteUrl, ...object(value) });
      const name = join("credentials", `${repoKey}.json`);
      contents.set(name, text);
    }
    // All input is validated before any final path is published.
    for (const [name, text] of contents) {
      if (name !== "origins.json") write(join(stage, name), text);
    }
    if (present(final)) {
      check(final, true);
      check(join(final, "credentials"), true);
      entries(final, ["origins.json", "credentials"]);
      entries(join(final, "credentials"), Object.keys(credentials).map(key => `${key}.json`));
      for (const [name, text] of contents) {
        const path = join(final, name);
        check(path, false);
        if (readFileSync(path, "utf8") !== text) refuse();
      }
    } else {
      renameSync(stage, final);
      stage = undefined;
      credentialStage = undefined;
      createdFiles.length = 0;
    }
    return paths(final);
  } catch { refuse(); }
  finally {
    // Only remove exact files created by this invocation, never recursive state.
    if (stage) {
      try {
        for (const file of createdFiles.reverse()) {
          if (process.platform === "win32") chmodSync(file, 0o600);
          unlinkSync(file);
        }
        if (credentialStage) rmdirSync(credentialStage);
        rmdirSync(stage);
      } catch { refuse(); }
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if (process.argv.length !== 2) refuse();
    materializeForge();
  } catch {
    process.stderr.write(`${FORGE_MATERIALIZATION_ERROR}\n`);
    process.exitCode = 1;
  }
}
