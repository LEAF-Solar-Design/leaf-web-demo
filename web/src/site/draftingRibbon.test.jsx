// @vitest-environment jsdom
//
// The drafting ribbon's contract (W4c-V1, generalized in W4d): it RENDERS an
// ordered cluster list — real commands through the catalog run path with
// 'ribbon' attribution, ToolsPanel-parity write gating with a visible reason,
// honest empty state, pressed toggles, and children (the engine's clusters)
// ahead of the data clusters.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { catalogClusters } from '../lib/ribbonClusters.js'

import DraftingRibbon, { RIBBON_HEIGHT_VAR, RibbonCluster, RibbonTool } from './DraftingRibbon.jsx'

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
    render(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun })} />)
    expect(screen.getByRole('toolbar', { name: 'Drafting tools' })).toBeTruthy()
    expect(document.querySelectorAll('.ribbon-cluster[data-family]')).toHaveLength(2)
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
    render(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun, writeLocked: true })} />)
    const write = screen.getByRole('button', { name: /delete-marked-panel \(unavailable/ })
    expect(write.disabled).toBe(true)
    expect(write.title).toMatch(/edit lock/)
    // The read tool stays live under the lock.
    expect(screen.getByRole('button', { name: 'count-by-layer' }).disabled).toBe(false)
    fireEvent.click(write)
    expect(onRequestRun).not.toHaveBeenCalled()
  })

  it('disables a write tool when the plan lacks the entitlement', () => {
    render(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {}, writeEntitled: false })} />)
    const write = screen.getByRole('button', { name: /delete-marked-panel \(unavailable/ })
    expect(write.disabled).toBe(true)
    expect(write.title).toMatch(/plan/)
  })

  it('disables everything while a run is in flight and exposes that reason', () => {
    render(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {}, running: true })} />)
    const read = screen.getByRole('button', { name: `count-by-layer (unavailable: a run is in flight)` })
    expect(read.disabled).toBe(true)
    expect(read.title).toBe('a run is in flight')
  })

  it('disables everything during version preview and exposes the read-only reason', () => {
    render(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {}, previewing: true })} />)
    const read = screen.getByRole('button', { name: `count-by-layer (unavailable: viewing a version, read-only)` })
    expect(read.disabled).toBe(true)
    expect(read.title).toBe('viewing a version, read-only')
  })

  it('renders the honest empty sentence with no clusters, and the empty fold as one note-only cluster', () => {
    const { unmount } = render(<DraftingRibbon clusters={[]} />)
    expect(screen.getByText('No tools for this surface yet.')).toBeTruthy()
    expect(document.querySelectorAll('.ribbon-tool')).toHaveLength(0)
    unmount()
    render(<DraftingRibbon clusters={catalogClusters([], { onRequestRun: () => {} })} />)
    expect(document.querySelectorAll('.ribbon-cluster')).toHaveLength(1)
    expect(screen.getByText('No tools for this surface yet.').className).toBe('ribbon-note')
    expect(document.querySelectorAll('.ribbon-tool')).toHaveLength(0)
  })

  it('renders children FIRST (the engine clusters lead the band), then the data clusters', () => {
    render(
      <DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {} })}>
        <RibbonCluster id="modify" label="Modify" note="opens on an imported DXF">
          <RibbonTool tool={{ id: 'm', label: 'move', disabled: true, reason: 'opens on an imported DXF' }} />
        </RibbonCluster>
      </DraftingRibbon>,
    )
    const clusters = [...document.querySelectorAll('.ribbon-cluster')]
    expect(clusters.map((el) => el.dataset.group || el.dataset.family)).toEqual(['modify', 'measurement', 'custom'])
    expect(screen.queryByText('No tools for this surface yet.')).toBeNull()
    // The group is a named landmark for assistive tech; the note is visible text.
    expect(screen.getByRole('group', { name: 'Modify' })).toBeTruthy()
    expect(screen.getByText('opens on an imported DXF').className).toBe('ribbon-note')
    const move = screen.getByRole('button', { name: 'move (unavailable: opens on an imported DXF)' })
    expect(move.title).toBe('opens on an imported DXF')
  })

  it('a family label is a real command that opens the family in the rail; a fixed group label is decoration', () => {
    const onOpenFamily = vi.fn()
    render(
      <DraftingRibbon clusters={[
        ...catalogClusters(FAMS, { onRequestRun: () => {}, onOpenFamily }),
        { id: 'view', label: 'View', tools: [{ id: 'fit', label: 'fit', onClick: () => {} }] },
      ]}
      />,
    )
    const label = screen.getByRole('button', { name: 'Open Measurement in the tool rail (1 tools)' })
    fireEvent.click(label)
    expect(onOpenFamily).toHaveBeenCalledWith(FAMS[0])
    // The label sits LAST in its cluster (bottom, the reference grammar).
    const cluster = label.closest('.ribbon-cluster')
    expect(cluster.lastElementChild).toBe(label)
    // A fixed group's label is not a button and is hidden from assistive tech.
    const view = document.querySelector('[data-group="view"] .ribbon-cluster-label')
    expect(view.tagName).toBe('SPAN')
    expect(view.getAttribute('aria-hidden')).toBe('true')
  })

  it('publishes its measured band height on the workspace card, with or without ResizeObserver', () => {
    // jsdom has no ResizeObserver and no layout: the one-shot measurement
    // still runs and the variable exists (0px here), so the import pane's
    // offset never falls back to a stale constant in a real browser.
    const { unmount } = render(
      <div className="workspace-card">
        <DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {} })} />
      </div>,
    )
    const card = document.querySelector('.workspace-card')
    expect(card.style.getPropertyValue(RIBBON_HEIGHT_VAR)).toBe('0px')
    unmount()
    // Unmount clears it: a card without a ribbon publishes nothing.
    expect(document.querySelector('.workspace-card')).toBeNull()
  })

  it('a pressed toggle carries aria-pressed and an expander carries aria-expanded + aria-controls', () => {
    render(
      <DraftingRibbon clusters={[{
        id: 'layers',
        label: 'Layers',
        tools: [
          { id: 'l:Panels', label: 'Panels', pressed: true, onClick: () => {} },
          { id: 'l:Roof', label: 'Roof', pressed: false, onClick: () => {} },
          { id: 'x', label: 'import-dxf', expanded: false, controls: 'pane', onClick: () => {} },
        ],
      }]}
      />,
    )
    expect(screen.getByRole('button', { name: 'Panels' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Roof' }).getAttribute('aria-pressed')).toBe('false')
    const importBtn = screen.getByRole('button', { name: 'import-dxf' })
    expect(importBtn.getAttribute('aria-expanded')).toBe('false')
    expect(importBtn.getAttribute('aria-controls')).toBe('pane')
    // A plain command carries neither state attribute.
    expect(screen.getByRole('button', { name: 'Panels' }).hasAttribute('aria-expanded')).toBe(false)
  })
})
