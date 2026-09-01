// data-instant (W0 craft #7): hotkey-driven surface changes must land on the
// frame the key was pressed. A key handler calls markInstant() BEFORE its
// state change; the [data-instant] CSS rule zeroes every transition/animation
// for that committed frame, and the attribute clears on a nested rAF so
// pointer-driven versions of the identical change keep their register motion.
// Idempotent and reentrant: overlapping calls extend the same frame window.
let pending = 0

export function markInstant() {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.instant = '1'
  const token = ++pending
  // Nested rAF (panel W0): a single rAF can fire before the browser
  // finalizes the style diff for the frame the attribute was meant to
  // cover, re-enabling transitions too early. The inner frame guarantees
  // the suppressed frame has committed before the attribute clears.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      // only the LAST caller clears, so back-to-back hotkeys stay instant
      if (token === pending) delete document.documentElement.dataset.instant
    })
  })
}
