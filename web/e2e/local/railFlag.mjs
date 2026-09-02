// The one-shell rail, armed per PAGE by intercepting the runtime-flags file
// the container writes at boot (deploy/write-runtime-flags.sh). Shared by
// every spec that runs a row under the studio: the interception must land
// BEFORE the first navigation, because index.html loads /runtime-flags.js
// synchronously ahead of the bundle and the reader latches at module eval.
export async function setRail(page, value) {
  await page.route('**/runtime-flags.js', (route) => route.fulfill({
    contentType: 'text/javascript',
    body: `window.__LEAF_FLAGS = { oneShell: '${value}' }`,
  }))
}
