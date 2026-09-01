/**
 * W2a mechanical dedupe: pins the roving-tablist keydown behavior both
 * shells shipped before the extraction (site/ToolCast.jsx's `moveTab` and
 * components/ProductSurfaceTabs.jsx's `moveProductTab`) — verified against
 * git history to be behaviorally identical (both focus AND click the target
 * tab), so this module takes no caller-differing option.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { moveRovingTab } from './roving.js'

function buildTabs(n) {
  const container = document.createElement('div')
  container.setAttribute('role', 'tablist')
  const tabs = []
  for (let i = 0; i < n; i += 1) {
    const tab = document.createElement('button')
    tab.setAttribute('role', 'tab')
    tab.dataset.index = String(i)
    container.appendChild(tab)
    tabs.push(tab)
  }
  document.body.appendChild(container)
  return { container, tabs }
}

function keyEvent(key, currentTarget) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  Object.defineProperty(event, 'currentTarget', { value: currentTarget })
  return event
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('moveRovingTab', () => {
  it('ignores keys outside the roving set', () => {
    const { container, tabs } = buildTabs(3)
    tabs[0].focus()
    moveRovingTab(keyEvent('a', container))
    expect(document.activeElement).toBe(tabs[0])
  })

  it('moves focus right and left with wraparound', () => {
    const { container, tabs } = buildTabs(3)
    tabs[0].focus()
    moveRovingTab(keyEvent('ArrowRight', container))
    expect(document.activeElement).toBe(tabs[1])
    moveRovingTab(keyEvent('ArrowLeft', container))
    expect(document.activeElement).toBe(tabs[0])
    // wrap backward past the start
    moveRovingTab(keyEvent('ArrowLeft', container))
    expect(document.activeElement).toBe(tabs[2])
    // wrap forward past the end
    moveRovingTab(keyEvent('ArrowRight', container))
    expect(document.activeElement).toBe(tabs[0])
  })

  it('Home/End jump to the first/last tab', () => {
    const { container, tabs } = buildTabs(4)
    tabs[2].focus()
    moveRovingTab(keyEvent('Home', container))
    expect(document.activeElement).toBe(tabs[0])
    moveRovingTab(keyEvent('End', container))
    expect(document.activeElement).toBe(tabs[3])
  })

  it('treats no current active tab as index 0 (Math.max clamp)', () => {
    const { container, tabs } = buildTabs(3)
    document.body.focus() // activeElement is not one of the tabs
    moveRovingTab(keyEvent('ArrowRight', container))
    expect(document.activeElement).toBe(tabs[1])
  })

  it('activates (clicks) the newly focused tab — both original callers relied on this', () => {
    const { container, tabs } = buildTabs(2)
    const clicked = []
    tabs.forEach((tab, i) => tab.addEventListener('click', () => clicked.push(i)))
    tabs[0].focus()
    moveRovingTab(keyEvent('ArrowRight', container))
    expect(document.activeElement).toBe(tabs[1])
    expect(clicked).toEqual([1])
  })

  it('does nothing when the tablist has no [role="tab"] children', () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    expect(() => moveRovingTab(keyEvent('ArrowRight', container))).not.toThrow()
  })

  it('calls preventDefault only when a roving key is handled', () => {
    const { container } = buildTabs(2)
    const handled = keyEvent('ArrowRight', container)
    let prevented = false
    handled.preventDefault = () => { prevented = true }
    moveRovingTab(handled)
    expect(prevented).toBe(true)

    const ignored = keyEvent('Tab', container)
    let ignoredPrevented = false
    ignored.preventDefault = () => { ignoredPrevented = true }
    moveRovingTab(ignored)
    expect(ignoredPrevented).toBe(false)
  })
})
