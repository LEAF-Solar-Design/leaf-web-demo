import React from 'react'
import { Blob as NativeBlob } from 'node:buffer'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ProjectPageEditor, { PagePreview } from './ProjectPageEditor.jsx'
import { downloadProjectPageSource, loadProjectPage, saveProjectPage, submitProjectNativePreview, loadProjectNativePreview, downloadProjectNativePreviewScreenshot } from './projectPageClient.js'
import { listProjectPhotos, readProjectPhotoPreview } from '../projects/api.js'
import { emptyPortfolio, importPortfolio, validatePortfolioDocument } from './portfolioDocument.js'
import { registerEscapeOwner } from '../lib/useEscapeOwner.js'

const auth = vi.hoisted(() => ({ logout: null, token: null, scope: 'account-a', unauthorized: vi.fn(), logouts: new Set(), tokens: new Set() }))
vi.mock('../auth.js', () => ({
  subscribeBeforeLogout: callback => { auth.logouts.add(callback); auth.logout = () => [...auth.logouts].forEach(fn => fn()); return () => auth.logouts.delete(callback) },
  subscribeTokenStored: callback => { auth.tokens.add(callback); auth.token = () => [...auth.tokens].forEach(fn => fn()); return () => auth.tokens.delete(callback) },
}))
vi.mock('../auth/activeScope.js', async importOriginal => ({ ...await importOriginal(), readActiveScope: () => auth.scope }))
vi.mock('../lib/composerDraft.js', () => ({ composerDraftAccountScope: () => auth.scope }))
vi.mock('../api.js', () => ({
  config: { apiBase: 'https://api.example.test' }, authHeaders: () => ({ Authorization: 'Bearer fixture' }),
  getStoredOrgId: () => 'org-a', noteUnauthorized: auth.unauthorized,
}))
vi.mock('../projects/api.js', () => ({ getStoredActorBindingId: () => 'binding-a', listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))
vi.mock('./projectPageClient.js', async importOriginal => ({
  ...await importOriginal(), loadProjectPage: vi.fn(), saveProjectPage: vi.fn(), downloadProjectPageSource: vi.fn(),
  submitProjectNativePreview: vi.fn(), loadProjectNativePreview: vi.fn(), downloadProjectNativePreviewScreenshot: vi.fn(),
}))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const OTHER = '22222222-2222-4222-8222-222222222222'
const document = { label: 'A page', headline: 'Coming soon', description: 'A little news', status: 'Opening soon', theme: 'cream', alignment: 'center' }
const page = (revision = 1, changes = {}) => ({ revision, digest: 'a'.repeat(64), document: { ...document, ...changes } })
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const button = name => screen.getByRole('button', { name })
const setup = props => render(<ProjectPageEditor projectId={PROJECT} accountScope="account-a" {...props} />)
const openSaved = async () => { fireEvent.click(button('Open page')); await screen.findByDisplayValue('A page') }

beforeEach(() => {
  vi.clearAllMocks()
  auth.scope = 'account-a'
  auth.logouts.clear(); auth.tokens.clear()
  listProjectPhotos.mockResolvedValue({ materials: [] })
  readProjectPhotoPreview.mockResolvedValue(new Blob(['photo'], { type: 'image/jpeg' }))
  loadProjectPage.mockResolvedValue(page())
  saveProjectPage.mockImplementation(async (_id, document, revision) => ({ ...page(revision + 1), document }))
  downloadProjectPageSource.mockResolvedValue(new Blob(['zip'], { type: 'application/zip' }))
  submitProjectNativePreview.mockResolvedValue(nativeJob('queued'))
  loadProjectNativePreview.mockResolvedValue(nativeJob('running'))
  downloadProjectNativePreviewScreenshot.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
  vi.stubGlobal('fetch', vi.fn())
  URL.createObjectURL = vi.fn(() => 'blob:page-source')
  URL.revokeObjectURL = vi.fn()
})

const PHOTO = { material_id: '33333333-3333-4333-8333-333333333333', sha256: 'b'.repeat(64), preview_sha256: 'c'.repeat(64), name: 'Cake.jpg' }
const PHOTO_TWO = { ...PHOTO, material_id: '44444444-4444-4444-8444-444444444444', sha256: 'd'.repeat(64), name: 'Bread.jpg' }
const imageRef = row => ({ material_id: row.material_id, sha256: row.sha256, preview_sha256: row.preview_sha256, title: row.name, alt: row.name })
const portfolioDraft = () => ({ ...emptyPortfolio(), categories: [{ slug: 'cakes', title: 'Cakes', description: '', hero: 0, images: [imageRef(PHOTO), imageRef(PHOTO_TWO)] }] })
const nativeJob = (status, changes = {}) => ({ job_id: OTHER, status, revision: 7, document_sha256: 'a'.repeat(64), archive_sha256: 'b'.repeat(64),
  ...(status === 'complete' ? { receipt: { kind: 'simulator', receipt_id: PROJECT, sha256: 'c'.repeat(64) }, evidence_basis: 'authenticated-worker-report' } : {}), ...changes })
const openPortfolio = async () => {
  loadProjectPage.mockResolvedValueOnce({ revision: 7, digest: 'a'.repeat(64), document: portfolioDraft() })
  fireEvent.click(button('Open page'))
  await screen.findByLabelText('Name')
}
const importFile = value => ({ name: 'portfolio.json', size: JSON.stringify(value).length, text: async () => JSON.stringify(value) })
const importDraft = () => {
  const value = portfolioDraft(); value.schema = 'leaf.portfolio.import.v1'
  value.categories[0].images = [PHOTO, PHOTO_TWO].map(row => ({ source_sha256: row.sha256, title: row.name, alt: row.name }))
  return value
}

describe('portfolio project document', () => {
  it('exports native source only from the exact saved portfolio revision', async () => {
    loadProjectPage.mockResolvedValueOnce({ revision: 7, digest: 'a'.repeat(64), document: portfolioDraft() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    setup()
    fireEvent.click(button('Open page'))
    await screen.findByRole('button', { name: 'Download native iOS source' })
    fireEvent.click(button('Download native iOS source'))
    await waitFor(() => expect(downloadProjectPageSource).toHaveBeenCalledWith(PROJECT, 7, { signal: expect.any(AbortSignal), target: 'ios' }))
    await screen.findByText(/This does not install or publish an app/)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Edited portfolio' } })
    expect(button('Download native iOS source')).toBeDisabled()
    click.mockRestore()
  })
  it('converts a loaded legacy page to a portfolio using the loaded revision', async () => {
    loadProjectPage.mockResolvedValueOnce(page(4))
    listProjectPhotos.mockResolvedValueOnce({ materials: [PHOTO] })
    setup()
    await openSaved()
    fireEvent.click(button('Create a portfolio'))
    fireEvent.click(button('Load project photos'))
    await screen.findByRole('option', { name: 'Cake.jpg' })
    fireEvent.change(screen.getByLabelText('Add project photo to Selected work'), { target: { value: PHOTO.material_id } })
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(saveProjectPage.mock.lastCall[2]).toBe(4)
    expect(saveProjectPage.mock.lastCall[1].schema).toBe('leaf.portfolio.v1')
  })

  it('resolves paginated imports from project metadata and saves edited caption, hero and order', async () => {
    const onPreview = vi.fn()
    listProjectPhotos.mockResolvedValueOnce({ materials: [PHOTO], next_cursor: 'next' }).mockResolvedValueOnce({ materials: [PHOTO_TWO] })
    setup({ onPreview })
    fireEvent.change(screen.getByLabelText('Import portfolio content'), { target: { files: [importFile(importDraft())] } })
    await screen.findByLabelText('Caption 1')
    expect(listProjectPhotos.mock.calls[1]).toEqual([PROJECT, expect.objectContaining({ cursor: 'next', expectedScope: 'account-a' })])
    expect(saveProjectPage).not.toHaveBeenCalled()
    expect(onPreview.mock.lastCall[0].document.categories[0].images[0]).toEqual(imageRef(PHOTO))
    fireEvent.change(screen.getByLabelText('Caption 1'), { target: { value: 'Chocolate celebration' } })
    fireEvent.change(screen.getByLabelText('Hero image'), { target: { value: '1' } })
    fireEvent.click(button('Move image 2 up'))
    expect(onPreview.mock.lastCall[0].document.categories[0]).toMatchObject({ hero: 0, images: [imageRef(PHOTO_TWO), { ...imageRef(PHOTO), title: 'Chocolate celebration' }] })
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(saveProjectPage.mock.lastCall[1].schema).toBe('leaf.portfolio.v1')
    const saved = saveProjectPage.mock.lastCall[1]
    loadProjectPage.mockResolvedValueOnce({ revision: 1, digest: 'a'.repeat(64), document: saved })
    fireEvent.change(screen.getByLabelText('Caption 1'), { target: { value: 'Temporary edit' } })
    fireEvent.click(button('Reload saved page'))
    await waitFor(() => expect(screen.getByLabelText('Caption 1').value).toBe('Bread.jpg'))
    expect(button('Download Next.js source').disabled).toBe(false)
  })

  it('retains the existing draft when a photo is missing or ambiguous and rejects trusted-metadata imports', async () => {
    const onPreview = vi.fn()
    setup({ onPreview })
    fireEvent.click(button('Create a page'))
    listProjectPhotos.mockResolvedValueOnce({ materials: [PHOTO] })
    fireEvent.change(screen.getByLabelText('Import portfolio content'), { target: { files: [importFile(importDraft())] } })
    await screen.findByText(/Missing photo/)
    expect(screen.getByLabelText('Headline').value).toBe('Coming soon')
    listProjectPhotos.mockResolvedValueOnce({ materials: [PHOTO, { ...PHOTO, material_id: OTHER }, PHOTO_TWO] })
    fireEvent.change(screen.getByLabelText('Import portfolio content'), { target: { files: [importFile(importDraft())] } })
    await screen.findByText(/Ambiguous photo/)
    expect(onPreview).toHaveBeenCalledTimes(1)
    const bad = importDraft(); bad.categories[0].images[0].material_id = PHOTO.material_id
    await expect(importPortfolio(importFile(bad), PROJECT, {})).rejects.toThrow('Unsupported portfolio fields')
  })

  it('creates a portfolio with required photo guidance, supports resume editing, and clears on logout', async () => {
    listProjectPhotos.mockResolvedValueOnce({ materials: [PHOTO] })
    const onPreview = vi.fn()
    setup({ onPreview })
    fireEvent.click(button('Create a portfolio'))
    expect(button('Save page').disabled).toBe(true)
    fireEvent.click(button('Load project photos'))
    await screen.findByRole('option', { name: 'Cake.jpg' })
    fireEvent.change(screen.getByLabelText('Add project photo to Selected work'), { target: { value: PHOTO.material_id } })
    fireEvent.click(button('Add resume section')); fireEvent.click(button('Add resume entry'))
    fireEvent.change(screen.getByLabelText('Entry title'), { target: { value: 'Pastry chef' } })
    expect(onPreview.mock.lastCall[0].document.resume[0].entries[0].title).toBe('Pastry chef')
    expect(button('Save page').disabled).toBe(false)
    act(() => auth.logout())
    expect(screen.queryByLabelText('Name')).toBeNull()
    expect(button('Create a portfolio').disabled).toBe(true)
  })

  it('fences an in-flight import on project and account switches', async () => {
    const pending = deferred(), onPreview = vi.fn()
    listProjectPhotos.mockReturnValueOnce(pending.promise)
    const view = setup({ onPreview })
    fireEvent.change(screen.getByLabelText('Import portfolio content'), { target: { files: [importFile(importDraft())] } })
    await waitFor(() => expect(listProjectPhotos).toHaveBeenCalled())
    auth.scope = 'account-b'
    view.rerender(<ProjectPageEditor projectId={OTHER} accountScope="account-b" onPreview={onPreview} />)
    await act(async () => pending.resolve({ materials: [PHOTO, PHOTO_TWO] }))
    expect(onPreview).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Name')).toBeNull()
  })

  it('loads private previews only for current views, closes by Escape, restores focus and revokes bytes', async () => {
    const view = render(<PagePreview document={portfolioDraft()} projectId={PROJECT} />)
    await waitFor(() => expect(readProjectPhotoPreview).toHaveBeenCalledTimes(1))
    expect(readProjectPhotoPreview.mock.lastCall).toEqual([PROJECT, PHOTO.material_id, expect.objectContaining({ expectedScope: 'account-a', signal: expect.any(AbortSignal) })])
    fireEvent.click(button('Cakes'))
    const trigger = button('View Cake.jpg')
    fireEvent.click(trigger)
    await screen.findByRole('dialog')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(window.document.activeElement).toBe(trigger)
    view.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })

  it('shows a preview failure without an indefinite loading message', async () => {
    readProjectPhotoPreview.mockRejectedValueOnce(new Error('Unavailable'))
    render(<PagePreview document={portfolioDraft()} projectId={PROJECT} />)
    await screen.findByRole('alert')
    expect(screen.queryByText('Loading photo…')).toBeNull()
  })

  it('closes the image before an underlying Studio drawer on Escape', () => {
    const closeDrawer = vi.fn()
    const unregister = registerEscapeOwner('fixture-drawer', closeDrawer, { layer: 'drawer' })
    try {
      render(<PagePreview document={portfolioDraft()} projectId={PROJECT} />)
      fireEvent.click(button('Cakes'))
      fireEvent.click(button('View Cake.jpg'))
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(closeDrawer).not.toHaveBeenCalled()
    } finally { unregister() }
  })

  it('leaves Escape to the active workspace when a portfolio viewer is hidden', () => {
    const view = render(<div data-testid="browser"><PagePreview document={portfolioDraft()} projectId={PROJECT} /></div>)
    fireEvent.click(button('Cakes'))
    fireEvent.click(button('View Cake.jpg'))
    view.getByTestId('browser').hidden = true
    const closeDrawer = vi.fn()
    const unregister = registerEscapeOwner('fixture-drawer', closeDrawer, { layer: 'drawer' })
    try {
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(closeDrawer).toHaveBeenCalledTimes(1)
    } finally { unregister() }
  })

  it('aborts old preview reads and drops their bytes on an account scope change', async () => {
    const pending = deferred()
    readProjectPhotoPreview.mockReturnValueOnce(pending.promise)
    render(<PagePreview document={portfolioDraft()} projectId={PROJECT} />)
    await waitFor(() => expect(readProjectPhotoPreview).toHaveBeenCalledTimes(1))
    const oldSignal = readProjectPhotoPreview.mock.lastCall[2].signal
    act(() => { auth.scope = 'account-b'; window.dispatchEvent(new Event('leaf:active-scope')) })
    expect(oldSignal.aborted).toBe(true)
    await waitFor(() => expect(readProjectPhotoPreview).toHaveBeenCalledTimes(2))
    await screen.findByRole('img', { name: 'Cake.jpg' })
    const urlsBeforeOldResponse = URL.createObjectURL.mock.calls.length
    await act(async () => pending.resolve(new Blob(['old account photo'])))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(urlsBeforeOldResponse)
    expect(readProjectPhotoPreview.mock.lastCall[2].expectedScope).toBe('account-b')
  })

  it('rejects unknown nested keys, boolean heroes, duplicate materials, unsafe contact and excessive documents', () => {
    const mutations = [d => { d.categories[0].hero = true }, d => { d.categories[0].slug = 'home' }, d => { d.contact.email = 'hello@example.com\n' },
      d => { d.contact.extra = '' }, d => { d.categories[0].images.push(imageRef(PHOTO)) }, d => { d.about = 'https://example.com' },
      d => { d.categories[0].images[0].preview_sha256 += '\n' }]
    for (const mutate of mutations) { const value = portfolioDraft(); mutate(value); expect(() => validatePortfolioDocument(value)).toThrow() }
    const value = portfolioDraft()
    const detached = validatePortfolioDocument(value)
    detached.categories[0].images[0].title = 'Changed'
    expect(value.categories[0].images[0].title).toBe('Cake.jpg')
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('project page editor', () => {
  it('waits for explicit creation and sends generic content and each edit to the preview', () => {
    const onPreview = vi.fn()
    setup({ onPreview })
    expect(loadProjectPage).not.toHaveBeenCalled()
    expect(onPreview).not.toHaveBeenCalled()
    fireEvent.click(button('Create a page'))
    expect(screen.getByLabelText('Headline').value).toBe('Coming soon')
    expect(onPreview.mock.lastCall[0].document.label).toBe('Your page')
    fireEvent.change(screen.getByLabelText('Headline'), { target: { value: 'Fresh news' } })
    expect(onPreview.mock.lastCall[0].document.headline).toBe('Fresh news')
    expect(button('Download Next.js source').disabled).toBe(true)
    expect(screen.getByText('Unsaved changes')).toBeTruthy()
  })

  it('opens only on request and saves against the loaded revision with duplicate protection', async () => {
    const pending = deferred()
    const onPreview = vi.fn()
    setup({ onPreview })
    await openSaved()
    expect(onPreview).toHaveBeenCalledWith({ document, name: 'A page' })
    fireEvent.change(screen.getByLabelText('Headline'), { target: { value: 'Updated' } })
    saveProjectPage.mockReturnValueOnce(pending.promise)
    fireEvent.click(button('Save page')); fireEvent.click(button('Save page'))
    expect(saveProjectPage).toHaveBeenCalledTimes(1)
    expect(saveProjectPage).toHaveBeenCalledWith(PROJECT, { ...document, headline: 'Updated' }, 1, { signal: expect.any(AbortSignal) })
    expect(screen.getByText('Saving…')).toBeTruthy()
    await act(async () => pending.resolve(page(2, { headline: 'Updated' })))
    expect(screen.getByText('Saved')).toBeTruthy()
    expect(button('Download Next.js source').disabled).toBe(false)
  })

  it('retains a failed draft and allows save retry', async () => {
    setup()
    fireEvent.click(button('Create a page'))
    fireEvent.change(screen.getByLabelText('Headline'), { target: { value: 'Keep this draft' } })
    saveProjectPage.mockRejectedValueOnce(new Error('Service unavailable. Try again.'))
    fireEvent.click(button('Save page'))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Headline').value).toBe('Keep this draft')
    expect(button('Save page').disabled).toBe(false)
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(saveProjectPage.mock.lastCall[2]).toBe(0)
  })

  it('keeps a conflicting draft until explicit reload and then uses the new revision', async () => {
    setup()
    await openSaved()
    fireEvent.change(screen.getByLabelText('Headline'), { target: { value: 'My draft' } })
    saveProjectPage.mockRejectedValueOnce(Object.assign(new Error('Saved page changed. Reload the saved page.'), { status: 409 }))
    fireEvent.click(button('Save page'))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Headline').value).toBe('My draft')
    expect(button('Save page').disabled).toBe(true)
    loadProjectPage.mockResolvedValueOnce(page(4, { headline: 'Other edit' }))
    fireEvent.click(button('Reload saved page'))
    await screen.findByDisplayValue('Other edit')
    fireEvent.change(screen.getByLabelText('Headline'), { target: { value: 'New draft' } })
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(saveProjectPage.mock.lastCall[2]).toBe(4)
  })

  it('distinguishes an absent page from an unavailable project without changing the preview', async () => {
    const onPreview = vi.fn()
    setup({ onPreview })
    loadProjectPage.mockResolvedValueOnce(null)
    fireEvent.click(button('Open page'))
    await screen.findByText('No saved page yet. Choose Create a page to begin.')
    loadProjectPage.mockRejectedValueOnce(Object.assign(new Error('This project is no longer available to you.'), { status: 404 }))
    fireEvent.click(button('Open page'))
    await screen.findByRole('alert')
    expect(onPreview).not.toHaveBeenCalled()
  })

  it.each(['project', 'account'])('discards a pending load after a %s switch', async kind => {
    const pending = deferred()
    loadProjectPage.mockReturnValueOnce(pending.promise)
    const onPreview = vi.fn()
    const view = setup({ onPreview })
    fireEvent.click(button('Open page'))
    const signal = loadProjectPage.mock.lastCall[1].signal
    view.rerender(<ProjectPageEditor projectId={kind === 'project' ? OTHER : PROJECT} accountScope={kind === 'account' ? 'account-b' : 'account-a'} onPreview={onPreview} />)
    await act(async () => pending.resolve(page()))
    expect(signal.aborted).toBe(true)
    expect(onPreview).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Headline')).toBeNull()
  })

  it('clears the editor on logout and ignores a late save', async () => {
    const pending = deferred()
    const onPreview = vi.fn()
    setup({ onPreview })
    fireEvent.click(button('Create a page'))
    saveProjectPage.mockReturnValueOnce(pending.promise)
    fireEvent.click(button('Save page'))
    act(() => auth.logout())
    const calls = onPreview.mock.calls.length
    await act(async () => pending.resolve(page()))
    expect(screen.queryByLabelText('Headline')).toBeNull()
    expect(button('Create a page').disabled).toBe(true)
    expect(onPreview).toHaveBeenCalledTimes(calls)
  })

  it('downloads only the saved revision through a fetched Blob and revokes it on unmount', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const view = setup()
    await openSaved()
    fireEvent.click(button('Download Next.js source'))
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
    expect(downloadProjectPageSource).toHaveBeenCalledWith(PROJECT, 1, { signal: expect.any(AbortSignal) })
    expect(click.mock.instances[0].href).toBe('blob:page-source')
    expect(click.mock.instances[0].download).toBe('nextjs-page-r1.zip')
    view.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:page-source')
  })

  it('ignores an export arriving after unmount', async () => {
    const pending = deferred()
    downloadProjectPageSource.mockReturnValueOnce(pending.promise)
    const view = setup()
    await openSaved()
    fireEvent.click(button('Download Next.js source'))
    view.unmount()
    await act(async () => pending.resolve(new Blob(['zip'])))
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('renders text as text and focuses the inspector field selected from preview', () => {
    const onSelectField = vi.fn()
    const preview = render(<PagePreview document={{ ...document, headline: '<img src=x onerror=alert(1)>' }} onSelectField={onSelectField} />)
    expect(preview.container.querySelector('img')).toBeNull()
    fireEvent.click(button('Edit headline'))
    expect(onSelectField).toHaveBeenCalledWith('headline')
    preview.unmount()
    const editor = setup({ selectedField: 'headline' })
    fireEvent.click(button('Create a page'))
    expect(window.document.activeElement).toBe(screen.getByLabelText('Headline'))
    screen.getByLabelText('Headline').blur()
    editor.rerender(<ProjectPageEditor projectId={PROJECT} accountScope="account-a" selectedField={{ field: 'headline', request: 2 }} />)
    expect(window.document.activeElement).toBe(screen.getByLabelText('Headline'))
  })
})

describe('saved portfolio iOS preview', () => {
  it('submits the exact revision once, polls serially, and displays the authenticated screenshot', async () => {
    const pending = deferred()
    submitProjectNativePreview.mockReturnValueOnce(pending.promise)
    setup(); await openPortfolio()
    vi.useFakeTimers()
    try {
      fireEvent.click(button('Build iOS preview')); fireEvent.click(button('Build iOS preview'))
      expect(submitProjectNativePreview).toHaveBeenCalledTimes(1)
      expect(submitProjectNativePreview).toHaveBeenCalledWith(PROJECT, 7, expect.any(String), { signal: expect.any(AbortSignal) })
      await act(async () => pending.resolve(nativeJob('queued')))
      expect(screen.getByText('Queued · Saved revision 7')).toBeTruthy()
      const running = deferred()
      loadProjectNativePreview.mockReturnValueOnce(running.promise).mockResolvedValueOnce(nativeJob('complete'))
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
      expect(loadProjectNativePreview).toHaveBeenCalledTimes(1)
      await act(async () => running.resolve(nativeJob('running')))
      expect(screen.getByText('Building · Saved revision 7')).toBeTruthy()
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
      expect(screen.getByText('Ready · Saved revision 7')).toBeTruthy()
      expect(screen.getByRole('img', { name: 'iOS simulator preview of saved portfolio revision 7' }).getAttribute('src')).toBe('blob:page-source')
      expect(downloadProjectNativePreviewScreenshot).toHaveBeenCalledWith(PROJECT, OTHER, { signal: expect.any(AbortSignal) })
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
      expect(loadProjectNativePreview).toHaveBeenCalledTimes(2)
    } finally { vi.useRealTimers() }
  })

  it('disables dirty submission and retains one key after an uncertain submission without losing edits', async () => {
    submitProjectNativePreview.mockRejectedValueOnce(Object.assign(new Error('private server details'), { status: 409 }))
    setup(); await openPortfolio()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Unsaved work' } })
    expect(button('Build iOS preview')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: portfolioDraft().name } })
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText(/Submission could not be confirmed/)
    expect(screen.queryByText('private server details')).toBeNull()
    const key = submitProjectNativePreview.mock.lastCall[2]
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('failed'))
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText('Failed · Saved revision 7')
    expect(submitProjectNativePreview.mock.lastCall[2]).toBe(key)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Keep this draft' } })
    expect(screen.getByLabelText('Name').value).toBe('Keep this draft')
    expect(button('Save page')).toBeEnabled()
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(saveProjectPage.mock.lastCall[2]).toBe(7)
  })

  it.each(['failed', 'cancelled'])('allows an explicit new build with a new key after %s', async status => {
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob(status)).mockResolvedValueOnce(nativeJob('failed'))
    setup(); await openPortfolio()
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText(`${status === 'failed' ? 'Failed' : 'Cancelled'} · Saved revision 7`)
    const key = submitProjectNativePreview.mock.lastCall[2]
    fireEvent.click(button('Build iOS preview'))
    await waitFor(() => expect(submitProjectNativePreview).toHaveBeenCalledTimes(2))
    expect(submitProjectNativePreview.mock.lastCall[2]).not.toBe(key)
  })

  it('retries a known-job read and screenshot failure with GET only', async () => {
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('queued'))
    loadProjectNativePreview.mockRejectedValueOnce(new Error('read failed')).mockResolvedValue(nativeJob('complete'))
    downloadProjectNativePreviewScreenshot.mockRejectedValueOnce(new Error('image failed')).mockResolvedValue(new Blob(['png']))
    setup(); await openPortfolio()
    vi.useFakeTimers()
    try {
      fireEvent.click(button('Build iOS preview'))
      await act(async () => {})
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
      expect(screen.getByText(/Could not check this preview/)).toBeTruthy()
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
      expect(loadProjectNativePreview).toHaveBeenCalledTimes(1)
      fireEvent.click(button('Check preview again'))
      await act(async () => {})
      expect(screen.queryByRole('img', { name: /iOS simulator/ })).toBeNull()
      fireEvent.click(button('Check preview again'))
      await act(async () => {})
      expect(screen.getByText('Ready · Saved revision 7')).toBeTruthy()
      expect(submitProjectNativePreview).toHaveBeenCalledTimes(1)
      expect(loadProjectNativePreview).toHaveBeenCalledTimes(3)
    } finally { vi.useRealTimers() }
  })

  it.each([{ job_id: PROJECT }, { revision: 8 }, { document_sha256: 'd'.repeat(64) }, { archive_sha256: 'd'.repeat(64) }])('rejects changed readback identity %j', async changes => {
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('queued'))
    loadProjectNativePreview.mockResolvedValueOnce(nativeJob('complete', changes))
    setup(); await openPortfolio()
    vi.useFakeTimers()
    try {
      fireEvent.click(button('Build iOS preview'))
      await act(async () => {})
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
      expect(screen.getByText(/Could not check this preview/)).toBeTruthy()
      expect(downloadProjectNativePreviewScreenshot).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('bounds observation at fifteen minutes and lets a known job be checked again', async () => {
    const pending = deferred()
    loadProjectNativePreview.mockReturnValueOnce(pending.promise)
    const view = setup(); await openPortfolio()
    vi.useFakeTimers()
    try {
      fireEvent.click(button('Build iOS preview'))
      await act(async () => {})
      await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000) })
      expect(loadProjectNativePreview).toHaveBeenCalledTimes(1)
      expect(loadProjectNativePreview.mock.lastCall[2].signal.aborted).toBe(true)
      expect(screen.getByText(/Observation paused after 15 minutes/)).toBeTruthy()
      expect(button('Check preview again')).toBeEnabled()
      await act(async () => pending.resolve(nativeJob('complete')))
      expect(downloadProjectNativePreviewScreenshot).not.toHaveBeenCalled()
      view.unmount()
    } finally { vi.useRealTimers() }
  })

  it.each(['project', 'account', 'scope', 'logout', 'unmount'])('aborts and ignores a late submission on %s invalidation', async kind => {
    const pending = deferred()
    submitProjectNativePreview.mockReturnValueOnce(pending.promise)
    const view = setup(); await openPortfolio()
    fireEvent.click(button('Build iOS preview'))
    const signal = submitProjectNativePreview.mock.lastCall[3].signal
    if (kind === 'unmount') view.unmount()
    else if (kind === 'logout') act(() => auth.logout())
    else if (kind === 'scope') act(() => { auth.scope = 'account-b'; window.dispatchEvent(new Event('leaf:active-scope')) })
    else view.rerender(<ProjectPageEditor projectId={kind === 'project' ? OTHER : PROJECT} accountScope={kind === 'account' ? 'account-b' : 'account-a'} />)
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(nativeJob('complete')))
    expect(downloadProjectNativePreviewScreenshot).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'iOS preview' })).toBeNull()
  })

  it('revokes screenshots on replacement, saved revision change, and logout', async () => {
    submitProjectNativePreview.mockResolvedValue(nativeJob('complete'))
    loadProjectNativePreview.mockResolvedValue(nativeJob('complete'))
    const view = setup(); await openPortfolio()
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText('Ready · Saved revision 7')
    fireEvent.click(button('Check preview again'))
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:page-source'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New saved work' } })
    fireEvent.click(button('Save page'))
    await screen.findByText('Saved')
    expect(screen.queryByRole('img', { name: /iOS simulator/ })).toBeNull()
    expect(button('Build iOS preview')).toBeEnabled()
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('complete', { revision: 8 }))
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText('Ready · Saved revision 8')
    const count = URL.revokeObjectURL.mock.calls.length
    act(() => auth.logout())
    expect(URL.revokeObjectURL.mock.calls.length).toBeGreaterThan(count)
    view.unmount()
  })

  it.each(['scope', 'logout', 'unmount'])('fences late screenshot bytes on %s', async kind => {
    const pending = deferred()
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('complete'))
    downloadProjectNativePreviewScreenshot.mockReturnValueOnce(pending.promise)
    const view = setup(); await openPortfolio()
    fireEvent.click(button('Build iOS preview'))
    await waitFor(() => expect(downloadProjectNativePreviewScreenshot).toHaveBeenCalledTimes(1))
    const signal = downloadProjectNativePreviewScreenshot.mock.lastCall[2].signal
    if (kind === 'unmount') view.unmount()
    else if (kind === 'logout') act(() => auth.logout())
    else act(() => { auth.scope = 'account-b'; window.dispatchEvent(new Event('leaf:active-scope')) })
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(new Blob(['late screenshot'])))
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it.each(['scope', 'unmount'])('revokes a ready screenshot on %s', async kind => {
    submitProjectNativePreview.mockResolvedValueOnce(nativeJob('complete'))
    const view = setup(); await openPortfolio()
    fireEvent.click(button('Build iOS preview'))
    await screen.findByText('Ready · Saved revision 7')
    if (kind === 'unmount') view.unmount()
    else act(() => { auth.scope = 'account-b'; window.dispatchEvent(new Event('leaf:active-scope')) })
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:page-source')
  })
})

