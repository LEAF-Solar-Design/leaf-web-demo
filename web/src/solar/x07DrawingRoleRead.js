import { useEffect, useRef, useState } from 'react'

export const ROLE_PAGE_SIZE = 10
const MAX_SETS = 10000
const label = value => typeof value === 'string' && value.length > 0 && value.length <= 512
const row = value => {
  const bounds = value?.insertion_bounds
  if (!value || !label(value.block) || !label(value.layer) || !Number.isSafeInteger(value.count) || value.count < 0 ||
      !Array.isArray(bounds) || bounds.length !== 4 || !bounds.every(Number.isFinite) ||
      bounds[0] > bounds[2] || bounds[1] > bounds[3]) throw new TypeError('Invalid drawing roles')
  return { block: value.block, layer: value.layer, count: value.count, insertion_bounds: [...bounds] }
}

export function normalizeDrawingRoles(value) {
  if (value?.schema !== 'leaf.drawing-role-discovery.v1' || !value.module ||
      !Array.isArray(value.module.candidates) || value.module.candidates.length > MAX_SETS ||
      typeof value.module.competing !== 'boolean') throw new TypeError('Invalid drawing roles')
  const result = { schema: value.schema, module: {
    first_choice: value.module.first_choice == null ? null : row(value.module.first_choice),
    candidates: value.module.candidates.map(row), competing: value.module.competing,
  } }
  for (const role of ['roof', 'obstruction']) {
    if (!Array.isArray(value[role]) || value[role].length > MAX_SETS) throw new TypeError('Invalid drawing roles')
    result[role] = value[role].map(row)
  }
  return result
}

function current(predicate) {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}

export function useDrawingRoleRead({ projectClient, solarBinding, authorityKey, isCurrent }) {
  const key = JSON.stringify([authorityKey, solarBinding])
  const [attempt, retryAttempt] = useState(0)
  const [view, setView] = useState(null)
  const pending = useRef(null)
  const queued = useRef(false)
  const available = typeof authorityKey === 'string' && authorityKey.length > 0 &&
    typeof projectClient?.readContext === 'function' && current(isCurrent)
  useEffect(() => {
    queued.current = false
    if (!available) return
    const controller = new AbortController()
    pending.current = controller
    let active = true
    const live = () => active && !controller.signal.aborted && current(isCurrent)
    const identity = { key, client: projectClient, predicate: isCurrent, attempt, live }
    setView({ ...identity, status: 'loading' })
    Promise.resolve().then(() => live() ? projectClient.readContext({ signal: controller.signal }) : null)
      .then(response => {
        if (!live()) return
        if (response?.ok !== true) throw new Error('Drawing roles unavailable')
        const data = normalizeDrawingRoles(response.data?.role_discovery)
        if (live()) setView({ ...identity, status: 'ready', data })
      }).catch(() => {
        if (live()) setView({ ...identity, status: 'error' })
      }).finally(() => {
        if (pending.current === controller) pending.current = null
      })
    return () => {
      active = false
      controller.abort()
      if (pending.current === controller) pending.current = null
    }
  }, [key, projectClient, isCurrent, attempt, available])
  const matches = available && view?.key === key && view?.client === projectClient &&
    view?.predicate === isCurrent && view?.attempt === attempt && view.live()
  const retry = () => {
    if (!available || pending.current || queued.current) return
    queued.current = true
    retryAttempt(value => value + 1)
  }
  return { available, status: matches ? view.status : 'loading', data: matches ? view.data : null,
    isCurrent: matches ? view.live : () => false, retry, readKey: key + ':' + attempt }
}
