import React from 'react'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const proposalAuth = vi.hoisted(() => ({ account: 'account-a', scope: 'org-a', org: 'org-a', actor: 'actor-a', logout: new Set(), token: new Set(), active: new Set() }))
vi.mock('../api.js', async importOriginal => ({ ...await importOriginal(), getStoredOrgId: () => proposalAuth.org }))
vi.mock('../lib/composerDraft.js', async importOriginal => ({ ...await importOriginal(), composerDraftAccountScope: () => proposalAuth.account }))
vi.mock('../auth/activeScope.js', async importOriginal => ({ ...await importOriginal(),
  readActiveScope: () => proposalAuth.scope,
  subscribeActiveScope: callback => { proposalAuth.active.add(callback); return () => proposalAuth.active.delete(callback) },
}))
vi.mock('../auth.js', async importOriginal => ({ ...await importOriginal(),
  subscribeBeforeLogout: callback => { proposalAuth.logout.add(callback); return () => proposalAuth.logout.delete(callback) },
  subscribeTokenStored: callback => { proposalAuth.token.add(callback); return () => proposalAuth.token.delete(callback) },
}))
vi.mock('../site/ProjectBoardGround.jsx', () => ({ ProjectBoardGround: () => null }))
vi.mock('../site/WorkspaceEnvironment.jsx', () => ({ default: ({ children, panel }) => <section data-testid="workspace" data-panel={panel}>{children}</section> }))
vi.mock('../site/projectPageClient.js', async importOriginal => ({ ...await importOriginal(),
  loadProjectPage: vi.fn(), saveProjectPage: vi.fn(), submitProjectNativePreview: vi.fn(),
}))
vi.mock('../projects/api.js', async importOriginal => ({ ...await importOriginal(),
  getStoredActorBindingId: () => proposalAuth.actor,
  listProjectPhotos: vi.fn(async () => ({ materials: [] })),
  readProjectPhotoPreview: vi.fn(async () => new Blob(['photo'], { type: 'image/jpeg' })),
}))

