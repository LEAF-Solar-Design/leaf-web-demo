// Reader for the container-written runtime flags (window.__LEAF_FLAGS,
// loaded synchronously by index.html before this bundle from
// /runtime-flags.js — served no-store, rewritten at container boot by
// deploy/write-runtime-flags.sh from the ECS task-definition env).
//
// NEVER a VITE fence: one shared image serves staging AND production, so the
// one-shell rollout is runtime state per environment (staging=1, prod=0).
//
// FAILS CLOSED on everything: absent file (dev 404, script error), absent
// global, wrong type, a throwing accessor (storage-locked webviews throw on
// bare window reads), or any value but the literal string '1' — all mean the
// OLD SHELL. A rollback (env flip + task restart) therefore needs no client
// cooperation beyond a reload.
export function readOneShellEnabled(globals = globalThis) {
  try {
    return globals?.__LEAF_FLAGS?.oneShell === '1'
  } catch {
    return false
  }
}

// Read ONCE at module evaluation, like the boot search: the shell branch is
// a boot decision, and a mid-session flip must not tear one shell down
// around live work — the new value lands on the next page load.
export const ONE_SHELL_ENABLED = readOneShellEnabled()
