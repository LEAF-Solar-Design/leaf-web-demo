// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import LightsControl from './LightsControl.jsx'

afterEach(() => { cleanup(); vi.useRealTimers() })
const reference = { id: 'ha-relay-device01', version: 1, digest: 'a'.repeat(64) }
const actions = ['light-turn-on', 'light-turn-off'].map((id) => ({ connection_id: 'ha-relay-device01', connector_ref: reference, operation_ref: { ...reference, id } }))
const lights = [{ id: 'light.kitchen', name: 'Kitchen' }, { id: 'light.all_lights', name: 'All lights' }, { id: 'light.hall' }]
function setup(overrides = {}, props = {}) {
  const client = {
    getActionCatalog: vi.fn().mockResolvedValue({ actions, next_cursor: null }),
    getConnection: vi.fn().mockResolvedValue({ grant_revision: 7 }),
    submitAction: vi.fn().mockResolvedValue({ status: 'admitted', run: { run_id: 'run-1', state: 'accepted' } }),
    getAction: vi.fn().mockResolvedValue({ run: { run_id: 'run-1', state: 'succeeded' } }),
    ...overrides,
  }
  const result = render(<LightsControl client={client} projectId="project" connectionId="ha-relay-device01" lights={lights} {...props} />)
  return { client, ...result }
}
const ready = () => screen.findByRole('group', { name: 'All lights' })
const tick = () => act(async () => vi.advanceTimersByTimeAsync(1000))

it('lists All lights first, per-light names and entity fallbacks without opaque data', async () => {
  const { container } = setup({}, { lights: [...lights, { entity_id: 'switch.secret', token: 'private-payload' }] })
  await ready()
  expect(screen.getAllByRole('group').map((row) => row.getAttribute('aria-label'))).toEqual(['All lights', 'Kitchen', 'light.hall'])
  expect(container.innerHTML).not.toMatch(/private-payload|switch.secret/)
})

it('turns All lights off using current grant and catalog references, then confirms Done', async () => {
  const { client } = setup()
  const row = within(await ready())
  vi.useFakeTimers()
  fireEvent.click(row.getByRole('button', { name: 'Off' }))
  await act(async () => {})
  expect(row.getByRole('status')).toHaveTextContent('Sending')
  expect(client.getConnection).toHaveBeenCalledWith('project', 'ha-relay-device01', { signal: expect.any(AbortSignal) })
  expect(client.submitAction).toHaveBeenCalledWith('project', {
    schema_version: 1, connection_id: 'ha-relay-device01', connector_ref: reference,
    operation_ref: actions[1].operation_ref, expected_grant_revision: 7,
    idempotency_key: expect.stringMatching(/^lights-[0-9a-f]{24}$/), arguments: { entity_id: 'light.all_lights' },
  }, { signal: expect.any(AbortSignal) })
  await tick()
  expect(client.getAction).toHaveBeenCalledWith('project', 'run-1', { signal: expect.any(AbortSignal) })
  expect(row.getByRole('status')).toHaveTextContent('Done')
})

it('sends the chosen color through light-turn-on with a fresh key for each tap', async () => {
  const { client } = setup({ submitAction: vi.fn().mockResolvedValue({ status: 'admitted', run: { state: 'succeeded' } }) })
  await ready()
  fireEvent.change(screen.getByLabelText('Kitchen color'), { target: { value: '#12ab34' } })
  await waitFor(() => expect(within(screen.getByRole('group', { name: 'Kitchen' })).getByRole('status')).toHaveTextContent('Done'))
  expect(client.submitAction.mock.calls[0][1]).toMatchObject({ operation_ref: actions[0].operation_ref, arguments: { entity_id: 'light.kitchen', color: '#12ab34' } })
  fireEvent.change(screen.getByLabelText('Kitchen color'), { target: { value: '#abcdef' } })
  await waitFor(() => expect(client.submitAction).toHaveBeenCalledTimes(2))
  expect(client.submitAction.mock.calls[0][1].idempotency_key).not.toBe(client.submitAction.mock.calls[1][1].idempotency_key)
})

