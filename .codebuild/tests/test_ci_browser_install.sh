#!/usr/bin/env bash
set -euo pipefail

# Exercise the actual CI install block without network access or apt changes.
# npx and python are executable PATH mocks, because the block runs the
# launch check and dependency stage under timeout(1).
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed -n '/^# LEAF_CI_BROWSER_INSTALL_BEGIN$/,/^# LEAF_CI_BROWSER_INSTALL_END$/p' \
  "$root/.codebuild/ci.sh" > "$tmp/install.sh"
[[ -s "$tmp/install.sh" ]]
# Production bounds are pinned here; the copy under test uses one second.
grep -qx 'browser_deps_timeout_s=420' "$tmp/install.sh"
grep -qx 'browser_launch_check_timeout_s=60' "$tmp/install.sh"
sed -i 's/^browser_deps_timeout_s=420$/browser_deps_timeout_s=1/' "$tmp/install.sh"
sed -i 's/^browser_launch_check_timeout_s=60$/browser_launch_check_timeout_s=1/' "$tmp/install.sh"
grep -qx 'browser_deps_timeout_s=1' "$tmp/install.sh"
grep -qx 'browser_launch_check_timeout_s=1' "$tmp/install.sh"

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
  'pdf about:blank '*) stage=launch; touch "$3" ;;
  *) exit 91 ;;
esac
if [[ "$stage" == launch && "$MODE/$TARGET/$producer" == hang/node/python ]]; then
  exit 100
fi
[[ "$producer" == "$TARGET" ]] || exit 0
hang() {
  # Reap the grandchild after timeout signals the whole process group.
  trap 'wait; exit 143' TERM
  sleep 60 &
  echo "$!" > "$CALLS.grandchild"
  wait
}
case "$stage/$MODE" in
  browser/browser-failure) exit 42 ;;
  launch/launch-hang|launch/hang-failure) hang ;;
  launch/fallback|launch/failure|launch/hang) exit 100 ;;
  deps/failure|deps/hang-failure) exit 100 ;;
  deps/hang) hang ;;
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

for mode in success fallback failure hang hang-failure browser-failure launch-hang; do
  for target in node python; do
    calls="$tmp/$mode-$target.calls"
    output="$tmp/$mode-$target.output"
    : > "$calls"
    status=0
    started=$SECONDS
    PATH="$tmp/bin:$PATH" MODE="$mode" TARGET="$target" CALLS="$calls" \
      INSTALL_BLOCK="$tmp/install.sh" bash "$tmp/driver.sh" > "$output" 2>&1 || status=$?
    elapsed=$((SECONDS - started))

    # Browser-only install precedes the launch; deps run only on launch failure.
    node_deps=0 node_browser=1 node_launch=1
    python_deps=0 python_browser=1 python_launch=1
    case "$mode/$target" in
      success/*) ;;
      fallback/node|failure/node|hang-failure/node|launch-hang/node) node_deps=1 ;;
      # After a timeout the later producer skips its dependency stage.
      hang/node) node_deps=1 ;;
      fallback/python|failure/python|hang/python|hang-failure/python|launch-hang/python) python_deps=1 ;;
      # A terminal browser-only failure stops before the Python producer.
      browser-failure/node) node_launch=0; python_browser=0; python_launch=0 ;;
      browser-failure/python) python_launch=0 ;;
      *) echo "FAIL: no expectation for $mode/$target" >&2; exit 1 ;;
    esac
    [[ "$(count 'node install --with-deps chromium' "$calls")" == "$node_deps" ]]
    [[ "$(count 'node install chromium' "$calls")" == "$node_browser" ]]
    [[ "$(count 'python install --with-deps chromium' "$calls")" == "$python_deps" ]]
    [[ "$(count 'python install chromium' "$calls")" == "$python_browser" ]]
    [[ "$(count 'node pdf about:blank .*' "$calls")" == "$node_launch" ]]
    [[ "$(count 'python pdf about:blank .*' "$calls")" == "$python_launch" ]]
    expected=()
    for producer in node python; do
      if [[ "$producer" == node ]]; then
        browser=$node_browser launch=$node_launch deps=$node_deps
      else
        browser=$python_browser launch=$python_launch deps=$python_deps
      fi
      if [[ "$browser" == 1 ]]; then expected+=("$producer install chromium"); fi
      if [[ "$launch" == 1 ]]; then expected+=("$producer pdf about:blank <temp>"); fi
      if [[ "$deps" == 1 ]]; then expected+=("$producer install --with-deps chromium"); fi
    done
    if [[ "$mode" != browser-failure ]]; then expected+=(continued); fi
    [[ "$(sed 's/\(pdf about:blank\) .*/\1 <temp>/' "$calls")" == "$(printf '%s\n' "${expected[@]}")" ]]
    # The launch artifact must be removed on success, failure and timeout.
    while read -r producer command url artifact; do
      if [[ "$command" == pdf && -e "$artifact" ]]; then
        echo "FAIL: launch artifact survived: $artifact" >&2
        exit 1
      fi
    done < "$calls"

    case "$mode" in
      success)
        absent 'install --with-deps' "$calls"
        absent 'launch check failed' "$output"
        [[ "$(grep -Ec '^browser launch check passed for (npx playwright|python -m playwright); OS dependency stage skipped \([0-9]+ ms\)$' "$output")" == 2 ]] ;;
      fallback|failure|launch-hang|hang-failure|hang)
        grep -q "launch check failed; running the OS dependency stage" "$output" ;;
      browser-failure)
        absent "$target pdf" "$calls"
        absent 'install --with-deps' "$calls" ;;
    esac
    case "$mode" in
      fallback|launch-hang)
        absent 'dependency install failed' "$output"
        absent 'exceeded' "$output" ;;
      failure|hang-failure)
        grep -q "dependency install failed" "$output" ;;
      hang)
        grep -q "dependency install exceeded 1s" "$output"
        if [[ "$target" == node ]]; then
          grep -q "earlier dependency stage exceeded 1s; skipping python -m playwright dependency stage" "$output"
        fi ;;
    esac
    case "$mode" in
      hang|hang-failure|launch-hang)
        # The bound, not the 60 s mock, ended the stage.
        (( elapsed < 30 ))
        # timeout killed the process group, including the grandchild.
        grandchild="$(cat "$calls.grandchild")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          kill -0 "$grandchild" 2>/dev/null || break
          sleep 0.2
        done
        if kill -0 "$grandchild" 2>/dev/null; then
          echo "FAIL: grandchild $grandchild survived the bound" >&2
          exit 1
        fi
        ;;
    esac

    if [[ "$mode" == browser-failure ]]; then
      # A missing browser stays terminal: nonzero, and the block stops.
      [[ "$status" == 42 ]]
      [[ "$(count continued "$calls")" == 0 ]]
    else
      [[ "$status" == 0 ]]
      [[ "$(count continued "$calls")" == 1 ]]
    fi
    echo "PASS: $mode ($target)"
  done
done
