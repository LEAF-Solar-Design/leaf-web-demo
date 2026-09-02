#!/bin/sh
# nginx:alpine entrypoint drop-in (/docker-entrypoint.d/40-runtime-flags.sh):
# writes the runtime flags the SPA reads synchronously before its bundle
# boots (web/public/runtime-flags.js is the build-time default this replaces).
#
# LEAF_ONE_SHELL_ENABLED comes from the ECS task definition. One shared image
# serves staging AND production, so shell rollout is RUNTIME state here,
# never a VITE build fence. FAIL CLOSED: anything but the literal '1'
# (absent, empty, 'true', 'yes') writes '0' — the old shell. Rollback is an
# env flip + task restart, no image rebuild.
set -eu
one_shell="${LEAF_ONE_SHELL_ENABLED:-0}"
case "$one_shell" in
  1) ;;
  *) one_shell=0 ;;
esac
printf 'window.__LEAF_FLAGS = { oneShell: "%s" }\n' "$one_shell" \
  > /usr/share/nginx/html/runtime-flags.js
