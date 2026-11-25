import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { link, lstat, mkdir, open, realpath, unlink } from "node:fs/promises";
import { dirname, isAbsolute, join, parse, resolve } from "node:path";
import type { StaticNextObjectStore } from "./codebuildStaticNextExecutor.js";

const custodyKey = /^static-next\/[a-f0-9]{64}\/[a-f0-9]{64}\/(?:receipt|cleaned)\.json$/;
const limit = 2 * 1024 * 1024;
const code = (e: unknown) => (e as NodeJS.ErrnoException).code;

/** Only the trusted harness writes to this mounted directory. */
export class DiskStaticNextCustodyStore implements StaticNextObjectStore {
  constructor(private readonly root: string) {
    if (!isAbsolute(root) || root !== resolve(root) || root === parse(root).root) {
      throw new Error("invalid custody root");
    }
  }

  private async path(key: string, create: boolean): Promise<string> {
    if (!custodyKey.test(key) || !key.endsWith(".json")) throw new Error("invalid custody key");
    if (await realpath(this.root) !== this.root) throw new Error("invalid custody root");
    const parts = key.split("/");
    const filename = parts.pop()!;
    let directory = this.root;
    for (const part of parts) {
      directory = join(directory, part);
      if (create) {
        try { await mkdir(directory, { mode: 0o700 }); }
        catch (error) { if (code(error) !== "EEXIST") throw error; }
      }
      if (!(await lstat(directory)).isDirectory()) throw new Error("invalid custody directory");
    }
    return join(directory, filename);
  }

  async get(key: string): Promise<Buffer | null> {
    // A missing mount must not be interpreted as an absent cleanup marker.
    await realpath(this.root);
    try {
      const path = await this.path(key, false);
      const entry = await lstat(path);
      if (!entry.isFile() || entry.size > limit) throw new Error("invalid custody object");
      const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const stat = await handle.stat();
        if (!stat.isFile() || stat.ino !== entry.ino || stat.dev !== entry.dev || stat.size > limit) {
          throw new Error("invalid custody object");
        }
        const body = await handle.readFile();
        if (body.length !== stat.size) throw new Error("invalid custody object");
        return body;
      } finally { await handle.close(); }
    } catch (error) {
      if (code(error) !== "ENOENT") throw error;
      await realpath(this.root);
      return null;
    }
  }

  async putIfAbsent(key: string, body: Buffer): Promise<"created" | "exists"> {
    if (!Buffer.isBuffer(body) || body.length > limit) throw new Error("invalid custody object");
    const target = await this.path(key, true);
    const temp = join(dirname(target), randomUUID() + ".tmp");
    const handle = await open(temp, "wx", 0o600);
    try {
      try { await handle.writeFile(body); await handle.sync(); }
      finally { await handle.close(); }
      // Linking a complete sibling file is atomic and cannot replace a winner.
      try { await link(temp, target); return "created"; }
      catch (error) { if (code(error) === "EEXIST") return "exists"; throw error; }
    } finally { await unlink(temp); }
  }
}
