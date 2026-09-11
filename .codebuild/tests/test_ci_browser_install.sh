#!/usr/bin/env bash
set -euo pipefail

# Exercise the actual CI install block without network access or apt changes.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed -n '/^# LEAF_CI_BROWSER_INSTALL_BEGIN$/,/^# LEAF_CI_BROWSER_INSTALL_END$/p' \
  "$root/.codebuild/ci.sh" > "$tmp/install.sh"
[[ -s "$tmp/install.sh" ]]

cat > "$tmp/driver.sh" <<'DRIVER'
set -euo pipefail
attempts=0
mock_install() {
  local producer="$1"
  shift
  printf '%s %s\n' "$producer" "$*" >> "$CALLS"
  [[ "$*" == 'install --with-deps chromium' || "$*" == 'install chromium' ]]
  if [[ "$producer" != "$TARGET" ]]; then
    return 0
  fi
  if [[ "$*" == 'install --with-deps chromium' ]]; then
    attempts=$((attempts + 1))
    case "$MODE" in
      success) return 0 ;;
      retry) [[ "$attempts" == 2 ]]; return ;;
      fallback|failure) return 100 ;;
    esac
  fi
  if [[ "$MODE" == failure ]]; then
    return 42
  fi
}
npx() {
  [[ "$1" == playwright ]]
  shift
  mock_install node "$@"
}
python() {
  [[ "$1 $2" == '-m playwright' ]]
  shift 2
  mock_install python "$@"
}
sleep() {
  [[ "$1" == 5 ]]
  echo sleep >> "$CALLS"
}
source "$INSTALL_BLOCK"
echo continued >> "$CALLS"
DRIVER

for mode in success retry fallback failure; do
  for target in node python; do
    calls="$tmp/$mode-$target.calls"
    output="$tmp/$mode-$target.output"
    status=0
    MODE="$mode" TARGET="$target" CALLS="$calls" INSTALL_BLOCK="$tmp/install.sh" \
      bash "$tmp/driver.sh" > "$output" 2>&1 || status=$?
    deps=1
    browsers=0
    sleeps=0
    if [[ "$mode" != success ]]; then
      deps=2
      sleeps=1
      grep -q 'retrying.*attempt 2/2' "$output"
    fi
    if [[ "$mode" == fallback || "$mode" == failure ]]; then
      browsers=1
      grep -q 'falling back to browser-only install' "$output"
      grep -q 'OS libraries may be missing' "$output"
    else
      ! grep -q 'browser-only' "$output"
    fi
    [[ "$(grep -c "^$target install --with-deps chromium$" "$calls" || true)" == "$deps" ]]
    [[ "$(grep -c "^$target install chromium$" "$calls" || true)" == "$browsers" ]]
    [[ "$(grep -c '^sleep$' "$calls" || true)" == "$sleeps" ]]
    if [[ "$mode" == failure ]]; then
      [[ "$status" == 42 ]]
      ! grep -q '^continued$' "$calls"
    else
      [[ "$status" == 0 ]]
      grep -q '^continued$' "$calls"
      grep -q '^node install --with-deps chromium$' "$calls"
      grep -q '^python install --with-deps chromium$' "$calls"
    fi
  done
  echo "PASS: $mode (Node and Python)"
done
