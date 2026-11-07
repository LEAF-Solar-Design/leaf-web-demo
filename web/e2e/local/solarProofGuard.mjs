import { basename, dirname, resolve } from 'node:path'

export function requireSolarFlags(env = process.env) {
  if (!['VITE_CAD_EDIT', 'VITE_SOLAR_FLOW_RAIL', 'VITE_SOLAR_SETTINGS_FORM'].every(key => env[key] === '1')) {
    throw new Error('UNSUPPORTED_LOCAL: solar proof needs VITE_CAD_EDIT, VITE_SOLAR_FLOW_RAIL and VITE_SOLAR_SETTINGS_FORM = 1')
  }
}

export async function installSpendGuard(page) {
  const hits = []
  const hosts = new Set(['developer.api.autodesk.com', 'api.leafdesign.ai'])
  await page.route(url => hosts.has(url.hostname), async route => {
    hits.push(new URL(route.request().url()).hostname)
    await route.abort()
  })
  return function assertNoSpend() {
    if (hits.length) throw new Error(`SPEND_GUARD: blocked requests to ${hits.join(', ')}`)
  }
}

export function assertRunRoot(storeDir) {
  if (typeof storeDir !== 'string' || !storeDir.trim()) throw new Error('Invalid solar proof store directory')
  const runRoot = dirname(resolve(storeDir))
  const name = basename(runRoot)
  if (name !== '.e2e-run' && !/^leaf-unified-local-e2e-\d{8}-\d{6}$(?![\s\S])/.test(name)) {
    throw new Error(`Invalid solar proof run root: ${runRoot}`)
  }
  return runRoot
}
