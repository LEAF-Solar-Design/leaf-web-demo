// Optional DOM observations. This collector never performs the task or supplies human feedback.
const timeout = 1500
const identifier = /^[A-Za-z][A-Za-z0-9_-]*(?:[.:][A-Za-z0-9_-]+)*$/
const dangerous = new Set(['__proto__', 'prototype', 'constructor'])
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const safeId = (value) => typeof value === 'string' && value.length <= 80
  && identifier.test(value) && !value.split(/[.:]/).some((part) => dangerous.has(part))
const data = (object, key) => {
  if (!object || typeof object !== 'object') return undefined
  const descriptor = Object.getOwnPropertyDescriptor(object, key)
  return descriptor && Object.hasOwn(descriptor, 'value') ? descriptor.value : undefined
}
// Playwright methods can live on prototypes; accessors are not read capabilities.
function read(object, method, ...args) {
  for (let owner = object; owner && typeof owner === 'object'; owner = Object.getPrototypeOf(owner)) {
    const descriptor = Object.getOwnPropertyDescriptor(owner, method)
    if (descriptor) return typeof descriptor.value === 'function'
      ? Reflect.apply(descriptor.value, object, args) : undefined
  }
}
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && /^ui-ux-dev:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)
  && !credential.test(value) && !/(?:^|[.:\[])\b(?:__proto__|prototype|constructor)\b/.test(value)

export async function collectUiUxMeasurements(options = {}) {
  const owned = new Set()
  try {
    const page = data(options, 'page'), declarations = data(options, 'controls')
    const completionLocator = data(options, 'completionLocator')
    const observed = data(options, 'completionObserved'), reference = data(options, 'reference')
    if (!safeReference(reference) || typeof observed !== 'boolean' || !Array.isArray(declarations)
      || declarations.length < 2 || declarations.length > 100) return null
    // Read only own data properties; never invoke declaration getters or coerce identifiers.
    const controls = []
    for (let i = 0; i < declarations.length; i += 1) {
      const declaration = data(declarations, String(i))
      const id = data(declaration, 'id'), locator = data(declaration, 'locator')
      if (!safeId(id) || !locator || controls.some((control) => control.id === id)) return null
      controls.push({ id, locator })
    }
    async function acquire(locator) {
      if (!locator || await read(locator, 'count') !== 1) return null
      const handle = await read(locator, 'elementHandle', { timeout })
      if (!handle) return null
      owned.add(handle)
      if (await read(locator, 'count') !== 1 || await read(handle, 'isVisible') !== true) return null
      return handle
    }
    const targets = []
    for (const control of controls) {
      const handle = await acquire(control.locator)
      if (!handle) return null
      targets.push(handle)
    }
    const completion = await acquire(completionLocator)
    if (!completion) return null
    const sample = await read(page, 'evaluate', ({ targets, completion }) => {
      if (new Set(targets).size !== targets.length) return null
      const rect = (target) => {
        if (!target.isConnected) return null
        const style = window.getComputedStyle(target)
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'
          || style.opacity === '0') return null
        const box = target.getBoundingClientRect()
        const { x, y, width, height } = box
        if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null
        return { x, y, width, height }
      }
      const rects = targets.map(rect), box = rect(completion)
      if (rects.some((value) => !value) || !box) return null
      const width = window.innerWidth, height = window.innerHeight
      if (![width, height].every((value) => Number.isFinite(value) && value > 0)) return null
      const acknowledgement = completion.innerText
      if (typeof acknowledgement !== 'string') return null
      const x = box.x + box.width / 2, y = box.y + box.height / 2
      const inside = x >= 0 && y >= 0 && x < width && y < height
      const hit = inside ? document.elementFromPoint(x, y) : null
      return { rects, acknowledgement,
        inPlace: inside && hit !== null && (hit === completion || completion.contains(hit)) }
    }, { targets, completion })
    const rects = data(sample, 'rects'), acknowledgement = data(sample, 'acknowledgement')
    const inPlace = data(sample, 'inPlace')
    if (!Array.isArray(rects) || rects.length !== controls.length || typeof inPlace !== 'boolean'
      || typeof acknowledgement !== 'string' || acknowledgement.length > 2000
      || credential.test(acknowledgement)) return null
    const measured = []
    for (let i = 0; i < controls.length; i += 1) {
      const source = data(rects, String(i)), box = { id: controls[i].id }
      for (const key of ['x', 'y', 'width', 'height']) {
        const value = data(source, key)
        if (!Number.isFinite(value) || ((key === 'width' || key === 'height') && value <= 0)) return null
        box[key] = value
      }
      measured.push(box)
    }
    return { reference, layout: { controls: measured },
      completion: { observed, acknowledgement, inPlace } }
  } catch { return null } finally {
    // Cleanup failures are optional-observer failures too; no exception or DOM text is logged.
    for (const handle of owned) {
      try { await read(handle, 'dispose') } catch { /* Best effort, once per acquired handle. */ }
    }
  }
}
