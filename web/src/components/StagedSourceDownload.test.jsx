import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { Blob as ByteBlob } from 'node:buffer'
import AuthorPanel from './AuthorPanel.jsx'
import StagedSourceDownload from './StagedSourceDownload.jsx'
import useAuthorStageController from '../controllers/useAuthorStageController.js'
import { downloadStagedSource, getSessionIdentity, subscribeUnauthorized } from '../api.js'
import { notifyActiveScopeChanged, readActiveScope } from '../auth/activeScope.js'

const token = `synthetic.${btoa(JSON.stringify({ sub: 'source-test-user', iss: 'synthetic' }))}.not-a-signature`
const fixtures = ['count-panels', 'measure-roof', 'select-edges'].map((name, index) => ({
  receipt: { state: 'staged', change_set_id: `12345678-1234-4234-8234-12345678900${index}`,
    staged_commit: String(index + 1).repeat(40), catalog_digest: 'a'.repeat(64) },
  tool: { name, kind: 'script', entry: `tools/${name}/tool.py`, description: 'Synthetic script' },
  code: 'This preview is deliberately not the downloaded source.',
}))
const bytesFor = (index) => new Uint8Array([...new TextEncoder().encode(`# synthetic ${index}\r\nprint('café')\n`), 0, 255])
const hash = (algorithm, bytes) => createHash(algorithm).update(bytes).digest('hex')
function responseFor(staged, bytes, overrides = {}) {
  return new Response(bytes, { headers: {
    'Content-Type': 'application/octet-stream', 'Content-Length': String(bytes.length),
    'X-Leaf-Source-Commit': staged.receipt.staged_commit, 'X-Leaf-Source-Path': staged.tool.entry,
    'X-Leaf-Source-Sha256': hash('sha256', bytes),
    'X-Leaf-Source-Blob': hash('sha1', Buffer.concat([Buffer.from(`blob ${bytes.length}\0`), bytes])),
    ...overrides,
  } })
}
const downloadButton = () => screen.getByRole('button', { name: 'Download staged source' })
let fetchMock
let click
let downloaded
let blobs
beforeEach(async () => {
  localStorage.clear()
  localStorage.setItem('leaf.jwt', token)
  vi.stubGlobal('crypto', webcrypto)
  fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetchMock)
  await getSessionIdentity(false)
  fetchMock.mockReset()
  blobs = []
  downloaded = []
  vi.stubGlobal('Blob', ByteBlob)
  const NativeURL = globalThis.URL
  vi.stubGlobal('URL', class extends NativeURL {
    static createObjectURL = vi.fn((blob) => { blobs.push(blob); return 'blob:synthetic-source' })
    static revokeObjectURL = vi.fn()
  })
  click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    downloaded.push({ filename: this.download, href: this.href })
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear() })

