import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

export interface RepoPathIdentity {
  path: string;
  dev: number;
  ino: number;
  nlink: number;
  kind: "file" | "directory" | "other";
}

export interface ResolvedRepoPath {
  root: string;
  relativePath: string;
  path: string;
  components: RepoPathIdentity[];
}

/** Reject names whose Win32 interpretation can differ from their lexical form. */
export function isWindowsUnsafeRepoComponent(component: string): boolean {
  if (component.includes(":") || /[ .]$/u.test(component)) return true;
  const normalized = component.toLowerCase();
  if (normalized === ".git" || /^git~[0-9]+$/u.test(normalized)) return true;
  const deviceBase = normalized.split(".", 1)[0] ?? "";
  return /^(?:con|prn|aux|nul|clock\$|com(?:[1-9]|[¹²³])|lpt(?:[1-9]|[¹²³]))$/u.test(deviceBase);
}

function contained(root: string, candidate: string): boolean {
  const fromRoot = relative(root, candidate);
  return fromRoot === "" ||
    (fromRoot !== ".." && !fromRoot.startsWith(`..${sep}`) && !isAbsolute(fromRoot));
}

function identity(path: string): RepoPathIdentity | null {
  const stat = lstatSync(path, { throwIfNoEntry: false });
  if (!stat || stat.isSymbolicLink()) return null;
  const kind = stat.isFile() ? "file" : stat.isDirectory() ? "directory" : "other";
  if (kind === "file" && stat.nlink > 1) return null;
  return { path, dev: stat.dev, ino: stat.ino, nlink: stat.nlink, kind };
}

export function resolveContainedRepoPath(rootDir: string, rel: unknown): ResolvedRepoPath | null {
  if (typeof rel !== "string" || !rel.trim() || rel.includes("\0")) return null;
  const norm = rel.replaceAll("\\", "/").replace(/^\.\//, "");
  if (norm.startsWith("/") || /^[A-Za-z]:/u.test(norm)) return null;
  const parts = norm.split("/");
  if (parts.includes("..") || parts.some((part) => part !== "." && isWindowsUnsafeRepoComponent(part))) return null;
  const root = realpathSync(rootDir);
  const target = resolve(root, norm);
  if (!contained(root, target)) return null;

  const components: RepoPathIdentity[] = [];
  let cursor = root;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index]!;
    if (part === "." || part === "") continue;
    const candidate = join(cursor, part);
    const entry = lstatSync(candidate, { throwIfNoEntry: false });
    if (!entry) {
      const unresolved = resolve(cursor, ...parts.slice(index));
      return contained(root, unresolved)
        ? { root, relativePath: norm, path: unresolved, components }
        : null;
    }
    const item = identity(candidate);
    if (!item) return null;
    const actual = realpathSync(candidate);
    const physicalParts = relative(root, actual).split(/[\\/]/u).filter(Boolean);
    if (!contained(root, actual) || relative(candidate, actual) !== "" ||
        physicalParts.some(isWindowsUnsafeRepoComponent)) return null;
    components.push({ ...item, path: actual });
    cursor = actual;
  }
  return { root, relativePath: norm, path: cursor, components };
}

export function revalidateResolvedRepoPath(snapshot: ResolvedRepoPath): ResolvedRepoPath {
  const current = resolveContainedRepoPath(snapshot.root, snapshot.relativePath);
  if (!current || current.path !== snapshot.path || current.components.length !== snapshot.components.length) {
    throw new Error("repository path changed or became unsafe");
  }
  for (let index = 0; index < current.components.length; index += 1) {
    const before = snapshot.components[index]!;
    const after = current.components[index]!;
    if (before.path !== after.path || before.dev !== after.dev || before.ino !== after.ino ||
        before.nlink !== after.nlink || before.kind !== after.kind) {
      throw new Error("repository path identity changed before operation");
    }
  }
  return current;
}

export function assertOpenedFileIdentity(
  snapshot: ResolvedRepoPath,
  stat: { dev: number; ino: number; nlink: number; isFile(): boolean },
): void {
  const final = snapshot.components.at(-1);
  if (!final || final.kind !== "file" || !stat.isFile() || stat.nlink !== 1 ||
      stat.dev !== final.dev || stat.ino !== final.ino) {
    throw new Error("repository file identity changed before operation");
  }
}
