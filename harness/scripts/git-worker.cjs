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
const { join, resolve } = require("node:path");
const readline = require("node:readline");

const trustedGitDirectories = new Set();

function trustSharedRepo(dir) {
  const root = (existsSync(dir) ? realpathSync(dir) : resolve(dir)).replaceAll("\\", "/");
  for (const candidate of [root, join(root, ".git")].map((path) => path.replaceAll("\\", "/"))) {
    trustedGitDirectories.add(candidate);
  }
}

function gitEnvironment() {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key === "GIT_CONFIG" || key.startsWith("GIT_CONFIG_")) delete env[key];
  }
  const trusted = [...trustedGitDirectories].sort();
  env.GIT_CONFIG_COUNT = String(trusted.length + 1);
  env.GIT_CONFIG_KEY_0 = "safe.directory";
  env.GIT_CONFIG_VALUE_0 = "";
  for (const [index, candidate] of trusted.entries()) {
    env[`GIT_CONFIG_KEY_${index + 1}`] = "safe.directory";
    env[`GIT_CONFIG_VALUE_${index + 1}`] = candidate;
  }
  return env;
}

function git(cwd, args, identity) {
  trustSharedRepo(cwd);
  const cfg = identity
    ? ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`]
    : [];
  return execFileSync("git", [...cfg, ...args], { cwd, encoding: "utf8", env: gitEnvironment() });
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
