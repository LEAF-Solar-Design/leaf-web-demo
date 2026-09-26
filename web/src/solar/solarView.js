import { isWriteTool } from '../lib/toolRecord.js'

export const SOLAR_VIEW_SCHEMA = 'leaf.solar-tool-view.v1'

export function solarView(row) {
  const view = row?.solar
  if (view == null) return { state: 'absent' }
  if (typeof view !== 'object' || Array.isArray(view)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(view))
    || view.schema !== SOLAR_VIEW_SCHEMA || view.name !== row.name
    || !['run_read', 'run_write', 'solve'].includes(view.entitlement)
    || typeof view.family !== 'string'
    || !Number.isInteger(view.wave) || view.wave < 1 || view.wave > 5
    || !Number.isInteger(view.order) || view.order < 0 || view.order > 9999
    || !view.interaction || typeof view.interaction !== 'object' || Array.isArray(view.interaction)
    || !['form', 'none', 'pick'].includes(view.interaction.mode)) {
    return { state: 'invalid' }
  }
  return { state: 'valid', view }
}

export function solarFormKeys(row, view) {
  const bound = new Set()
  const steps = Array.isArray(view?.interaction?.pick) ? view.interaction.pick : []
  for (const step of steps) {
    if (typeof step?.key === 'string') bound.add(step.key)
    if (Array.isArray(step?.keys)) for (const key of step.keys) bound.add(key)
  }
  if (isWriteTool(row)) bound.add('drawing_id')
  return Object.keys(row?.params?.properties || {}).filter((key) => !bound.has(key))
}
