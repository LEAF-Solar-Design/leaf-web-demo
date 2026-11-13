import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import ProjectSourceCandidateReview from './ProjectSourceCandidateReview.jsx'
import useProjectSourceController from './useProjectSourceController.js'

const PROJECT = '11111111-1111-4111-8111-111111111111'
const REQUEST = '22222222-2222-4222-8222-222222222222'
const ORG = '33333333-3333-4333-8333-333333333333'
const saved = (state = 'awaiting_confirmation', version = 1) => ({ schema: 'leaf.browser-project-source.v1', project_id: PROJECT,
  source_request_id: REQUEST, state, version, receipt_digest: 'a'.repeat(64), base_commit: 'b'.repeat(40),
  source_commit: 'c'.repeat(40), source_tree: 'd'.repeat(40), changed_paths: ['app/a-very-long-directory-name/page.jsx'],
  created_at: '2026-11-01T12:00:00Z', updated_at: '2026-11-01T12:00:00Z' })
const response = (value) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
function Host({ startAgentTurn, profile = 'browser', projectId = PROJECT, sourceContext = 'browser' }) {
  const controller = useProjectSourceController({ projectId, orgId: ORG, signedIn: true, mock: false, startAgentTurn, sourceContext })
  return <><button onClick={() => controller.open('Create a quiet reading page.')}>Build source</button>
    <span>{profile}</span><ProjectSourceCandidateReview controller={controller} /></>
}
beforeEach(() => {
  localStorage.setItem('leaf.jwt', `test.${btoa(JSON.stringify({ sub: 'reader' }))}.test`)
  localStorage.setItem('leaf.org_id', ORG)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

describe('iOS source review', () => {
  const native = (state = 'published') => ({ ...saved(state), schema: 'leaf.native-project-source.v1', changed_paths: ['Sources/ContentView.swift'] })
  const job = (status = 'queued', changes = {}) => ({ job_id: ORG, status,
    schema: 'leaf.project-repository-preview-source.v1', source_profile: 'ios-swiftui-xcodegen-v1', repo_key: PROJECT,
    source_request_id: REQUEST, source_version: 1, receipt_digest: 'a'.repeat(64), source_commit: 'c'.repeat(40),
    source_tree: 'd'.repeat(40), archive_sha256: 'e'.repeat(64),
    ...(status === 'complete' ? { receipt: { kind: 'simulator', receipt_id: PROJECT, sha256: 'f'.repeat(64) },
      evidence_basis: 'authenticated-worker-report' } : {}), ...changes })
  const openPreview = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Build iOS source preview' })).toBeEnabled())
  }
  const materials = [{ material_id: ORG, project_id: PROJECT, org_id: ORG, actor_binding_id: ORG,
    name: 'Authentic transcript.txt', media_type: 'text/plain', byte_size: 1000, sha256: 'e'.repeat(64) },
  { material_id: REQUEST, project_id: PROJECT, org_id: ORG, actor_binding_id: ORG,
    name: 'Teacher movie.mov', media_type: 'video/quicktime', byte_size: 55000000, sha256: 'f'.repeat(64) }]
  const bundleReply = (changes = {}) => {
    const bytes = new TextEncoder().encode(`# v3 git bundle\n@object-format=sha1\n${'c'.repeat(40)} refs/heads/main\n\nPACK`)
    return new Response(changes.bytes || bytes, { headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Disposition': `attachment; filename="project-source-${'c'.repeat(40)}.bundle"`,
      'Content-Length': String(bytes.byteLength), 'X-Leaf-Bundle-Sha256': createHash('sha256').update(bytes).digest('hex'),
      'X-Leaf-Source-Commit': 'c'.repeat(40), 'X-Leaf-Source-Tree': 'd'.repeat(40),
      'X-Leaf-Source-Version': '1', 'X-Leaf-Source-Receipt-Digest': 'a'.repeat(64),
      'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', ...changes.headers,
    } })
  }

  it.each([
    ['commit', { headers: { 'X-Leaf-Source-Commit': 'f'.repeat(40) } }],
    ['tree', { headers: { 'X-Leaf-Source-Tree': 'f'.repeat(40) } }],
    ['version', { headers: { 'X-Leaf-Source-Version': '2' } }],
    ['receipt', { headers: { 'X-Leaf-Source-Receipt-Digest': 'f'.repeat(64) } }],
    ['hash', { headers: { 'X-Leaf-Bundle-Sha256': 'f'.repeat(64) } }],
    ['length', { headers: { 'Content-Length': '1' } }],
    ['oversize', { headers: { 'Content-Length': String(64 * 1024 * 1024 + 1) } }],
    ['type', { headers: { 'Content-Type': 'text/html' } }],
    ['filename', { headers: { 'Content-Disposition': 'attachment; filename="other.bundle"' } }],
    ['cache', { headers: { 'Cache-Control': 'public' } }],
    ['encoding', { headers: { 'Content-Encoding': 'gzip' } }],
    ['truncated body', { bytes: new Uint8Array([1, 2, 3]) }],
  ])('refuses a native source download with mismatched %s', async (_kind, changes) => {
    vi.stubGlobal('crypto', webcrypto)
    const NativeURL = URL
    const create = vi.fn()
    vi.stubGlobal('URL', class extends NativeURL { static createObjectURL = create; static revokeObjectURL = vi.fn() })
    vi.stubGlobal('fetch', vi.fn(async url => String(url).includes('/export?') ? bundleReply(changes)
      : response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])))
    render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare source download' }))
    await screen.findByText(/Project source download could not be verified/)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it.each(['project', 'account', 'surface', 'close'])('discards a late native export after %s changes', async kind => {
    vi.stubGlobal('crypto', webcrypto)
    const NativeURL = URL
    const create = vi.fn()
    vi.stubGlobal('URL', class extends NativeURL { static createObjectURL = create; static revokeObjectURL = vi.fn() })
    let finish, signal
    vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
      if (String(url).includes('/export?')) {
        signal = init.signal
        return new Promise(resolve => { finish = () => resolve(bundleReply()) })
      }
      return response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])
    }))
    const props = { startAgentTurn: vi.fn(), sourceContext: 'native' }
    const host = render(<Host {...props} />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare source download' }))
    await waitFor(() => expect(finish).toEqual(expect.any(Function)))
    if (kind === 'project') host.rerender(<Host {...props} projectId={ORG} />)
    else if (kind === 'surface') host.rerender(<Host {...props} sourceContext="browser" />)
    else if (kind === 'account') act(() => {
      localStorage.setItem('leaf.jwt', `test.${btoa(JSON.stringify({ sub: 'other-reader' }))}.test`)
      window.dispatchEvent(new Event('leaf:active-scope'))
    })
    else fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    if (['project', 'account', 'surface'].includes(kind)) expect(signal.aborted).toBe(true)
    await act(async () => { finish() })
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('submits the reviewed repository request once and observes authenticated job state', async () => {
    vi.stubGlobal('crypto', webcrypto)
    let status = 'queued'
    const fetch = vi.fn(async (url, init = {}) => {
      if (String(url).includes('/native-previews')) return response(job(init.method === 'POST' ? 'queued' : status))
      return response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])
    })
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn()
    render(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    await openPreview()
    expect(screen.getByText(`Source commit: ${'c'.repeat(40)}`)).toBeVisible()
    expect(screen.getByText(`Source tree: ${'d'.repeat(40)}`)).toBeVisible()
    const build = screen.getByRole('button', { name: 'Build iOS source preview' })
    act(() => { fireEvent.click(build); fireEvent.click(build) })
    await screen.findByText('Repository iOS preview: queued')
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(JSON.parse(posts[0][1].body)).toEqual({ source_request_id: REQUEST })
    expect(posts[0][1].headers['Idempotency-Key']).toEqual(expect.any(String))
    expect(build).toBeDisabled()
    for (const next of ['running', 'complete']) {
      status = next
      fireEvent.click(screen.getByRole('button', { name: 'Check source preview again' }))
      await screen.findByText(`Repository iOS preview: ${next}`)
    }
    expect(screen.getByText(/Simulator evidence: .*authenticated worker report/)).toBeVisible()
    expect(startAgentTurn).not.toHaveBeenCalled()
  })

  it('reconciles a lost submission after reload using the original idempotency key', async () => {
    vi.stubGlobal('crypto', webcrypto)
    let reachable = false
    const fetch = vi.fn(async (url, init = {}) => {
      if (String(url).includes('/native-previews')) {
        if (!reachable) throw new TypeError('Connection lost after submission')
        return response(job('running'))
      }
      return response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])
    })
    vi.stubGlobal('fetch', fetch)
    const host = render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await screen.findByText('Preview outcome unknown. Check the existing preview before starting another.')
    expect(screen.getByRole('button', { name: 'Build iOS source preview' })).toBeDisabled()
    const first = fetch.mock.calls.find(([, init]) => init?.method === 'POST')[1]
    host.unmount()
    reachable = true
    render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Repository iOS preview: running')
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(posts[1][1].headers['Idempotency-Key']).toBe(first.headers['Idempotency-Key'])
    expect(posts[1][1].body).toBe(first.body)
    fireEvent.click(screen.getByRole('button', { name: 'Check source preview again' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check source preview again' })).toBeEnabled())
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(2)
    expect(fetch.mock.lastCall[0]).toContain(`/native-previews/${ORG}`)
  })

  it('polls the same running job and stops observation at a terminal server state', async () => {
    vi.stubGlobal('crypto', webcrypto)
    let status = 'queued'
    const fetch = vi.fn(async (url) => response(String(url).includes('/native-previews') ? job(status)
      : String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()]))
    vi.stubGlobal('fetch', fetch)
    const host = render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await screen.findByText('Repository iOS preview: queued')
    vi.useFakeTimers()
    try {
      status = 'running'
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Check source preview again' })) })
      expect(screen.getByText('Repository iOS preview: running')).toBeVisible()
      status = 'failed'
      await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
      expect(screen.getByText('Repository iOS preview: failed')).toBeVisible()
      const count = fetch.mock.calls.length
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
      expect(fetch.mock.calls).toHaveLength(count)
      expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    } finally { host.unmount(); vi.useRealTimers() }
  })

  it('reloads the preview for an explicitly selected older source request', async () => {
    vi.stubGlobal('crypto', webcrypto)
    const latest = { ...native(), source_request_id: ORG, created_at: '2026-11-02T12:00:00Z', updated_at: '2026-11-02T12:00:00Z' }
    const fetch = vi.fn(async (url, init = {}) => {
      if (String(url).includes('/native-previews')) return response(job(init.method === 'POST' ? 'queued' : 'running'))
      if (String(url).includes('/reference-files')) return response({ materials: [], next_cursor: null })
      return response(String(url).endsWith('/requests') ? [latest, native()] : native())
    })
    vi.stubGlobal('fetch', fetch)
    const host = render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Source request 2: published' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Build iOS source preview' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await screen.findByText('Repository iOS preview: queued')
    host.unmount()
    render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Repository iOS preview: running')
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(fetch.mock.calls.find(([, init]) => init?.method === 'POST')[1].body).toBe(JSON.stringify({ source_request_id: REQUEST }))
    expect(fetch.mock.lastCall[0]).toContain(`/native-previews/${ORG}`)
  })

  it('restores the same job after switching surfaces and never renders cached state before server readback', async () => {
    vi.stubGlobal('crypto', webcrypto)
    let finishRead
    let deferRead = false
    const fetch = vi.fn(async (url, init = {}) => {
      if (String(url).includes('/native-previews')) {
        if (init.method !== 'POST' && deferRead) return new Promise(resolve => { finishRead = () => resolve(response(job('complete'))) })
        return response(job())
      }
      return response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null }
        : [String(url).includes('/native/') ? native() : saved('published')])
    })
    vi.stubGlobal('fetch', fetch)
    const props = { startAgentTurn: vi.fn() }
    const host = render(<Host {...props} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await screen.findByText('Repository iOS preview: queued')
    host.rerender(<Host {...props} />)
    expect(screen.queryByText(/Repository iOS preview:/)).not.toBeInTheDocument()
    deferRead = true
    host.rerender(<Host {...props} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await waitFor(() => expect(finishRead).toEqual(expect.any(Function)))
    expect(screen.queryByText('Repository iOS preview: queued')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Build iOS source preview' })).toBeDisabled()
    await act(async () => { finishRead() })
    await screen.findByText('Repository iOS preview: complete')
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })

  it.each(['source_request_id', 'source_version', 'receipt_digest', 'source_commit', 'source_tree'])('rejects preview readback with a different %s', async field => {
    vi.stubGlobal('crypto', webcrypto)
    const wrong = { source_request_id: PROJECT, source_version: 2, receipt_digest: 'f'.repeat(64), source_commit: 'b'.repeat(40), source_tree: 'b'.repeat(40) }
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/native-previews') ? job('complete', { [field]: wrong[field] })
      : String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])))
    render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await screen.findByText('Preview outcome unknown. Check the existing preview before starting another.')
    expect(screen.queryByText('Repository iOS preview: complete')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Build iOS source preview' })).toBeDisabled()
  })

  it.each(['project', 'account'])('aborts and fences a late preview response after a %s change', async kind => {
    vi.stubGlobal('crypto', webcrypto)
    let finish, signal
    vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
      if (String(url).includes('/native-previews')) {
        signal = init.signal
        return new Promise(resolve => { finish = () => resolve(response(job('complete'))) })
      }
      return response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [native()])
    }))
    const props = { startAgentTurn: vi.fn(), sourceContext: 'native' }
    const host = render(<Host {...props} />)
    await openPreview()
    fireEvent.click(screen.getByRole('button', { name: 'Build iOS source preview' }))
    await waitFor(() => expect(finish).toEqual(expect.any(Function)))
    if (kind === 'project') host.rerender(<Host {...props} projectId={ORG} />)
    else act(() => {
      localStorage.setItem('leaf.jwt', `test.${btoa(JSON.stringify({ sub: 'other-reader' }))}.test`)
      window.dispatchEvent(new Event('leaf:active-scope'))
    })
    expect(signal.aborted).toBe(true)
    await act(async () => { finish() })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText(/Repository iOS preview:/)).not.toBeInTheDocument()
  })

  it('native_edit_ui shows create and edit guidance, retained movie and enabled saved-source editing', async () => {
    const fetch = vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials, next_cursor: null } : [native()]))
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn()
    render(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    expect(screen.getByRole('dialog', { name: 'iOS project source' })).toBeVisible()
    expect(screen.getByText(/one through eight nonempty TXT or JSON/)).toHaveTextContent('at most 1 MiB each and 4 MiB total')
    expect(screen.getByText(/Build an iOS simulator preview from saved repository source below/)).toBeVisible()
    expect(screen.getByText(/a transcript is a text reference, not video processing/)).toBeVisible()
    expect(screen.getByRole('checkbox', { name: 'Authentic transcript.txt' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Teacher movie.mov' })).toBeDisabled()
    expect(screen.getByText('Retained project material; unavailable for this source adapter.')).toBeVisible()
    expect(screen.getByText(/Editing saved iOS source can use existing project source and data with no new originals/)).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Stage source' })).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeEnabled()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(fetch.mock.calls.some(([url]) => String(url).includes('/export'))).toBe(false)
    expect(startAgentTurn).not.toHaveBeenCalled()
  })

  it('native_edit_ui requires a supported original for create and retains modal keyboard behavior', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [])))
    render(<Host startAgentTurn={vi.fn()} sourceContext="native" />)
    const opener = screen.getByRole('button', { name: 'Build source' })
    opener.focus(); fireEvent.click(opener)
    await screen.findByText('Choose one through eight TXT or JSON originals.')
    fireEvent.click(screen.getByRole('button', { name: 'Use this reference selection' }))
    expect(screen.getByRole('button', { name: 'Stage source' })).toBeDisabled()
    expect(screen.queryByText(/continue with the brief only/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeDisabled()
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('native_edit_ui explicitly reviews zero new originals, stages an edit once and fences pending review', async () => {
    let durable = native()
    const posts = []
    vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
      if (String(url).includes('/reference-files')) return response({ materials: [materials[1]], next_cursor: null })
      if (init.method === 'POST') {
        posts.push({ url: String(url), ...init })
        durable = { ...native('awaiting_confirmation'), source_request_id: JSON.parse(init.body).source_request_id }
      }
      return response(String(url).endsWith('/requests') && init.method !== 'POST' ? [durable] : durable)
    }))
    const startAgentTurn = vi.fn(async () => ({ session_id: 'edit-session', turn_id: 'edit-turn' }))
    render(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    const stage = screen.getByRole('button', { name: 'Stage source' })
    expect(stage).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Teacher movie.mov' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Use this reference selection' }))
    await waitFor(() => expect(stage).toBeEnabled())
    act(() => { fireEvent.click(stage); fireEvent.click(stage) })
    await screen.findByText('Source ready for review')
    expect(posts).toHaveLength(1)
    expect(JSON.parse(posts[0].body)).toEqual({ source_request_id: expect.any(String),
      requirements: 'Create a quiet reading page.', source_operation: 'edit', material_refs: [], expected_base_commit: 'c'.repeat(40) })
    expect(startAgentTurn).toHaveBeenCalledTimes(1)
    expect(startAgentTurn.mock.calls[0][1]).toMatchObject({ browser_action: 'edit', source_profile: 'ios-swiftui-xcodegen-v1',
      expected_base_commit: 'c'.repeat(40), material_refs: [] })
    expect(stage).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Confirm changes' })).toBeEnabled()
    expect(screen.getByText('Candidate version 1')).toBeVisible()
    expect(screen.getByText(`Candidate digest: ${'a'.repeat(64)}`)).toBeVisible()
    expect(screen.getByText('Sources/ContentView.swift')).toBeVisible()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('native_edit_ui disables staging and confirmation after a stale edit has an unknown readback', async () => {
    const fetch = vi.fn(async (url, init = {}) => {
      if (String(url).includes('/reference-files')) return response({ materials, next_cursor: null })
      if (init.method === 'POST') return new Response('{}', { status: 409 })
      if (String(url).includes('/requests/')) return new Response('{}', { status: 404 })
      return response([native()])
    })
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn(async () => ({ session_id: 'edit-session', turn_id: 'edit-turn' }))
    render(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    const stage = screen.getByRole('button', { name: 'Stage source' })
    await waitFor(() => expect(stage).toBeEnabled())
    fireEvent.click(stage)
    await screen.findByText('Publication outcome unknown. Refresh the original request.')
    expect(stage).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Confirm changes' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Authentic transcript.txt' })).toBeChecked()
    expect(screen.getByText('Original request: Create a quiet reading page.')).toBeVisible()
    expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(startAgentTurn).toHaveBeenCalledTimes(1)
  })

  it('closes and clears the prior profile review, then reads the proper durable history on return', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials, next_cursor: null }
      : [String(url).includes('/native/') ? native() : saved('published')])))
    const startAgentTurn = vi.fn()
    const host = render(<Host startAgentTurn={startAgentTurn} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    expect(screen.getByText('app/a-very-long-directory-name/page.jsx')).toBeVisible()
    host.rerender(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Sources/ContentView.swift')
    expect(screen.queryByText('app/a-very-long-directory-name/page.jsx')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeEnabled()
    host.rerender(<Host startAgentTurn={startAgentTurn} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('app/a-very-long-directory-name/page.jsx')
    expect(screen.queryByText('Sources/ContentView.swift')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeEnabled())
    expect(startAgentTurn).not.toHaveBeenCalled()
  })
})

