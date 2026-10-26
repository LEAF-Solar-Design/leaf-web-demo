// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CustomizePanel from './CustomizePanel.jsx'
import { getPlatformChange, landPlatformChange, mergePlatformChange, proposePlatformChange } from '../api.js'

vi.mock('../api.js', () => ({
  getPlatformChange: vi.fn(),
  landPlatformChange: vi.fn(),
  mergePlatformChange: vi.fn(),
  proposePlatformChange: vi.fn(),
}))

const first = {
  change_id: '11111111-1111-1111-1111-111111111111',
  commit_sha: 'a'.repeat(40),
  title: 'First change',
  state: 'landed',
  review: { state: 'passed', pr_state: 'open' },
}
const second = { ...first, change_id: '22222222-2222-2222-2222-222222222222', commit_sha: 'b'.repeat(40), title: 'Second change' }

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function select(record) {
  fireEvent.click(screen.getByRole('button', { name: new RegExp(record.title) }))
  await screen.findByText(record.title, { selector: '.ops-section-copy' })
}

function recordSection() {
  return within(screen.getByRole('region', { name: 'Change record' }))
}

// Batch clicks before the busy render to exercise overlapping completions
// even though the rendered buttons block new requests once busy.
function clickTogether(...buttons) {
  act(() => {
    for (const button of buttons) button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  localStorage.setItem('leaf.customize.recent.test', JSON.stringify([first, second]))
  getPlatformChange.mockImplementation(async (id) => ({ ...(id === first.change_id ? first : second) }))
})

afterEach(() => { cleanup(); localStorage.clear() })

describe('self-edit confirmations and stale responses', () => {
  it('requires a fresh Merge confirmation after selecting another change', async () => {
    render(<CustomizePanel tenant="test" />)
    await select(first)
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(screen.queryByText(/Merge commit/)).not.toBeNull()
    await select(second)
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(mergePlatformChange).not.toHaveBeenCalled()
    expect(screen.queryByText(`Merge commit ${second.commit_sha.slice(0, 12)} into main?`)).not.toBeNull()
    mergePlatformChange.mockResolvedValue({ ...second, state: 'merged' })
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    await screen.findByText('Merged', { selector: '.state' })
    expect(mergePlatformChange).toHaveBeenCalledWith(second.change_id, second.commit_sha)
  })

  it('requires a fresh Merge confirmation when the same change has a new commit', async () => {
    render(<CustomizePanel tenant="test" />)
    await select(first)
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    getPlatformChange.mockResolvedValue({ ...first, commit_sha: second.commit_sha })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    await screen.findByText(second.commit_sha.slice(0, 12), { selector: '.cst-mono' })
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(mergePlatformChange).not.toHaveBeenCalled()
    expect(screen.queryByText(`Merge commit ${second.commit_sha.slice(0, 12)} into main?`)).not.toBeNull()
  })

  it('clears Merge confirmation when proposing a new change', async () => {
    render(<CustomizePanel tenant="test" />)
    await select(first)
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New change' } })
    fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
    proposePlatformChange.mockResolvedValue(second)
    fireEvent.click(screen.getByRole('button', { name: 'Propose' }))
    await screen.findByText(second.title, { selector: '.ops-section-copy' })
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect(mergePlatformChange).not.toHaveBeenCalled()
  })

  it('blocks both refresh entry points while a proposal is pending and remembers its result', async () => {
    const proposed = { ...second, change_id: '33333333-3333-3333-3333-333333333333', title: 'New proposal' }
    const pending = deferred()
    proposePlatformChange.mockReturnValue(pending.promise)
    render(<CustomizePanel tenant="test" />)
    await select(first)
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: proposed.title } })
    fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose' }))
    expect(proposePlatformChange).toHaveBeenCalledTimes(1)
    const mergeButton = screen.getByRole('button', { name: 'Merge' })
    expect(mergeButton.disabled).toBe(true)
    fireEvent.click(mergeButton)
    expect(mergePlatformChange).not.toHaveBeenCalled()
    const refreshButton = screen.getByRole('button', { name: 'Refresh status' })
    expect(refreshButton.disabled).toBe(true)
    fireEvent.click(refreshButton)
    const recent = within(screen.getByRole('region', { name: 'Recent changes' }))
    for (const button of recent.getAllByRole('button')) {
      expect(button.disabled).toBe(true)
      fireEvent.click(button)
    }
    expect(getPlatformChange).toHaveBeenCalledTimes(1)
    expect(recordSection().queryByText(first.title)).not.toBeNull()
    await act(async () => { pending.resolve(proposed) })
    expect(recordSection().queryByText(proposed.title)).not.toBeNull()
    expect(recent.getByRole('button', { name: `${proposed.change_id.slice(0, 8)} · ${proposed.title}` }).disabled).toBe(false)
    expect(JSON.parse(localStorage.getItem('leaf.customize.recent.test'))[0]).toEqual({
      change_id: proposed.change_id,
      title: proposed.title,
    })
    expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
  })

  it('keeps a new refresh busy when the replaced record refresh settles late', async () => {
    const older = deferred()
    const newer = deferred()
    render(<CustomizePanel tenant="test" />)
    await select(first)
    getPlatformChange.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: second.title } })
    fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
    proposePlatformChange.mockResolvedValue(second)
    fireEvent.click(screen.getByRole('button', { name: 'Propose' }))
    await screen.findByText(second.title, { selector: '.ops-section-copy' })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
    expect(getPlatformChange).toHaveBeenNthCalledWith(3, second.change_id)
    expect(screen.getByRole('button', { name: 'Refreshing…' }).disabled).toBe(true)
    await act(async () => { older.reject(new Error('The old change could not be loaded.')) })
    expect(recordSection().queryByText(second.title)).not.toBeNull()
    expect(recordSection().queryByText(second.commit_sha.slice(0, 12))).not.toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: 'Refreshing…' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Merge' }).disabled).toBe(true)
    await act(async () => { newer.resolve({ ...second, state: 'merged' }) })
    expect(recordSection().queryByText('Merged', { selector: '.state' })).not.toBeNull()
    expect(recordSection().queryByText(second.title)).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
  })

  for (const action of ['Land', 'Merge']) {
    it(`enables ${action} and refresh for a proposal while the replaced record's refresh stays pending`, async () => {
      const proposed = { ...second, state: action === 'Land' ? 'approved' : 'landed' }
      const pending = deferred()
      const proposal = deferred()
      proposePlatformChange.mockReturnValue(proposal.promise)
      render(<CustomizePanel tenant="test" />)
      await select(first)
      getPlatformChange.mockReturnValueOnce(pending.promise)
      fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
      expect(screen.getByRole('button', { name: 'Refreshing…' }).disabled).toBe(true)
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: proposed.title } })
      fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
      fireEvent.click(screen.getByRole('button', { name: 'Propose' }))
      expect(proposePlatformChange).toHaveBeenCalledTimes(1)
      await act(async () => { proposal.resolve(proposed) })
      expect(recordSection().queryByText(proposed.title)).not.toBeNull()
      expect(recordSection().queryByText(proposed.commit_sha.slice(0, 12))).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
      expect(screen.getByRole('button', { name: action }).disabled).toBe(false)
      await act(async () => { pending.resolve({ ...first, state: 'merged' }) })
      expect(recordSection().queryByText(proposed.title)).not.toBeNull()
      expect(recordSection().queryByText(proposed.commit_sha.slice(0, 12))).not.toBeNull()
      expect(recordSection().queryByText('Merged', { selector: '.state' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
      expect(screen.getByRole('button', { name: action }).disabled).toBe(false)
      expect(screen.queryByRole('alert')).toBeNull()
    })

    it(`keeps a newer ${action} busy when an older request settles first`, async () => {
      const original = { ...first, state: action === 'Land' ? 'approved' : 'landed' }
      const proposed = { ...second, state: original.state }
      getPlatformChange.mockResolvedValue(original)
      const older = deferred()
      const newer = deferred()
      const mutate = action === 'Land' ? landPlatformChange : mergePlatformChange
      mutate.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
      render(<CustomizePanel tenant="test" />)
      await select(first)
      fireEvent.click(screen.getByRole('button', { name: action }))
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: second.title } })
      fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
      proposePlatformChange.mockResolvedValue(proposed)
      clickTogether(screen.getByRole('button', { name: action }), screen.getByRole('button', { name: 'Propose' }))
      expect(mutate).toHaveBeenNthCalledWith(1, first.change_id, first.commit_sha)
      await screen.findByText(second.title, { selector: '.ops-section-copy' })
      expect(screen.getByRole('button', { name: action }).disabled).toBe(false)
      fireEvent.click(screen.getByRole('button', { name: action }))
      fireEvent.click(screen.getByRole('button', { name: action }))
      expect(mutate).toHaveBeenNthCalledWith(2, second.change_id, second.commit_sha)
      const busyLabel = action === 'Land' ? 'Landing…' : 'Merging…'
      expect(screen.getByRole('button', { name: busyLabel }).disabled).toBe(true)
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(true)
      await act(async () => {
        older.resolve({ ...original, state: action === 'Land' ? 'landed' : 'merged' })
      })
      expect(recordSection().queryByText(second.title)).not.toBeNull()
      expect(screen.getByRole('button', { name: busyLabel }).disabled).toBe(true)
      expect(screen.getByRole('button', { name: 'Keep' }).disabled).toBe(true)
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(true)
      fireEvent.click(screen.getByRole('button', { name: busyLabel }))
      fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }))
      expect(mutate).toHaveBeenCalledTimes(2)
      expect(getPlatformChange).toHaveBeenCalledTimes(1)
      await act(async () => {
        newer.resolve({ ...proposed, state: action === 'Land' ? 'landed' : 'merged' })
      })
      expect(recordSection().queryByText(action === 'Land' ? 'Landed' : 'Merged', { selector: '.state' })).not.toBeNull()
      expect(recordSection().queryByText(second.title)).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
    })

    it(`blocks refresh during a pending ${action} and displays its result`, async () => {
      const original = { ...first, state: action === 'Land' ? 'approved' : 'landed' }
      getPlatformChange.mockResolvedValue(original)
      const pending = deferred()
      const mutate = action === 'Land' ? landPlatformChange : mergePlatformChange
      mutate.mockReturnValue(pending.promise)
      render(<CustomizePanel tenant="test" />)
      await select(first)
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: second.title } })
      fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
      fireEvent.click(screen.getByRole('button', { name: action }))
      fireEvent.click(screen.getByRole('button', { name: action }))
      expect(mutate).toHaveBeenCalledWith(first.change_id, first.commit_sha)
      const proposeButton = screen.getByRole('button', { name: 'Propose' })
      expect(proposeButton.disabled).toBe(true)
      fireEvent.click(proposeButton)
      expect(proposePlatformChange).not.toHaveBeenCalled()
      const refreshButton = screen.getByRole('button', { name: 'Refresh status' })
      expect(refreshButton.disabled).toBe(true)
      fireEvent.click(refreshButton)
      // Recent selections also use refresh; its guard must preserve the
      // pending mutation even when called through an enabled button.
      fireEvent.click(screen.getByRole('button', { name: new RegExp(second.title) }))
      expect(getPlatformChange).toHaveBeenCalledTimes(1)
      await act(async () => {
        pending.resolve({ ...original, state: action === 'Land' ? 'landed' : 'merged' })
      })
      expect(recordSection().queryByText(action === 'Land' ? 'Landed' : 'Merged', { selector: '.state' })).not.toBeNull()
      expect(recordSection().queryByText(first.title)).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
      expect(screen.getByRole('button', { name: 'Propose' }).disabled).toBe(false)
    })

    it(`applies a pending ${action} result after an overlapping proposal fails`, async () => {
      const original = { ...first, state: action === 'Land' ? 'approved' : 'landed' }
      getPlatformChange.mockResolvedValue(original)
      const pending = deferred()
      const proposal = deferred()
      const mutate = action === 'Land' ? landPlatformChange : mergePlatformChange
      mutate.mockReturnValue(pending.promise)
      proposePlatformChange.mockReturnValue(proposal.promise)
      render(<CustomizePanel tenant="test" />)
      await select(first)
      fireEvent.change(screen.getByLabelText('Title'), { target: { value: second.title } })
      fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
      fireEvent.click(screen.getByRole('button', { name: action }))
      clickTogether(screen.getByRole('button', { name: action }), screen.getByRole('button', { name: 'Propose' }))
      expect(mutate).toHaveBeenCalledWith(first.change_id, first.commit_sha)
      expect(proposePlatformChange).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('button', { name: action }).disabled).toBe(true)
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(true)
      await act(async () => { proposal.reject(new Error('The proposal could not be saved.')) })
      expect(screen.queryByRole('alert')).not.toBeNull()
      expect(recordSection().queryByText(first.title)).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Propose' }).disabled).toBe(true)
      expect(screen.getByRole('button', { name: action }).disabled).toBe(true)
      await act(async () => {
        pending.resolve({ ...original, state: action === 'Land' ? 'landed' : 'merged' })
      })
      expect(recordSection().queryByText(action === 'Land' ? 'Landed' : 'Merged', { selector: '.state' })).not.toBeNull()
      expect(recordSection().queryByText(first.title)).not.toBeNull()
      expect(screen.getByRole('button', { name: 'Propose' }).disabled).toBe(false)
      expect(screen.getByRole('button', { name: 'Refresh status' }).disabled).toBe(false)
    })

    for (const fails of [false, true]) {
      it(`ignores a stale ${action} ${fails ? 'error' : 'result'} after another change is proposed`, async () => {
        const original = { ...first, state: action === 'Land' ? 'approved' : 'landed' }
        getPlatformChange.mockImplementation(async (id) => id === first.change_id ? original : second)
        const pending = deferred()
        const mutate = action === 'Land' ? landPlatformChange : mergePlatformChange
        mutate.mockReturnValue(pending.promise)
        render(<CustomizePanel tenant="test" />)
        await select(first)
        fireEvent.click(screen.getByRole('button', { name: action }))
        fireEvent.change(screen.getByLabelText('Title'), { target: { value: second.title } })
        fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.js' } })
        proposePlatformChange.mockResolvedValue(second)
        clickTogether(screen.getByRole('button', { name: action }), screen.getByRole('button', { name: 'Propose' }))
        expect(mutate).toHaveBeenCalledWith(first.change_id, first.commit_sha)
        await screen.findByText(second.title, { selector: '.ops-section-copy' })
        await act(async () => {
          if (fails) pending.reject(new Error('The old change could not be saved.'))
          else pending.resolve({ ...original, state: action === 'Land' ? 'landed' : 'merged' })
        })
        expect(recordSection().queryByText(second.title)).not.toBeNull()
        expect(recordSection().queryByText(second.commit_sha.slice(0, 12))).not.toBeNull()
        expect(screen.queryByRole('alert')).toBeNull()
      })
    }
  }
})
