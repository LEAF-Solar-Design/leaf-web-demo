import { test, expect, unsupportedLocal } from './_harness.mjs'

test('PromptBox submits to the real local prompt router', async ({ page, stack }) => {
  // Routing may also start a conversation; keep that downstream turn mock-only.
  if (stack.env?.LEAF_AGENT_MOCK !== '1') unsupportedLocal('Prompt routing requires the mock agent to prevent downstream provider spend')
  const response = await page.goto('/app')
  expect(response.ok()).toBe(true)
  await expect(page.locator('.studio-shell[data-scene="app"][data-mode="console"]')).toBeVisible()

  // site/WorkspaceEnvironment.jsx: the chooser's real entry controls.
  const environment = page.locator('section.workspace-environment')
  if (await environment.count() && await environment.getAttribute('data-entered') !== 'true') {
    const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
    if (await resume.isVisible()) await resume.click()
    else await environment.getByRole('button', { name: 'Describe what to create', exact: true }).click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
  }

  // components/PromptBox.jsx: textarea data-testid="command-bar" and
  // Enter dispatchPrompt(); api.js nlPrompt POSTs /api/nl-prompt.
  const prompt = page.getByTestId('command-bar')
  await expect(prompt).toBeVisible()
  const routed = page.waitForResponse((reply) => reply.request().method() === 'POST'
    && new URL(reply.url()).origin === stack.baseURL
    && new URL(reply.url()).pathname === '/api/nl-prompt', { timeout: 15_000 })
  await prompt.fill('Summarize the purpose of this drawing')
  await prompt.press('Enter')
  const reply = await routed
  expect(reply.fromServiceWorker()).toBe(false)
  expect(reply.status()).toBe(200)
  expect(reply.request().postDataJSON()).toMatchObject({ text: 'Summarize the purpose of this drawing' })
  const body = await reply.json()
  expect(body).toHaveProperty('lane')
  expect(body).toHaveProperty('tool')
  expect(typeof body.lane).toBe('string')
  expect(body.lane.length).toBeGreaterThan(0)
})
