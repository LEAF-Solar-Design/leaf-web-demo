import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { WorkflowScheduleDraftBrowser } from './WorkflowScheduleDraftBrowser.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'schedule-draft-orders', version: 2, digest: 'a'.repeat(64) }
const other = { ...reference, id: 'schedule-draft-other', digest: 'c'.repeat(64) }
const draft = { schema_version: 1, id: reference.id, version: reference.version, title: '<Saved orders>',
  workflow_ref: { id: 'orders', version: 3, digest: 'b'.repeat(64) }, state: 'draft',
  schedule: { kind: 'daily', timezone: 'US/Eastern', hour: 9, minute: 5, gap_policy: 'next_valid_minute', fold_policy: 'first_occurrence' } }
const clone = value => JSON.parse(JSON.stringify(value))
const click = name => fireEvent.click(screen.getByRole('button', { name }))
const select = (ref = reference) => click(`Select draft ${ref.id}, version ${ref.version}`)
function setup() {
  const parent = new AbortController()
  const client = { listWorkflowScheduleDrafts: vi.fn(async () => ({ references: [clone(reference), clone(other)], nextCursor: 'private-cursor' })),
    getWorkflowScheduleDraft: vi.fn(async () => clone(draft)) }
  const props = { projectId, client, signal: parent.signal, contextKey: 'private-context', guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, client, parent }
}
async function load() { click('Load drafts'); await screen.findByRole('button', { name: `Select draft ${reference.id}, version ${reference.version}` }) }
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('does not assert that a draft is selected on mount or after listing', async () => {
  const { props } = setup()
  render(<WorkflowScheduleDraftBrowser {...props} />)
  expect(screen.queryByText('This is a saved draft. Nothing is scheduled or authorized to run.')).toBeNull()
  await load()
  expect(screen.queryByText('This is a saved draft. Nothing is scheduled or authorized to run.')).toBeNull()
})

it('removes the selected-draft assertion while reading another draft', async () => {
  const { props, client } = setup()
  render(<WorkflowScheduleDraftBrowser {...props} />)
  await load(); select()
  await screen.findByRole('heading', { name: draft.title })
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise(() => {}))
  select(other)
  expect(screen.queryByText('This is a saved draft. Nothing is scheduled or authorized to run.')).toBeNull()
})

it('is inert on mount, time and callback-only rerenders; guards explicit list and exact detail reads', async () => {
  const { props, client } = setup()
  const { rerender } = render(<WorkflowScheduleDraftBrowser {...props} mode="dark" />)
  rerender(<WorkflowScheduleDraftBrowser {...props} guard={signal => props.guard(signal)} onForbidden={() => props.onForbidden()} mode="dark" />)
  expect(client.listWorkflowScheduleDrafts).not.toHaveBeenCalled()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
  await load()
  expect(client.listWorkflowScheduleDrafts).toHaveBeenCalledOnce()
  expect(props.guard).toHaveBeenCalledTimes(2)
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  select()
  const heading = await screen.findByRole('heading', { name: draft.title })
  expect(heading).toBeInTheDocument()
  expect(props.guard).toHaveBeenCalledTimes(4)
  expect(client.getWorkflowScheduleDraft.mock.calls[0]).toEqual([projectId, reference, { signal: expect.any(AbortSignal) }])
  expect(screen.getByText('09:05')).toBeInTheDocument()
  for (const text of ['US/Eastern', 'next_valid_minute', 'first_occurrence', 'orders', 'Daily',
    'This is a saved draft. Nothing is scheduled or authorized to run.']) expect(screen.getByText(text)).toBeInTheDocument()
  expect(document.activeElement).toBe(heading.closest('[role="status"]'))
  expect(screen.queryByRole('textbox')).toBeNull()
  for (const secret of [reference.digest, draft.workflow_ref.digest, 'private-context', 'private-cursor']) expect(document.body.textContent).not.toContain(secret)
})

