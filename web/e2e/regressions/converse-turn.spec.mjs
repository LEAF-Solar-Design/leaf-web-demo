import { test, expect, unsupportedLocal } from './_harness.mjs'

test('Converse creates a session, streams a reply and cancels to idle', async ({ page, stack }) => {
  // stack.mjs privateEnvironment owns the launched harness environment; do
  // not trust an ambient host flag or send a turn to an unverified agent.
  if (stack.env?.LEAF_AGENT_MOCK !== '1') unsupportedLocal('Converse regression requires LEAF_AGENT_MOCK=1; real agents are forbidden')

  // site/productSurfaces.js: Ops seats the Project ribbon, including the
  // ribbonClusters.js conversation:new action, labelled New conversation.
  const response = await page.goto('/app?surface=ops')
  expect(response.ok()).toBe(true)
  await expect(page.locator('.studio-shell[data-scene="app"][data-mode="console"]')).toBeVisible()

  // site/WorkspaceEnvironment.jsx supplies these chooser controls when seated.
  const environment = page.locator('section.workspace-environment')
  if (await environment.count() && await environment.getAttribute('data-entered') !== 'true') {
    const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
    if (await resume.isVisible()) await resume.click()
    else await environment.getByRole('button', { name: 'Describe what to create', exact: true }).click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
  }
  // App.jsx gates onNew on signedIn, even on the auth-off local stack.
  // Keep that unavailable entry red instead of bypassing it with an API call.
  const create = page.getByRole('button', { name: /^New conversation(?:$|\s)/ })
  if (await create.count() === 0) {
    test.fail(true, 'Missing New conversation entry control on the local Ops Project ribbon (productSurfaces.js / ribbonClusters.js)')
  }
  await expect(create).toBeVisible()
  if (await create.isDisabled()) {
    test.fail(true, 'Missing usable New conversation control on the local stack: App.jsx gates conversation.onNew on signedIn')
  }
  await expect(create).toBeEnabled()
  await create.click()

  // New conversation clears the old session; the first prompt attaches a new
  // one. PromptBox.jsx command-bar/Enter and converse.js POST /api/sessions.
  const session = page.waitForResponse((reply) => reply.request().method() === 'POST'
    && new URL(reply.url()).origin === stack.baseURL
    && new URL(reply.url()).pathname === '/api/sessions', { timeout: 15_000 })
  await page.getByTestId('command-bar').fill('list layers')
  await page.getByTestId('command-bar').press('Enter')
  const created = await session
  expect(created.ok()).toBe(true)
  expect((await created.json()).session_id).toBeTruthy()

  // ConversePanel.jsx renders streamed text in .converse-msg.assistant and
  // exposes Stop (Interrupt this turn) while busy, then a writable Reply.
  const panel = page.locator('.converse-card')
  await expect(panel.locator('.converse-msg.assistant').first()).toContainText(/\S/)
  const cancel = panel.getByRole('button', { name: 'Stop', exact: true })
  if (!await cancel.isVisible()) {
    test.fail(true, 'Missing cancel-turn control after the first assistant event: the scripted mock finishes list layers before ConversePanel can expose Stop')
  }
  await expect(cancel).toBeVisible()
  await cancel.click()
  await expect(cancel).toHaveCount(0)
  await expect(panel.getByRole('textbox', { name: 'Reply to the assistant', exact: true })).toBeEditable()
})
