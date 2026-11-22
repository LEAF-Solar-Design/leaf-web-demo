import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TEST_TIME_SCALE } from '../testTimeScale.js'
import ProjectPageEditor from './ProjectPageEditor.jsx'
import { loadProjectPage, saveProjectPage, submitProjectNativePreview } from './projectPageClient.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {}, subscribeTokenStored: () => () => {} }))
vi.mock('../auth/activeScope.js', async original => ({ ...await original(), readActiveScope: () => 'b02-synthetic' }))
vi.mock('../lib/composerDraft.js', () => ({ composerDraftAccountScope: () => 'b02-synthetic' }))
vi.mock('../api.js', () => ({ config: { apiBase: 'https://fixture.invalid' }, getStoredOrgId: () => 'org-fixture' }))
vi.mock('../projects/api.js', () => ({ getStoredActorBindingId: () => 'actor-fixture', listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))
vi.mock('./projectPageClient.js', async original => ({ ...await original(), loadProjectPage: vi.fn(), saveProjectPage: vi.fn(), submitProjectNativePreview: vi.fn() }))

// This persistence test renders 94 photo editors. Match visible button text without
// recomputing accessible names across every photo control on each transition.
const button = name => screen.getByText(name, { selector: 'button', exact: true })

const PROJECT = '11111111-1111-4111-8111-111111111111'
const clone = value => JSON.parse(JSON.stringify(value))
const document = {
  schema: 'leaf.portfolio.v1', name: 'Synthetic portfolio', headline: 'Selected work', introduction: '', about: '',
  contact: { email: '', location: '' }, resume: [], theme: 'light',
  categories: [{ slug: 'work', title: 'Work', description: 'Original gallery description', hero: 0,
    images: Array.from({ length: 94 }, (_, index) => ({
      material_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
      sha256: (index + 1).toString(16).padStart(64, '0'), preview_sha256: (index + 95).toString(16).padStart(64, '0'),
      title: `Photo ${index + 1}`, alt: `Synthetic photo ${index + 1}`,
    })) }],
}

function Host() {
  const [editor, setEditor] = useState(null)
  return <>
    <ProjectPageEditor projectId={PROJECT} accountScope="b02-synthetic" onSession={setEditor} />
    <button disabled={!editor?.state.canUndo || Boolean(editor?.state.busy)} onClick={() => editor.session.undo()}>Undo recovery</button>
    <output data-testid="saved-revision">{editor?.state.savedRevision}</output>
  </>
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('saves a gallery edit, undoes and saves recovery, then reloads all 94 assignments', async () => {
  let committed = { revision: 1, digest: 'a'.repeat(64), document: clone(document) }
  loadProjectPage.mockImplementation(async project => { expect(project).toBe(PROJECT); return clone(committed) })
  saveProjectPage.mockImplementation(async (project, draft, revision) => {
    expect(project).toBe(PROJECT)
    expect(revision).toBe(committed.revision)
    committed = { revision: revision + 1, digest: String(revision + 1).repeat(64), document: clone(draft) }
    return clone(committed)
  })
  render(<Host />)
  fireEvent.click(button('Open page'))
  const field = await screen.findByLabelText('Category description')
  expect(field.value).toBe(document.categories[0].description)
  fireEvent.change(field, { target: { value: 'Changed gallery description' } })
  fireEvent.click(button('Save page'))
  await waitFor(() => expect(screen.getByTestId('saved-revision').textContent).toBe('2'))
  expect(committed.document.categories[0].description).toBe('Changed gallery description')
  fireEvent.click(button('Undo recovery'))
  expect(field.value).toBe(document.categories[0].description)
  expect(committed.document.categories[0].description).toBe('Changed gallery description')
  fireEvent.click(button('Save page'))
  await waitFor(() => expect(screen.getByTestId('saved-revision').textContent).toBe('3'))
  expect(committed.document).toEqual(document)
  fireEvent.change(field, { target: { value: 'Discard this unsaved draft' } })
  fireEvent.click(button('Reload saved page'))
  await waitFor(() => expect(screen.getByLabelText('Category description').value).toBe(document.categories[0].description))
  expect(button('Undo recovery').disabled).toBe(true)
  expect(committed.document.categories[0].images).toHaveLength(94)
  expect(committed.document.categories[0].images).toEqual(document.categories[0].images)
  expect(saveProjectPage).toHaveBeenCalledTimes(2)
  expect(loadProjectPage).toHaveBeenCalledTimes(2)
  expect(submitProjectNativePreview).not.toHaveBeenCalled()
}, 20_000 * TEST_TIME_SCALE)