it('deduplicates pending selection, supersedes a different selection and ignores late results', async () => {
  const { props, client } = setup()
  let finish
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    .mockResolvedValueOnce({ ...clone(draft), id: other.id, title: 'Newer draft' })
  render(<WorkflowScheduleDraftBrowser {...props} />); await load(); select(); select()
  await waitFor(() => expect(client.getWorkflowScheduleDraft).toHaveBeenCalledOnce())
  expect(screen.getByText('Loading saved draft.')).toBeInTheDocument()
  select(other)
  await screen.findByText('Newer draft')
  expect(client.getWorkflowScheduleDraft.mock.calls[0][2].signal.aborted).toBe(true)
  await act(async () => finish(clone(draft)))
  expect(screen.queryByText(draft.title)).toBeNull()
  expect(client.getWorkflowScheduleDraft).toHaveBeenCalledTimes(2)
})

it('cancels with exact explicit retry even if a client mutates its request copy', async () => {
  const { props, client } = setup()
  let finish
  client.getWorkflowScheduleDraft.mockImplementationOnce((_, ref) => {
    ref.id = 'mutated'; ref.digest = 'd'.repeat(64)
    return new Promise(resolve => { finish = resolve })
  })
  render(<WorkflowScheduleDraftBrowser {...props} />); await load(); select()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  click('Cancel draft read')
  expect(screen.getByText('Draft read cancelled.')).toBeInTheDocument()
  click('Retry draft read')
  await screen.findByText(draft.title)
  expect(client.getWorkflowScheduleDraft.mock.calls[1][1]).toEqual(reference)
  await act(async () => finish({ ...draft, title: 'Late result' }))
  expect(screen.queryByText('Late result')).toBeNull()
})

it.each(['preflight', 'get', 'acceptance'])('times out an abort-ignoring %s and retries with fresh authority', async (phase) => {
  vi.useFakeTimers()
  const { props, client } = setup()
  render(<WorkflowScheduleDraftBrowser {...props} />)
  await act(async () => click('Load drafts'))
  let rejectExpired
  const stalled = () => new Promise((_, reject) => { rejectExpired = reject })
  if (phase === 'preflight') props.guard.mockImplementationOnce(stalled)
  else if (phase === 'acceptance') props.guard.mockResolvedValueOnce().mockImplementationOnce(stalled)
  else client.getWorkflowScheduleDraft.mockImplementationOnce(stalled)
  await act(async () => select())
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  expect(screen.getByText('The saved draft read timed out. Retry the read.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Cancel draft read' })).toBeNull()
  await act(async () => click('Retry draft read'))
  expect(screen.getByText(draft.title)).toBeInTheDocument()
  await act(async () => rejectExpired({ status: 403, message: 'private-error' }))
  expect(props.onForbidden).not.toHaveBeenCalled()
  expect(screen.getByText(draft.title)).toBeInTheDocument()
  expect(vi.getTimerCount()).toBe(0)
})

it.each([409, 503])('uses fixed read failure for %s and retains the selected reference for retry', async status => {
  const { props, client } = setup()
  client.getWorkflowScheduleDraft.mockRejectedValueOnce({ status, message: 'private-error' })
  render(<WorkflowScheduleDraftBrowser {...props} />); await load(); select()
  await screen.findByText('The saved draft could not be read. Retry the read.')
  expect(document.body.textContent).not.toContain('private-error')
  click('Retry draft read'); await screen.findByText(draft.title)
  expect(client.getWorkflowScheduleDraft.mock.calls.map(([, ref]) => ref)).toEqual([reference, reference])
})

it.each([{ id: 'wrong' }, { version: 1 }, { state: 'active' }, { extra: true },
  { schedule: { ...draft.schedule, hour: 25 } }])('rejects unbound or noncanonical detail %j', async patch => {
  const { props, client } = setup()
  client.getWorkflowScheduleDraft.mockResolvedValueOnce({ ...clone(draft), ...patch })
  render(<WorkflowScheduleDraftBrowser {...props} />); await load(); select()
  await screen.findByText('The saved draft could not be read. Retry the read.')
  expect(screen.queryByText(draft.title)).toBeNull()
})

it.each(['list', 'detail', 'guard'])('clears private state and calls the current forbidden callback on %s authorization loss', async phase => {
  for (const status of [401, 403]) {
    const { props, client } = setup()
    if (phase === 'list') client.listWorkflowScheduleDrafts.mockRejectedValueOnce({ status })
    if (phase === 'guard') props.guard.mockRejectedValueOnce({ status })
    if (phase === 'detail') client.getWorkflowScheduleDraft.mockRejectedValueOnce({ status })
    render(<WorkflowScheduleDraftBrowser {...props} />)
    if (phase === 'detail') { await load(); select() } else click('Load drafts')
    await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(draft.title)).toBeNull()
    cleanup()
  }
})