vi.mock('../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))
vi.mock('../engineChanges.js', async (importOriginal) => ({
  ...await importOriginal(),
  listEngineChanges: vi.fn(async () => ({ kind: 'forbidden' })),
}))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(async () => []),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import { openStream } from '../converse.js'
import ConversePanel from './ConversePanel.jsx'
import { createCatalogController } from '../controllers/catalog/createCatalogController.js'
import { BrowserAssetGround } from '../site/BrowserAssetGround.jsx'
import { emptyPortfolio } from '../site/portfolioDocument.js'
import { loadProjectPage, saveProjectPage, submitProjectNativePreview } from '../site/projectPageClient.js'
import { deliverProjectPageProposal, subscribeProjectPageProposals } from '../site/projectPageProposalDelivery.js'

const C = 'leaf.customization.v1'
const I = '7f3a51f0-9d9a-43be-8d29-cbdba31249c8'
const H = 'a'.repeat(64)
const P = { contract: C, change_set_id: I, status: 'published', catalog_digest: H }
const publication = { sessionId: 'session-1', changeSetId: I, catalogDigest: H }
const oldTools = [{ name: 'old-tool' }]
const oldCatalog = { families: [{ family_id: 'custom', capabilities: oldTools }], source: 'server' }
const subscriptions = []
const owners = []
const event = (patch = {}) => ({
  v: 1, session_id: 'session-1', turn_id: 'turn-1', seq: 1, type: 'tool_result',
  data: { tool: 'request_publication', ok: true, summary: 'The tool was published', result: { ...P } },
  ...patch,
})
const resultEvent = (patch) => event({ data: { ...event().data, result: { ...P, ...patch } } })
const flush = async () => { await act(async () => { await Promise.resolve() }) }
async function deliver(env, subscription = subscriptions.at(-1)) {
  await act(async () => { subscription.handlers.onEvent(env); await Promise.resolve() })
}
async function owner() {
  const services = {
    getTools: vi.fn(async () => oldTools),
    getCapabilities: vi.fn(async () => oldCatalog),
    routePrompt: vi.fn(async () => ({ lane: 'run', tool: null })),
  }
  const controller = createCatalogController({ services, adapters: { draftStorage: null }, context: { mock: true } })
  owners.push(controller)
  controller.start()
  await flush()
  expect(services.getTools).toHaveBeenCalledTimes(1)
  expect(services.getCapabilities).toHaveBeenCalledTimes(1)
  services.getTools.mockClear()
  services.getCapabilities.mockClear()
  const changed = vi.fn(controller.actions.refreshCatalog)
  const claim = vi.fn(controller.consumeCatalogPublication)
  return { controller, services, changed, claim }
}
function panel(o, props = {}) {
  return <ConversePanel sessionId="session-1" onDismiss={vi.fn()}
    onCatalogChanged={o.changed} consumeCatalogPublication={o.claim} {...props} />
}
function cycle(o, count = 1) {
  expect(o.changed).toHaveBeenCalledTimes(count)
  expect(o.services.getTools).toHaveBeenCalledTimes(count)
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(count)
}
beforeEach(() => {
  subscriptions.length = 0
  proposalAuth.account = 'account-a'; proposalAuth.scope = 'org-a'
  proposalAuth.org = 'org-a'; proposalAuth.actor = 'actor-a'
  proposalAuth.logout.clear(); proposalAuth.token.clear(); proposalAuth.active.clear()
  openStream.mockImplementation((_session, _seq, handlers) => {
    const close = vi.fn()
    subscriptions.push({ handlers, close })
    return { close }
  })
})

const projectProposal = () => {
  const document = emptyPortfolio()
  document.categories[0].images = [{ material_id: I, sha256: H, preview_sha256: H, title: 'Photo', alt: 'A photo' }]
  return { source: { project_id: I, revision: 7, digest: H }, proposed_document: { ...document, headline: 'Conversation headline' },
    proposed_digest: 'b'.repeat(64), edits: [{ id: 'headline', value: 'Conversation headline' }], model: 'fixture', usage: {} }
}
const proposalEvent = (patch = {}) => event({ data: { tool: 'propose_project_page', ok: true, result: projectProposal() }, ...patch })
const savedPortfolio = () => ({ revision: 7, digest: H, document: { ...projectProposal().proposed_document, headline: 'Selected work' } })
function proposalGround(props = {}) {
  return <BrowserAssetGround signedIn accountScope="account-a" projectId={I} browserActive {...props} />
}

const hashDocument = value => createHash('sha256').update(JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item)).digest('hex')
const photoSource = () => { const source = savedPortfolio(); source.digest = hashDocument(source.document); return source }
const photoResult = () => {
  const source = photoSource(), document = JSON.parse(JSON.stringify(source.document))
  const material_id = '22222222-2222-4222-8222-222222222222'
  document.categories[0].images.push({ material_id, sha256: 'c'.repeat(64), preview_sha256: 'd'.repeat(64), title: 'Crème cake', alt: 'Cake on a plate' })
  return { schema: 'leaf.project-photo-assignment-proposal.v1', source: { project_id: I, revision: source.revision, digest: source.digest },
    proposed_document: document, proposed_digest: hashDocument(document), changes: [{ op: 'assign', material_id, material_name: 'Original cake.jpg',
      gallery_slug: 'selected-work', gallery_title: 'Selected work', index: 1, title: 'Crème cake', alt: 'Cake on a plate' }] }
}
const photoEvent = (patch = {}) => event({ seq: 2, data: { tool: 'propose_project_photo_assignments', ok: true, result: photoResult() }, ...patch })
const startPhotoTurn = () => deliver(event({ type: 'turn_started', data: {} }))

