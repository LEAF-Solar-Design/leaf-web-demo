// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import VersionHistory from './VersionHistory.jsx'
import { restoreDrawingVersion } from '../api.js'

vi.mock('../api.js', () => ({
  config: { mockDefault: false },
  getDrawingVersions: vi.fn(),
  restoreDrawingVersion: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const data = { drawing_id: 'drawing-1', head: 2, latest: 2, versions: [{ v: 2 }, { v: 1 }] }
const props = { data, onClose: () => {}, onPreview: () => {}, onRetry: vi.fn() }

describe('version restore returns to the drawing before the request', () => {
  it('closes Start once before requesting the confirmed restore', async () => {
    let finish
    const pending = new Promise((resolve) => { finish = resolve })
    const onBeforeRestore = vi.fn()
    const onRestored = vi.fn()
    restoreDrawingVersion.mockImplementation(() => {
      expect(onBeforeRestore).toHaveBeenCalledTimes(1)
      return pending
    })
    render(<VersionHistory {...props} onBeforeRestore={onBeforeRestore} onRestored={onRestored} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }))
    expect(onBeforeRestore).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Restore v1' }))
    expect(onBeforeRestore).toHaveBeenCalledTimes(1)
    expect(restoreDrawingVersion).toHaveBeenCalledWith(false, 'drawing-1', 1, undefined)
    expect(onBeforeRestore.mock.invocationCallOrder[0]).toBeLessThan(restoreDrawingVersion.mock.invocationCallOrder[0])
    expect(onRestored).not.toHaveBeenCalled()
    const result = { head: 3 }
    await act(async () => { finish(result); await pending })
    expect(onRestored).toHaveBeenCalledWith(result)
    expect(onBeforeRestore).toHaveBeenCalledTimes(1)
  })

  it('does not close Start or restore while mutations are blocked', () => {
    const onBeforeRestore = vi.fn()
    const { rerender } = render(<VersionHistory {...props} onBeforeRestore={onBeforeRestore} />)
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }))
    rerender(<VersionHistory {...props} onBeforeRestore={onBeforeRestore} mutationBlocked />)
    const confirm = screen.getByRole('button', { name: 'Restore v1' })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(restoreDrawingVersion).not.toHaveBeenCalled()
    expect(onBeforeRestore).not.toHaveBeenCalled()
  })
})
