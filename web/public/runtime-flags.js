// Runtime flags, DEFAULT COPY (everything off). The web container's
// entrypoint (deploy/write-runtime-flags.sh) OVERWRITES this file at boot
// from the ECS task-definition env — one shared image serves staging AND
// production, so shell rollout is runtime state, never a VITE build fence.
// This copy is what the dev server serves and what a container with no flag
// env serves. Loaded synchronously by index.html BEFORE the bundle; every
// reader fails closed, so a missing or stale file means the old shell.
window.__LEAF_FLAGS = { oneShell: '0' }
