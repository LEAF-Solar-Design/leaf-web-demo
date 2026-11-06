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
  const materials = [{ material_id: ORG, project_id: PROJECT, org_id: ORG, actor_binding_id: ORG,
    name: 'Authentic transcript.txt', media_type: 'text/plain', byte_size: 1000, sha256: 'e'.repeat(64) },
  { material_id: REQUEST, project_id: PROJECT, org_id: ORG, actor_binding_id: ORG,
    name: 'Teacher movie.mov', media_type: 'video/quicktime', byte_size: 55000000, sha256: 'f'.repeat(64) }]

  it('native_edit_ui shows create and edit guidance, retained movie and enabled saved-source editing', async () => {
    const fetch = vi.fn(async (url) => response(String(url).includes('/reference-files') ? { materials, next_cursor: null } : [native()]))
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn()
    render(<Host startAgentTurn={startAgentTurn} sourceContext="native" />)
    fireEvent.click(screen.getByRole('button', { name: 'Build source' }))
    await screen.findByText('Private source saved')
    expect(screen.getByRole('dialog', { name: 'iOS project source' })).toBeVisible()
    expect(screen.getByText(/one through eight nonempty TXT or JSON/)).toHaveTextContent('at most 1 MiB each and 4 MiB total')
    expect(screen.getByText(/iOS app build, package, simulator\/device runtime and delivery remain pending/)).toBeVisible()
    expect(screen.getByText(/a transcript is a text reference, not video processing/)).toBeVisible()
    expect(screen.getByRole('checkbox', { name: 'Authentic transcript.txt' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Teacher movie.mov' })).toBeDisabled()
    expect(screen.getByText('Retained project material; unavailable for this source adapter.')).toBeVisible()
    expect(screen.getByText(/Editing saved iOS source can use existing project source and data with no new originals/)).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Stage source' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Prepare source download' })).not.toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: 'Prepare source download' })).not.toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: 'Prepare source download' })).not.toBeInTheDocument()
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
  it('prepares verified private Git bytes with one GET and requires an explicit download link click', async () => {
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
    const fetch = vi.fn(async (url) => {
      if (String(url).includes('/reference-files')) return response({ materials: [], next_cursor: null })
      if (String(url).includes('/export?')) return new Response(bytes, { headers: {
        'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${filename}"`,
        'Content-Length': String(bytes.byteLength), 'X-Leaf-Bundle-Sha256': hash,
        'X-Leaf-Source-Commit': 'c'.repeat(40), 'X-Leaf-Source-Tree': 'd'.repeat(40),
        'X-Leaf-Source-Version': '1', 'X-Leaf-Source-Receipt-Digest': 'a'.repeat(64),
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
      } })
      return response(String(url).endsWith('/requests') ? [saved('published')] : saved('published'))
    })
    vi.stubGlobal('fetch', fetch)
    const startAgentTurn = vi.fn()
    const host = render(<Host startAgentTurn={startAgentTurn} />)
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
    expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    expect(startAgentTurn).not.toHaveBeenCalled()
    host.rerender(<Host startAgentTurn={startAgentTurn} profile="solar" />)
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
