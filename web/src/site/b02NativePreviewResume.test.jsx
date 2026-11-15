import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ProjectPageEditor from './ProjectPageEditor.jsx'
import { loadProjectPage, submitProjectNativePreview } from './projectPageClient.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {}, subscribeTokenStored: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => 'b02-synthetic', subscribeActiveScope: () => () => {} }))
vi.mock('../lib/composerDraft.js', () => ({ composerDraftAccountScope: () => 'b02-synthetic' }))
vi.mock('../api.js', () => ({ config: { apiBase: 'https://fixture.invalid' }, getStoredOrgId: () => 'org-fixture' }))
vi.mock('../projects/api.js', () => ({ getStoredActorBindingId: () => 'actor-fixture' }))
vi.mock('./PortfolioEditor.jsx', () => ({ default: () => null, PortfolioPreview: () => null }))
vi.mock('./ElementInspector.jsx', () => ({ default: () => null, InlineText: () => null }))
vi.mock('./projectPageClient.js', async original => ({ ...await original(), loadProjectPage: vi.fn(), submitProjectNativePreview: vi.fn() }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const JOB = '22222222-2222-4222-8222-222222222222'
const document = { schema: 'leaf.portfolio.v1', name: 'Synthetic portfolio', headline: 'Work', introduction: '', about: '',
  contact: { email: '', location: '' }, resume: [], theme: 'light', categories: [{ slug: 'work', title: 'Work', description: '', hero: 0,
    images: [{ material_id: JOB, sha256: 'b'.repeat(64), preview_sha256: 'c'.repeat(64), title: 'Synthetic photo', alt: 'Synthetic photo' }] }] }
const open = async () => {
  const view = render(<ProjectPageEditor projectId={PROJECT} accountScope="b02-synthetic" />)
  fireEvent.click(screen.getByRole('button', { name: 'Open page' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Build iOS preview' }).disabled).toBe(false))
  return view
}
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  loadProjectPage.mockResolvedValue({ revision: 16, digest: 'a'.repeat(64), document })
  submitProjectNativePreview.mockResolvedValue({ job_id: JOB, status: 'queued', revision: 16,
    document_sha256: 'a'.repeat(64), archive_sha256: 'd'.repeat(64) })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

it('reuses the original submission after the editor unmounts and reopens', async () => {
  const first = await open()
  fireEvent.click(screen.getByRole('button', { name: 'Build iOS preview' }))
  await waitFor(() => expect(submitProjectNativePreview).toHaveBeenCalledTimes(1))
  const key = submitProjectNativePreview.mock.calls[0][2]
  first.unmount()
  await open()
  expect(submitProjectNativePreview).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Build iOS preview' }))
  await waitFor(() => expect(submitProjectNativePreview).toHaveBeenCalledTimes(2))
  expect(submitProjectNativePreview.mock.calls[1].slice(0, 3)).toEqual([PROJECT, 16, key])
})

it('does not submit when the recovery key cannot be durably saved', async () => {
  await open()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fireEvent.click(screen.getByRole('button', { name: 'Build iOS preview' }))
  expect(submitProjectNativePreview).not.toHaveBeenCalled()
  expect(screen.getByText('The preview submission identity could not be restored or saved. No build was submitted.')).toBeTruthy()
})
