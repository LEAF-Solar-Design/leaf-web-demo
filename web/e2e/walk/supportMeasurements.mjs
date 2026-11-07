// Optional, read-only evidence. The caller owns error triggering and assertions.
const timeout = 1500
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const dangerous = new Set(['__proto__', 'prototype', 'constructor'])
const safeText = (value, limit) => typeof value === 'string' && value.length <= limit && !credential.test(value)
const observe = async (read) => { try { return await read() } catch { return undefined } }

// Match the deliberately bounded root-only subset used by accessibility capture.
function rootName(snapshot) {
  if (typeof snapshot !== 'string' || snapshot.length > 65536) return undefined
  const lines = snapshot.replace(/\r\n/g, '\n').trimEnd().split('\n')
  const root = /^- ([a-z][a-z0-9-]{0,63})(?: ("(?:[^"\\\r\n]|\\.)*"))?(:)?$/.exec(lines[0])
  if (!root || lines.slice(1).some((line) => line.trim() && !/^ {2,}\S/.test(line))
    || (lines.length > 1 && !root[3])) return undefined
  try {
    const name = root[2] === undefined ? '' : JSON.parse(root[2])
    return safeText(name, 200) ? name : undefined
  } catch { return undefined }
}

export async function collectSupportMeasurements(options = {}) {
  const owned = new Set()
  try {
    // Read declarations as data, without invoking caller getters or coercion.
    if (!options || typeof options !== 'object' || Array.isArray(options)) return null
    const descriptors = Object.getOwnPropertyDescriptors(options)
    if (Reflect.ownKeys(descriptors).some((key) => typeof key !== 'string'
      || dangerous.has(key) || !Object.hasOwn(descriptors[key], 'value'))) return null
    const value = (key) => Object.hasOwn(descriptors, key) ? descriptors[key].value : undefined
    const page = value('page'), reference = value('reference'), errorObserved = value('errorObserved')
    if (typeof reference !== 'string' || reference.length > 200
      || !/^support:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(reference)
      || reference.split(/[.:\[\]@-]/).some((part) => dangerous.has(part))
      || typeof errorObserved !== 'boolean') return null
    const measured = { reference, error: { observed: errorObserved } }
    if (!errorObserved) return measured
    const errorLocator = value('errorLocator'), explanationLocator = value('explanationLocator')
    const recoveryLocator = value('recoveryLocator')
    async function acquire(locator) {
      if (await locator.count() !== 1 || await locator.isVisible() !== true) return undefined
      const handle = await locator.elementHandle({ timeout })
      if (!handle) return undefined
      owned.add(handle)
      return await handle.isVisible() === true ? handle : undefined
    }
    const error = await observe(() => acquire(errorLocator))
    const explanation = await observe(() => acquire(explanationLocator))
    const recovery = await observe(() => acquire(recoveryLocator))
    async function readText(target, distinctFrom) {
      if (!target || await target.isVisible() !== true) return undefined
      const text = await page.evaluate(({ target, distinctFrom }) => {
        if (!target.isConnected || target === distinctFrom) return undefined
        return target.innerText
      }, { target, distinctFrom })
      return safeText(text, 2000) ? text : undefined
    }
    const message = await observe(() => readText(error))
    const whatHappened = await observe(() => readText(explanation, error))
    if (message !== undefined) measured.error.message = message
    if (whatHappened !== undefined) measured.error.whatHappened = whatHappened
    if (recovery) {
      const name = await observe(async () => {
        const snapshot = await recoveryLocator.ariaSnapshot({ timeout })
        // A Locator may now refer to a replacement. Attribute no name to it.
        const candidate = await acquire(recoveryLocator)
        if (!candidate || await recovery.isVisible() !== true
          || await page.evaluate(({ original, candidate }) => original.isConnected
            && original === candidate, { original: recovery, candidate }) !== true) return undefined
        return rootName(snapshot)
      })
      if (name !== undefined) measured.error.recoveryName = name
      const reachable = await observe(async () => {
        if (await recovery.isVisible() !== true) return undefined
        return page.evaluate((target) => {
          if (!target.isConnected) return undefined
          const box = target.getBoundingClientRect()
          const width = window.innerWidth, height = window.innerHeight
          if (![box.x, box.y, box.width, box.height, width, height].every(Number.isFinite)
            || box.width < 0 || box.height < 0 || width <= 0 || height <= 0) return undefined
          const x = box.x + box.width / 2, y = box.y + box.height / 2
          if (box.width === 0 || box.height === 0 || x < 0 || y < 0 || x >= width || y >= height) return false
          const hit = document.elementFromPoint(x, y)
          return hit !== null && (hit === target || target.contains(hit))
        }, recovery)
      })
      if (typeof reachable === 'boolean') measured.error.recoveryReachable = reachable
    }
    return measured
  } catch { return null } finally {
    await Promise.all([...owned].map((handle) => observe(() => handle.dispose())))
  }
}
