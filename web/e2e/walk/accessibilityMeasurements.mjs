// Optional observations only. The caller owns every assertion and keyboard action.
const timeout = 1500
const mismatch = '__focus_mismatch__'
const identifier = /^[A-Za-z][A-Za-z0-9_.:-]{0,79}$/
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const safeKey = (key) => typeof key === 'string' && identifier.test(key) && key !== mismatch
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && /^accessibility:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)

// Deliberately a small snapshot subset: one root, an optional JSON-quoted name,
// and optional indented children. Other YAML/ARIA syntax stays unmeasured.
function rootSemantics(snapshot) {
  if (typeof snapshot !== 'string' || snapshot.length > 65536) return {}
  const lines = snapshot.replace(/\r\n/g, '\n').trimEnd().split('\n')
  const root = /^- ([a-z][a-z0-9-]{0,63})(?: ("(?:[^"\\\r\n]|\\.)*"))?(:)?$/.exec(lines[0])
  if (!root || lines.slice(1).some((line) => line.trim() && !/^ {2,}\S/.test(line))
    || (lines.length > 1 && !root[3])) return {}
  let name = ''
  try { if (root[2] !== undefined) name = JSON.parse(root[2]) } catch { return {} }
  const result = { role: root[1] }
  if (name.length <= 200 && !credential.test(name)) result.name = name
  return result
}

export function createAccessibilityMeasurementCapture({ page, dialog, openTarget, returnTarget, reference } = {}) {
  if (!safeReference(reference) || !safeKey(openTarget?.key) || !safeKey(returnTarget?.key)) {
    throw new TypeError('Expected a safe accessibility reference and declared target keys')
  }
  // Copy declarations: subsequent caller mutations must not rewrite the trace.
  const targets = { open: { key: openTarget.key, locator: openTarget.locator },
    return: { key: returnTarget.key, locator: returnTarget.locator } }
  const measured = { reference, overlay: {}, focus: {}, diagnostics: { tabSamples: 0 } }
  const owned = new Set()
  let original, openHandle, returnHandle
  let started = false, ready = false, escaped = false, disposed = false
  let tabFailed = false, tabIncomplete = false
  let pending = Promise.resolve()
  const observe = async (read) => { try { return await read() } catch { return undefined } }
  const enqueue = (work) => {
    pending = pending.then(() => disposed ? undefined : observe(work))
    return pending
  }
  async function acquire(locator, visible = false) {
    if (await locator.count() !== 1 || (visible && !await locator.isVisible())) return undefined
    const handle = await locator.elementHandle({ timeout })
    if (handle) owned.add(handle)
    return handle || undefined
  }
  async function focusKey(target, key) {
    if (!target) return undefined
    return page.evaluate(({ target, key, mismatch }) =>
      target.isConnected && document.activeElement === target ? key : mismatch,
    { target, key, mismatch })
  }
  return {
    opened() {
      return enqueue(async () => {
        if (started) return
        started = true
        original = await observe(() => acquire(dialog, true))
        if (!original) return
        const connected = await observe(() => page.evaluate((target) => target.isConnected, original))
        if (connected !== true) return
        ready = true
        openHandle = await observe(() => acquire(targets.open.locator))
        returnHandle = await observe(() => acquire(targets.return.locator))
        const actual = await observe(() => focusKey(openHandle, targets.open.key))
        if (actual === targets.open.key || actual === mismatch) {
          measured.focus.openTarget = targets.open.key
          measured.focus.actualOpenTarget = actual
        }
        const semantics = await observe(async () => {
          const snapshot = await dialog.ariaSnapshot({ timeout })
          // ariaSnapshot is a Locator API. Verify it still addresses the retained
          // element before attributing its root semantics to this trace.
          const candidate = await acquire(dialog, true)
          if (!candidate || await page.evaluate(({ original, candidate }) =>
            original.isConnected && original === candidate, { original, candidate }) !== true) return {}
          return rootSemantics(snapshot)
        })
        if (semantics) Object.assign(measured.overlay, semantics)
        const modal = await observe(() => page.evaluate((target) => {
          if (!target.isConnected) return undefined
          const value = target.getAttribute('aria-modal')
          if (value === 'true') return true
          if (value === 'false') return false
          if (value === null) return target.matches(':modal')
          return undefined
        }, original))
        if (typeof modal === 'boolean') measured.overlay.modal = modal
      })
    },
    afterTab() {
      return enqueue(async () => {
        if (!ready || escaped) return
        const inside = await observe(() => page.evaluate((target) =>
          target.isConnected && target.contains(document.activeElement), original))
        if (typeof inside !== 'boolean') {
          tabIncomplete = true
          delete measured.focus.tabRemainsInside
          return
        }
        measured.diagnostics.tabSamples += 1
        tabFailed ||= !inside
        if (!tabIncomplete) measured.focus.tabRemainsInside = !tabFailed
      })
    },
    afterEscape() {
      return enqueue(async () => {
        if (!ready || escaped) return
        escaped = true
        const deadline = Date.now() + 500
        const readClosed = () => observe(async () => {
          const connected = await page.evaluate((target) => target.isConnected, original)
          if (connected === false) return true
          if (connected === true) {
            const dismissed = await page.evaluate((target) =>
              target.closest('[inert], [aria-hidden="true"]') !== null, original)
            if (dismissed === true) return true
            const visible = await original.isVisible()
            return typeof visible === 'boolean' ? !visible : undefined
          }
          return undefined
        })
        let closed = await readClosed()
        while (closed === false && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, Math.min(25, deadline - Date.now())))
          closed = await readClosed()
        }
        if (typeof closed === 'boolean') measured.focus.escapeClosed = closed
        const actual = await observe(() => focusKey(returnHandle, targets.return.key))
        if (actual === targets.return.key || actual === mismatch) {
          measured.focus.returnTarget = targets.return.key
          measured.focus.actualReturnTarget = actual
        }
      })
    },
    async dispose() {
      disposed = true
      await pending
      // Remove ownership before awaiting, so concurrent/repeated disposal is safe.
      const handles = [...owned]
      owned.clear()
      await Promise.all(handles.map((handle) => observe(() => handle.dispose())))
    },
    result() { return JSON.parse(JSON.stringify(measured)) },
  }
}
