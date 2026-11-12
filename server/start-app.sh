#!/bin/sh
set -eu

# Import the immutable image snapshot before requests, so the first source read
# does not spend the gate deadline unpacking Git objects. A failed import keeps
# the new source ref selected; authoring refuses rather than reading stale main.
if [ -n "${LEAF_PLATFORM_SOURCE_SNAPSHOT_DIR:-}" ] && [ -z "${LEAF_PLATFORM_REPO_SOURCE_REF:-}" ]; then
  export LEAF_PLATFORM_REPO_SOURCE_REF="refs/leaf/deployed/${LEAF_SOURCE_SHA:-unknown}"
  if ! python -c 'import os; from pathlib import Path; from platform_customize import repo_dir; from platform_source_snapshot import install_snapshot; install_snapshot(Path(os.environ["LEAF_PLATFORM_SOURCE_SNAPSHOT_DIR"]), repo_dir(), os.environ.get("LEAF_SOURCE_SHA", ""))' >/dev/null 2>&1; then
    printf '%s\n' 'platform_source_snapshot_unavailable' >&2
  fi
fi

credential_dir="/tmp/leaf-instant-control"

cleanup_raw_credentials() {
  unset LEAF_INSTANT_CONTROL_SECRET
  unset LEAF_INSTANT_CONTROL_CA_PEM
  unset LEAF_INSTANT_CONTROL_CLIENT_CERT_PEM
  unset LEAF_INSTANT_CONTROL_CLIENT_KEY_PEM
}

if [ "${LEAF_INSTANT_EXECUTION_ENABLED:-0}" = "1" ]; then
  : "${LEAF_INSTANT_CONTROL_SECRET:?instant control API secret is required}"
  : "${LEAF_INSTANT_CONTROL_CA_PEM:?instant control CA PEM is required}"
  : "${LEAF_INSTANT_CONTROL_CLIENT_CERT_PEM:?instant control client certificate PEM is required}"
  : "${LEAF_INSTANT_CONTROL_CLIENT_KEY_PEM:?instant control client key PEM is required}"

  umask 077
  rm -rf "$credential_dir"
  mkdir -p "$credential_dir"
  printf '%s' "$LEAF_INSTANT_CONTROL_SECRET" > "$credential_dir/control-secret.txt"
  printf '%s' "$LEAF_INSTANT_CONTROL_CA_PEM" > "$credential_dir/ca.pem"
  printf '%s' "$LEAF_INSTANT_CONTROL_CLIENT_CERT_PEM" > "$credential_dir/client-cert.pem"
  printf '%s' "$LEAF_INSTANT_CONTROL_CLIENT_KEY_PEM" > "$credential_dir/client-key.pem"
  chmod 0600 "$credential_dir/control-secret.txt" "$credential_dir/ca.pem" \
    "$credential_dir/client-cert.pem" "$credential_dir/client-key.pem"

  export LEAF_INSTANT_CONTROL_SECRET_FILE="$credential_dir/control-secret.txt"
  export LEAF_INSTANT_CONTROL_CA_FILE="$credential_dir/ca.pem"
  export LEAF_INSTANT_CONTROL_CLIENT_CERT_FILE="$credential_dir/client-cert.pem"
  export LEAF_INSTANT_CONTROL_CLIENT_KEY_FILE="$credential_dir/client-key.pem"
fi

cleanup_raw_credentials
exec uvicorn app:app --host 0.0.0.0 --port 8130