describe('original-photo stream delivery to review and revision CAS', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto)
    loadProjectPage.mockImplementation(async () => photoSource())
    URL.createObjectURL = vi.fn(() => 'blob:photo-review')
    URL.revokeObjectURL = vi.fn()
  })

  it('reviews the actual A shape through Browser, detaches delivery and explicitly saves the entire draft with revision CAS', async () => {
    const o = await owner(), client = await vi.importActual('../site/projectPageClient.js')
    const fetchPage = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ page: { revision: 8, digest: photoResult().proposed_digest, document: photoResult().proposed_document } }) }))
    vi.stubGlobal('fetch', fetchPage)
    saveProjectPage.mockImplementationOnce(client.saveProjectPage)
    render(<>{proposalGround()}{panel(o)}</>)
    await startPhotoTurn()
    const env = photoEvent(), expected = JSON.parse(JSON.stringify(env.data.result.proposed_document))
    await deliver(env)
    const review = await screen.findByRole('region', { name: 'Review proposed photo assignments' })
    expect(review).toHaveTextContent('Original cake.jpg')
    expect(review).toHaveTextContent('Crème cake')
    expect(review).toHaveTextContent('Cake on a plate')
    env.data.result.proposed_document.categories[0].images[0].title = 'Injected'
    env.data.result.proposed_document.categories[0].images[1].sha256 = '0'.repeat(64)
    env.data.result.changes[0].title = 'Injected'
    // A normal completed turn leaves explicit review available.
    await deliver(event({ seq: 3, type: 'turn_completed', data: {} }))
    expect(fetchPage).not.toHaveBeenCalled()
    expect(saveProjectPage).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Use proposed assignments' }))
    const editor = within(screen.getByRole('region', { name: 'Page editor' }))
    expect(editor.getByText('Review the draft, then Save page.')).toBeTruthy()
    expect(saveProjectPage).not.toHaveBeenCalled()
    fireEvent.click(editor.getByRole('button', { name: 'Save page' }))
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1))
    expect(saveProjectPage.mock.lastCall.slice(0, 3)).toEqual([I, expected, 7])
    expect(JSON.parse(fetchPage.mock.lastCall[1].body)).toEqual({ expected_revision: 7, document: expected })
    expect(fetchPage.mock.lastCall[0]).toContain(`/api/projects/${I}/page`)
    expect(fetchPage.mock.lastCall[1].method).toBe('PUT')
    expect(expected.categories[0].images[0]).toEqual(photoSource().document.categories[0].images[0])
    expect(submitProjectNativePreview).not.toHaveBeenCalled()
  })

  it.each(['missing start', 'missing turn', 'foreign turn', 'blank turn', 'long turn', 'zero seq', 'wrong session', 'wrong schema'])('rejects %s before review', async mode => {
    const o = await owner()
    render(<>{proposalGround()}{panel(o)}</>)
    if (mode !== 'missing start') await startPhotoTurn()
    const env = photoEvent()
    if (mode === 'missing turn') delete env.turn_id
    if (mode === 'foreign turn') env.turn_id = 'other-turn'
    if (mode === 'blank turn') env.turn_id = ' '
    if (mode === 'long turn') env.turn_id = 't'.repeat(201)
    if (mode === 'zero seq') env.seq = 0
    if (mode === 'wrong session') env.session_id = 'other-session'
    if (mode === 'wrong schema') env.data.result.schema = 'unknown'
    await deliver(env)
    await flush()
    expect(screen.queryByRole('region', { name: 'Review proposed photo assignments' })).toBeNull()
    expect(saveProjectPage).not.toHaveBeenCalled()
  })

  it.each(['turn', 'org', 'actor', 'session', 'account', 'scope', 'logout', 'unmount'])('refuses Use after %s changes', async mode => {
    const o = await owner(), view = render(<>{proposalGround()}{panel(o)}</>)
    await startPhotoTurn(); await deliver(photoEvent())
    await screen.findByRole('region', { name: 'Review proposed photo assignments' })
    if (mode === 'turn') await deliver(event({ type: 'turn_started', turn_id: 'turn-2', seq: 3, data: {} }))
    if (mode === 'org' || mode === 'actor') proposalAuth[mode] = 'changed'
    if (mode === 'session') view.rerender(<>{proposalGround()}{panel(o, { sessionId: 'session-2' })}</>)
    if (mode === 'account') { proposalAuth.account = 'other'; act(() => [...proposalAuth.token].forEach(fn => fn())) }
    if (mode === 'scope') { proposalAuth.scope = 'other'; act(() => [...proposalAuth.active].forEach(fn => fn())) }
    if (mode === 'logout') act(() => [...proposalAuth.logout].forEach(fn => fn()))
    if (mode === 'unmount') view.unmount()
    const use = screen.queryByRole('button', { name: 'Use proposed assignments' })
    if (use) fireEvent.click(use)
    expect(screen.queryByText('Review the draft, then Save page.')).toBeNull()
    expect(saveProjectPage).not.toHaveBeenCalled()
  })

  it('keeps the photo tool discriminator separate from the legacy text result', async () => {
    const o = await owner()
    render(<>{proposalGround()}{panel(o)}</>)
    await startPhotoTurn()
    await deliver(photoEvent({ data: { tool: 'propose_project_photo_assignments', ok: true, result: projectProposal() } }))
    expect(screen.queryByRole('region', { name: 'Review proposed text' })).toBeNull()
    expect(saveProjectPage).not.toHaveBeenCalled()
  })
})

