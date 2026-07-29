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
const readline = require("node:readline");

function git(cwd, args, identity) {
  // Match the in-process path: trust only this tenant directory for this call.
  const cfg = [
    "-c",
    `safe.directory=${cwd}`,
    ...(identity ? ["-c", `user.name=${identity.name}`, "-c", `user.email=${identity.email}`] : []),
  ];
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
