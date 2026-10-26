import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TextEncoder } from 'node:util'
import { ActionDefinitionPanel } from './ActionDefinitionPanel.jsx'
import { ManualWorkflowDefinitionPanel } from './ManualWorkflowDefinitionPanel.jsx'
import { createIntegrationsClient, integrationRequestError } from './client.js'

const reference = { id: 'connector', version: 1, digest: 'a'.repeat(64) }
const entry = { connection_id: 'orders', connection_title: 'Orders', connector_ref: reference,
  operation_ref: { ...reference, id: 'send' }, title: 'Send order', form: { fields: [], submit_label: 'Send' } }
function props() {
  return { projectId: '11111111-1111-4111-8111-111111111111', signal: new AbortController().signal,
    guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn(), onSaved: vi.fn(), client: {
      getActionCatalog: vi.fn().mockResolvedValue({ actions: [entry], next_cursor: null }),
      saveElement: vi.fn(async (_, element) => ({ element, revision: 1 })),
    } }
}
const click = (name) => fireEvent.click(screen.getByRole('button', { name }))
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
async function actionDraft() {
  click('Load permitted actions')
  await screen.findByRole('option', { name: /Send order/ })
  await waitFor(() => expect(screen.getByLabelText('Form title')).toBeEnabled())
  change('Form title', 'Order form'); change('Form stable ID', 'order-form')
  change('Permitted action', screen.getByRole('option', { name: /Send order/ }).value)
  click('Save action form')
}
beforeEach(() => vi.stubGlobal('TextEncoder', TextEncoder))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('allows another action form after verified success', async () => {
  const input = props()
  render(<ActionDefinitionPanel {...input} />)
  await actionDraft()
  fireEvent.click(await screen.findByRole('button', { name: 'Create another' }))
  expect(screen.getByLabelText('Form title')).toBeEnabled()
  expect(screen.getByLabelText('Form stable ID')).toHaveValue('')
})
it('abandons uncertain action creation without resubmitting and blocks the abandoned ID', async () => {
  const input = props()
  input.client.saveElement.mockRejectedValue(new Error('lost'))
  render(<ActionDefinitionPanel {...input} />)
  await actionDraft()
  fireEvent.click(await screen.findByRole('button', { name: 'Leave recovery and create a different form' }))
  change('Form title', 'Order form'); change('Form stable ID', 'order-form')
  change('Permitted action', screen.getByRole('option', { name: /Send order/ }).value)
  click('Save action form')
  await screen.findByRole('alert')
  expect(input.client.saveElement).toHaveBeenCalledTimes(1)
})
it('identifies and focuses the step missing an action', async () => {
  render(<ManualWorkflowDefinitionPanel {...props()} />)
  click('Open action catalog')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeEnabled())
  change('Definition title', 'Order workflow'); change('Definition stable ID', 'orders')
  click('Save workflow definition')
  expect(await screen.findByRole('alert')).toHaveTextContent('Step 1: Choose a catalog action.')
  await waitFor(() => expect(screen.getByLabelText('Step 1 action')).toHaveFocus())
})
it('reports an attempted submit before the catalog loads', async () => {
  render(<ManualWorkflowDefinitionPanel {...props()} />)
  change('Definition title', 'Order workflow'); change('Definition stable ID', 'orders')
  fireEvent.submit(screen.getByLabelText('Definition title').closest('form'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Step 1: Choose a catalog action.')
})
it('distinguishes missing resources and write permissions without suggesting a read retry', () => {
  expect(integrationRequestError(404, 'GET', '/integrations/elements/view?revision=999')).toMatchObject({ retryable: false, message: expect.stringContaining('could not be found') })
  expect(integrationRequestError(403, 'DELETE', '/integrations/connections/one').message).toContain('remove this connection')
  expect(integrationRequestError(409, 'POST', '/integrations/manual-workflows').message).toContain('workflow conflicts')
})
it('marks missing site setup as non-retryable with administrator guidance', async () => {
  const client = createIntegrationsClient({ origin: '' })
  await expect(client.getScope(props().projectId, { signal: props().signal })).rejects.toMatchObject({ retryable: false,
    message: 'Integrations are not set up for this site. Ask your administrator to enable them.' })
})
