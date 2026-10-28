import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ProjectPageEditor, { PagePreview } from './ProjectPageEditor.jsx'
import { downloadProjectPageSource, loadProjectPage, saveProjectPage } from './projectPageClient.js'

const auth = vi.hoisted(() => ({ logout: null, token: null, scope: 'account-a', unauthorized: vi.fn() }))
vi.mock('../auth.js', () => ({
  subscribeBeforeLogout: callback => { auth.logout = callback; return () => {} },
  subscribeTokenStored: callback => { auth.token = callback; return () => {} },
}))
vi.mock('../lib/composerDraft.js', () => ({ composerDraftAccountScope: () => auth.scope }))
vi.mock('../api.js', () => ({
  config: { apiBase: 'https://api.example.test' }, authHeaders: () => ({ Authorization: 'Bearer fixture' }),
  getStoredOrgId: () => 'org-a', noteUnauthorized: auth.unauthorized,
}))
vi.mock('../projects/api.js', () => ({ getStoredActorBindingId: () => 'binding-a' }))
vi.mock('./projectPageClient.js', async importOriginal => ({
  ...await importOriginal(), loadProjectPage: vi.fn(), saveProjectPage: vi.fn(), downloadProjectPageSource: vi.fn(),
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
  loadProjectPage.mockResolvedValue(page())
  saveProjectPage.mockImplementation(async (_id, document, revision) => ({ ...page(revision + 1), document }))
  downloadProjectPageSource.mockResolvedValue(new Blob(['zip'], { type: 'application/zip' }))
  vi.stubGlobal('fetch', vi.fn())
  URL.createObjectURL = vi.fn(() => 'blob:page-source')
  URL.revokeObjectURL = vi.fn()
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

describe('authenticated page transport', () => {
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
