// Optional observations only: failure to measure leaves the persona UNKNOWN.
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
      const hitsTarget = (x, y) => {
        const hit = document.elementFromPoint(x, y)
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
      return {
        input: { touch: navigator.maxTouchPoints > 0, coarsePointer: window.matchMedia('(pointer: coarse)').matches },
        viewport: { width, height, deviceScaleFactor: window.devicePixelRatio },
        control: {
          reachable: inside(cx, cy) && hitsTarget(cx, cy),
          clipped: box.x < 0 || box.y < 0 || box.x + box.width > width || box.y + box.height > height
            || corners.some(([x, y]) => inside(x, y) && !hitsTarget(x, y)),
        },
      }
    }, { target: handle, box })
    if (!sample || !Object.values(sample.viewport).every((value) => Number.isFinite(value) && value > 0)) return null
    const rect = Object.fromEntries(['x', 'y', 'width', 'height'].map((key) => [key, box[key]]))
    return { reference, input: sample.input, viewport: sample.viewport,
      target: { role: typeof locatorDescription.role === 'string' ? locatorDescription.role : '',
        name: typeof locatorDescription.name === 'string' ? locatorDescription.name : '', box: rect },
      control: { ...sample.control, rect } }
  } catch { return null }
}

// Keep activation outside the optional-evidence catches so its verdict is preserved.
export async function activateWithMobileTouchMeasurements({ page, locator, probe, viewport, addFragment, activate }) {
  let handle
  try {
    if (viewport === 'phone' && (!probe.locator.trigger || probe.locator.trigger === 'click')
      && !probe.locator.keyboardAction && probe.assertion.kind !== 'disabled_with_reason'
      && probe.assertion.kind !== 'navigates'
      && !['script-file-picker', 'signed-out-session', 'viewer-overview-pan'].includes(probe.assertion.target)
      && await locator.count() === 1) handle = await locator.elementHandle()
  } catch { /* A missing or ambiguous handle supplies no measurements. */ }
  await activate()
  try {
    if (!handle) return
    const measurements = await collectMobileTouchMeasurements({ page, handle, viewport,
      reference: `mobile-touch:${probe.featureId}[${probe.state}]@${viewport}`,
      locatorDescription: probe.locator.phone || probe.locator })
    if (measurements) addFragment('mobile-touch', measurements)
  } catch { /* Optional evidence never changes the functional verdict. */ }
}
