// Optional observations only: failure to measure leaves the persona UNKNOWN.
// Return the diagnostic alongside measurements so persona fragments keep their existing shape.
export async function collectMobileTouchMeasurements({ page, handle, viewport, reference, locatorDescription = {} } = {}) {
  try {
    if (viewport !== 'phone' || !handle || typeof reference !== 'string'
      || !/^mobile-touch:[A-Za-z][A-Za-z0-9_.:-]*\[[A-Za-z0-9_.:-]+\]@phone$/.test(reference)) return null
    const box = await handle.boundingBox()
    if (!box || !['x', 'y', 'width', 'height'].every((key) => Number.isFinite(box[key]))
      || box.width < 0 || box.height < 0) return null
    const sample = await page.evaluate(({ target, box }) => {
      if (!target.isConnected) return null
      const width = window.innerWidth
      const height = window.innerHeight
      const inside = (x, y) => x >= 0 && y >= 0 && x < width && y < height
      const hits = new Map()
      const hitsTarget = (x, y) => {
        const hit = document.elementFromPoint(x, y)
        hits.set(`${x},${y}`, hit)
        return hit !== null && (hit === target || target.contains(hit))
      }
      const cx = box.x + box.width / 2
      const cy = box.y + box.height / 2
      const corners = [
        [box.x + 2, box.y + 2],
        [box.x + box.width - 2, box.y + 2],
        [box.x + 2, box.y + box.height - 2],
        [box.x + box.width - 2, box.y + box.height - 2],
      ]
      const measurements = {
        input: { touch: navigator.maxTouchPoints > 0, coarsePointer: window.matchMedia('(pointer: coarse)').matches },
        viewport: { width, height, deviceScaleFactor: window.devicePixelRatio },
        control: {
          reachable: inside(cx, cy) && hitsTarget(cx, cy),
          clipped: box.x < 0 || box.y < 0 || box.x + box.width > width || box.y + box.height > height
            || corners.some(([x, y]) => inside(x, y) && !hitsTarget(x, y)),
        },
      }
      let diagnostic
      if (measurements.control.clipped) {
        try {
          const style = window.getComputedStyle(target)
          const nameOf = (element) => {
            const labelledBy = element.getAttribute('aria-labelledby')
            if (labelledBy) {
              const labels = labelledBy.trim().split(/\s+/).map(id => document.getElementById(id)).filter(Boolean)
              if (labels.length) return labels.map(label => label.textContent || '').join(' ')
            }
            const label = element.getAttribute('aria-label')
            if (label?.trim()) return label
            if (element.labels?.length) return Array.from(element.labels, label => label.textContent || '').join(' ')
            const tag = element.tagName.toLowerCase()
            if (tag === 'img' || (tag === 'input' && element.type === 'image')) return element.getAttribute('alt') || ''
            if (tag === 'input' && ['button', 'submit', 'reset'].includes(element.type)) return element.value || ''
            const role = element.getAttribute('role') || ''
            if (['button', 'a', 'summary', 'option'].includes(tag)
              || ['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'option', 'heading'].includes(role)) {
              return element.textContent || ''
            }
            return element.getAttribute('title') || ''
          }
          diagnostic = {
            corners: corners.map(([x, y], index) => {
              const offscreen = !inside(x, y)
              const key = `${x},${y}`
              const hit = offscreen ? null : hits.has(key) ? hits.get(key) : document.elementFromPoint(x, y)
              return {
                position: ['top-left', 'top-right', 'bottom-left', 'bottom-right'][index],
                hit: hit ? { tag: hit.tagName.toLowerCase(), role: hit.getAttribute('role') || '',
                  name: nameOf(hit).trim().slice(0, 60) } : null,
                insideTarget: hit !== null && (hit === target || target.contains(hit)), offscreen,
              }
            }),
            borderRadiusPx: { topLeft: parseFloat(style.borderTopLeftRadius), topRight: parseFloat(style.borderTopRightRadius),
              bottomLeft: parseFloat(style.borderBottomLeftRadius), bottomRight: parseFloat(style.borderBottomRightRadius) },
          }
        } catch { /* A diagnostic read must not discard a valid measurement. */ }
      }
      return { ...measurements, diagnostic }
    }, { target: handle, box })
    if (!sample || !Object.values(sample.viewport).every((value) => Number.isFinite(value) && value > 0)) return null
    const rect = Object.fromEntries(['x', 'y', 'width', 'height'].map((key) => [key, box[key]]))
    const measurements = { reference, input: sample.input, viewport: sample.viewport,
      target: { role: typeof locatorDescription.role === 'string' ? locatorDescription.role : '',
        name: typeof locatorDescription.name === 'string' ? locatorDescription.name : '', box: rect },
      control: { ...sample.control, rect } }
    return { measurements, ...(sample.diagnostic ? { diagnostic: sample.diagnostic } : {}) }
  } catch { return null }
}

// Keep activation outside the optional-evidence catches so its verdict is preserved.
export async function activateWithMobileTouchMeasurements({ page, locator, probe, viewport, addFragment, activate, locatorDescription, evidence }) {
  let handle
  try {
    if (viewport === 'phone' && (!probe.locator.trigger || probe.locator.trigger === 'click')
      && !probe.locator.keyboardAction && probe.assertion.kind !== 'disabled_with_reason'
      && probe.assertion.kind !== 'navigates'
      && !['script-file-picker', 'signed-out-session', 'viewer-overview-pan'].includes(probe.assertion.target)
      && await locator.count() === 1) handle = await locator.elementHandle()
  } catch { /* A missing or ambiguous handle supplies no measurements. */ }
  let before
  try {
    if (handle) before = await collectMobileTouchMeasurements({ page, handle, viewport,
      reference: `mobile-touch:${probe.featureId}[${probe.state}]@${viewport}`,
      locatorDescription: locatorDescription || probe.locator.phone || probe.locator })
  } catch { /* Optional evidence never changes the functional verdict. */ }
  await activate()
  try {
    if (!handle) return
    const after = await collectMobileTouchMeasurements({ page, handle, viewport,
      reference: `mobile-touch:${probe.featureId}[${probe.state}]@${viewport}`,
      locatorDescription: locatorDescription || probe.locator.phone || probe.locator })
    const sample = after || before
    if (sample) {
      addFragment('mobile-touch', sample.measurements)
      if (sample.measurements.control.clipped && sample.diagnostic && evidence) {
        evidence.touchClipDiagnostic = { ...evidence.touchClipDiagnostic,
          [sample.measurements.reference]: sample.diagnostic }
      }
    }
  } catch { /* Optional evidence never changes the functional verdict. */ }
}
