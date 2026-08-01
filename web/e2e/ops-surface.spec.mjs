import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

async function install(page, { secret = 'fixture-secret', conflictOnAccountPut = false } = {}) {
  const state = makeCatProofState()
  const evidence = {
    list: 0, mutations: [], opsHeaders: [], nonOpsSecrets: [],
    accountReads: 0, accountWrites: [], accountAuth: [], accountOpsSecrets: [],
  }
  let disabled = false
  let approvalRequired = false
  let revision = 0
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const requestSecret = request.headers()['x-ops-secret'] || null
    if (url.pathname === '/api/admin/account-controls') {
      const authorized = request.headers().authorization === 'Bearer fixture-jwt'
      evidence.accountAuth.push(request.headers().authorization || null)
      if (requestSecret) evidence.accountOpsSecrets.push(requestSecret)
      if (request.method() === 'GET') evidence.accountReads += 1
      if (!authorized) {
        await route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: 'account admin required' }) })
        return
      }
      if (request.method() === 'PUT') {
        const body = request.postDataJSON()
        evidence.accountWrites.push(body)
        if (conflictOnAccountPut) {
          await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'revision conflict' }) })
          return
        }
        approvalRequired = body.tool_publication_approval_required
        revision += 1
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ tool_publication_approval_required: approvalRequired, revision }),
      })
      return
    }
    if (url.pathname === '/api/ops/tenants') {
      evidence.list += 1
      evidence.opsHeaders.push(requestSecret)
      const forbidden = requestSecret !== secret
      await route.fulfill({
        status: forbidden ? 403 : 200,
        contentType: 'application/json',
        body: JSON.stringify(forbidden ? { error: { message: 'ops role required' } } : {
          tenants: [{ tenant_id: 'tenant-alpha', runs: 7, usd_est: 0.125, disabled }],
        }),
      })
      return
    }
    const match = url.pathname.match(/^\/api\/ops\/tenants\/([^/]+)\/(disable|enable)$/)
    if (match) {
      evidence.opsHeaders.push(requestSecret)
      const forbidden = requestSecret !== secret
      disabled = match[2] === 'disable'
      if (!forbidden) evidence.mutations.push({ tenant: match[1], action: match[2] })
      await route.fulfill({ status: forbidden ? 403 : 200, contentType: 'application/json', body: JSON.stringify({ disabled }) })
      return
    }
    if (requestSecret) evidence.nonOpsSecrets.push({ path: url.pathname, secret: requestSecret })
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  return evidence
}

test('gated unified ops lists and disables a tenant with two-step confirmation', async ({ page }) => {
  const evidence = await install(page)
  await page.addInitScript(() => {
    localStorage.setItem('leaf.ops_secret', 'fixture-secret')
    localStorage.setItem('leaf.jwt', 'fixture-jwt')
  })
  await page.goto('/try?ops=1')
  const ops = page.getByRole('dialog', { name: 'Internal ops' })
  await expect(ops.getByRole('button', { name: 'Hide drawer' })).toBeFocused()
  await expect(ops).toContainText('Require independent approval before publishing authored tools')
  await expect(ops).toContainText('Off. Authored tools can publish without an independent approval.')
  await ops.getByRole('button', { name: /^Off: require independent approval/ }).click()
  await expect(ops).toContainText('Turn on strict independent publication approval?')
  await ops.getByRole('button', { name: 'Keep current' }).click()
  expect(evidence.accountWrites).toEqual([])
  await ops.getByRole('button', { name: /^Off: require independent approval/ }).click()
  await ops.getByRole('button', { name: 'Confirm' }).click()
  await expect(ops).toContainText('On. An independent trusted approver must approve each publication.')
  expect(evidence.accountWrites).toEqual([{
    tool_publication_approval_required: true,
    expected_revision: 0,
  }])
  await ops.getByRole('button', { name: /^On: require independent approval/ }).click()
  await expect(ops).toContainText('Turn off independent publication approval?')
  await ops.getByRole('button', { name: 'Keep current' }).click()
  await ops.getByRole('button', { name: /^On: require independent approval/ }).click()
  await ops.getByRole('button', { name: 'Confirm' }).click()
  await expect(ops).toContainText('Off. Authored tools can publish without an independent approval.')
  expect(evidence.accountWrites).toEqual([
    { tool_publication_approval_required: true, expected_revision: 0 },
    { tool_publication_approval_required: false, expected_revision: 1 },
  ])
  await expect(ops).toContainText('tenant-alpha')
  await expect(ops).toContainText('Active')
  await ops.getByRole('button', { name: 'Disable' }).click()
  await expect(ops).toContainText('Disable tenant-alpha?')
  await ops.getByRole('button', { name: 'Keep' }).click()
  expect(evidence.mutations).toEqual([])
  await ops.getByRole('button', { name: 'Disable' }).click()
  await ops.getByRole('button', { name: 'Disable' }).click()
  await expect(ops).toContainText('Disabled')
  expect(evidence.mutations).toEqual([{ tenant: 'tenant-alpha', action: 'disable' }])
  expect(evidence.opsHeaders.length).toBeGreaterThanOrEqual(2)
  expect(evidence.opsHeaders.every((value) => value === 'fixture-secret')).toBe(true)
  expect(evidence.accountAuth.every((value) => value === 'Bearer fixture-jwt')).toBe(true)
  expect(evidence.accountOpsSecrets).toEqual([])
  expect(evidence.nonOpsSecrets).toEqual([])
  await expect(page.locator('body')).not.toContainText('fixture-secret')
  await expect(page).not.toHaveURL(/fixture-secret/)
  await page.keyboard.press('Escape')
  await expect(ops).toHaveCount(0)
  await expect(page).toHaveURL(/\/try\?ops=1$/)
})

test('ops role denial is calm and the flag is the only entry', async ({ page }) => {
  const denied = await install(page)
  await page.goto('/try?ops=1')
  await expect(page.getByText(/ops role required/)).toBeVisible()
  await expect(page.getByText(/Account owner required/)).toBeVisible()
  await expect(page.getByText('tenant-alpha')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Disable' })).toHaveCount(0)
  expect(denied.mutations).toEqual([])

  const unflagged = await page.context().newPage()
  const evidence = await install(unflagged)
  await unflagged.addInitScript(() => localStorage.setItem('leaf.ops_secret', 'fixture-secret'))
  await unflagged.goto('/try')
  await expect(unflagged.getByRole('dialog', { name: 'Internal ops' })).toHaveCount(0)
  expect(evidence.list).toBe(0)
})

test('account-control revision conflict asks for a refresh and preserves the displayed state', async ({ page }) => {
  const evidence = await install(page, { conflictOnAccountPut: true })
  await page.addInitScript(() => {
    localStorage.setItem('leaf.ops_secret', 'fixture-secret')
    localStorage.setItem('leaf.jwt', 'fixture-jwt')
  })
  await page.goto('/try?ops=1')
  const ops = page.getByRole('dialog', { name: 'Internal ops' })
  await ops.getByRole('button', { name: /^Off: require independent approval/ }).click()
  await ops.getByRole('button', { name: 'Confirm' }).click()
  await expect(ops.getByRole('alert')).toContainText('Account controls changed elsewhere. Refresh before trying again.')
  await expect(ops.getByRole('button', { name: /^Off: require independent approval/ })).toBeVisible()
  expect(evidence.accountWrites).toEqual([{
    tool_publication_approval_required: true,
    expected_revision: 0,
  }])
})
