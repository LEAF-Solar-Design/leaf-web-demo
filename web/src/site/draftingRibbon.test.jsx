// @vitest-environment jsdom
//
// The drafting ribbon's contract (W4c-V1, generalized in W4d): it RENDERS an
// ordered cluster list — real commands through the catalog run path with
// 'ribbon' attribution, ToolsPanel-parity write gating with a visible reason,
// honest empty state, pressed toggles, and children (the engine's clusters)
// ahead of the data clusters.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createPortal } from 'react-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { catalogClusters, profileRibbonTabs } from '../lib/ribbonClusters.js'
import { REASONS, reasonCode } from '../lib/actionRegistry.js'

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
  it('shows the catalog failure and retries from the band, then clears on recovery', () => {
    const onRetryCatalog = vi.fn()
    const { rerender } = render(<DraftingRibbon clusters={[]} catalogError="Network request failed" onRetryCatalog={onRetryCatalog} />)
    const status = screen.getByRole('status')
    expect(status.textContent).toContain("Couldn't load tools: Network request failed")
    expect(screen.queryByText('No tools for this surface yet.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    expect(onRetryCatalog).toHaveBeenCalledTimes(1)
    rerender(<DraftingRibbon clusters={catalogClusters(FAMS, { onRequestRun: () => {} })} onRetryCatalog={onRetryCatalog} />)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry', exact: true })).toBeNull()
    expect(screen.getByRole('button', { name: 'count-by-layer' })).toBeTruthy()
  })

  it('keeps catalog recovery outside the overflow panel host', () => {
    render(<DraftingRibbon clusters={profileRibbonTabs('solar')[1].clusters} tab="solar" visiblePanelCount={0} catalogError="Network request failed" onRetryCatalog={() => {}} />)
    const status = screen.getByRole('status')
    expect(status.parentElement).toBe(screen.getByRole('toolbar', { name: 'Drafting tools' }))
    fireEvent.click(screen.getByRole('button', { name: 'More panels' }))
    expect(status.parentElement).toBe(screen.getByRole('toolbar', { name: 'Drafting tools' }))
    expect(status.querySelector('button').textContent).toBe('Retry')
  })

  it('does not report a failure for a genuinely empty catalog', () => {
    render(<DraftingRibbon clusters={[]} />)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry', exact: true })).toBeNull()
    expect(screen.getByText('No tools for this surface yet.')).toBeTruthy()
  })

  it('C-05 row9 exposes data-state only for a string tool state', () => {
    render(<>
      <RibbonTool tool={{ id: 'ship:readiness', label: 'Apple readiness: ready', state: 'ready' }} />
      <RibbonTool tool={{ id: 'open', label: 'Open' }} />
      <RibbonTool tool={{ id: 'other', label: 'Other', state: true }} />
    </>)
    expect(screen.getByRole('button', { name: 'Apple readiness: ready' }).getAttribute('data-state')).toBe('ready')
    expect(screen.getByRole('button', { name: 'Open' }).hasAttribute('data-state')).toBe(false)
    expect(screen.getByRole('button', { name: 'Other' }).hasAttribute('data-state')).toBe(false)
  })

  it('renders a profile tab outside the catalog placement tabs', () => {
    const solar = profileRibbonTabs('solar')[1]
    render(<DraftingRibbon tab={solar.id} clusters={solar.clusters} />)
    expect(screen.getByRole('toolbar', { name: 'Drafting tools' }).getAttribute('data-tab')).toBe('solar')
    expect(screen.getByRole('group', { name: 'Stringing' })).toBeTruthy()
    expect(screen.getByRole('group', { name: 'Equipment placement' })).toBeTruthy()
    const empty = screen.getByRole('button', { name: 'Stringing (unavailable: No stringing tools in this catalog yet)' })
    expect(empty.disabled).toBe(true)
    expect(empty.title).toBe('No stringing tools in this catalog yet')
  })

  it('exposes local codes only for disabled tools with registered reasons', () => {
    const reason = REASONS['unsavedEngineEdits']
    render(<>
      <RibbonTool tool={{ id: 'save', label: 'Save', disabled: true, reason }} />
      <RibbonTool tool={{ id: 'open', label: 'Open', reason }} />
      <RibbonTool tool={{ id: 'other', label: 'Other', disabled: true, reason: 'unregistered reason' }} />
    </>)
    expect(screen.getByRole('button', { name: /Save \(unavailable/ }).getAttribute('data-reason-code')).toBe(reasonCode(reason))
    expect(screen.getByRole('button', { name: 'Open' }).hasAttribute('data-reason-code')).toBe(false)
    expect(screen.getByRole('button', { name: /Other \(unavailable/ }).hasAttribute('data-reason-code')).toBe(false)
  })
  it('exposes a collapsed panel through More panels without remounting tools', () => {
    const visiblePanelCount = 1
    const onCopy = vi.fn()
    render(<DraftingRibbon visiblePanelCount={visiblePanelCount} clusters={[
      { id: 'draw', label: 'Draw', tools: [{ id: 'line', label: 'Line' }] },
      { id: 'modify', label: 'Modify', tools: [{ id: 'move', label: 'Move' }] },
      { id: 'clipboard', label: 'Clipboard', tools: [{ id: 'copy', label: 'Copy', onClick: onCopy }] },
    ]} />)
    const copy = document.querySelector('[data-tool="copy"]')
    const more = screen.getByRole('button', { name: 'More panels' })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(copy.closest('.ribbon-cluster').hidden).toBe(true)
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(more.getAttribute('aria-controls')).contains(copy)).toBe(true)
    expect(screen.getByRole('group', { name: 'Clipboard' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy' })).toBe(copy)
    expect(document.activeElement.dataset.tool).toBe('line')
    fireEvent.click(copy)
    expect(onCopy).toHaveBeenCalledTimes(1)
    // Activating an enabled overflow tool closes the overflow (ROV1).
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(document.getElementById('drafting-ribbon').hasAttribute('data-overflow-open')).toBe(false)
    expect(copy.closest('.ribbon-cluster').hidden).toBe(true)
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    copy.focus()
    fireEvent.keyDown(copy, { key: 'Escape' })
    expect(document.activeElement).toBe(more)
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(copy.closest('.ribbon-cluster').hidden).toBe(true)
  })

  describe('ROV the overflow closes on tool activation only', () => {
    const overflowClusters = (handlers = {}) => [
      { id: 'draw', label: 'Draw', tools: [{ id: 'line', label: 'Line', onClick: handlers.line }] },
      { id: 'modify', label: 'Modify', tools: [{ id: 'move', label: 'Move', onClick: handlers.move }] },
      { id: 'clipboard', label: 'Clipboard', tools: [
        { id: 'copy', label: 'Copy', onClick: handlers.copy },
        { id: 'paste', label: 'Paste', disabled: true, reason: 'nothing to paste yet', onClick: handlers.paste },
      ] },
    ]
    const openOverflow = () => {
      const more = screen.getByRole('button', { name: 'More panels' })
      fireEvent.click(more)
      expect(more.getAttribute('aria-expanded')).toBe('true')
      return more
    }

    it('ROV2 a focused overflow tool whose cluster closes hands focus to More panels', () => {
      const onCopy = vi.fn()
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters({ copy: onCopy })} />)
      const more = openOverflow()
      const copy = document.querySelector('[data-tool="copy"]')
      copy.focus()
      fireEvent.click(copy)
      expect(onCopy).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('false')
      expect(copy.closest('.ribbon-cluster').hidden).toBe(true)
      expect(document.activeElement).toBe(more)
    })

    it('ROV3 a disabled overflow tool leaves the overflow open and runs nothing', () => {
      const onPaste = vi.fn()
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters({ paste: onPaste })} />)
      const more = openOverflow()
      const paste = document.querySelector('[data-tool="paste"]')
      expect(paste.disabled).toBe(true)
      fireEvent.click(paste)
      expect(onPaste).not.toHaveBeenCalled()
      expect(more.getAttribute('aria-expanded')).toBe('true')
      expect(document.getElementById('drafting-ribbon').getAttribute('data-overflow-open')).toBe('true')
    })

    it('ROV4 focus moving between overflow tools keeps the overflow open', () => {
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters()} />)
      const more = openOverflow()
      document.querySelector('[data-tool="move"]').focus()
      document.querySelector('[data-tool="copy"]').focus()
      expect(document.activeElement.dataset.tool).toBe('copy')
      expect(more.getAttribute('aria-expanded')).toBe('true')
    })

    it('ROV5 a click inside the open panels that is not on a tool keeps the overflow open', () => {
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters()} />)
      const more = openOverflow()
      fireEvent.click(screen.getByRole('group', { name: 'Clipboard' }))
      fireEvent.click(document.getElementById('drafting-ribbon-panels'))
      expect(more.getAttribute('aria-expanded')).toBe('true')
    })

    it('ROV6 a visible tool with the overflow closed runs as before and opens nothing', () => {
      const onLine = vi.fn()
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters({ line: onLine })} />)
      const more = screen.getByRole('button', { name: 'More panels' })
      fireEvent.click(document.querySelector('[data-tool="line"]'))
      expect(onLine).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('false')
      expect(document.getElementById('drafting-ribbon').hasAttribute('data-overflow-open')).toBe(false)
    })

    // Script's Run script and Choose script wear the ribbon-tool class for styling but carry no data-tool: they are
    // a panel's own controls, and the report a run writes sits in the same panel, so the overflow must stay open.
    it('ROV7 a panel control styled as a ribbon tool keeps the overflow open and its feedback visible', () => {
      const onRun = vi.fn()
      const clusters = [...overflowClusters(), { id: 'script', label: 'Script', tools: [], extra: (
        <span>
          <button type="button" className="cp-run ribbon-tool" data-size="row" onClick={onRun}>Run script</button>
          <span data-testid="rov7-report">Script stopped before running: line 1.</span>
        </span>
      ) }]
      render(<DraftingRibbon visiblePanelCount={1} clusters={clusters} />)
      const more = openOverflow()
      const run = screen.getByRole('button', { name: 'Run script' })
      expect(run.hasAttribute('data-tool')).toBe(false)
      expect(run.closest('.ribbon-cluster').hidden).toBe(false)
      fireEvent.click(run)
      expect(onRun).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('true')
      expect(screen.getByTestId('rov7-report').closest('.ribbon-cluster').hidden).toBe(false)
    })

    it('ROV8 a tool rendered outside the panels through a portal leaves the overflow open', () => {
      const onFar = vi.fn()
      function Far() {
        return createPortal(<RibbonTool tool={{ id: 'far', label: 'Far', onClick: onFar }} />, document.body)
      }
      const clusters = [...overflowClusters(), { id: 'portal', label: 'Portal', tools: [], extra: <Far /> }]
      render(<DraftingRibbon visiblePanelCount={1} clusters={clusters} />)
      const more = openOverflow()
      const far = document.querySelector('[data-tool="far"]')
      expect(document.getElementById('drafting-ribbon-panels').contains(far)).toBe(false)
      fireEvent.click(far)
      expect(onFar).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('true')
    })

    it('ROV9 a click on the label inside an enabled overflow tool closes the overflow', () => {
      const onCopy = vi.fn()
      render(<DraftingRibbon visiblePanelCount={1} clusters={overflowClusters({ copy: onCopy })} />)
      const more = openOverflow()
      fireEvent.click(document.querySelector('[data-tool="copy"] .ribbon-tool-label'))
      expect(onCopy).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('false')
    })

    it('ROV10 a plain button inside an open panel keeps the overflow open', () => {
      const onPlain = vi.fn()
      const clusters = [...overflowClusters(), { id: 'plain', label: 'Plain', tools: [], extra: (
        <button type="button" onClick={onPlain}>Plain action</button>
      ) }]
      render(<DraftingRibbon visiblePanelCount={1} clusters={clusters} />)
      const more = openOverflow()
      fireEvent.click(screen.getByRole('button', { name: 'Plain action' }))
      expect(onPlain).toHaveBeenCalledTimes(1)
      expect(more.getAttribute('aria-expanded')).toBe('true')
    })
  })

  it('restores every panel when capacity returns and hides the unused overflow control', () => {
    const clusters = [{ id: 'clipboard', label: 'Clipboard', tools: [{ id: 'copy', label: 'Copy' }] }]
    const { rerender } = render(<DraftingRibbon clusters={clusters} visiblePanelCount={0} />)
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull()
    rerender(<DraftingRibbon clusters={clusters} visiblePanelCount={1} />)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'More panels' })).toBeNull()
  })

  it('hands focus to More panels when closing over a tool and when cancel targets that hidden tool', () => {
    render(<DraftingRibbon visiblePanelCount={0} clusters={[
      { id: 'clipboard', label: 'Clipboard', tools: [{ id: 'copy', label: 'Copy' }] },
    ]} />)
    const more = screen.getByRole('button', { name: 'More panels' })
    fireEvent.click(more)
    const copy = screen.getByRole('button', { name: 'Copy' })
    copy.focus()
    fireEvent.click(more)
    expect(copy.closest('.ribbon-cluster').hidden).toBe(true)
    expect(document.activeElement).toBe(more)
    const prompt = document.createElement('input')
    document.body.appendChild(prompt)
    prompt.focus()
    // The engine cancel path calls focus directly, which cannot emit a
    // focusin event for a display:none tool in a browser.
    copy.focus()
    expect(document.activeElement).toBe(more)
    prompt.remove()
  })

  it('moves focus when reduced capacity hides the focused panel and restores native focus on unmount', () => {
    const clusters = [{ id: 'clipboard', label: 'Clipboard', tools: [{ id: 'copy', label: 'Copy' }] }]
    const { rerender, unmount } = render(<DraftingRibbon clusters={clusters} visiblePanelCount={1} />)
    const copy = screen.getByRole('button', { name: 'Copy' })
    copy.focus()
    rerender(<DraftingRibbon clusters={clusters} visiblePanelCount={0} />)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'More panels' }))
    unmount()
    expect(Object.hasOwn(copy, 'focus')).toBe(false)
  })

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
