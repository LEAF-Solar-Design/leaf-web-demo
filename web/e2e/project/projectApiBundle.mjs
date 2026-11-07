import { build as esbuild } from 'esbuild'
import { fileURLToPath } from 'node:url'
import { environmentDefinitions } from '../../scripts/next-runtime.mjs'

export const MAX_BUNDLE_BYTES = 4 * 1024 * 1024
export const BUILD_TIMEOUT_MS = 30_000

// Instantiate once in the spec's worker module; cache failures as well as success.
export function createProjectApiBundle(env = process.env, build = esbuild) {
  let pending
  return () => pending ??= (async () => {
    let timer
    try {
      const result = await Promise.race([
        build({ entryPoints: [fileURLToPath(new URL('../../src/api.js', import.meta.url))],
          globalName: '__pp3bApi', bundle: true, write: false, platform: 'browser', format: 'iife',
          define: environmentDefinitions(env, false), logLevel: 'silent' }),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('PP3B_API_BUILD')), BUILD_TIMEOUT_MS) }),
      ])
      const content = result.outputFiles?.[0]?.text
      if (result.outputFiles?.length !== 1 || typeof content !== 'string' || !content.length ||
        Buffer.byteLength(content) > MAX_BUNDLE_BYTES) throw new Error('PP3B_API_BUILD')
      return content
    } catch { throw new Error('PP3B_API_BUILD') }
    finally { clearTimeout(timer) }
  })()
}

export async function injectProjectApi(page, origin, content) {
  try {
    if (new URL(page.url()).origin !== origin) throw new Error('PP3B_API_ORIGIN')
    await page.addScriptTag({ content })
  } catch { throw new Error('PP3B_API_INJECT') }
}
