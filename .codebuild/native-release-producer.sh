#!/usr/bin/env bash
# Fixed candidate entrypoint; the trusted Terraform bootstrap owns provenance.
set -euo pipefail
[[ $# == 0 ]] || { echo 'Forge producer accepts no arguments' >&2; exit 2; }
: "${LEAF_FORGE_SOURCE_DIR:?}" "${LEAF_FORGE_CONTRACT_DIR:?}" "${LEAF_FORGE_SOLVER_DIR:?}"
: "${LEAF_FORGE_REQUEST_FILE:?}" "${LEAF_FORGE_OUTPUT_DIR:?}" "${LEAF_NATIVE_REQUEST_B64:?}"
case "${LEAF_NATIVE_MODE:-}" in gate|release) ;; *) exit 2 ;; esac
for tool in python3.12 timeout git mktemp; do command -v "$tool" >/dev/null; done
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE BASH_ENV ENV
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
cd -- "$LEAF_FORGE_SOURCE_DIR"
venv="$(mktemp -d /tmp/leaf-forge-venv.XXXXXX)"
trap 'rm -rf -- "$venv"' EXIT
timeout --kill-after=30s 120s python3.12 -I -m venv "$venv"
timeout --kill-after=30s 300s "$venv/bin/python" -I -m pip install --disable-pip-version-check boto3==1.43.3
timeout --kill-after=30s 300s "$venv/bin/python" -I scripts/ci/native_release_producer.py forge --admit-only
export PATH="$venv/bin:$PATH"
if [[ "$LEAF_NATIVE_MODE" == gate ]]; then
  command -v npm >/dev/null
  timeout --kill-after=30s 10s node -e 'if (process.versions.node.split(".")[0] !== "22") process.exit(2)'
  timeout --kill-after=30s 300s npm install -g npm@10
  timeout --kill-after=30s 900s python -I -m pip install --disable-pip-version-check \
    -r server/requirements.txt -r server/requirements-auth.txt \
    -r da/requirements.txt -r platform/requirements.txt \
    -r scripts/requirements-ci.txt -r executor/control_plane/requirements.txt \
    -r executor/runtime/requirements.txt
  (cd harness && timeout --kill-after=30s 600s npm ci)
  (cd web && timeout --kill-after=30s 600s npm ci && timeout --kill-after=30s 600s npx --no-install playwright install --with-deps chromium)
  timeout --kill-after=30s 600s python -I -m playwright install --with-deps chromium
  export LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
  timeout --kill-after=30s 2800s python -I scripts/ci/native_release_producer.py forge
else
  command -v aws >/dev/null
  command -v docker >/dev/null
  timeout --kill-after=30s 60s aws ecr get-login-password --region us-east-1 |
    timeout --kill-after=30s 60s docker login --username AWS --password-stdin 807034087062.dkr.ecr.us-east-1.amazonaws.com
  timeout --kill-after=30s 30s docker buildx version
  timeout --kill-after=30s 4800s python -I scripts/ci/native_release_producer.py forge
fi