describe('conversation delivery to private portfolio review', () => {
  beforeEach(() => {
    loadProjectPage.mockImplementation(async () => savedPortfolio())
    URL.createObjectURL = vi.fn(() => 'blob:proposal-photo')
    URL.revokeObjectURL = vi.fn()
  })

  it('routes the exact successful event to Browser and Project review, then Use changes only the draft and preview', async () => {
    const select = vi.fn(), enter = vi.fn(), o = await owner()
    render(<>{proposalGround({ onSelect: select, onEnterWorkspace: enter })}{panel(o)}</>)
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
    expect(select).toHaveBeenCalledWith('browser')
    expect(enter).toHaveBeenCalled()
    expect(screen.getByTestId('workspace')).toHaveAttribute('data-panel', 'project')
    expect(screen.getByLabelText('Headline')).toHaveValue('Selected work')
    fireEvent.click(screen.getByRole('button', { name: 'Use proposed changes' }))
    expect(screen.getByLabelText('Headline')).toHaveValue('Conversation headline')
    expect(screen.getByRole('heading', { name: 'Conversation headline' })).toBeTruthy()
    expect(saveProjectPage).not.toHaveBeenCalled()
    expect(submitProjectNativePreview).not.toHaveBeenCalled()
    expect(o.claim).not.toHaveBeenCalled()
  })

  it('consumes a duplicate once, and Discard preserves the saved original', async () => {
    const select = vi.fn(), o = await owner()
    render(<>{proposalGround({ onSelect: select })}{panel(o)}</>)
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
    fireEvent.click(screen.getByRole('button', { name: 'Discard proposal' }))
    const count = select.mock.calls.length
    await deliver(proposalEvent())
    expect(screen.queryByRole('region', { name: 'Review proposed text' })).toBeNull()
    expect(select).toHaveBeenCalledTimes(count)
    expect(loadProjectPage).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Headline')).toHaveValue('Selected work')
    expect(saveProjectPage).not.toHaveBeenCalled()
  })

  it.each(['failed', 'wrong tool', 'wrong session', 'missing sequence', 'oversized'])('ignores %s without blocking the transcript or consuming a later success', async mode => {
    const o = await owner()
    const view = render(<>{proposalGround()}{panel(o)}</>)
    const bad = proposalEvent()
    if (mode === 'failed') bad.data.ok = false
    if (mode === 'wrong tool') bad.data.tool = 'other_tool'
    if (mode === 'wrong session') bad.session_id = 'other-session'
    if (mode === 'missing sequence') delete bad.seq
    if (mode === 'oversized') bad.data.summary = '😀'.repeat(270000)
    await deliver(event({ type: 'turn_started', data: {} }))
    await deliver(event({ type: 'tool_call', data: { tool: bad.data.tool, args_summary: 'text change' } }))
    await expect(deliver(bad)).resolves.toBeUndefined()
    expect(view.container.querySelector('.converse-step')).toHaveAttribute('data-state', mode === 'failed' ? 'failed' : 'done')
    expect(loadProjectPage).not.toHaveBeenCalled()
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
  })

  it('does not consume a stale rejected event and can accept its corrected replay', async () => {
    const o = await owner()
    render(<>{proposalGround()}{panel(o)}</>)
    const bad = proposalEvent(); bad.data.result.source.revision = 6
    await deliver(bad)
    await waitFor(() => expect(screen.getByLabelText('Headline')).toHaveValue('Selected work'))
    expect(screen.queryByRole('region', { name: 'Review proposed text' })).toBeNull()
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
  })

  it('accepts durable session and sequence without requiring a turn identifier', async () => {
    const o = await owner()
    render(<>{proposalGround()}{panel(o)}</>)
    const env = proposalEvent(); delete env.turn_id
    await deliver(env)
    await screen.findByRole('region', { name: 'Review proposed text' })
  })

  it('clears project dedupe on a project switch while preserving it across profile changes', async () => {
    const o = await owner(), view = render(<>{proposalGround()}{panel(o)}</>)
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
    fireEvent.click(screen.getByRole('button', { name: 'Discard proposal' }))
    view.rerender(<>{proposalGround({ profile: 'ios' })}{panel(o)}</>)
    await deliver(proposalEvent())
    expect(screen.queryByRole('region', { name: 'Review proposed text' })).toBeNull()
    view.rerender(<>{proposalGround({ projectId: '22222222-2222-4222-8222-222222222222' })}{panel(o)}</>)
    view.rerender(<>{proposalGround()}{panel(o)}</>)
    await deliver(proposalEvent())
    await screen.findByRole('region', { name: 'Review proposed text' })
    expect(loadProjectPage).toHaveBeenCalledTimes(2)
  })

  it.each(['session', 'account', 'org', 'project', 'logout', 'unmount'])('refuses an old stream after %s changes', async mode => {
    const o = await owner()
    const view = render(<>{proposalGround()}{panel(o)}</>)
    const old = subscriptions.at(-1)
    if (mode === 'session') view.rerender(<>{proposalGround()}{panel(o, { sessionId: 'session-2' })}</>)
    if (mode === 'project') view.rerender(<>{proposalGround({ projectId: '22222222-2222-4222-8222-222222222222' })}{panel(o)}</>)
    if (mode === 'account') {
      proposalAuth.account = 'account-b'
      act(() => [...proposalAuth.token].forEach(callback => callback()))
    }
    if (mode === 'org') {
      proposalAuth.scope = 'org-b'
      act(() => [...proposalAuth.active].forEach(callback => callback()))
    }
    if (mode === 'logout') act(() => [...proposalAuth.logout].forEach(callback => callback()))
    if (mode === 'unmount') view.unmount()
    await deliver(proposalEvent(), old)
    expect(loadProjectPage).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Review proposed text' })).toBeNull()
  })

  it('has no pending delivery for a future subscriber', async () => {
    const o = await owner()
    const view = render(panel(o))
    await deliver(proposalEvent())
    view.rerender(<>{proposalGround()}{panel(o)}</>)
    await flush()
    expect(loadProjectPage).not.toHaveBeenCalled()
    expect(deliverProjectPageProposal({ accountScope: 'foreign', result: projectProposal(), isCurrent: () => true })).toBe(false)
  })

  it('isolates a throwing subscriber from transcript ingestion and later delivery', async () => {
    const o = await owner()
    const unsubscribe = subscribeProjectPageProposals(() => { throw new Error('Intake failed') })
    try {
      const view = render(panel(o))
      await deliver(event({ type: 'turn_started', data: {} }))
      await deliver(event({ type: 'tool_call', data: { tool: 'propose_project_page', args_summary: 'text change' } }))
      await expect(deliver(proposalEvent())).resolves.toBeUndefined()
      expect(view.container.querySelector('.converse-step')).toHaveAttribute('data-state', 'done')
    } finally { unsubscribe() }
  })
})
afterEach(() => {
  cleanup()
  for (const controller of owners.splice(0)) controller.destroy()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('chat publication delivery to the catalog owner', () => {
  it('C41-01 a published result notifies the owner once with its identity and digest', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event())
    expect(o.changed).toHaveBeenCalledWith(publication)
    expect(o.claim).toHaveBeenCalledWith(publication)
    cycle(o)
  })

  it('C41-02 the same event delivered twice refreshes once', async () => {
    const o = await owner()
    render(panel(o))
    const env = event()
    await deliver(env); await deliver(env)
    cycle(o)
  })

  it('C41-03 another sequence, turn or digest for the same publication refreshes nothing more', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event())
    await deliver(event({ seq: 2 }))
    await deliver(event({ seq: 3, turn_id: 'other-turn' }))
    await deliver(resultEvent({ catalog_digest: 'b'.repeat(64) }))
    cycle(o)
  })

  it('C41-04 a remount replaying from zero refreshes nothing more', async () => {
    const o = await owner()
    const first = render(panel(o))
    await deliver(event())
    first.unmount()
    render(panel(o))
    expect(openStream.mock.calls.map((call) => call[1])).toEqual([0, 0])
    await deliver(event())
    cycle(o)
  })

  it('C41-05 a StrictMode replay of the subscription refreshes once', async () => {
    const o = await owner()
    render(<React.StrictMode>{panel(o)}</React.StrictMode>)
    expect(subscriptions).toHaveLength(2)
    expect(subscriptions[0].close).toHaveBeenCalledTimes(1)
    await deliver(event(), subscriptions[0])
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event(), subscriptions[1])
    await deliver(event(), subscriptions[1])
    cycle(o)
  })

  it('C41-06 awaiting approval consumes nothing and the later publication refreshes once', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event({ data: { ...event().data, result: { contract: C, change_set_id: I, status: 'awaiting_approval' } } }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each(['denied', 'staging', 'failed'])('C41-07 a %s result consumes nothing and a later success still refreshes', async (status) => {
    const o = await owner()
    render(panel(o))
    await deliver(event({ data: { ...event().data,
      ok: status !== 'failed',
      result: status === 'failed' ? { ...P } : { contract: C, change_set_id: I, status },
    } }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each([
    ['data that is null', (env) => { env.data = null }],
    ['data that is an array', (env) => { env.data = [] }],
    ['an absent result', (env) => { delete env.data.result }],
    ['a null result', (env) => { env.data.result = null }],
    ['an array result', (env) => { env.data.result = [] }],
    ['another contract', (env) => { env.data.result.contract = 'other.v1' }],
    ['another tool', (env) => { env.data.tool = 'other_tool' }],
    ['another event type', (env) => { env.type = 'tool_call' }],
    ['ok as the string true', (env) => { env.data.ok = 'true' }],
    ['an absent change set id', (env) => { delete env.data.result.change_set_id }],
    ['a null change set id', (env) => { env.data.result.change_set_id = null }],
    ['a numeric change set id', (env) => { env.data.result.change_set_id = 7 }],
    ['an empty change set id', (env) => { env.data.result.change_set_id = '' }],
    ['a padded change set id', (env) => { env.data.result.change_set_id = ` ${I} ` }],
    ['a 129 character change set id', (env) => { env.data.result.change_set_id = 'a'.repeat(129) }],
    ['an absent catalog digest', (env) => { delete env.data.result.catalog_digest }],
    ['a null catalog digest', (env) => { env.data.result.catalog_digest = null }],
    ['a numeric catalog digest', (env) => { env.data.result.catalog_digest = 7 }],
    ['an empty catalog digest', (env) => { env.data.result.catalog_digest = '' }],
    ['a padded catalog digest', (env) => { env.data.result.catalog_digest = ` ${H} ` }],
    ['a 129 character catalog digest', (env) => { env.data.result.catalog_digest = 'a'.repeat(129) }],
  ])('C41-08 %s is rejected and consumes nothing', async (_label, corrupt) => {
    const o = await owner()
    render(panel(o))
    const env = event()
    corrupt(env)
    // Even malformed data carries persuasive prose; only the structured result counts.
    if (env.data === null) env.summary = 'The tool was published'
    else env.data.summary = 'The tool was published'
    await deliver(env)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it('C41-08 identifiers of length 1 and 128 are accepted unchanged', async () => {
    const o = await owner()
    render(panel(o))
    for (const length of [1, 128]) {
      const id = 'Z'.repeat(length), digest = 'Q'.repeat(length)
      await deliver(resultEvent({ change_set_id: id, catalog_digest: digest }))
      expect(o.changed).toHaveBeenLastCalledWith({ sessionId: 'session-1', changeSetId: id, catalogDigest: digest })
    }
    cycle(o, 2)
  })

  it('C41-08 a missing callback or claim consumes nothing', async () => {
    const o = await owner()
    const view = render(panel(o, { onCatalogChanged: null }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    view.rerender(panel(o, { consumeCatalogPublication: null }))
    await deliver(event())
    cycle(o, 0)
    view.unmount()
    render(panel(o))
    await deliver(event())
    cycle(o)
  })

  it('C41-09 two change sets refresh twice and the same id in another session is its own publication', async () => {
    const o = await owner()
    const view = render(panel(o))
    await deliver(event())
    await deliver(resultEvent({ change_set_id: 'second-change' }))
    cycle(o, 2)
    view.rerender(panel(o, { sessionId: 'session-2' }))
    await deliver(event({ session_id: 'session-2' }))
    cycle(o, 3)
  })

  it('C41-10 a replaced callback is the one that runs and the stream is not reopened', async () => {
    const o = await owner()
    const view = render(panel(o))
    await deliver(event())
    const next = vi.fn(o.controller.actions.refreshCatalog)
    const nextClaim = vi.fn(o.controller.consumeCatalogPublication)
    view.rerender(panel(o, { onCatalogChanged: next, consumeCatalogPublication: nextClaim }))
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
    await deliver(resultEvent({ change_set_id: 'new-change' }))
    expect(o.changed).toHaveBeenCalledTimes(1)
    expect(o.claim).toHaveBeenCalledTimes(1)
    expect(next).toHaveBeenCalledTimes(1)
    expect(nextClaim).toHaveBeenCalledTimes(1)
    expect(o.services.getTools).toHaveBeenCalledTimes(2)
    expect(o.services.getCapabilities).toHaveBeenCalledTimes(2)
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
  })

  it("C41-11 an old session's handler claims nothing after a switch or cleanup", async () => {
    const o = await owner()
    const view = render(panel(o))
    const old = subscriptions[0]
    view.rerender(panel(o, { sessionId: 'session-2' }))
    expect(old.close).toHaveBeenCalledTimes(1)
    await deliver(event(), old)
    expect(o.claim).not.toHaveBeenCalled()
    const current = subscriptions[1]
    view.unmount()
    await deliver(event(), old)
    await deliver(event({ session_id: 'session-2' }), current)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
  })

  it('C41-11 an envelope with a missing or different session is rejected', async () => {
    const o = await owner()
    render(panel(o))
    const missing = event()
    delete missing.session_id
    await deliver(missing)
    await deliver(event({ session_id: '' }))
    await deliver(event({ session_id: 'session-other' }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each(['the flat list', 'the grouped catalog', 'both lists'])('C41-12 %s failing keeps the publication consumed and the retry action recovers', async (label) => {
    const o = await owner()
    render(panel(o))
    const flatFails = label !== 'the grouped catalog', groupedFails = label !== 'the flat list'
    if (flatFails) o.services.getTools.mockRejectedValueOnce(new Error('flat failed'))
    if (groupedFails) o.services.getCapabilities.mockRejectedValueOnce(new Error('grouped failed'))
    await deliver(event())
    cycle(o)
    expect(o.controller.getState().tools).toEqual(oldTools)
    expect(o.controller.getState().toolsError).toBe(flatFails ? 'flat failed' : null)
    expect(o.controller.getState().catalog).toEqual(groupedFails ? { families: [], source: null } : oldCatalog)
    expect(o.controller.getState().catalogError).toBe(groupedFails ? 'grouped failed' : null)
    await deliver(event())
    cycle(o)
    if (flatFails) await o.controller.actions.retryTools()
    if (groupedFails) await o.controller.actions.loadCatalog()
    expect(o.controller.getState().toolsError).toBeNull()
    expect(o.controller.getState().catalogError).toBeNull()
    expect(o.controller.getState().tools).toEqual(oldTools)
    expect(o.controller.getState().catalog).toEqual(oldCatalog)
    expect(o.services.getTools).toHaveBeenCalledTimes(flatFails ? 2 : 1)
    expect(o.services.getCapabilities).toHaveBeenCalledTimes(groupedFails ? 2 : 1)
    expect(o.changed).toHaveBeenCalledTimes(1)
  })

  it("C41-14 the refreshed catalog shows the server's new tool and never the chat's", async () => {
    const o = await owner()
    render(panel(o))
    const serverTool = { name: 'new-server-tool' }
    const chatTool = { name: 'chat-invention' }
    const serverCatalog = { families: [{ family_id: 'custom', capabilities: [serverTool] }], source: 'server' }
    o.services.getTools.mockResolvedValueOnce([serverTool])
    o.services.getCapabilities.mockResolvedValueOnce(serverCatalog)
    await deliver(event({ data: { ...event().data, invented_tool: chatTool } }))
    cycle(o)
    expect(o.controller.getState().tools).toEqual([serverTool])
    expect(o.controller.getState().catalog.families).toEqual(serverCatalog.families)
    expect(o.controller.getState().tools).not.toContainEqual(chatTool)
    expect(o.controller.getState().catalog.families.flatMap((family) => family.capabilities)).not.toContainEqual(chatTool)
  })

  it.each([
    ['the number 1', 1],
    ['the string true', 'true'],
    ['an object', {}],
  ])('C41-16 a claim returning %s notifies nothing', async (_label, value) => {
    const o = await owner()
    let claims = 0
    render(panel(o, { consumeCatalogPublication: () => { claims++; return value } }))
    await deliver(event())
    expect(claims).toBe(1)
    cycle(o, 0)
  })

  it.each(['a claim', 'a callback'])('C41-16 %s that throws does not interrupt the transcript', async (label) => {
    const o = await owner()
    let claims = 0, callbacks = 0
    const claim = label === 'a claim' ? () => {
      if (++claims === 1) throw new Error('claim failed')
      return true
    } : o.controller.consumeCatalogPublication
    const changed = (value) => {
      callbacks++
      if (label === 'a callback' && callbacks === 1) throw new Error('callback failed')
      return o.changed(value)
    }
    const view = render(panel(o, { consumeCatalogPublication: claim, onCatalogChanged: changed }))
    await deliver(event({ type: 'turn_started', data: {} }))
    await deliver(event({ type: 'tool_call', data: { tool: 'request_publication', args_summary: 'first change' } }))
    await expect(deliver(event())).resolves.toBeUndefined()
    const row = view.container.querySelector('.converse-step')
    expect(row).toHaveAttribute('data-state', 'done')
    expect(row.querySelector('.converse-step-state')).toHaveTextContent('Done')
    expect(row).toHaveTextContent('The tool was published')
    const before = callbacks
    await deliver(resultEvent({ change_set_id: 'second-change-after-throw' }))
    expect(callbacks).toBe(before + 1)
    expect(o.changed).toHaveBeenLastCalledWith({ ...publication, changeSetId: 'second-change-after-throw' })
  })

  it('C41-11 an event delivered by close during unmount claims nothing', async () => {
    const o = await owner()
    openStream.mockImplementation((_session, _seq, handlers) => {
      const close = () => handlers.onEvent(event())
      subscriptions.push({ handlers, close })
      return { close }
    })
    const view = render(panel(o))
    view.unmount()
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
  })

  it('C41-11 an envelope whose session only coerces to the opened session is rejected', async () => {
    const o = await owner()
    render(panel(o, { sessionId: '42' }))
    await deliver(event({ session_id: 42 }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event({ session_id: '42' }))
    expect(o.changed).toHaveBeenCalledWith({ ...publication, sessionId: '42' })
    cycle(o)
  })

  it.each(['awaiting_approval', 'denied', 'staging'])('C41-07 a %s result that carries a change set and a digest consumes nothing', async (status) => {
    const o = await owner()
    render(panel(o))
    await deliver(resultEvent({ status }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each([
    ['data that is an array carrying valid fields', (env) => { env.data = Object.assign([], env.data) }],
    ['a result that is an array carrying valid fields', (env) => { env.data.result = Object.assign([], env.data.result) }],
    ['a change set id padded only at the start', (env) => { env.data.result.change_set_id = ` ${I}` }],
    ['a change set id padded only at the end', (env) => { env.data.result.change_set_id = `${I} ` }],
    ['a catalog digest padded only at the start', (env) => { env.data.result.catalog_digest = ` ${H}` }],
    ['a catalog digest padded only at the end', (env) => { env.data.result.catalog_digest = `${H} ` }],
  ])('C41-08 %s consumes nothing', async (_label, corrupt) => {
    const o = await owner()
    render(panel(o))
    const env = event()
    corrupt(env)
    await deliver(env)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it('C41-08 a 1 character change set id with a 128 character digest is accepted unchanged', async () => {
    const o = await owner()
    render(panel(o))
    const changeSetId = 'Z', catalogDigest = 'Q'.repeat(128)
    await deliver(resultEvent({ change_set_id: changeSetId, catalog_digest: catalogDigest }))
    expect(o.changed).toHaveBeenCalledWith({ sessionId: 'session-1', changeSetId, catalogDigest })
    cycle(o)
  })

  it.each(['a callback', 'a claim'])('C41-08 %s that is truthy but not a function consumes nothing', async (label) => {
    const o = await owner()
    const view = render(panel(o, label === 'a callback' ? { onCatalogChanged: {} } : { consumeCatalogPublication: {} }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    view.rerender(panel(o))
    await deliver(event())
    expect(o.claim).toHaveBeenCalledTimes(1)
    cycle(o)
  })

  it('C41-10 removing the callback stops notifications and the stream is not reopened', async () => {
    const o = await owner()
    const view = render(panel(o))
    view.rerender(panel(o, { onCatalogChanged: null }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
  })
})
