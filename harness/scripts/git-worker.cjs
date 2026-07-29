#!/usr/bin/env node
/* Pre-spawned git worker: performs add/commit/rev-parse for the harness.
 * WHY: after hosting an Agent SDK `claude` process tree, the harness node
 * process can no longer spawn git.exe (0xC0000142 STATUS_DLL_INIT_FAILED).
 * This worker is spawned at harness BOOT, before any SDK session, so its
 * spawn context stays clean. Protocol: one JSON object per line on stdin
 * {id, dir, message, name, email} -> one JSON line on stdout
 * {id, ok, commit?, error?}. Never logs repo contents or secrets. */
"use strict";
const { execFileSync } = require("node:child_process");
const { existsSync, realpathSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const readline = require("node:readline");

const trustedGitDirectories = new Set();

function trustSharedRepo(dir) {
  const root = (existsSync(dir) ? realpathSync(dir) : resolve(dir)).replaceAll("\\", "/");
  const configScope = process.env.GIT_CONFIG_GLOBAL || "<default>";
  for (const candidate of [root, join(root, ".git")].map((path) => path.replaceAll("\\", "/"))) {
    const cacheKey = `${configScope}\0${candidate}`;
    if (trustedGitDirectories.has(cacheKey)) continue;
    execFileSync("git", ["config", "--global", "--add", "safe.directory", candidate], {
      cwd: tmpdir(),
      encoding: "utf8",
    });
    trustedGitDirectories.add(cacheKey);
  }
}

function git(cwd, args, identity) {
  trustSharedRepo(cwd);
  const cfg = identity
    ? ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`]
    : [];
  return execFileSync("git", [...cfg, ...args], { cwd, encoding: "utf8" });
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  let req = null;
  try {
    req = JSON.parse(line);
    const identity = { name: req.name, email: req.email };
    git(req.dir, ["add", "-A"], identity);
    git(req.dir, ["commit", "-m", req.message, `--author=${req.name} <${req.email}>`], identity);
    const commit = git(req.dir, ["rev-parse", "HEAD"]).trim();
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, commit }) + "\n");
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ id: req && req.id, ok: false, error: String(e && e.message) }) + "\n",
    );
  }
});
process.stdin.on("end", () => process.exit(0));