it.each(['contextKey', 'projectId', 'client', 'signal', 'availability', 'parent', 'unmount'])('discards pending detail on %s loss', async kind => {
  const { props, client, parent } = setup()
  let finish
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender, unmount } = render(<WorkflowScheduleDraftBrowser {...props} />)
  await load(); select(); await waitFor(() => expect(finish).toBeTypeOf('function'))
  if (kind === 'parent') act(() => parent.abort())
  else if (kind === 'unmount') unmount()
  else rerender(<WorkflowScheduleDraftBrowser {...props} {...({
    contextKey: { contextKey: 'new-context' }, projectId: { projectId: '22222222-2222-4222-8222-222222222222' },
    client: { client: { ...client } }, signal: { signal: new AbortController().signal }, availability: { availability: 'unavailable' },
  })[kind]} />)
  expect(client.getWorkflowScheduleDraft.mock.calls[0][2].signal.aborted).toBe(true)
  await act(async () => finish(clone(draft)))
  expect(screen.queryByText(draft.title)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry draft read' })).toBeNull()
})

it('clears pending detail when paging and lets the picker alone own the list deadline', async () => {
  vi.useFakeTimers()
  const { props, client } = setup()
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise(() => {}))
  const { unmount } = render(<WorkflowScheduleDraftBrowser {...props} />)
  await act(async () => click('Load drafts'))
  await act(async () => select())
  // Drain jsdom's zero-delay focus events without advancing the read deadline.
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(vi.getTimerCount()).toBe(1)
  client.listWorkflowScheduleDrafts.mockImplementationOnce(() => new Promise(() => {}))
  await act(async () => click('Next'))
  expect(client.getWorkflowScheduleDraft.mock.calls[0][2].signal.aborted).toBe(true)
  expect(screen.queryByText('Loading saved draft.')).toBeNull()
  expect(client.listWorkflowScheduleDrafts.mock.calls[1][1]).toMatchObject({ limit: 25, after: 'private-cursor' })
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(vi.getTimerCount()).toBe(1)
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})

it.each(['context', 'parent', 'unmount'])('discards late list pages after %s loss', async kind => {
  const { props, client, parent } = setup()
  let finish
  client.listWorkflowScheduleDrafts.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender, unmount } = render(<WorkflowScheduleDraftBrowser {...props} />)
  click('Load drafts')
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const listSignal = client.listWorkflowScheduleDrafts.mock.calls[0][1].signal
  if (kind === 'context') rerender(<WorkflowScheduleDraftBrowser {...props} contextKey="new-context" />)
  else if (kind === 'parent') act(() => parent.abort())
  else unmount()
  expect(listSignal.aborted).toBe(true)
  await act(async () => finish({ references: [reference], nextCursor: null }))
  expect(screen.queryByRole('button', { name: `Select draft ${reference.id}, version ${reference.version}` })).toBeNull()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
})

it('rejects an injected malformed page without partial references and revalidates explicit retry', async () => {
  const { props, client } = setup()
  client.listWorkflowScheduleDrafts.mockResolvedValueOnce({ references: [reference, { ...other, digest: 'bad' }], nextCursor: null })
  render(<WorkflowScheduleDraftBrowser {...props} />)
  click('Load drafts')
  await screen.findByRole('button', { name: 'Retry' })
  expect(screen.queryByRole('button', { name: `Select draft ${reference.id}, version ${reference.version}` })).toBeNull()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  const before = props.guard.mock.calls.length
  click('Retry')
  await screen.findByRole('button', { name: `Select draft ${reference.id}, version ${reference.version}` })
  expect(props.guard).toHaveBeenCalledTimes(before + 2)
})

it('makes no mount or elapsed-time requests and cleans all timers on unmount', async () => {
  vi.useFakeTimers()
  const { props, client } = setup()
  const { unmount } = render(<WorkflowScheduleDraftBrowser {...props} />)
  await act(async () => vi.advanceTimersByTimeAsync(90000))
  expect(props.guard).not.toHaveBeenCalled()
  expect(client.listWorkflowScheduleDrafts).not.toHaveBeenCalled()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  await act(async () => click('Load drafts'))
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise(() => {}))
  await act(async () => select())
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(vi.getTimerCount()).toBe(0)
})
