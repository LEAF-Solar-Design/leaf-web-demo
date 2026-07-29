#!/bin/sh
set -eu

credential_dir="/tmp/leaf-instant-executor"

cleanup_raw_credentials() {
  unset LEAF_INSTANT_EXECUTOR_CA_PEM
  unset LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM
  unset LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM
}

if [ "${LEAF_INSTANT_EXECUTION_ENABLED:-0}" = "1" ]; then
  : "${LEAF_INSTANT_EXECUTOR_CA_PEM:?executor CA PEM is required}"
  : "${LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM:?executor client certificate PEM is required}"
  : "${LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM:?executor client key PEM is required}"

  umask 077
  rm -rf "$credential_dir"
  mkdir -p "$credential_dir"
  printf '%s' "$LEAF_INSTANT_EXECUTOR_CA_PEM" > "$credential_dir/ca.pem"
  printf '%s' "$LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM" > "$credential_dir/client-cert.pem"
  printf '%s' "$LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM" > "$credential_dir/client-key.pem"
  chmod 0600 "$credential_dir/ca.pem" "$credential_dir/client-cert.pem" "$credential_dir/client-key.pem"

  export LEAF_INSTANT_EXECUTOR_CA_FILE="$credential_dir/ca.pem"
  export LEAF_INSTANT_EXECUTOR_CERT_FILE="$credential_dir/client-cert.pem"
  export LEAF_INSTANT_EXECUTOR_KEY_FILE="$credential_dir/client-key.pem"
fi

# Replace the bootstrap shell so the long-running harness environment contains
# file paths, never the raw PEM values delivered by ECS.
cleanup_raw_credentials
exec node dist/scripts/serve.js
