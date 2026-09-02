// @vitest-environment jsdom
//
// The drafting ribbon's contract (W4c-V1): real commands through the
// catalog run path with 'ribbon' attribution, ToolsPanel-parity write
// gating with a visible reason, and an honest empty state.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from './DraftingRibbon.jsx'

afterEach(cleanup)

const FAMS = [
  {
    family_id: 'measurement',
    label: 'Measurement',
    capabilities: [
      { name: 'count-by-layer', description: 'Counts entities per layer.', capabilities: ['drawing.read'] },
    ],
  },
  {
    family_id: 'custom',
    label: 'Custom authored tools',
    capabilities: [
      { name: 'delete-marked-panel', description: 'Deletes the marked panel.', capabilities: ['drawing.write'] },
    ],
  },
]

describe('DraftingRibbon', () => {
  it('renders one cluster per family and arms the catalog run path with ribbon attribution', () => {
    const onRequestRun = vi.fn()
    render(<DraftingRibbon families={FAMS} onRequestRun={onRequestRun} />)
    expect(screen.getByRole('toolbar', { name: 'Drafting tools' })).toBeTruthy()
    expect(document.querySelectorAll('.ribbon-cluster')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'count-by-layer' }))
    expect(onRequestRun).toHaveBeenCalledTimes(1)
    const [tool, params, rationale, source] = onRequestRun.mock.calls[0]
    expect(tool.name).toBe('count-by-layer')
    // null params -> the confirm strip computes the tool's schema defaults.
    expect(params).toBeNull()
    expect(rationale).toMatch(/confirm/i)
    expect(source).toBe('ribbon')
  })

  it('disables a write tool under the single-writer lock, with the reason readable', () => {
    const onRequestRun = vi.fn()
    render(<DraftingRibbon families={FAMS} onRequestRun={onRequestRun} writeLocked />)
    const write = screen.getByRole('button', { name: /delete-marked-panel \(unavailable/ })
    expect(write.disabled).toBe(true)
    expect(write.title).toMatch(/edit lock/)
    // The read tool stays live under the lock.
    expect(screen.getByRole('button', { name: 'count-by-layer' }).disabled).toBe(false)
    fireEvent.click(write)
    expect(onRequestRun).not.toHaveBeenCalled()
  })

  it('disables a write tool when the plan lacks the entitlement', () => {
    render(<DraftingRibbon families={FAMS} onRequestRun={() => {}} writeEntitled={false} />)
    const write = screen.getByRole('button', { name: /delete-marked-panel \(unavailable/ })
    expect(write.disabled).toBe(true)
    expect(write.title).toMatch(/plan/)
  })

  it('disables everything while a run is in flight', () => {
    render(<DraftingRibbon families={FAMS} onRequestRun={() => {}} running />)
    expect(screen.getByRole('button', { name: 'count-by-layer' }).disabled).toBe(true)
  })

  it('renders the honest empty sentence for a surface with no registered families', () => {
    render(<DraftingRibbon families={[]} onRequestRun={() => {}} />)
    expect(screen.getByText('No tools for this surface yet.')).toBeTruthy()
    expect(document.querySelectorAll('.ribbon-tool')).toHaveLength(0)
  })
})
