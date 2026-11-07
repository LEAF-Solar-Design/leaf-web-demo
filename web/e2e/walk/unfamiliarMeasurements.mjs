// Optional read-only observations. Functional actions and verdicts belong to the caller.
const timeout = 1500
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const dangerous = (value) => value.split(/[.:[\]-]/).some((part) =>
  ['__proto__', 'prototype', 'constructor'].includes(part))
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && /^unfamiliar:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)
  && !/[\r\n]/.test(value) && !dangerous(value) && !credential.test(value)

// Only a single root with a JSON-quoted name and optional indented children.
// Unsupported snapshot syntax is missing evidence, never a guessed label.
function rootSemantics(snapshot) {
  if (typeof snapshot !== 'string' || snapshot.length > 65536) return {}
  const lines = snapshot.replace(/\r\n/g, '\n').trimEnd().split('\n')
  const root = /^- ([a-z][a-z0-9-]{0,63})(?: ("(?:[^"\\\r\n]|\\.)*"))?(:)?$/.exec(lines[0])
  if (!root || dangerous(root[1])
    || lines.slice(1).some((line) => line.trim() && !/^ {2,}\S/.test(line))
    || (lines.length > 1 && !root[3])) return {}
  let name = ''
  try { if (root[2] !== undefined) name = JSON.parse(root[2]) } catch { return {} }
  const result = { role: root[1] }
  if (name.length <= 200 && !credential.test(name)) result.name = name
  return result
}

// nextStepFound must come from a prior successful real labeled navigation action.
// This observer cannot establish discoverability from DOM reachability alone.
export async function collectUnfamiliarMeasurements(options) {
  const owned = new Set()
  const observe = async (read) => { try { return await read() } catch { return undefined } }
  try {
    if (!options || typeof options !== 'object') return null
    // Read only own data properties. Do not invoke caller getters or serialize inputs.
    const input = {}
    for (const key of ['page', 'primaryLocator', 'navigationLocator', 'reference', 'nextStepFound']) {
      const descriptor = Object.getOwnPropertyDescriptor(options, key)
      if (descriptor && !Object.hasOwn(descriptor, 'value')) return null
      input[key] = descriptor?.value
    }
    const { page, primaryLocator, navigationLocator, reference, nextStepFound } = input
    if (!safeReference(reference) || !page || !primaryLocator || !navigationLocator) return null
    const result = { reference, primary: {}, navigation: {} }
    if (typeof nextStepFound === 'boolean') result.navigation.nextStepFound = nextStepFound
    async function acquire(locator) {
      if (await locator.count() !== 1) return undefined
      const handle = await locator.elementHandle({ timeout })
      if (handle) owned.add(handle)
      return handle || undefined
    }
    async function same(locator, original) {
      const candidate = await acquire(locator)
      return !!candidate && await page.evaluate(({ original, candidate }) =>
        original.isConnected && original === candidate, { original, candidate }) === true
    }
    async function semantics(locator, original) {
      if (!await same(locator, original)) return {}
      const snapshot = await locator.ariaSnapshot({ timeout })
      if (!await same(locator, original)) return {}
      return rootSemantics(snapshot)
    }
    const primary = await observe(() => acquire(primaryLocator))
    const navigation = await observe(() => acquire(navigationLocator))
    if (primary) {
      Object.assign(result.primary, await observe(() => semantics(primaryLocator, primary)))
      const reachable = await observe(async () => {
        const visible = await primary.isVisible()
        if (typeof visible !== 'boolean') return undefined
        return page.evaluate(({ target, visible }) => {
          if (!target.isConnected) return undefined
          if (!visible) return false
          const rect = target.getBoundingClientRect()
          const width = window.innerWidth, height = window.innerHeight
          if (![rect.x, rect.y, rect.width, rect.height, width, height].every(Number.isFinite)
            || width <= 0 || height <= 0 || rect.width < 0 || rect.height < 0) return undefined
          const style = window.getComputedStyle(target)
          if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)
            || rect.width === 0 || rect.height === 0) return false
          const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2
          if (x < 0 || y < 0 || x >= width || y >= height) return false
          const hit = document.elementFromPoint(x, y)
          return hit !== null && (hit === target || target.contains(hit))
        }, { target: primary, visible })
      })
      if (typeof reachable === 'boolean') result.primary.reachable = reachable
    }
    if (navigation) Object.assign(result.navigation,
      await observe(() => semantics(navigationLocator, navigation)))
    // Only new records and safe primitives escape; no handles or input objects survive.
    return result
  } catch { return null } finally {
    await Promise.all([...owned].map((handle) => observe(() => handle.dispose())))
  }
}
