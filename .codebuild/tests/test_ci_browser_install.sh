#!/usr/bin/env bash
set -euo pipefail

# Exercise the actual CI install block without network access or apt changes.
# npx and python are executable PATH mocks, because the block runs the
# dependency stage under timeout(1), which cannot run a shell function.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed -n '/^# LEAF_CI_BROWSER_INSTALL_BEGIN$/,/^# LEAF_CI_BROWSER_INSTALL_END$/p' \
  "$root/.codebuild/ci.sh" > "$tmp/install.sh"
[[ -s "$tmp/install.sh" ]]
# The production bound is pinned here; the copy under test uses one second.
grep -qx 'browser_deps_timeout_s=420' "$tmp/install.sh"
sed -i 's/^browser_deps_timeout_s=420$/browser_deps_timeout_s=1/' "$tmp/install.sh"
grep -qx 'browser_deps_timeout_s=1' "$tmp/install.sh"

mkdir "$tmp/bin"
cat > "$tmp/bin/mock-playwright" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
case "$(basename "$0")" in
  npx) producer=node; [[ "$1" == playwright ]] || exit 90; shift ;;
  python) producer=python; [[ "$1 $2" == '-m playwright' ]] || exit 90; shift 2 ;;
  *) exit 90 ;;
esac
printf '%s %s\n' "$producer" "$*" >> "$CALLS"
case "$*" in
  'install --with-deps chromium') stage=deps ;;
  'install chromium') stage=browser ;;
  *) exit 91 ;;
esac
[[ "$producer" == "$TARGET" ]] || exit 0
if [[ "$stage" == deps ]]; then
  case "$MODE" in
    success) exit 0 ;;
    fallback|failure) exit 100 ;;
    hang|hang-failure)
      # A grandchild stands in for apt under the playwright process.
      sleep 60 &
      echo "$!" > "$CALLS.grandchild"
      wait
      exit 0 ;;
  esac
fi
case "$MODE" in
  failure|hang-failure) exit 42 ;;
esac
exit 0
MOCK
chmod +x "$tmp/bin/mock-playwright"
ln -s mock-playwright "$tmp/bin/npx"
ln -s mock-playwright "$tmp/bin/python"

cat > "$tmp/driver.sh" <<'DRIVER'
set -euo pipefail
source "$INSTALL_BLOCK"
echo continued >> "$CALLS"
DRIVER

count() { grep -c "^$1$" "$2" || true; }
# `! cmd` never trips set -e, so negative checks fail explicitly.
absent() {
  if grep -q "$1" "$2"; then
    echo "FAIL: unexpected '$1' in $2" >&2
    exit 1
  fi
}

for mode in success fallback failure hang hang-failure; do
  for target in node python; do
    calls="$tmp/$mode-$target.calls"
    output="$tmp/$mode-$target.output"
    : > "$calls"
    status=0
    started=$SECONDS
    PATH="$tmp/bin:$PATH" MODE="$mode" TARGET="$target" CALLS="$calls" \
      INSTALL_BLOCK="$tmp/install.sh" bash "$tmp/driver.sh" > "$output" 2>&1 || status=$?
    elapsed=$((SECONDS - started))

    # Expected calls per producer: dependency stage, then browser-only stage.
    node_deps=1 node_browser=0 python_deps=1 python_browser=0
    case "$mode/$target" in
      success/*) ;;
      fallback/node) node_browser=1 ;;
      # After a timeout the later producer skips its dependency stage.
      hang/node) node_browser=1; python_deps=0; python_browser=1 ;;
      fallback/python|hang/python) python_browser=1 ;;
      # A terminal browser-only failure stops before the Python producer.
      failure/node|hang-failure/node) node_browser=1; python_deps=0 ;;
      failure/python|hang-failure/python) python_browser=1 ;;
      *) echo "FAIL: no expectation for $mode/$target" >&2; exit 1 ;;
    esac
    [[ "$(count 'node install --with-deps chromium' "$calls")" == "$node_deps" ]]
    [[ "$(count 'node install chromium' "$calls")" == "$node_browser" ]]
    [[ "$(count 'python install --with-deps chromium' "$calls")" == "$python_deps" ]]
    [[ "$(count 'python install chromium' "$calls")" == "$python_browser" ]]

    case "$mode" in
      success)
        absent 'browser-only' "$output" ;;
      fallback|failure)
        grep -q "dependency install failed; trying the distinct browser-only stage" "$output"
        absent 'exceeded' "$output" ;;
      hang|hang-failure)
        grep -q "dependency install exceeded 1s" "$output"
        # The bound, not the 60 s mock, ended the stage.
        (( elapsed < 30 ))
        # timeout killed the process group, so the apt stand-in is gone too.
        grandchild="$(cat "$calls.grandchild")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          kill -0 "$grandchild" 2>/dev/null || break
          sleep 0.2
        done
        if kill -0 "$grandchild" 2>/dev/null; then
          echo "FAIL: dependency grandchild $grandchild survived the bound" >&2
          exit 1
        fi
        if [[ "$mode/$target" == hang/node ]]; then
          grep -q "earlier dependency stage exceeded 1s; python -m playwright goes straight" "$output"
        fi ;;
    esac

    if [[ "$mode" == failure || "$mode" == hang-failure ]]; then
      # A missing browser stays terminal: nonzero, and the block stops.
      [[ "$status" == 42 ]]
      [[ "$(count continued "$calls")" == 0 ]]
    else
      [[ "$status" == 0 ]]
      [[ "$(count continued "$calls")" == 1 ]]
    fi
  done
  echo "PASS: $mode (Node and Python)"
done
