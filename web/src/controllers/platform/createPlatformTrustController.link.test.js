import { describe, expect, it, vi } from 'vitest'
import { createPlatformTrustController } from './createPlatformTrustController.js'

const fakeToken = 'sk-ant-oat01-FAKE-link-secret'
const serviceError = 'The account service could not finish the link. Reload and check your Claude accounts before you retry.'

describe('honest Claude link outcomes', () => {
  it.each([null, undefined, {}, { linked: false }, { linked: 'true' }])('requires linked:true for %j', async (value) => {
    const controller = createPlatformTrustController({ services: { linkClaudeGrant: async () => value }, formatError: () => 'custom error' })
    expect(await controller.linkClaude(fakeToken, 'oauth')).toBeNull()
    expect(controller.getSnapshot()).toMatchObject({ grant: null, grantBusy: false,
      grantErr: 'The server did not confirm the link. Reload and check your Claude accounts before you retry.' })
  })

  it('returns the confirmed response unchanged', async () => {
    const value = { linked: true, accounts: [{ id: 'a', plan: 'max' }] }
    const linkClaudeGrant = vi.fn().mockResolvedValue(value)
    const controller = createPlatformTrustController({ services: { linkClaudeGrant } })
    expect(await controller.linkClaude(fakeToken, 'oauth', 'Work', 'max')).toBe(value)
    expect(linkClaudeGrant).toHaveBeenCalledWith(fakeToken, 'oauth', 'Work', 'max')
    expect(controller.getSnapshot()).toMatchObject({ grant: value, grantBusy: false, grantErr: null })
  })

  it.each([
    [400, 'That credential was not accepted. Get a new one and paste it again.'],
    [422, 'That credential was not accepted. Get a new one and paste it again.'],
    [401, 'Your session has expired. Sign in again, then link your account.'],
    [403, 'Only the workspace owner can link Claude accounts.'],
    [500, serviceError], [503, serviceError], [409, 'Safe conflict'],
  ])('maps numeric HTTP %s without exposing the response body', async (status, message) => {
    const controller = createPlatformTrustController({ services: {
      linkClaudeGrant: async () => { throw { status, message: 'Safe conflict', body: fakeToken } },
    } })
    await controller.linkClaude(fakeToken, 'oauth')
    expect(controller.getSnapshot().grantErr).toBe(message)
    expect(JSON.stringify(controller.getSnapshot())).not.toContain(fakeToken)
    expect(controller.getSnapshot().authRequired).toBe(status === 401)
  })

  it('redacts a submitted token from fallback errors', async () => {
    const controller = createPlatformTrustController({ services: {
      linkClaudeGrant: async () => { throw new Error(`Rejected ${fakeToken}`) },
    } })
    await controller.linkClaude(`  ${fakeToken}  `, 'oauth')
    expect(controller.getSnapshot().grantErr).toBe(serviceError)
  })

  it('does not let a stale response replace newer grant state', async () => {
    let resolve
    const controller = createPlatformTrustController({ services: {
      linkClaudeGrant: () => new Promise((done) => { resolve = done }),
    } })
    const request = controller.linkClaude(fakeToken, 'oauth')
    controller.clearSessionReads()
    resolve({ linked: true })
    expect(await request).toBeNull()
    expect(controller.getSnapshot().grant).toBeNull()
  })
})
