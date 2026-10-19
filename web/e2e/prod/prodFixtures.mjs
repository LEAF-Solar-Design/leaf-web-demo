import { test as base, expect } from '@playwright/test'
import { validateProdConfig } from './prodConfig.mjs'
import { createGuardedContext, denialLedger } from './prodGuard.mjs'

const rawBrowsers = new WeakMap()
const activeLedgers = new WeakMap()
const admittedConfig = validateProdConfig()

export { expect }
export const test = base.extend({
  browser: [async ({ browser }, use) => {
    const facade = new Proxy(Object.create(null), {
      get(target, key) {
        if (key === 'then' || typeof key === 'symbol') return undefined
        return () => {
          activeLedgers.get(base.info())?.record('ESCAPE')
          throw new Error('PROD_POLICY_ESCAPE')
        }
      },
    })
    rawBrowsers.set(facade, browser)
    try { await use(facade) } finally { rawBrowsers.delete(facade) }
  }, { scope: 'worker' }],
  prodConfig: [admittedConfig, { option: true }],
  prodOffline: [false, { option: true }],
  prodAuthenticated: [false, { option: true }],
  _prodLedger: async ({}, use) => { await use(denialLedger()) },
  _prodAdmission: [async ({ prodConfig, prodOffline, _prodLedger }, use, testInfo) => {
    // Also validate defensively when this fixture is used without the prod config.
    if (!prodOffline) validateProdConfig()
    if (!prodConfig.enabled) testInfo.skip(true, 'PROD_OPTIONAL_DISABLED')
    activeLedgers.set(testInfo, _prodLedger)
    testInfo.attach = async () => {
      _prodLedger.record('ARTIFACT')
      throw new Error('PROD_POLICY_ARTIFACT')
    }
    try { await use() } finally {
      activeLedgers.delete(testInfo)
      testInfo.annotations.push({ type: 'prod-denials', description: JSON.stringify(_prodLedger.snapshot()) })
      _prodLedger.assertClean()
    }
  }, { auto: 'all-hooks-included', box: true }],
  // Playwright 1.60.0's artifact auto fixture also writes error-context.md.
  // Keep its lifecycle metadata, but never start the recorder or context capture.
  // The offline failure canary pins this private dependency to the locked version.
  _setupArtifacts: [async ({}, use) => { await use() }, { auto: 'all-hooks-included', title: 'trace recording', box: true, timeout: 0 }],
  _prodSession: async ({ browser, prodConfig, prodOffline, prodAuthenticated, _prodLedger, _prodAdmission }, use) => {
    const session = await createGuardedContext(rawBrowsers.get(browser), {
      origin: prodConfig.origin, token: prodConfig.token, authenticated: prodAuthenticated,
    }, _prodLedger, { offline: prodOffline })
    try { await use(session) } finally { await session.close() }
  },
  context: async ({ _prodSession }, use) => { await use(_prodSession.context) },
  request: async ({ _prodSession }, use) => { await use(_prodSession.request) },
  page: async ({ context }, use) => { await use(await context.newPage()) },
})
