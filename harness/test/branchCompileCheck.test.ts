import { mkdtempSync, existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32 } from "node:zlib";
import { afterEach, describe, expect, it, vi } from "vitest";
import { branchCompileCheck, branchCompileDisabled } from "../src/vendor/mushy-author/agent/tools/branchCompileCheck.js";

const dirs: string[] = [];
const accept = { schema: "leaf.tool-check.v1", ok: true, decision: "accept", diagnostics: [],
  package_digest: "a".repeat(64), allowlist_sha256: "b".repeat(64), framework: "test" };
const diagnostics = ["main.csx(11,99): CS0117: unknown member", "lib/a.csx(12,3): scan: denied namespace: System.IO"];
const refused = { ...accept, ok: false, decision: "compile_failed", diagnostics };
const files = { "tool.json": "{}", "main.csx": "// π\nreturn 1;" };

afterEach(() => {
  vi.unstubAllEnvs();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function checker(body: string) {
  const dir = mkdtempSync(join(tmpdir(), "fake-checker-"));
  dirs.push(dir);
  const script = join(dir, "checker.mjs");
  writeFileSync(script, 'import fs from "node:fs";\n' + body);
  const opts = { bin: process.execPath, command: [process.execPath, script], refs: dir };
  return { dir, run: (timeoutMs?: number) => branchCompileCheck("test-tool", files, { ...opts, timeoutMs }) };
}
function emit(doc: unknown, exit: number): string {
  return `console.log(${JSON.stringify(JSON.stringify(doc))}); process.exitCode = ${exit};`;
}

// Both the pinned archives and the transport use stored entries with explicit sizes.
function storedEntries(zip: Buffer): Record<string, string> {
  const result: Record<string, string> = {};
  const end = zip.length - 22;
  expect(zip.readUInt32LE(end)).toBe(0x06054b50);
  const count = zip.readUInt16LE(end + 10);
  let central = zip.readUInt32LE(end + 16);
  for (let i = 0; i < count; i++) {
    expect(zip.readUInt32LE(central)).toBe(0x02014b50);
    expect(zip.readUInt16LE(central + 10)).toBe(0); // stored
    const offset = zip.readUInt32LE(central + 42);
    expect(zip.readUInt32LE(offset)).toBe(0x04034b50);
    const nameLength = zip.readUInt16LE(central + 28);
    const name = zip.toString("utf8", central + 46, central + 46 + nameLength);
    const start = offset + 30 + zip.readUInt16LE(offset + 26) + zip.readUInt16LE(offset + 28);
    const bytes = zip.subarray(start, start + zip.readUInt32LE(central + 24));
    expect(crc32(bytes)).toBe(zip.readUInt32LE(central + 16));
    result[name] = bytes.toString("utf8");
    central += 46 + nameLength + zip.readUInt16LE(central + 30) + zip.readUInt16LE(central + 32);
  }
  return result;
}

describe("Branch compiler child protocol", () => {
  it("submits sorted stored entries with an isolated HOME and reference cache, then removes the directory", async () => {
    vi.stubEnv("LEAF_TEST_PARENT_SECRET", "must stay in parent");
    const fake = checker(`
      const argv = process.argv.slice(2);
      fs.writeFileSync(process.env.NUGET_PACKAGES + "/capture.json", JSON.stringify({
        argv, env: process.env, zip: fs.readFileSync(argv[1]).toString("base64"),
      }));
      ${emit(accept, 0)}`);
    expect(await fake.run()).toEqual({ status: "accept", allowlistSha256: accept.allowlist_sha256, ms: expect.any(Number),
      summary: { pass: true, decision: "accept", errors: 0 } });
    const { argv, env, zip } = JSON.parse(readFileSync(join(fake.dir, "capture.json"), "utf8"));
    expect(argv).toEqual(["--package", join(env.HOME, "package.zip")]);
    expect(dirname(env.HOME)).toBe(tmpdir());
    expect(existsSync(env.HOME)).toBe(false);
    // libuv supplies mandatory Win32 system variables in CreateProcessW.
    // The Linux staging child must have exactly the six harness-supplied keys.
    if (process.platform === "win32") {
      for (const key of Object.keys(env)) {
        if (/^(HOMEDRIVE|HOMEPATH|LOGONSERVER|SYSTEMDRIVE|SYSTEMROOT|TEMP|USERDOMAIN|USERNAME|USERPROFILE|WINDIR)$/i.test(key)) {
          delete env[key];
        }
      }
    }
    expect(env).toEqual({ HOME: env.HOME, PATH: process.env.PATH, NUGET_PACKAGES: fake.dir,
      DOTNET_CLI_TELEMETRY_OPTOUT: "1", DOTNET_NOLOGO: "1", DOTNET_gcServer: "0" });
    const entries = storedEntries(Buffer.from(zip, "base64"));
    expect(Object.keys(entries)).toEqual(["test-tool/main.csx", "test-tool/tool.json"]);
    expect(Object.values(entries)).toEqual([files["main.csx"], files["tool.json"]]);
  });

  it("preserves the tool reply and parses compiler and scanner locations", async () => {
    expect(await checker(emit(refused, 1)).run()).toEqual({
      status: "refused", decision: "compile_failed", diagnostics, ms: expect.any(Number),
      summary: { pass: false, decision: "compile_failed", errors: 2 },
      structured: [
        { file: "main.csx", line: 11, column: 99, code: "CS0117", message: "unknown member" },
        { file: "lib/a.csx", line: 12, column: 3, code: "scan", message: "denied namespace: System.IO" },
      ],
    });
  });

  it("keeps malformed package diagnostics without locations", async () => {
    const text = "x".repeat(520);
    expect(await checker(emit({ ...refused, decision: "malformed", package_digest: null,
      allowlist_sha256: null, diagnostics: [text] }, 1)).run()).toMatchObject({
      decision: "malformed", structured: [{ file: "", line: 0, column: 0, code: "unparsed", message: text.slice(0, 500) }],
    });
  });

  it.each([
    ["exit 2", emit(refused, 2)], ["disagreeing exit", emit(refused, 0)],
    ["inconsistent ok", emit({ ...accept, ok: false }, 0)],
    ["extra key", emit({ ...accept, extra: 0 }, 0)], ["bad hash", emit({ ...accept, package_digest: "x" }, 0)],
    ["wrong schema", emit({ ...accept, schema: "other" }, 0)],
    ["too many diagnostics", emit({ ...refused, diagnostics: Array(51).fill("err") }, 1)],
    ["invalid JSON", 'console.log("not json");'], ["second line", emit(accept, 0) + 'console.log("{}");'],
    ["terminated child", 'process.kill(process.pid, "SIGKILL");'],
    ["oversized stdout", 'process.stdout.write("x".repeat(1024 * 1024 + 1));'],
  ])("fails closed for %s", async (_name, body) => {
    expect(await checker(body).run()).toMatchObject({ status: "unavailable", code: "bad_output",
      summary: { pass: false, decision: "unavailable", errors: 0 } });
  });

  it("kills a checker that exceeds its deadline", async () => {
    expect(await checker("setTimeout(() => {}, 60000);").run(500)).toMatchObject({ status: "unavailable", code: "timeout" });
  });

  it("reports an executable that cannot be started", async () => {
    const { dir } = checker("");
    expect(await branchCompileCheck("test-tool", files, { bin: join(dir, "missing"), refs: dir })).toMatchObject({
      status: "unavailable", code: "spawn_failed",
    });
  });

  it("requires both paths and disables only for the literal off", async () => {
    vi.stubEnv("LEAF_BRANCH_COMPILE_BIN", undefined);
    vi.stubEnv("LEAF_BRANCH_COMPILE_REFS", undefined);
    for (const opts of [{}, { bin: process.execPath }, { refs: tmpdir() }]) {
      expect(await branchCompileCheck("test-tool", files, opts)).toMatchObject({ status: "unavailable", code: "not_configured" });
    }
    expect(branchCompileDisabled({ LEAF_BRANCH_COMPILE_CHECK: "off" })).toBe(true);
    expect(branchCompileDisabled({ LEAF_BRANCH_COMPILE_CHECK: "OFF" })).toBe(false);
    expect(branchCompileDisabled({})).toBe(false);
  });

  it("limits concurrent children to two and includes queue time in each deadline", async () => {
    const fake = checker(`
      const log = process.env.NUGET_PACKAGES + "/events";
      fs.appendFileSync(log, "start\\n");
      await new Promise(resolve => setTimeout(resolve, 450));
      fs.appendFileSync(log, "end\\n");
      ${emit(accept, 0)}`);
    const results = await Promise.all([fake.run(), fake.run(), fake.run(), fake.run(100)]);
    expect(results.map(r => r.status)).toEqual(["accept", "accept", "accept", "unavailable"]);
    expect(results[3]).toMatchObject({ code: "busy" });
    let active = 0;
    let maximum = 0;
    for (const event of readFileSync(join(fake.dir, "events"), "utf8").trim().split("\n")) {
      active += event === "start" ? 1 : -1;
      maximum = Math.max(maximum, active);
    }
    expect(active).toBe(0);
    expect(maximum).toBeLessThanOrEqual(2);
  });
});

const configured = Boolean(process.env.LEAF_BRANCH_COMPILE_BIN && process.env.LEAF_BRANCH_COMPILE_REFS);
const parity = fileURLToPath(new URL("../branch-compile/parity/", import.meta.url));
describe(configured ? "Branch Windows CLI parity" : "Branch Windows CLI parity (requires LEAF_BRANCH_COMPILE_BIN and LEAF_BRANCH_COMPILE_REFS)", () => {
  if (!configured) console.info("Skipping Branch CLI parity: LEAF_BRANCH_COMPILE_BIN and LEAF_BRANCH_COMPILE_REFS are required.");
  it.skipIf(!configured).each(["good", "typo-bycli", "html-escaped-ops", "allowlist-violation"])("%s", async name => {
    const entries = storedEntries(readFileSync(join(parity, name + ".zip")));
    const root = Object.keys(entries)[0].split("/")[0];
    const source = Object.fromEntries(Object.entries(entries).map(([path, body]) => [path.slice(root.length + 1), body]));
    const outcome = await branchCompileCheck(root, source);
    const { stdout } = JSON.parse(readFileSync(join(parity, "expected", name + ".json"), "utf8"));
    expect(outcome.summary.decision).toBe(stdout.decision);
    expect(outcome.status === "refused" ? outcome.diagnostics : []).toEqual(stdout.diagnostics);
    if (outcome.status === "accept") expect(outcome.allowlistSha256).toBe(stdout.allowlist_sha256);
  }, 35_000);
});