describe('Browser source review', () => {
  it.each(['browser', 'native'])('prepares verified private %s Git bytes with one GET and an explicit download link click', async sourceContext => {
    vi.stubGlobal('crypto', webcrypto)
    const NativeURL = URL
    const create = vi.fn(() => 'blob:source-bundle')
    const revoke = vi.fn()
    vi.stubGlobal('URL', class extends NativeURL {
      static createObjectURL = create
      static revokeObjectURL = revoke
    })
    const bytes = new TextEncoder().encode(`# v3 git bundle\n@object-format=sha1\n${'c'.repeat(40)} refs/heads/main\n\nPACK`)
    const hash = createHash('sha256').update(bytes).digest('hex')
    const filename = `project-source-${'c'.repeat(40)}.bundle`
    const durable = { ...saved('published'), schema: `leaf.${sourceContext}-project-source.v1` }
    const fetch = vi.fn(async (url) => {
      if (String(url).includes('/reference-files')) return response({ materials: [], next_cursor: null })
      if (String(url).includes('/export?')) return new Response(bytes, { headers: {
        'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${filename}"`,
        'Content-Length': String(bytes.byteLength), 'X-Leaf-Bundle-Sha256': hash,
        'X-Leaf-Source-Commit': 'c'.repeat(40), 'X-Leaf-Source-Tree': 'd'.repeat(40),
        'X-Leaf-Source-Version': '1', 'X-Leaf-Source-Receipt-Digest': 'a'.repeat(64),
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
      } })
      return response(String(url).endsWith('/requests') ? [durable] : durable)
    })
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn()
    const host = render(<Host startAgentTurn={startAgentTurn} sourceContext={sourceContext} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    const prepare = screen.getByRole('button', { name: 'Prepare source download' })
    await waitFor(() => expect(prepare).toBeEnabled())
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    act(() => { fireEvent.click(prepare); fireEvent.click(prepare) })
    await screen.findByText('Source download ready')
    const link = screen.getByRole('link', { name: 'Download Git source bundle' })
    expect(link).toHaveAttribute('href', 'blob:source-bundle')
    expect(link).toHaveAttribute('download', filename)
    expect(link.tabIndex).toBe(0)
    expect(screen.getByText(`SHA256: ${hash}`)).toBeVisible()
    expect(create).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls.filter(([url]) => String(url).includes('/export?'))).toHaveLength(1)
    const [url, init] = fetch.mock.calls.find(([url]) => String(url).includes('/export?'))
    expect(String(url)).toContain(`/api/projects/${PROJECT}/${sourceContext}/source/requests/${REQUEST}/export?`)
    expect(Object.fromEntries(new URL(String(url), 'https://example.test').searchParams)).toEqual({
      source_commit: durable.source_commit, source_tree: durable.source_tree,
      receipt_digest: durable.receipt_digest, version: String(durable.version),
    })
    expect(init).toMatchObject({ method: 'GET', cache: 'no-store', redirect: 'error' })
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(startAgentTurn).not.toHaveBeenCalled()
    host.rerender(<Host startAgentTurn={startAgentTurn} profile="solar" sourceContext={sourceContext} />)
    expect(screen.getByRole('link')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(revoke).toHaveBeenCalledWith('blob:source-bundle')
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeEnabled())
  })

  it('uses the real hook/API, shows source-only save, and requires an explicit single confirmation', async () => {
    let durable = saved()
    const posts = []
    vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
      if (String(url).includes('/reference-files')) return response({ materials: [], next_cursor: null })
      if (init.method === 'POST') { posts.push({ url, ...init }); durable = saved('published', 2) }
      return response(String(url).endsWith('/requests') ? [durable] : durable)
    }))
    const startAgentTurn = vi.fn(async () => ({ session_id: 'returned-session', turn_id: 'returned-turn' }))
    const host = render(<Host startAgentTurn={startAgentTurn} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Source ready for review')
    expect(posts).toHaveLength(0)
    expect(startAgentTurn).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Original requirements' })).toHaveValue('')
    expect(screen.getByText('app/a-very-long-directory-name/page.jsx')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Recover' })).toBeDisabled()
    host.rerender(<Host startAgentTurn={startAgentTurn} profile="solar" />)
    expect(screen.getByRole('dialog')).toBeVisible()
    const confirm = screen.getByRole('button', { name: 'Confirm changes' })
    act(() => { fireEvent.click(confirm); fireEvent.click(confirm) })
    await screen.findByText('Private source saved')
    expect(posts).toHaveLength(1)
    expect(startAgentTurn).toHaveBeenCalledTimes(1)
    expect(screen.getByText('App build and private preview are still pending.')).toBeVisible()
    expect(within(screen.getByRole('dialog')).queryByRole('link')).not.toBeInTheDocument()
    expect(confirm).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    expect(posts).toHaveLength(1)
    host.rerender(<Host startAgentTurn={startAgentTurn} projectId={ORG} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Private source saved')).not.toBeInTheDocument()
  })

  it('contains keyboard focus, includes the textarea in Tab order, owns Escape and restores the opener', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [])))
    render(<Host startAgentTurn={vi.fn()} />)
    const opener = screen.getByRole('button', { name: 'Build source' })
    opener.focus(); fireEvent.click(opener)
    const dialog = screen.getByRole('dialog', { name: 'Browser project source' })
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh source' })).toBeEnabled())
    const textarea = screen.getByRole('textbox', { name: 'Original requirements' })
    expect(textarea.tabIndex).toBe(0)
    textarea.focus()
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    textarea.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh source' })).toBeEnabled())
    screen.getByRole('button', { name: 'Refresh source' }).focus()
    fireEvent.keyDown(document.activeElement, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(screen.getByRole('button', { name: 'Refresh source' })).toHaveFocus()
    expect(dialog.style.maxHeight).toBe('calc(100dvh - 24px)')
    expect(dialog.style.overflowWrap).toBe('anywhere')
    fireEvent.keyDown(document.activeElement, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('shows retained unsupported originals and incomplete list limits without claiming consumption', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials: [{
      material_id: REQUEST, project_id: PROJECT, org_id: ORG, actor_binding_id: ORG, name: `${'long-name-'.repeat(12)}.mov`,
      media_type: 'video/quicktime', byte_size: 10000, sha256: 'e'.repeat(64),
    }], next_cursor: 'invalid' } : [])))
    render(<Host startAgentTurn={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Retained project material; unavailable for this source adapter.')
    expect(screen.getByText(/Reference list incomplete or unavailable/)).toBeVisible()
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Stage source' })).toBeDisabled()
    expect(screen.queryByText(/consumed|built app|preview ready/i)).not.toBeInTheDocument()
  })

  it.each(['publishing', 'rejected', 'conflicted', 'failed'])('enables only actual state actions for %s', async (state) => {
    vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials: [], next_cursor: null } : [saved(state)])))
    render(<Host startAgentTurn={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText(`Source state: ${state}`)
    expect(screen.getByRole('button', { name: 'Confirm changes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Prepare source download' })).toBeDisabled()
    if (state === 'publishing') expect(screen.getByRole('button', { name: 'Recover' })).toBeEnabled()
    else expect(screen.getByRole('button', { name: 'Recover' })).toBeDisabled()
  })
})
