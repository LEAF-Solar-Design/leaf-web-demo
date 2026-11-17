import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07DrawingRoleReview from './X07DrawingRoleReview.jsx'
import X07DrawingRoleProfileReview from './X07DrawingRoleProfileReview.jsx'
import { execFileSync } from 'node:child_process'
import path from 'node:path'

afterEach(cleanup)
const row = { block: 'Review Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] }
const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
  module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
const scope = { authorityKey: 'customer-a-session', solarBinding: { project_id: 'synthetic-project' }, isCurrent: () => true }

it('requires one explicit confirmation and passes captured authority and drawing binding', async () => {
  let finish
  const confirm = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  fireEvent.submit(screen.getByRole('form', { name: 'Confirm drawing roles' }))
  await act(async () => {})
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0][0]).toEqual({ block: 'Review Panel', layer: 'MODULES', count: 3, design_window: [0, 0, 2, 2] })
  expect(confirm.mock.calls[0][1].authorityKey).toBe('customer-a-session')
  expect(confirm.mock.calls[0][1].solarBinding).toEqual(scope.solarBinding)
  await act(async () => { finish({ ok: true }) })
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
})

it('aborts a retired read and never offers stale confirmation', async () => {
  let resolve
  const client = { readContext: vi.fn(() => new Promise(done => { resolve = done })) }
  const confirm = vi.fn()
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  await act(async () => {})
  view.rerender(<X07DrawingRoleReview {...scope} isCurrent={() => false} projectClient={client} onConfirm={confirm} />)
  expect(client.readContext.mock.calls[0][0].signal.aborted).toBe(true)
  await act(async () => { resolve(reply) })
  expect(screen.queryByRole('button', { name: 'Confirm roles' })).toBeNull()
  expect(confirm).not.toHaveBeenCalled()
})

it('aborts pending confirmation on authority replacement and ignores its success', async () => {
  let finish
  const confirm = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await act(async () => {})
  view.rerender(<X07DrawingRoleReview {...scope} authorityKey="new-authority" projectClient={client} onConfirm={confirm} />)
  expect(confirm.mock.calls[0][1].signal.aborted).toBe(true)
  await act(async () => { finish({ ok: true }) })
  expect(screen.queryByText('Roles confirmed.')).toBeNull()
  expect(await screen.findByRole('button', { name: 'Confirm roles' })).toBeTruthy()
})

it('requires an explicit successful acknowledgment and retains choice for retry', async () => {
  for (const result of [null, undefined, true, 'saved', {}, { ok: 1 }, { ok: false }]) {
    const confirm = vi.fn().mockResolvedValueOnce(result).mockResolvedValueOnce({ ok: true })
    const client = { readContext: vi.fn().mockResolvedValue(reply) }
    const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Roles could not be saved.')
    expect(screen.queryByText('Roles confirmed.')).toBeNull()
    expect(screen.getByRole('radio', { name: /Review Panel/ })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
    expect(confirm).toHaveBeenCalledTimes(2)
    view.unmount()
  }
  const confirm = vi.fn().mockRejectedValueOnce(new Error('Retry the save')).mockResolvedValueOnce({ ok: true })
  render(<X07DrawingRoleReview {...scope} projectClient={{ readContext: async () => reply }} onConfirm={confirm} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await screen.findByRole('alert')
  expect(screen.queryByText('Roles confirmed.')).toBeNull()
  expect(screen.getByRole('radio', { name: /Review Panel/ })).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
})

it('retains an off-page selection with at most ten mounted choices and no refetch', async () => {
  const candidates = Array.from({ length: 23 }, (_, index) => ({ ...row, block: `Choice ${index}`, count: index + 1 }))
  const data = { ...reply.data.role_discovery, module: { first_choice: candidates[0], candidates, competing: true } }
  const client = { readContext: vi.fn().mockResolvedValue({ ok: true, data: { role_discovery: data } }) }
  const confirm = vi.fn().mockResolvedValue({ ok: true })
  render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  await screen.findByRole('button', { name: 'Confirm roles' })
  expect(screen.getAllByRole('radio')).toHaveLength(10)
  fireEvent.click(screen.getByRole('radio', { name: /Choice 4 on/ }))
  const next = screen.getByRole('button', { name: 'Next choices' })
  next.focus()
  expect(next).toHaveFocus()
  fireEvent.click(next)
  expect(screen.getByText('Choices page 2 of 3')).toBeTruthy()
  expect(screen.getAllByRole('radio')).toHaveLength(10)
  expect(screen.getByText(/Selected module set:/)).toHaveTextContent('Choice 4')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0][0].block).toBe('Choice 4')
  expect(client.readContext).toHaveBeenCalledTimes(1)
})

