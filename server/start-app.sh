#!/bin/sh
set -eu

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
