/**
 * The recording half of record-loaded-modules.mjs. Runs on the loader thread.
 *
 * `load` rather than `resolve` is the honest hook: resolve fires for
 * specifiers node may still decline to load, while load fires for modules it
 * actually evaluates. The guard asks "does importing this LOAD Hono", so the
 * evaluated set is the set that answers it.
 */
let port;

export async function initialize(data) {
  port = data.port;
}

export async function load(url, context, nextLoad) {
  if (port && url.startsWith("file:")) port.postMessage(url);
  return nextLoad(url, context);
}
