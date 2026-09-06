import { describe, expect, it } from 'vitest'
import {
  allowedStagingHostnames,
  assertAllowedStagingHost,
  resolveStagingBaseURL,
  StagingHostError,
} from '../../e2e/staging/stagingConfig.mjs'

describe('staging host boundary', () => {
  it('defaults to the allowed staging host', () => {
    const baseURL = resolveStagingBaseURL({})
    expect(new URL(baseURL).hostname).toBe('platform-staging.leafdesign.ai')
    expect(allowedStagingHostnames({}).has('platform-staging.leafdesign.ai')).toBe(true)
    expect(assertAllowedStagingHost(baseURL, {}).hostname).toBe('platform-staging.leafdesign.ai')
  })

  it.each(['app.leafdesign.ai', 'platform.leafdesign.ai', 'APP.LEAFDESIGN.AI'])(
    'refuses the production override %s',
    (host) => {
      const env = { LEAF_E2E_STAGING_ALLOW_HOST: host }
      expect(() => allowedStagingHostnames(env)).toThrow(StagingHostError)
      expect(() => allowedStagingHostnames(env)).toThrow(`${host} is a production host`)
    },
  )

  it('accepts a non-production leafdesign.ai override', () => {
    const host = 'review-42.leafdesign.ai'
    const env = { LEAF_E2E_STAGING_ALLOW_HOST: host }
    expect(allowedStagingHostnames(env).has(host)).toBe(true)
    expect(assertAllowedStagingHost(`https://${host}`, env).hostname).toBe(host)
  })

  it('refuses a host outside leafdesign.ai', () => {
    const env = { LEAF_E2E_STAGING_ALLOW_HOST: 'review-42.example.com' }
    expect(() => allowedStagingHostnames(env)).toThrow(StagingHostError)
  })
})
