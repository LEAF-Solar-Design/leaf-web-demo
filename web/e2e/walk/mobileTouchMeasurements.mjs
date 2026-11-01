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
      const positions = ['top-left', 'top-right', 'bottom-left', 'bottom-right']
      const radiusKeys = ['topLeft', 'topRight', 'bottomLeft', 'bottomRight']
      let style
      let mode = 'rectangular'
      const radiiPx = Object.fromEntries(radiusKeys.map(key => [key, null]))
      try {
        style = window.getComputedStyle(target)
        const unsupported = (reason) => { if (!mode.startsWith('unresolved:')) mode = `unresolved:${reason}` }
        if (style.clipPath && style.clipPath !== 'none') unsupported('clip-path')
        if ([style.maskImage, style.webkitMaskImage, style.maskBorderSource, style.webkitMaskBoxImageSource]
          .some(value => value && value !== 'none')) unsupported('mask')
        if (style.transform && !['none', 'matrix(1, 0, 0, 1, 0, 0)',
          'matrix3d(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)'].includes(style.transform)) unsupported('transform')
        if ((style.translate && !['none', '0px', '0px 0px', '0px 0px 0px'].includes(style.translate))
          || (style.rotate && !['none', '0deg'].includes(style.rotate))
          || (style.scale && !['none', '1', '1 1', '1 1 1'].includes(style.scale))) unsupported('transform')
        for (const [index, value] of [style.borderTopLeftRadius, style.borderTopRightRadius,
          style.borderBottomLeftRadius, style.borderBottomRightRadius].entries()) {
          const parts = (value || '').trim().split(/\s+/)
          if (parts.some(part => part.includes('%'))) { unsupported('percentage-radius'); continue }
          if (parts.length > 2 || parts.some(part => !/^(?:\d+(?:\.\d+)?|\.\d+)px$|^0$/.test(part))) {
            unsupported('radius'); continue
          }
          const [horizontal, vertical = horizontal] = parts.map(Number.parseFloat)
          if (horizontal !== vertical) { unsupported('elliptical-radius'); continue }
          radiiPx[radiusKeys[index]] = Math.min(horizontal, box.width / 2, box.height / 2)
        }
        if (mode === 'rectangular' && Object.values(radiiPx).some(radius => radius > 0)) mode = 'rounded'
      } catch { mode = 'unresolved:style-unavailable' }
      const insets = radiusKeys.map((key, index) => {
        const inset = mode === 'rounded' ? Math.max(2, Math.ceil(radiiPx[key] * (1 - 1 / Math.sqrt(2))) + 2) : 2
        return { position: positions[index], x: inset, y: inset }
      })
      const corners = [
        [box.x + insets[0].x, box.y + insets[0].y],
        [box.x + box.width - insets[1].x, box.y + insets[1].y],
        [box.x + insets[2].x, box.y + box.height - insets[2].y],
        [box.x + box.width - insets[3].x, box.y + box.height - insets[3].y],
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
          if (!style) throw new Error('style unavailable')
          const identityOf = (element) => ({ tag: element.tagName.toLowerCase(),
            id: (element.id || '').slice(0, 60),
            classes: Array.from(element.classList || []).slice(0, 3).map(name => name.slice(0, 40)) })
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
              const parentPath = []
              for (let parent = hit?.parentElement; parent && parentPath.length < 3; parent = parent.parentElement) {
                parentPath.push(identityOf(parent))
                if (parent.tagName.toLowerCase() === 'body') break
              }
              return {
                position: ['top-left', 'top-right', 'bottom-left', 'bottom-right'][index],
                hit: hit ? { ...identityOf(hit), role: hit.getAttribute('role') || '',
                  name: nameOf(hit).trim().slice(0, 60) } : null,
                parentPath,
                insideTarget: hit !== null && (hit === target || target.contains(hit)), offscreen,
              }
            }),
            borderRadiusPx: { topLeft: parseFloat(style.borderTopLeftRadius), topRight: parseFloat(style.borderTopRightRadius),
              bottomLeft: parseFloat(style.borderBottomLeftRadius), bottomRight: parseFloat(style.borderBottomRightRadius) },
            geometry: { mode, insets, radiiPx },
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
