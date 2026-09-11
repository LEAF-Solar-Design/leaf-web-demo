#!/usr/bin/env bash
set -euo pipefail

# Playwright supplies Chromium. The unused Chrome apt index must not prevent
# Ubuntu from supplying its OS libraries; signature/hash checks stay enabled.
python - <<'LEAF_CI_APT_SOURCE_FILTER'
from pathlib import Path
import re


def is_unused_chrome(uri):
    return uri in {
        f"{scheme}://dl.google.com/linux/chrome-stable/deb{suffix}"
        for scheme in ("http", "https") for suffix in ("", "/")
    }


def filter_list(text):
    result = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s*deb(?:-src)?\s+(?:\[[^\]\r\n]*\]\s+)?(\S+)", line)
        result.append("# leaf-ci unused Chrome source: " + line
                      if match and is_unused_chrome(match.group(1)) else line)
    return "".join(result)


def filter_deb822(text):
    # Retain separators and every unrelated stanza byte-for-byte.
    parts = re.split(r"((?:\r?\n)[ \t]*(?:\r?\n))", text)
    for index in range(0, len(parts), 2):
        stanza = parts[index]
        match = re.search(r"(?im)^URIs:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*", stanza)
        if not match:
            continue
        uris = match.group().split(":", 1)[1].split()
        kept = [uri for uri in uris if not is_unused_chrome(uri)]
        if kept == uris:
            continue
        if kept:
            stanza = stanza[:match.start()] + "URIs: " + " ".join(kept) + stanza[match.end():]
        else:
            # Comment the complete stanza; never leave an enabled URI-less entry.
            stanza = "".join("# leaf-ci unused Chrome source: " + line
                             for line in stanza.splitlines(keepends=True))
        parts[index] = stanza
    return "".join(parts)


def disable_unused_chrome_sources(root):
    candidates = [root / "sources.list"]
    directory = root / "sources.list.d"
    if directory.is_dir():
        candidates.extend(sorted(directory.glob("*.list")))
        candidates.extend(sorted(directory.glob("*.sources")))
    for path in candidates:
        if not path.exists():
            continue
        original = path.read_bytes()
        text = original.decode("utf-8")
        updated = filter_deb822(text) if path.suffix == ".sources" else filter_list(text)
        if updated != text:
            path.write_bytes(updated.encode("utf-8"))


if __name__ == "__main__":
    disable_unused_chrome_sources(Path("/etc/apt"))
LEAF_CI_APT_SOURCE_FILTER

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
# LEAF_CI_BROWSER_INSTALL_BEGIN
# standard:7.0 needs OS libraries, and --with-deps shells out to apt, so this
# step used to inherit archive.ubuntu.com's availability. Measured 2026-09-11
# between 06:37Z and 07:30Z: leaf-ci-leaf-web-demo failed SIX of seven
# consecutive builds right here, on main and on every merge-queue group, with
# "Could not connect to archive.ubuntu.com:80 ... connection timed out" against
# all six mirror IPs, before a single suite ran, and every queued PR ejected
# behind it. A fallback is honest rather than a papered-over failure because in
# that same log every library Chromium needs read "is already the newest
# version" (libasound2, libcairo2, libcups2, libdbus-1-3, libdrm2, libxkbcommon0,
# libxrandr2, libatk*); the only packages the mirror could not serve were
# optional FONT packages (fonts-ipafont-gothic, fonts-freefont-ttf,
# fonts-unifont, fonts-wqy-zenhei, xfonts-*), which change glyph coverage in a
# rendered page and nothing any suite in this repo asserts. Two attempts, not
# more: each failed --with-deps costs a full apt timeout against six unreachable
# IPs, and the fallback is what saves the build. A missing BROWSER BINARY is
# still a hard failure: the last call is unguarded, so set -e fails the build.
install_ci_browser() {
  local attempt
  for attempt in 1 2; do
    if "$@" install --with-deps chromium; then
      return 0
    fi
    if [[ "$attempt" == 1 ]]; then
      echo "WARNING: $* dependency install failed; retrying in 5 seconds (attempt 2/2)." >&2
      sleep 5
    fi
  done
  echo "WARNING: $* dependency install failed after 2 attempts; falling back to browser-only install. OS libraries may be missing; browser proofs will check runtime usability." >&2
  "$@" install chromium
}

install_ci_browser npx playwright
# Install the Python producer's pinned browser with the gate interpreter too.
install_ci_browser python -m playwright
# LEAF_CI_BROWSER_INSTALL_END
cd "$CODEBUILD_SRC_DIR"
echo "--- 6/6 Run unsharded test gate and print scoreboard"
export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1
export LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
mkdir -p /tmp/gate-results
gate_status=0
python scripts/run-all-gates.py --jobs "${LEAF_GATE_JOBS:-4}" --retry 1 --result-json /tmp/gate-results/gate-result.json --log-dir /tmp/gate-logs || gate_status=$?
if [[ -f /tmp/gate-results/gate-result.json ]]; then
  tail -n 200 /tmp/gate-results/gate-result.json || true
else
  echo "No gate result JSON was written (runner exit $gate_status)"
fi
exit "$gate_status"
