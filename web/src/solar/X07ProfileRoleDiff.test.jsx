import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Diff, { compareRoleMaps } from './X07ProfileRoleDiff.jsx'
import Editor from './X07CustomerProfileEditor.jsx'
import Review from './X07DrawingRoleProfileReview.jsx'

afterEach(cleanup)
const moduleRole = { block: 'Panel', layer: 'MODULES' }
const roles = { module: moduleRole, roof: { layer: 'ROOF' } }
const record = (version = 1, value = roles) => ({ customer_key: 'customer-a', profile_id: 'profile-a', version, roles: value })

it('shows exact added, removed and Unicode-distinct changed values with no callbacks', () => {
  const before = { module: { block: 'P\u00e9', layer: ' MODULES ' }, roof: { layer: 'ROOF' } }
  const after = { module: { layer: 'MODULES', block: 'Pe\u0301' }, obstruction: { layer: 'OBSTRUCTION' } }
  const callback = vi.fn(), bytes = JSON.stringify([before, after])
  render(<Diff beforeRoles={before} afterRoles={after} onChange={callback} onApply={callback} />)
  expect(screen.getByRole('status')).toHaveTextContent('1 added, 1 removed, 1 changed.')
  expect(within(screen.getByRole('region', { name: 'Removed roof' })).getByText('"ROOF"')).toBeTruthy()
  const values = [...screen.getByRole('region', { name: 'Changed module' }).querySelectorAll('code')].map(node => node.textContent)
  expect(values).toEqual(['"P\u00e9"', '" MODULES "', '"Pe\u0301"', '"MODULES"'])
  expect(screen.getByRole('region', { name: 'Added obstruction' })).toHaveTextContent('"OBSTRUCTION"')
  expect(callback).not.toHaveBeenCalled(); expect(JSON.stringify([before, after])).toBe(bytes)
})

it('ignores role and field key order and refuses malformed or excessive values boundedly', () => {
  expect(compareRoleMaps(roles, { roof: { layer: 'ROOF' }, module: { layer: 'MODULES', block: 'Panel' } })).toEqual({ added: {}, removed: {}, changed: {} })
  for (const value of [null, [], { module: { layer: true } }, { foreign: { layer: 'PRIVATE' } }, { module: { block: 'x'.repeat(4097) } }]) {
    const view = render(<Diff beforeRoles={roles} afterRoles={value} />)
    expect(screen.getByRole('status')).toHaveTextContent('Role changes are unavailable.')
    expect(document.body.textContent).not.toContain('PRIVATE'); view.unmount()
  }
})

it('previews draft changes without writes, clears on reset and retains differences after save refusal', async () => {
  const save = vi.fn().mockRejectedValue(new Error('PRIVATE_SAVE'))
  render(<Editor customerKey="customer-a" drawingKey="drawing-a" loadProfile={async () => record()} saveProfile={save} />)
  const layer = await screen.findByRole('textbox', { name: 'Module layer' })
  const preview = () => screen.getByRole('region', { name: 'Draft changes from loaded version 1' })
  expect(within(preview()).getByRole('status')).toHaveTextContent('No role changes.')
  fireEvent.change(layer, { target: { value: 'EDITED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Remove roof role' }))
  expect(preview()).toHaveTextContent('0 added, 1 removed, 1 changed.')
  expect(preview()).toHaveTextContent('"EDITED"'); expect(save).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Reset draft' }))
  expect(within(preview()).getByRole('status')).toHaveTextContent('No role changes.')
  fireEvent.change(layer, { target: { value: 'KEPT' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByRole('alert')
  expect(preview()).toHaveTextContent('"KEPT"'); expect(preview()).toHaveTextContent('1 changed.')
  expect(layer).toHaveValue('KEPT'); expect(save).toHaveBeenCalledTimes(1)
  expect(document.body.textContent).not.toContain('PRIVATE_SAVE')
})

it('shows pinned drift only for role values and follows current loads and saves without changing old pins', async () => {
  const pin = { ...record(), project_key: 'project-a' }, bytes = JSON.stringify(pin)
  const row = { block: 'Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] }
  const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1', module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
  render(<Review customerKey="customer-a" drawingKey="drawing-a" solarBinding={{ project_id: 'project-a' }}
    authorityKey="authority-a" projectClient={{ readContext: async () => reply }} isCurrent={() => true}
    onConfirm={async () => ({ ok: true, drawing_key: 'drawing-a', applied_profile: pin })}
    loadProfile={async () => record(2)} saveProfile={async (customer, next) => record(3, next)} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await screen.findByText('Profile version: 2')
  const drift = () => screen.getByRole('region', { name: 'Pinned versus current roles' })
  expect(within(drift()).getByRole('status')).toHaveTextContent('No role changes.')
  fireEvent.click(screen.getByRole('button', { name: 'Remove roof role' }))
  expect(within(drift()).getByRole('status')).toHaveTextContent('No role changes.')
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile version: 3')
  expect(drift()).toHaveTextContent('0 added, 1 removed, 0 changed.')
  expect(within(drift()).getByRole('region', { name: 'Removed roof' })).toHaveTextContent('"ROOF"')
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Pinned drawing roles' })).toHaveTextContent('ROOF')
  expect(JSON.stringify(pin)).toBe(bytes)
})