it('humanizes a refusal and never shows Done or secret-bearing errors', async () => {
  const { client, container } = setup({ submitAction: vi.fn().mockResolvedValue({ status: 'refused', code: 'forbidden', message: 'private-error' }) })
  const row = within(await ready())
  fireEvent.click(row.getByRole('button', { name: 'On' }))
  await waitFor(() => expect(row.getByRole('status')).toHaveTextContent('You do not have permission'))
  expect(screen.queryByText('Done')).not.toBeInTheDocument()
  expect(container.innerHTML).not.toContain('private-error')
  expect(client.getAction).not.toHaveBeenCalled()
})

it('locks all controls for the row during submission and polling', async () => {
  let settle
  const { client } = setup({ getConnection: vi.fn(() => new Promise((resolve) => { settle = resolve })) })
  const row = within(await ready())
  vi.useFakeTimers()
  fireEvent.click(row.getByRole('button', { name: 'On' }))
  fireEvent.click(row.getByRole('button', { name: 'Off' }))
  fireEvent.change(row.getByLabelText('All lights color'), { target: { value: '#123456' } })
  expect(client.getConnection).toHaveBeenCalledTimes(1)
  expect(row.getByRole('button', { name: 'Off' })).toBeDisabled()
  await act(async () => settle({ grant_revision: 9 }))
  fireEvent.click(row.getByRole('button', { name: 'Off' }))
  expect(client.submitAction).toHaveBeenCalledTimes(1)
  await tick()
  expect(row.getByRole('button', { name: 'Off' })).not.toBeDisabled()
})

it('asks to connect when the catalog has only another connection', async () => {
  setup({ getActionCatalog: vi.fn().mockResolvedValue({ actions: actions.map((action) => ({ ...action, connection_id: 'other' })) }) })
  expect(await screen.findByText('Connect to Ops first')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'On' })).not.toBeInTheDocument()
})

it('follows at most four catalog pages and refreshes the controls', async () => {
  const getActionCatalog = vi.fn().mockImplementation(async (_, { after }) => ({ actions: after === '3' ? actions : [], next_cursor: String(Number(after || 0) + 1) }))
  setup({ getActionCatalog })
  await ready()
  expect(getActionCatalog).toHaveBeenCalledTimes(4)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await ready()
  expect(getActionCatalog).toHaveBeenCalledTimes(8)
})

it('uses a named group as All lights and offers loading when no lights exist', async () => {
  const { unmount } = setup({}, { lights: [{ id: 'group.everything', name: 'All lights' }] })
  expect(within(await ready()).getByText('group.everything')).toBeInTheDocument()
  unmount()
  const onLoadLights = vi.fn()
  setup({}, { lights: [], onLoadLights })
  fireEvent.click(await screen.findByRole('button', { name: 'Load lights' }))
  expect(onLoadLights).toHaveBeenCalledTimes(1)
})

it('humanizes failed snapshots and stops polling', async () => {
  const { client } = setup({ getAction: vi.fn().mockResolvedValue({ run: { state: 'failed' }, receipts: [{ failure_code: 'unavailable', message: 'secret' }] }) })
  const row = within(await ready())
  vi.useFakeTimers()
  fireEvent.click(row.getByRole('button', { name: 'Off' }))
  await tick()
  expect(row.getByRole('status')).toHaveTextContent('The lights are unavailable')
  await tick()
  expect(client.getAction).toHaveBeenCalledTimes(1)
})

it('stops after fifteen seconds and cancels polling when unmounted', async () => {
  const { client, unmount } = setup({ getAction: vi.fn().mockResolvedValue({ run: { state: 'running' } }) })
  const row = within(await ready())
  vi.useFakeTimers()
  fireEvent.click(row.getByRole('button', { name: 'On' }))
  await act(async () => {})
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(row.getByRole('status')).toHaveTextContent('did not confirm in time')
  const polls = client.getAction.mock.calls.length
  expect(polls).toBeGreaterThan(0)
  expect(polls).toBeLessThanOrEqual(15)
  fireEvent.click(row.getByRole('button', { name: 'Off' }))
  await act(async () => {})
  unmount()
  await tick()
  expect(client.getAction).toHaveBeenCalledTimes(polls)
})
