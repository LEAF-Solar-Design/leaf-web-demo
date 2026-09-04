// W4g-5c: the Clipboard panel keeps the reference's LAST seat. App renders
// an empty cluster there carrying a slot div, and the engine consumer
// portals the real tools into it. The slot lives INSIDE the tab-switched
// cluster list, so every switch away from the Draw tab and back unmounts the
// cluster and creates a NEW slot div; the first cut of useSlot resolved once
// by id and kept portaling into the detached original, so the panel rendered
// empty after the first tab switch and the proof's copy click found nothing.
// These rows pin the re-resolution below the e2e.
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'

import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'

class IdleWorker {
  constructor() { this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage() {}
  terminate() {}
}

/** A fresh seat object each time, the way App's memo hands DraftingRibbon a
 *  new cluster list on every tab change. */
const seat = () => ({
  id: 'clipboard', label: 'Clipboard', kind: 'group', tools: [],
  extra: <div id="cockpit-clipboard-slot" className="ribbon-cluster-tools" />,
})

const DRAW_TAB = ['draw', 'modify', 'clipboard']

function Tree({ clusters, panels }) {
  return (
    <DraftingRibbon clusters={clusters}>
      <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={panels} />
    </DraftingRibbon>
  )
}

function mount() {
  const createWorker = vi.fn(() => new IdleWorker())
  const ui = (clusters, panels) => (
    <EngineSessionProvider createWorker={createWorker}>
      <Tree clusters={clusters} panels={panels} />
    </EngineSessionProvider>
  )
  const view = render(ui([seat()], DRAW_TAB))
  return { ...view, ui }
}

const slot = () => document.getElementById('cockpit-clipboard-slot')
const toolsIn = (node) => (node ? [...node.querySelectorAll('[data-tool^="clipboard:"]')].map((el) => el.dataset.tool) : [])

afterEach(() => cleanup())

describe('W4g-5c the Clipboard seat', () => {
  it('the three tools render INSIDE the slot App seats last, in the reference\'s order', () => {
    mount()
    expect(toolsIn(slot())).toEqual(['clipboard:pasteClip', 'clipboard:cutClip', 'clipboard:copyClip'])
    // and nowhere else: the consumer renders no cluster of its own for them
    expect(document.querySelectorAll('[data-tool^="clipboard:"]')).toHaveLength(3)
    expect(document.querySelectorAll('.ribbon-cluster[data-group="clipboard"]')).toHaveLength(1)
  })

  it('survives a tab switch away and back, which re-creates the slot as a NEW node', () => {
    const { rerender, ui } = mount()
    const first = slot()
    expect(toolsIn(first)).toHaveLength(3)
    // Away from Draw: the cluster list no longer carries the seat and the
    // consumer shows no panels; the slot div is gone from the document.
    rerender(ui([], []))
    expect(slot()).toBeNull()
    expect(document.querySelectorAll('[data-tool^="clipboard:"]')).toHaveLength(0)
    // Back to Draw with a fresh seat object: a different DOM node.
    rerender(ui([seat()], DRAW_TAB))
    const second = slot()
    expect(second).not.toBeNull()
    expect(second).not.toBe(first)
    expect(first.isConnected).toBe(false)
    // The defect: tools portaled into the detached `first`, and `second`
    // stayed empty. The fix: they are in the live node.
    expect(toolsIn(second)).toHaveLength(3)
    expect(toolsIn(first)).toHaveLength(0)
  })

  it('does not thrash: re-rendering with the SAME slot node keeps the same target', () => {
    const { rerender, ui } = mount()
    const before = slot()
    // Several re-renders with the seat still mounted: the bailout keeps the
    // portal on the same node, so nothing is unmounted and remounted.
    const button = before.querySelector('[data-tool="clipboard:pasteClip"]')
    rerender(ui([seat()], DRAW_TAB))
    rerender(ui([seat()], DRAW_TAB))
    expect(slot()).toBe(before)
    expect(before.querySelector('[data-tool="clipboard:pasteClip"]')).toBe(button)
  })
})