describe('authenticated page transport', () => {
  it('uses producer-shaped native jobs and exact authenticated submit, status and screenshot contracts', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    const png = new NativeBlob([Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')], { type: 'image/png' })
    fetch.mockResolvedValueOnce({ ok: true, status: 202, json: async () => nativeJob('queued') })
    expect(await client.submitProjectNativePreview(PROJECT, 7, 'preview-key')).toEqual(nativeJob('queued'))
    expect(fetch.mock.lastCall[0]).toBe(`https://api.example.test/api/projects/${PROJECT}/native-previews`)
    expect(fetch.mock.lastCall[1]).toMatchObject({ method: 'POST', body: '{"revision":7}', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'preview-key' }, cache: 'no-store' })
    for (const status of ['running', 'complete', 'failed', 'cancelled']) {
      fetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => nativeJob(status) })
      expect(await client.loadProjectNativePreview(PROJECT, OTHER)).toEqual(nativeJob(status))
      expect(fetch.mock.lastCall[0]).toBe(`https://api.example.test/api/projects/${PROJECT}/native-previews/${OTHER}`)
    }
    fetch.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'content-type': 'image/png' }), blob: async () => png })
    expect(await client.downloadProjectNativePreviewScreenshot(PROJECT, OTHER)).toBe(png)
    expect(fetch.mock.lastCall[0]).toBe(`https://api.example.test/api/projects/${PROJECT}/native-previews/${OTHER}/screenshot`)
    for (const [, options] of fetch.mock.calls) expect(options.headers).toMatchObject({ Authorization: 'Bearer fixture', 'X-Org-Id': 'org-a', 'X-Actor-Binding-Id': 'binding-a' })
  })

  it('rejects malformed native requests and producer status, identity, digest and receipt readbacks', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    for (const [project, revision, key] of [[OTHER + '/x', 7, 'key'], [PROJECT + '\n', 7, 'key'], [PROJECT, 0, 'key'], [PROJECT, true, 'key'], [PROJECT, Number.MAX_SAFE_INTEGER + 1, 'key'], [PROJECT, 7, ''], [PROJECT, 7, 'x'.repeat(201)], [PROJECT, 7, 'bad\nkey'], [PROJECT, 7, 'key\n']]) {
      await expect(client.submitProjectNativePreview(project, revision, key)).rejects.toThrow()
    }
    await expect(client.loadProjectNativePreview(PROJECT, '../job')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
    for (const changes of [{ status: 'submitted' }, { job_id: PROJECT }, { job_id: OTHER + '\n' }, { document_sha256: 'A'.repeat(64) }, { document_sha256: 'a'.repeat(64) + '\n' }, { archive_sha256: 'short' }, { revision: true },
      { receipt: { kind: 'fixture', receipt_id: PROJECT, sha256: 'c'.repeat(64) } }, { receipt: { kind: 'simulator', receipt_id: 'bad', sha256: 'c'.repeat(64) } }, { evidence_basis: 'fixture' }]) {
      fetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => nativeJob('complete', changes) })
      await expect(client.loadProjectNativePreview(PROJECT, OTHER)).rejects.toThrow('iOS preview')
    }
    fetch.mockResolvedValueOnce({ ok: true, status: 202, json: async () => nativeJob('queued', { revision: 8 }) })
    await expect(client.submitProjectNativePreview(PROJECT, 7, 'key')).rejects.toThrow('iOS preview')
  })

  it('bounds PNG responses before buffering and rejects non-PNG bytes', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    for (const response of [
      { headers: new Headers({ 'content-type': 'text/html' }) },
      { headers: new Headers({ 'content-type': 'image/png', 'content-length': String(16 * 1024 * 1024 + 1) }) },
      { headers: new Headers({ 'content-type': 'image/png' }), blob: async () => new NativeBlob(['not png']) },
      { headers: new Headers({ 'content-type': 'image/png' }), blob: async () => new NativeBlob(['x'.repeat(64)]) },
    ]) {
      fetch.mockResolvedValueOnce({ ok: true, status: 200, ...response })
      await expect(client.downloadProjectNativePreviewScreenshot(PROJECT, OTHER)).rejects.toThrow('iOS preview')
    }
    const reader = { read: vi.fn().mockResolvedValue({ done: false, value: { byteLength: 16 * 1024 * 1024 + 1 } }), cancel: vi.fn(), releaseLock: vi.fn() }
    fetch.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'content-type': 'image/png' }), body: { getReader: () => reader } })
    await expect(client.downloadProjectNativePreviewScreenshot(PROJECT, OTHER)).rejects.toThrow('iOS preview')
    expect(reader.read).toHaveBeenCalledTimes(1)
    expect(reader.cancel).toHaveBeenCalled()
    expect(reader.releaseLock).toHaveBeenCalled()
  })

  it('keeps native failures bounded, uses unauthorized handling and times out with abort cleanup', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    fetch.mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ detail: 'SECRET' }) })
    await expect(client.submitProjectNativePreview(PROJECT, 7, 'same-key')).rejects.toThrow('Could not access the iOS preview')
    fetch.mockResolvedValueOnce({ ok: false, status: 401 })
    await expect(client.loadProjectNativePreview(PROJECT, OTHER)).rejects.toMatchObject({ status: 401 })
    expect(auth.unauthorized).toHaveBeenCalledTimes(1)
    vi.useFakeTimers()
    try {
      fetch.mockImplementation(async (_url, { signal }) => ({ ok: true, status: 200,
        json: () => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })),
      }))
      const timed = expect(client.loadProjectNativePreview(PROJECT, OTHER)).rejects.toThrow('iOS preview service took too long')
      await vi.advanceTimersByTimeAsync(30000); await timed
      const controller = new AbortController()
      const cancelled = expect(client.loadProjectNativePreview(PROJECT, OTHER, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
      await vi.advanceTimersByTimeAsync(0)
      controller.abort(); await cancelled
    } finally { vi.useRealTimers() }
  })

  it('uses the current project identity on reads, writes and exact revision exports', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    fetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ page: null }) })
    expect(await client.loadProjectPage(PROJECT)).toBeNull()
    fetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ page: page() }) })
    await client.saveProjectPage(PROJECT, document, 0)
    expect(JSON.parse(fetch.mock.lastCall[1].body)).toEqual({ expected_revision: 0, document })
    const blob = new Blob(['zip'])
    fetch.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'content-type': 'application/zip' }), blob: async () => blob })
    expect(await client.downloadProjectPageSource(PROJECT, 1)).toBe(blob)
    expect(fetch.mock.lastCall[0]).toBe(`https://api.example.test/api/projects/${PROJECT}/page/source?revision=1`)
    for (const [, options] of fetch.mock.calls) expect(options.headers).toMatchObject({ Authorization: 'Bearer fixture', 'X-Org-Id': 'org-a', 'X-Actor-Binding-Id': 'binding-a' })
  })

  it('retains status for stale recovery and uses the existing unauthorized rail', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    fetch.mockResolvedValueOnce({ ok: false, status: 409 })
    await expect(client.saveProjectPage(PROJECT, document, 1)).rejects.toMatchObject({ status: 409 })
    fetch.mockResolvedValueOnce({ ok: false, status: 401 })
    await expect(client.loadProjectPage(PROJECT)).rejects.toMatchObject({ status: 401 })
    expect(auth.unauthorized).toHaveBeenCalledTimes(1)
  })

  it('rejects executable fields and malformed successful payloads before preview or download', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    for (const headline of ['<script>x</script>', 'https://example.test', 'bad\u0000text', '  \n', 'bad\u200btext', 'bad\ud800text']) {
      expect(() => client.validatePageDocument({ ...document, headline })).toThrow()
    }
    fetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ page: { ...page(), revision: 0 } }) })
    await expect(client.loadProjectPage(PROJECT)).rejects.toThrow('invalid saved page')
    fetch.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'content-type': 'text/html' }) })
    await expect(client.downloadProjectPageSource(PROJECT, 1)).rejects.toThrow('source archive')
  })

  it('bounds response bodies with a timeout and preserves caller cancellation', async () => {
    const client = await vi.importActual('./projectPageClient.js')
    vi.useFakeTimers()
    try {
      fetch.mockImplementation(async (_url, { signal }) => ({ ok: true, status: 200,
        json: () => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })),
      }))
      const timed = expect(client.loadProjectPage(PROJECT)).rejects.toThrow('took too long')
      await vi.advanceTimersByTimeAsync(30000)
      await timed
      const controller = new AbortController()
      const cancelled = expect(client.loadProjectPage(PROJECT, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
      await vi.advanceTimersByTimeAsync(0)
      controller.abort()
      await cancelled
    } finally { vi.useRealTimers() }
  })
})
