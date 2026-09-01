// Roving-tablist keydown helper for a `[role="tab"]` group: ArrowLeft/Right
// moves focus with wraparound, Home/End jump to the first/last tab, and the
// newly focused tab is activated (focus + click) immediately.
//
// Extracted from site/ToolCast.jsx (~line 208, `moveTab`) and
// components/ProductSurfaceTabs.jsx (~line 38, `moveProductTab`). The two
// were already behaviorally identical (both focus AND click the target tab;
// only the key-check style and the -1/clamp idiom differed cosmetically) —
// no caller-differing option needed.
const ROVING_KEYS = ['ArrowLeft', 'ArrowRight', 'Home', 'End']

export function moveRovingTab(event) {
  if (!ROVING_KEYS.includes(event.key)) return
  const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')]
  if (!tabs.length) return
  const current = Math.max(0, tabs.indexOf(document.activeElement))
  let next = current
  if (event.key === 'ArrowRight') next = (current + 1) % tabs.length
  if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  event.preventDefault()
  tabs[next].focus()
  tabs[next].click()
}
