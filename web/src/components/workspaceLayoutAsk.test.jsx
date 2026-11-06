import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import WorkspaceLayoutAsk from './WorkspaceLayoutAsk.jsx'
import { getSurfaceConfig, submitSurfaceConfig } from '../api.js'
import { refreshSurfaceConfigOverlay } from '../site/useSurfaceContract.js'
vi.mock('../api.js', () => ({ getSurfaceConfig: vi.fn(), submitSurfaceConfig: vi.fn() }))
vi.mock('../site/useSurfaceContract.js', () => ({ refreshSurfaceConfigOverlay: vi.fn() }))
afterEach(cleanup)
beforeEach(() => { vi.resetAllMocks(); refreshSurfaceConfigOverlay.mockResolvedValue(undefined) })
const receipt = { sha256: 'a'.repeat(64), authored_at: '2026-11-01T12:00:00Z' }
const props = { profile: 'cad', scopeKey: 'tenant/project', position: 'above-tools', onPreview: vi.fn(), onClose: vi.fn() }
const click = name => fireEvent.click(screen.getByRole('button', { name }))

it.each([
  ['Move the workspace header below the tools', 'below-tools'],
  ['the header below the cad tools should be above it', 'above-tools'],
])('previews the request "%s" without persistence and defaults to preview only', (request, expected) => {
  render(<WorkspaceLayoutAsk {...props} />)
  fireEvent.change(screen.getByLabelText('Layout request'), { target: { value: request } })
  click('Preview request')
  expect(props.onPreview).toHaveBeenLastCalledWith(expected)
  expect(screen.getByRole('button', { name: 'Apply layout' })).toBeDisabled()
  expect(screen.getByText(/Preview only/)).toBeTruthy()
  click('Cancel preview')
  expect(props.onPreview).toHaveBeenLastCalledWith(null)
  expect(getSurfaceConfig).not.toHaveBeenCalled()
  expect(submitSurfaceConfig).not.toHaveBeenCalled()
})

it('applies once, confirms readback, and undoes only its field against a fresh overlay', async () => {
  let surfaces = { cad: { toolbar: { home: 'draw' } }, solar: { authoring: true } }
  getSurfaceConfig.mockImplementation(async () => ({ surfaces, source: receipt }))
  submitSurfaceConfig.mockImplementation(async next => { surfaces = next; return receipt })
  render(<WorkspaceLayoutAsk {...props} mock={false} canApplyLayout />)
  click('Below tools')
  click('Apply layout')
  click('Apply layout')
  await screen.findByText(/Layout saved/)
  expect(submitSurfaceConfig).toHaveBeenCalledTimes(1)
  expect(surfaces.cad.toolbar).toEqual({ home: 'draw', workspaceHeader: 'below-tools' })
  surfaces = { ...surfaces, cad: { ...surfaces.cad, authoring: false } }
  click('Undo layout')
  await screen.findByText(/Layout restored/)
  expect(surfaces).toEqual({ cad: { toolbar: { home: 'draw' }, authoring: false }, solar: { authoring: true } })
})

it('refuses undo after another header change', async () => {
  let surfaces = {}
  getSurfaceConfig.mockImplementation(async () => ({ surfaces, source: receipt }))
  submitSurfaceConfig.mockImplementation(async next => { surfaces = next; return receipt })
  render(<WorkspaceLayoutAsk {...props} mock={false} canApplyLayout />)
  click('Below tools'); click('Apply layout')
  await screen.findByText(/Layout saved/)
  surfaces = { cad: { toolbar: { workspaceHeader: 'above-tools' } } }
  click('Undo layout')
  await screen.findByText(/header changed since Apply/)
  expect(submitSurfaceConfig).toHaveBeenCalledTimes(1)
})

it('does not submit after scope or eligibility changes during the fresh read', async () => {
  let resolve
  getSurfaceConfig.mockReturnValue(new Promise(r => { resolve = r }))
  const { rerender } = render(<WorkspaceLayoutAsk {...props} mock={false} canApplyLayout />)
  click('Below tools'); click('Apply layout')
  rerender(<WorkspaceLayoutAsk {...props} scopeKey="tenant/other" mock={false} canApplyLayout={false} />)
  await act(async () => resolve({ surfaces: {} }))
  expect(submitSurfaceConfig).not.toHaveBeenCalled()
})

it('does not claim success or retry writes after an uncertain save', async () => {
  getSurfaceConfig.mockResolvedValue({ surfaces: {} })
  submitSurfaceConfig.mockRejectedValue(new Error('lost response'))
  render(<WorkspaceLayoutAsk {...props} mock={false} canApplyLayout />)
  click('Below tools'); click('Apply layout')
  await screen.findByText(/Save outcome is unconfirmed/)
  expect(screen.getByRole('button', { name: 'Apply layout' })).toBeDisabled()
  expect(refreshSurfaceConfigOverlay).not.toHaveBeenCalled()
})
