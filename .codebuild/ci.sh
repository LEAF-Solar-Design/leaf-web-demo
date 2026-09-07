#!/usr/bin/env bash
set -euo pipefail

# leaf-web-demo native CI: contract, license-fence, and Test gate UNSHARDED.
# The queue leg lives in .codebuild/mq.sh and is not this script's job.
# Workflows still dark: speculate-platform-images, prewarm-staging-cutover,
# the two PostgreSQL gates behind path filters, simulator-gate, the qualify-*
# dispatch workflows, and dispatch-staging-deploys.
npm i -g npm@10
echo "node=$(node -v 2>/dev/null || true) npm=$(npm -v 2>/dev/null || true) python=$(python --version 2>&1)"
BASE_REF="${CODEBUILD_WEBHOOK_BASE_REF:-}"; BASE_REF="${BASE_REF##refs/heads/}"; BASE_REF="${BASE_REF:-main}"
if git rev-parse --verify -q "origin/$BASE_REF" >/dev/null; then echo "origin/$BASE_REF"; elif git rev-parse --verify -q "$BASE_REF" >/dev/null; then echo "$BASE_REF"; else echo "FATAL: base ref $BASE_REF not in clone"; git branch -a | head -20; exit 1; fi > /tmp/base_ref
echo "base=$(cat /tmp/base_ref) head=$(git rev-parse --short HEAD) event=${CODEBUILD_WEBHOOK_EVENT:-manual}"
export BASE="$(cat /tmp/base_ref)"

echo "=== job contract ==="
cd "$CODEBUILD_SRC_DIR"
echo "--- 1/2 Install contract test dependencies"
python -m pip install pytest "PyYAML>=6"
cd "$CODEBUILD_SRC_DIR"
echo "--- 2/2 Run workflow shape contract"
PYTHONSAFEPATH=1 python -m pytest -q \
  tests/test_contract_workflow_shape.py \
  tests/test_dispatch_staging_deploys_shape.py

echo "=== job license-fence ==="
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 1/4 Install web dependencies"
npm ci
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 2/4 Build web bundle"
npm run build
cd "$CODEBUILD_SRC_DIR"
echo "--- 3/4 License fence self-test"
python scripts/check_license_fence.py --self-test
cd "$CODEBUILD_SRC_DIR"
echo "--- 4/4 License fence scan"
python scripts/check_license_fence.py .

echo "=== job test-gate ==="
cd "$CODEBUILD_SRC_DIR"
echo "--- 1/6 Upgrade pip"
python -m pip install --upgrade pip
cd "$CODEBUILD_SRC_DIR"
echo "--- 2/6 Install Python dependencies"
python -m pip install \
  -r server/requirements.txt \
  -r server/requirements-auth.txt \
  -r da/requirements.txt \
  -r platform/requirements.txt \
  -r scripts/requirements-ci.txt \
  -r executor/control_plane/requirements.txt \
  -r executor/runtime/requirements.txt
cd "$CODEBUILD_SRC_DIR/harness"
echo "--- 3/6 Install harness dependencies"
npm ci
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 4/6 Reuse web dependencies"
# The license-fence job installed a clean web tree in this same build.
# No intervening step changes its dependencies, so reuse that npm ci.
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 5/6 Install Chromium for browser proofs"
# The workflow uses `npx playwright install chromium`; --with-deps is added here on purpose
# because standard:7.0 lacks Chromium's OS libraries and the build runs as root.
npx playwright install --with-deps chromium
# Install the Python producer's pinned browser with the gate interpreter too.
python -m playwright install --with-deps chromium
cd "$CODEBUILD_SRC_DIR"
echo "--- 6/6 Run unsharded test gate and print scoreboard"
export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1
export LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
mkdir -p /tmp/gate-results
gate_status=0
python scripts/run-all-gates.py --retry 1 --result-json /tmp/gate-results/gate-result.json --log-dir /tmp/gate-logs || gate_status=$?
if [[ -f /tmp/gate-results/gate-result.json ]]; then
  tail -n 200 /tmp/gate-results/gate-result.json || true
else
  echo "No gate result JSON was written (runner exit $gate_status)"
fi
exit "$gate_status"