it('refuses duplicate block/layer identities before confirmation with a distinct control', async () => {
  const confirm = vi.fn()
  for (const duplicate of [{ ...row, count: 8 }, { ...row, insertion_bounds: [0, 0, 9, 9] }]) {
    const data = { ...reply.data.role_discovery, module: { first_choice: row, candidates: [row, duplicate], competing: true } }
    const view = render(<X07DrawingRoleReview {...scope} projectClient={{ readContext: async () => ({ ok: true, data: { role_discovery: data } }) }} onConfirm={confirm} />)
    await screen.findByText('Drawing roles could not be read.')
    expect(screen.queryByRole('button', { name: 'Confirm roles' })).toBeNull()
    expect(confirm).not.toHaveBeenCalled()
    view.unmount()
  }
  const data = { ...reply.data.role_discovery, module: { first_choice: row, candidates: [row, { ...row, block: 'Distinct Panel' }], competing: true } }
  render(<X07DrawingRoleReview {...scope} projectClient={{ readContext: async () => ({ ok: true, data: { role_discovery: data } }) }} onConfirm={confirm} />)
  expect(await screen.findByRole('radio', { name: /Distinct Panel/ })).toBeTruthy()
  expect(confirm).not.toHaveBeenCalled()
})

it('settles the captured callback once and uses its replacement only on explicit retry', async () => {
  let finish
  const old = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const next = vi.fn().mockResolvedValue({ ok: true })
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={old} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await act(async () => {})
  view.rerender(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={next} />)
  expect(old).toHaveBeenCalledTimes(1)
  expect(next).not.toHaveBeenCalled()
  await act(async () => { finish({ ok: false }) })
  await screen.findByRole('alert')
  expect(next).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(next).toHaveBeenCalledTimes(1)
  expect(client.readContext).toHaveBeenCalledTimes(1)
})

it('does not submit through a removed callback and requires a new explicit click', async () => {
  const old = vi.fn(), next = vi.fn().mockResolvedValue({ ok: true })
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={old} />)
  await screen.findByRole('button', { name: 'Confirm roles' })
  view.rerender(<X07DrawingRoleReview {...scope} projectClient={client} />)
  expect(screen.getByRole('button', { name: 'Confirm roles' })).toBeDisabled()
  fireEvent.submit(screen.getByRole('form', { name: 'Confirm drawing roles' }))
  expect(old).not.toHaveBeenCalled()
  view.rerender(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={next} />)
  expect(next).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(next).toHaveBeenCalledTimes(1)
})

it('runs the actual review through the disposable Python adapter bridge', () => {
  const root = path.resolve(process.cwd(), '..')
  const output = execFileSync(process.execPath, [path.join(root, 'tools/xbench/tests/x07_role_review_adapter_journey.cjs'),
    '--deps', path.resolve('node_modules')], { cwd: root, timeout: 30000, encoding: 'utf8' })
  expect(output).toContain('PASS: role review adapter journey')
}, 40000)

it('hides synchronous and rejected private callback errors behind a fixed refusal', async () => {
  for (const callback of [() => { throw new Error('PRIVATE_SYNC_SENTINEL') }, () => Promise.reject(new Error('PRIVATE_ASYNC_SENTINEL'))]) {
    const confirm = vi.fn().mockImplementationOnce(callback).mockResolvedValueOnce({ ok: true })
    const view = render(<X07DrawingRoleReview {...scope} projectClient={{ readContext: async () => reply }} onConfirm={confirm} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/^Roles could not be saved\. Try again\.$/)
    expect(document.body.textContent).not.toContain('PRIVATE_')
    expect(screen.getByRole('radio', { name: /Review Panel/ })).toBeChecked()
    expect(screen.queryByText('Roles confirmed.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
    expect(confirm).toHaveBeenCalledTimes(2)
    view.unmount()
  }
})

it('restarts a cancelled review before submission with a fresh scoped read and explicit click', async () => {
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const confirm = vi.fn().mockResolvedValue({ ok: true }), cancel = vi.fn()
  render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} onCancel={cancel} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
  expect(cancel).toHaveBeenCalledTimes(1)
  expect(confirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Read roles again' }))
  await screen.findByRole('button', { name: 'Confirm roles' })
  expect(client.readContext).toHaveBeenCalledTimes(2)
  expect(confirm).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
})

it('composes confirmation and optional-role editing while retaining the supplied old pin', async () => {
  const pin = { customer_key: 'synthetic-a', profile_id: 'profile-a', version: 1 }
  const profile = { ...pin, roles: { module: { block: 'Review Panel', layer: 'MODULES' } } }
  const loadProfile = vi.fn().mockResolvedValue(profile)
  const saveProfile = vi.fn(async (customer, roles) => ({ ...profile, version: 2, roles }))
  render(<X07DrawingRoleProfileReview {...scope} customerKey="synthetic-a" drawingKey="drawing-a"
    projectClient={{ readContext: async () => reply }} onConfirm={async () => ({ ok: true, applied_profile: pin })}
    loadProfile={loadProfile} saveProfile={saveProfile} />)
  expect(loadProfile).not.toHaveBeenCalled()
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Add roof role' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Roof layer' }), { target: { value: 'ROOF' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  expect(await screen.findByText('Profile version: 2')).toBeTruthy()
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
  expect(saveProfile.mock.calls[0][0]).toBe('synthetic-a')
  expect(saveProfile.mock.calls[0][1].roof).toEqual({ layer: 'ROOF' })
  expect(pin.version).toBe(1)
})