describe('saved staged source download', () => {
  it.each(fixtures.map((staged, index) => [staged.tool.name, staged, index]))(
    'downloads verified exact bytes from the real review for %s', async (_name, staged, index) => {
      const original = JSON.stringify(staged)
      const onPublish = vi.fn()
      const onAuthor = vi.fn()
      fetchMock.mockResolvedValue(responseFor(staged, bytesFor(index)))
      render(<AuthorPanel stageActivity={{ result: staged }} onAuthor={onAuthor} onPublish={onPublish} />)
      fireEvent.click(downloadButton())
      await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock).toHaveBeenCalledWith(
        `${window.location.origin}/api/author/stages/${staged.receipt.change_set_id}?view=source`,
        expect.objectContaining({ method: 'GET',
          cache: 'no-store', redirect: 'error', signal: expect.any(AbortSignal) }))
      const { headers } = fetchMock.mock.calls[0][1]
      expect(Object.keys(headers)).toEqual(['Authorization'])
      expect(headers.Authorization.split(' ')).toEqual(['Bearer', token])
      expect(new Uint8Array(await blobs[0].arrayBuffer())).toEqual(bytesFor(index))
      expect(blobs[0].type).toBe('application/octet-stream')
      expect(downloaded[0].filename).toBe(`${staged.tool.name}.py`)
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:synthetic-source')
      expect(document.querySelector('a[download]')).toBeNull()
      expect(JSON.stringify(staged)).toBe(original)
      expect(onPublish).not.toHaveBeenCalled()
      expect(onAuthor).not.toHaveBeenCalled()
    })

  it('offers the download after the actual controller looks up an existing saved request', async () => {
    const staged = fixtures[1]
    const job = { contract: 'leaf.customization-stage-job.v1', status: 'staged',
      change_set_id: staged.receipt.change_set_id,
      poll_url: `/api/author/stages/${staged.receipt.change_set_id}`, result: staged }
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      contract: 'leaf.customization-stage-recovery.v1', status: 'found', job,
    }))).mockResolvedValueOnce(responseFor(staged, bytesFor(1)))
    function Lookup() {
      const stageActivity = useAuthorStageController({ mock: false })
      return <AuthorPanel stageActivity={stageActivity} />
    }
    render(<Lookup />)
    fireEvent.change(screen.getByLabelText('Existing authoring request ID'), { target: { value: staged.receipt.change_set_id } })
    fireEvent.click(screen.getByRole('button', { name: 'Check existing request' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Download staged source' }))
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls.map((call) => call[1].method)).toEqual(['POST', 'GET'])
    expect(fetchMock.mock.calls[0][0]).toBe('/api/author/stage/recover')
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ change_set_id: staged.receipt.change_set_id }))
    expect(new Uint8Array(await blobs[0].arrayBuffer())).toEqual(bytesFor(1))
  })

  it.each([
    ['commit', { 'X-Leaf-Source-Commit': 'f'.repeat(40) }],
    ['path', { 'X-Leaf-Source-Path': fixtures[1].tool.entry }],
    ['sha256', { 'X-Leaf-Source-Sha256': 'f'.repeat(64) }],
    ['git blob', { 'X-Leaf-Source-Blob': 'f'.repeat(40) }],
    ['short body', { 'Content-Length': '200' }],
    ['long body', { 'Content-Length': '1' }],
    ['unbounded length', { 'Content-Length': '262145' }],
    ['missing length', { 'Content-Length': '' }],
    ['media type', { 'Content-Type': 'text/html' }],
    ['encoding', { 'Content-Encoding': 'gzip' }],
  ])('rejects %s mismatch before any Blob or mutation', async (_name, headers) => {
    const staged = fixtures[0]
    const original = JSON.stringify(staged)
    fetchMock.mockResolvedValue(responseFor(staged, bytesFor(0), headers))
    render(<AuthorPanel stageActivity={{ result: staged }} />)
    fireEvent.click(downloadButton())
    await screen.findByText('Source download unavailable. Refresh the saved request and try again.')
    expect(blobs).toEqual([])
    expect(click).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][1].method).toBe('GET')
    expect(JSON.stringify(staged)).toBe(original)
  })

  it('uses the app 401 invalidation path and never downloads', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeUnauthorized(listener)
    fetchMock.mockResolvedValue(new Response(null, { status: 401 }))
    render(<StagedSourceDownload staged={fixtures[0]} />)
    fireEvent.click(downloadButton())
    await waitFor(() => expect(listener).toHaveBeenCalledTimes(1))
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    expect(blobs).toEqual([])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('rejects absent sessions and mock mode before fetching', async () => {
    const expectedScope = readActiveScope()
    localStorage.removeItem('leaf.jwt')
    await expect(downloadStagedSource(fixtures[0], { expectedScope })).rejects.toThrow()
    localStorage.setItem('leaf.jwt', token)
    await getSessionIdentity(true)
    await expect(downloadStagedSource(fixtures[0], { expectedScope })).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['unmount', 'scope', 'receipt'])('discards a pending response on %s change', async (change) => {
    let resolve
    fetchMock.mockReturnValue(new Promise((done) => { resolve = done }))
    const view = render(<StagedSourceDownload staged={fixtures[0]} />)
    fireEvent.click(downloadButton())
    if (change === 'unmount') view.unmount()
    if (change === 'receipt') view.rerender(<StagedSourceDownload staged={fixtures[1]} />)
    if (change === 'scope') act(() => {
      localStorage.setItem('leaf.org_id', 'other-synthetic-org')
      notifyActiveScopeChanged()
    })
    await act(async () => { resolve(responseFor(fixtures[0], bytesFor(0))) })
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
    expect(blobs).toEqual([])
  })

  it('refuses redirects and hides non-script or demo receipts', async () => {
    fetchMock.mockRejectedValue(new TypeError('redirect refused'))
    const view = render(<StagedSourceDownload staged={fixtures[0]} />)
    fireEvent.click(downloadButton())
    await screen.findByText('Source download unavailable. Refresh the saved request and try again.')
    expect(blobs).toEqual([])
    for (const staged of [{ ...fixtures[0], demo: true }, { ...fixtures[0], receipt: null },
      { ...fixtures[0], tool: { ...fixtures[0].tool, kind: 'view' } }]) {
      view.rerender(<StagedSourceDownload staged={staged} />)
      expect(screen.queryByRole('button')).toBeNull()
    }
  })
})
