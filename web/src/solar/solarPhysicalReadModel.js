import { isSolarReadResult } from './solarReadResultModel.js'

export const PHYSICAL_READ_TOOLS = Object.freeze(['solar-physical-shade', 'solar-physical-export'])
export const PHYSICAL_EXPORT_FORMATS = Object.freeze(['terrain-csv', 'shade-azal-matrix', 'shade-sam', 'shade-per-panel'])
const HASH = /^[0-9a-f]{64}$/
const DRAWING = /^[a-z0-9][a-z0-9_-]{0,62}$/
const refused = (reason) => ({ ok: false, reason })

export function sameHead(a, b) {
  if (!a || !b || !HASH.test(a.state?.artifact_id ?? '') || !HASH.test(b.state?.artifact_id ?? '')) return false
  const keys = ['schema', 'drawing_id', 'project_id', 'index', 'parent']
  const refs = ['schema', 'artifact_id', 'media_type', 'filename', 'byte_length', 'content_sha256', 'source_version', 'download']
  return keys.every((k) => a[k] === b[k]) && refs.every((k) => a.state[k] === b.state[k])
}

export function buildPhysicalRead({ toolName, drawingId, head, format } = {}) {
  try {
    if (!PHYSICAL_READ_TOOLS.includes(toolName) || typeof drawingId !== 'string' || !DRAWING.test(drawingId)) return refused('invalid')
    if (!head) return refused('no_head')
    if (head.drawing_id !== drawingId || typeof head.state?.artifact_id !== 'string' || !HASH.test(head.state.artifact_id)) return refused('invalid')
    if (toolName === 'solar-physical-shade') return { ok: true, params: { drawing_id: drawingId } }
    if (!PHYSICAL_EXPORT_FORMATS.includes(format)) return refused('invalid')
    return { ok: true, params: { drawing_id: drawingId, expected_head: head.state.artifact_id, format } }
  } catch { return refused('invalid') }
}

export function physicalReadData(envelope, binding) {
  try {
    if (envelope?.ok !== true) return refused(envelope?.error?.reason_code === 'PHYSICAL_EXPORT_HEAD_MOVED' ? 'head_moved' : 'refused')
    const data = envelope.result
    const { toolName, drawingId, projectId = null, drawingVersion, head, format } = binding
    if (!PHYSICAL_READ_TOOLS.includes(toolName) || !isSolarReadResult(data) || data.tool !== toolName
      || data.drawing_id !== drawingId || data.project_id !== (projectId ?? head?.project_id)
      || data.source_version !== drawingVersion || data.drawing_changed !== false
      || (envelope.tool !== undefined && envelope.tool !== toolName)
      || (envelope.drawing_changed !== undefined && envelope.drawing_changed !== false)
      || !data.output || typeof data.output !== 'object' || Array.isArray(data.output)) return refused('unreadable')
    const output = data.output
    if (!sameHead(output.head, head)) return refused('head_moved')
    if (toolName === 'solar-physical-shade') {
      if (output.schema !== 'leaf.solar-physical-shade.v1') return refused('unreadable')
    } else {
      if (output.summary?.schema !== 'leaf.solar-physical-export.v1' || !PHYSICAL_EXPORT_FORMATS.includes(format)
        || output.summary.format !== format) return refused('unreadable')
      if (!sameHead(output.summary.head, head) || !sameHead(output.head, output.summary.head)) return refused('head_moved')
    }
    return { ok: true, data }
  } catch { return refused('unreadable') }
}

export function physicalReadReason(code) {
  return code === 'PHYSICAL_EXPORT_HEAD_MOVED'
    ? 'The terrain changed, so review the refreshed preview before running again'
    : 'The terrain request stopped'
}
