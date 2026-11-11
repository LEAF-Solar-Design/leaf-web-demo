import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReferenceMaterialIntake from './ReferenceMaterialIntake.jsx'
import { listProjectReferences, readProjectReferenceOriginal, registerProjectArloInput, uploadProjectReference } from '../projects/api.js'

const authority = vi.hoisted(() => ({ scope: 'account-a', listeners: new Set(), logouts: new Set() }))
vi.mock('../auth.js', () => ({ subscribeBeforeLogout: fn => { authority.logouts.add(fn); return () => authority.logouts.delete(fn) } }))
vi.mock('../auth/activeScope.js', () => ({
  readActiveScope: () => authority.scope,
  subscribeActiveScope: fn => { authority.listeners.add(fn); return () => authority.listeners.delete(fn) },
}))
vi.mock('../projects/api.js', () => ({
  listProjectReferences: vi.fn(), readProjectReferenceOriginal: vi.fn(),
  registerProjectArloInput: vi.fn(), uploadProjectReference: vi.fn(),
}))

const project = '11111111-2222-4333-8444-555555555555'
const otherProject = '22222222-3333-4444-8555-666666666666'
const cursor = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff'
const orgId = '99999999-8888-4777-8666-555555555555'
const row = (name, id = name) => ({
  material_id: id, name, byte_size: 14, media_type: 'text/plain',
  sha256: 'a'.repeat(64), created_at: '2026-10-01T12:00:00Z',
})
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const choose = (...files) => fireEvent.change(screen.getByLabelText('Original reference files'), { target: { files } })
const original = name => new File(['original bytes'], name, { type: 'text/plain' })
const ready = () => waitFor(() => expect(screen.getByRole('button', { name: 'Add reference files' }).disabled).toBe(false))
const saved = () => within(screen.getByRole('list', { name: 'Saved reference files' }))
const changeScope = scope => act(() => { authority.scope = scope; for (const listener of authority.listeners) listener() })
const logout = () => act(() => { for (const clear of authority.logouts) clear() })
const arloMaterial = { ...row('feeder.json', 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'), media_type: 'application/json' }
const arloInput = {
  organization_id: orgId, project_id: project, material_id: arloMaterial.material_id,
  original_sha256: arloMaterial.sha256, input_version_id: '44444444-5555-4666-8777-888888888888',
  request: { contract: 'arlo_design_request_v1', seed: 17 },
}
const runArlo = () => fireEvent.click(screen.getByRole('button', { name: 'Run ARLO with feeder.json' }))

beforeEach(() => {
  vi.resetAllMocks()
  authority.scope = 'account-a'
  authority.listeners.clear()
  authority.logouts.clear()
  listProjectReferences.mockResolvedValue({ materials: [] })
  readProjectReferenceOriginal.mockResolvedValue(new Blob(['original bytes'], { type: 'text/plain' }))
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:reference-original')
    static revokeObjectURL = vi.fn()
  })
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('shared project original reference intake', () => {
  it('runs only after an explicit click and reuses the retry key for the registered source', async () => {
    const onRunArlo = vi.fn().mockRejectedValueOnce(new Error('Could not reach ARLO. Try again.'))
      .mockResolvedValueOnce({ ok: true })
    registerProjectArloInput.mockResolvedValue(arloInput)
    uploadProjectReference.mockResolvedValue({ material: arloMaterial })
    render(<ReferenceMaterialIntake projectId={project} orgId={orgId} onRunArlo={onRunArlo} />)
    await ready()
    choose(new File(['{}'], arloMaterial.name, { type: 'application/json' }))
    await screen.findByRole('button', { name: 'Run ARLO with feeder.json' })
    expect(registerProjectArloInput).not.toHaveBeenCalled()
    expect(onRunArlo).not.toHaveBeenCalled()

    runArlo()
    expect((await screen.findByRole('alert')).textContent).toContain('Could not reach ARLO')
    runArlo()
    await screen.findByText('ARLO result saved to this project.')
    expect(registerProjectArloInput).toHaveBeenCalledTimes(2)
    expect(registerProjectArloInput).toHaveBeenCalledWith(project, arloMaterial.material_id,
      expect.objectContaining({ orgId, expectedScope: 'account-a', key: `arlo-reference-${arloMaterial.material_id}` }))
    expect(onRunArlo).toHaveBeenCalledTimes(2)
    expect(onRunArlo).toHaveBeenCalledWith(arloInput,
      expect.objectContaining({ scope: 'account-a', idempotencyKey: expect.any(String) }))
    expect(onRunArlo.mock.calls[0][1].idempotencyKey).toBe(onRunArlo.mock.calls[1][1].idempotencyKey)
  })

  it('refuses a registration for a different material', async () => {
    const onRunArlo = vi.fn()
    listProjectReferences.mockResolvedValue({ materials: [arloMaterial] })
    registerProjectArloInput.mockResolvedValue({ ...arloInput, material_id: otherProject })
    render(<ReferenceMaterialIntake projectId={project} orgId={orgId} onRunArlo={onRunArlo} />)
    await ready()
    runArlo()
    expect((await screen.findByRole('alert')).textContent).toContain('did not match')
    expect(onRunArlo).not.toHaveBeenCalled()
  })

  it('drops a late registration after the active account changes', async () => {
    const pending = deferred()
    const onRunArlo = vi.fn()
    listProjectReferences.mockResolvedValueOnce({ materials: [arloMaterial] })
    registerProjectArloInput.mockReturnValue(pending.promise)
    render(<ReferenceMaterialIntake projectId={project} orgId={orgId} onRunArlo={onRunArlo} />)
    await ready()
    runArlo()
    const options = registerProjectArloInput.mock.calls[0][2]
    changeScope('account-b')
    await act(async () => pending.resolve(arloInput))
    expect(options.signal.aborted).toBe(true)
    expect(onRunArlo).not.toHaveBeenCalled()
    expect(screen.queryByText('Preparing ARLO input…')).toBeNull()
  })

  it('offers complete gzip JSONL originals and connects picker to upload, saved list and download', async () => {
    const { gzipSync } = await import('node:zlib')
    const compressed = gzipSync(Array.from({ length: 6001 }, (_, row) => JSON.stringify({ row })).join('\n'))
    const corpus = new File([compressed], 'BTC.jsonl.gz', { type: 'application/gzip' })
    const material = { ...row(corpus.name, 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
      media_type: 'application/gzip', byte_size: corpus.size }
    uploadProjectReference.mockResolvedValue({ material })
    readProjectReferenceOriginal.mockResolvedValue(new Blob([compressed], { type: 'application/gzip' }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    expect(screen.getByLabelText('Original reference files').accept).toContain('.jsonl.gz')
    choose(corpus)
    await waitFor(() => expect(saved().getByText(corpus.name)).toBeTruthy())
    expect(uploadProjectReference.mock.calls[0][1]).toBe(corpus)
    expect(saved().getByText('Uploaded · Preview unavailable · Model not consumed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: `Download original ${corpus.name}` }))
    await waitFor(() => expect(click).toHaveBeenCalledOnce())
    expect(readProjectReferenceOriginal).toHaveBeenCalledWith(project, material.material_id,
      expect.objectContaining({ expectedScope: 'account-a' }))
    click.mockRestore()
  })

  it('loads saved originals on initial open and reopens the same project from the server', async () => {
    listProjectReferences.mockResolvedValue({ materials: [row('shared.txt')] })
    const view = render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    expect(saved().getByText('shared.txt')).toBeTruthy()
    expect(saved().getByText('Uploaded · Preview unavailable · Model not consumed')).toBeTruthy()
    expect(screen.getByText(/Uploading does not start an author turn or build/)).toBeTruthy()
    expect(readProjectReferenceOriginal).not.toHaveBeenCalled()
    expect(uploadProjectReference).not.toHaveBeenCalled()
    const firstSignal = listProjectReferences.mock.calls[0][1].signal
    view.unmount()
    expect(firstSignal.aborted).toBe(true)
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    expect(saved().getByText('shared.txt')).toBeTruthy()
    expect(listProjectReferences).toHaveBeenCalledTimes(2)
    for (const [id, options] of listProjectReferences.mock.calls) {
      expect(id).toBe(project)
      expect(options).toMatchObject({ expectedScope: 'account-a', cursor: null })
      expect(options.signal.aborted).toBe(options.signal === firstSignal)
    }
  })

  it('keeps a list error visible and reloads on request', async () => {
    listProjectReferences.mockRejectedValueOnce(new Error('You do not have permission to do that in this project.'))
      .mockResolvedValueOnce({ materials: [row('shared.txt')] })
    render(<ReferenceMaterialIntake projectId={project} />)
    expect((await screen.findByRole('alert')).textContent).toContain('You do not have permission')
    fireEvent.click(screen.getByRole('button', { name: 'Reload reference files' }))
    await ready()
    expect(saved().getByText('shared.txt')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('uploads sequentially, blocks duplicate submissions and retries only failed originals', async () => {
    const pending = deferred()
    const first = original('first.txt'), second = original('second.txt')
    uploadProjectReference.mockReturnValueOnce(pending.promise)
      .mockRejectedValueOnce(new Error('The reference could not be saved. Try again.'))
      .mockResolvedValueOnce({ material: row('second.txt') })
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    choose(first, second)
    expect(uploadProjectReference).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Add reference files' }).disabled).toBe(true)
    expect(screen.getByLabelText('Original reference files').disabled).toBe(true)
    choose(original('duplicate.txt'))
    expect(uploadProjectReference).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve({ material: row('first.txt') }))
    const retry = await screen.findByRole('button', { name: 'Retry failed reference files' })
    expect(within(screen.getByRole('list', { name: 'Reference upload progress' })).getByText('second.txt: The reference could not be saved. Try again.')).toBeTruthy()
    expect(saved().getAllByRole('listitem')).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toContain('1 uploaded; 1 need a retry.')
    fireEvent.click(retry)
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('2 uploaded; 0 need a retry.'))
    expect(uploadProjectReference.mock.calls.map(call => call[1])).toEqual([first, second, second])
    expect(uploadProjectReference.mock.calls[0][1]).toBe(first)
    expect(uploadProjectReference.mock.calls[2][1]).toBe(second)
    for (const [id, , options] of uploadProjectReference.mock.calls) {
      expect(id).toBe(project)
      expect(options.expectedScope).toBe('account-a')
      expect(options.signal.aborted).toBe(false)
    }
    expect(saved().getAllByRole('listitem')).toHaveLength(2)
    expect(saved().getAllByText(/Model not consumed/)).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Retry failed reference files' })).toBeNull()
  })

  it('loads another page while preserving and deduplicating prior rows', async () => {
    const pending = deferred()
    listProjectReferences.mockResolvedValueOnce({ materials: [row('first.txt')], next_cursor: cursor })
      .mockReturnValueOnce(pending.promise)
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Load more reference files' }))
    expect(saved().getByText('first.txt')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Load more reference files' }).disabled).toBe(true)
    expect(listProjectReferences.mock.calls[1]).toEqual([project, expect.objectContaining({ cursor, expectedScope: 'account-a' })])
    await act(async () => pending.resolve({ materials: [row('first.txt'), row('second.txt')] }))
    expect(saved().getByText('first.txt')).toBeTruthy()
    expect(saved().getByText('second.txt')).toBeTruthy()
    expect(saved().getAllByRole('listitem')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Load more reference files' })).toBeNull()
  })

  it.each(['project', 'scope'])('discards late list responses after a %s switch', async transition => {
    const pending = deferred()
    listProjectReferences.mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ materials: [row('current.txt')] })
    const view = render(<ReferenceMaterialIntake projectId={project} />)
    const oldSignal = listProjectReferences.mock.calls[0][1].signal
    if (transition === 'project') view.rerender(<ReferenceMaterialIntake projectId={otherProject} />)
    else changeScope('account-b')
    await ready()
    expect(oldSignal.aborted).toBe(true)
    await act(async () => pending.resolve({ materials: [row('private.txt')], next_cursor: cursor }))
    expect(saved().getByText('current.txt')).toBeTruthy()
    expect(screen.queryByText('private.txt')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Load more reference files' })).toBeNull()
    expect(listProjectReferences.mock.lastCall).toEqual([
      transition === 'project' ? otherProject : project,
      expect.objectContaining({ expectedScope: transition === 'scope' ? 'account-b' : 'account-a' }),
    ])
  })

  it.each(['project', 'scope'])('clears saved rows and queued files and ignores late uploads after a %s switch', async transition => {
    const pending = deferred()
    listProjectReferences.mockResolvedValueOnce({ materials: [row('old-private.txt')] })
    uploadProjectReference.mockReturnValueOnce(pending.promise)
    const view = render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    choose(original('private.txt'), original('never-upload.txt'))
    const signal = uploadProjectReference.mock.calls[0][2].signal
    if (transition === 'project') view.rerender(<ReferenceMaterialIntake projectId={otherProject} />)
    else changeScope('account-b')
    await ready()
    expect(signal.aborted).toBe(true)
    expect(screen.queryByText('old-private.txt')).toBeNull()
    expect(screen.queryByRole('list', { name: 'Reference upload progress' })).toBeNull()
    await act(async () => pending.resolve({ material: row('private.txt') }))
    expect(screen.queryByText('private.txt')).toBeNull()
    expect(uploadProjectReference).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe('0 reference files in this project.')
  })

  it('clears private state and aborts uploads at before-logout even before the scope changes', async () => {
    const pending = deferred()
    listProjectReferences.mockResolvedValueOnce({ materials: [row('saved-private.txt')] })
    uploadProjectReference.mockReturnValueOnce(pending.promise)
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    choose(original('private.txt'), original('never-upload.txt'))
    const signal = uploadProjectReference.mock.calls[0][2].signal
    logout()
    expect(authority.scope).toBe('account-a')
    expect(signal.aborted).toBe(true)
    expect(screen.queryByRole('list')).toBeNull()
    await act(async () => pending.resolve({ material: row('private.txt') }))
    expect(screen.queryByText('private.txt')).toBeNull()
    choose(original('after-logout.txt'))
    expect(uploadProjectReference).toHaveBeenCalledTimes(1)
    changeScope(null)
    expect(screen.getByText('Sign in and open a project to add original reference files.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Add reference files' })).toBeNull()
  })

  it('aborts a pending initial list at logout and ignores its late response', async () => {
    const pending = deferred()
    listProjectReferences.mockReturnValueOnce(pending.promise)
    render(<ReferenceMaterialIntake projectId={project} />)
    const signal = listProjectReferences.mock.calls[0][1].signal
    logout()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve({ materials: [row('private.txt')] }))
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.getByRole('status').textContent).toBe('0 reference files in this project.')
  })

  it('aborts on unmount, unsubscribes and leaves a reopened session free of late upload results', async () => {
    const pending = deferred()
    uploadProjectReference.mockReturnValueOnce(pending.promise)
    const view = render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    expect(authority.listeners.size).toBe(1)
    expect(authority.logouts.size).toBe(1)
    choose(original('private.txt'), original('never-upload.txt'))
    const signal = uploadProjectReference.mock.calls[0][2].signal
    view.unmount()
    expect(signal.aborted).toBe(true)
    expect(authority.listeners.size).toBe(0)
    expect(authority.logouts.size).toBe(0)
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    await act(async () => pending.resolve({ material: row('private.txt') }))
    expect(screen.queryByRole('list')).toBeNull()
    expect(uploadProjectReference).toHaveBeenCalledTimes(1)
  })

  it.each(['signed out', 'no project'])('does not fetch when %s, and loads on becoming eligible', async condition => {
    if (condition === 'signed out') authority.scope = null
    const view = render(<ReferenceMaterialIntake projectId={condition === 'no project' ? null : project} />)
    expect(listProjectReferences).not.toHaveBeenCalled()
    expect(screen.getByText('Sign in and open a project to add original reference files.')).toBeTruthy()
    if (condition === 'signed out') changeScope('account-a')
    else view.rerender(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    expect(listProjectReferences).toHaveBeenCalledTimes(1)
  })

  it('downloads the original Blob once while pending, sets the filename and revokes its URL after saving starts', async () => {
    const pending = deferred(), blob = new Blob(['original bytes'], { type: 'text/plain' })
    listProjectReferences.mockResolvedValueOnce({ materials: [row('saved.txt', 'saved-id')] })
    readProjectReferenceOriginal.mockReturnValueOnce(pending.promise)
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
      expect(this.download).toBe('saved.txt')
      expect(this.getAttribute('href')).toBe('blob:reference-original')
      expect(this.isConnected).toBe(true)
    })
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Download original saved.txt' }))
    expect(screen.getByRole('button', { name: 'Downloading…' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Downloading…' }))
    expect(readProjectReferenceOriginal).toHaveBeenCalledExactlyOnceWith(project, 'saved-id', expect.objectContaining({ expectedScope: 'account-a' }))
    vi.useFakeTimers()
    await act(async () => pending.resolve(blob))
    expect(URL.createObjectURL).toHaveBeenCalledExactlyOnceWith(blob)
    expect(click).toHaveBeenCalledTimes(1)
    expect(document.querySelector('a[download]')).toBeNull()
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(1000))
    expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:reference-original')
  })

  it.each(['logout', 'unmount'])('revokes an outstanding original download URL immediately on %s', async transition => {
    listProjectReferences.mockResolvedValueOnce({ materials: [row('saved.txt')] })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const view = render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    vi.useFakeTimers()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Download original saved.txt' })))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    if (transition === 'logout') logout()
    else view.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:reference-original')
    act(() => vi.advanceTimersByTime(1000))
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('saved.txt')).toBeNull()
  })

  it('shows a download permission failure without creating a URL and allows another attempt', async () => {
    listProjectReferences.mockResolvedValueOnce({ materials: [row('saved.txt')] })
    readProjectReferenceOriginal.mockRejectedValueOnce(new Error('You do not have permission to do that in this project.'))
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Download original saved.txt' }))
    expect((await screen.findByRole('alert')).textContent).toContain('You do not have permission')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Download original saved.txt' }).disabled).toBe(false)
    expect(saved().getByText('saved.txt')).toBeTruthy()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByRole('button', { name: 'Download original saved.txt' }))
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1))
    expect(readProjectReferenceOriginal).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does not create a download URL for an original returned after logout', async () => {
    const pending = deferred()
    listProjectReferences.mockResolvedValueOnce({ materials: [row('private.txt')] })
    readProjectReferenceOriginal.mockReturnValueOnce(pending.promise)
    render(<ReferenceMaterialIntake projectId={project} />)
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Download original private.txt' }))
    const signal = readProjectReferenceOriginal.mock.calls[0][2].signal
    logout()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(new Blob(['private bytes'])))
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(screen.queryByRole('list')).toBeNull()
  })
})
