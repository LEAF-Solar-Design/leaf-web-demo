import { sentence } from '../components/SchemaForm.jsx'
import { isWriteTool } from './toolRecord.js'

export const NOT_PROVIDED = [
  'Engineering validation evidence for permit accuracy',
  'Assumptions and limitations',
  'Applicable code standards',
  'Reviewer sign-off',
]

function buildExplanation(tool) {
  const record = tool ?? {}
  const name = typeof record.name === 'string' && record.name.trim() ? record.name : 'Unnamed tool'
  const version = record.version == null ? null : String(record.version)
  const summary = typeof record.description === 'string' && record.description.trim()
    ? sentence(record.description) : 'No description provided.'
  const editsDrawing = isWriteTool(record)
  const effect = { editsDrawing, label: editsDrawing ? 'Edits the open drawing' : 'Reads the drawing; does not change it' }
  const capabilities = Array.isArray(record.capabilities) ? [...record.capabilities] : []
  const properties = record.params?.properties
  const required = Array.isArray(record.params?.required) ? record.params.required : []
  const inputs = properties && typeof properties === 'object' && !Array.isArray(properties)
    ? Object.entries(properties).map(([key, property]) => ({
      name: key,
      type: Array.isArray(property?.type) ? property.type.filter((type) => typeof type === 'string').join(' or ') || 'any'
        : typeof property?.type === 'string' && property.type ? property.type : 'any',
      required: required.includes(key),
      description: typeof property?.description === 'string' ? property.description : null,
      default: property?.default,
    })) : []
  const schemas = [record.returns_schema, record.returns]
  const outputDescription = schemas.find((schema) => typeof schema?.description === 'string' && schema.description)
  const outputType = schemas.find((schema) => typeof schema?.type === 'string')
  const outputs = outputDescription?.description ?? (outputType ? `type ${outputType.type}` : 'Not declared')
  const author = record.provenance?.author
  const provenance = [
    { label: 'Source', value: author === 'agent' ? 'Agent-authored' : author ? 'Platform built-in' : 'Not declared' },
    { label: 'Review status', value: record.review?.status ? String(record.review.status) : 'Not declared' },
  ]
  for (const [label, value, digest] of [
    ['Engine operation', record.engine_op, false],
    ['Catalog digest', record.catalog_digest, true],
    ['Manifest', record.tool_manifest_sha256, true],
  ]) {
    if (value != null && value !== '') provenance.push({ label, value: digest ? String(value).slice(0, 12) : String(value) })
  }
  const limits = []
  if (Number.isFinite(record.timeout_ms)) limits.push({ label: 'Time limit', value: `${Math.round(record.timeout_ms / 1000)} s` })
  if (typeof record.idempotent === 'boolean') limits.push({ label: 'Safe to re-run', value: record.idempotent ? 'Yes' : 'No' })
  const notProvided = [...NOT_PROVIDED]
  const text = [
    `${name}${version !== null ? ` v${version}` : ''}`,
    summary,
    `Effect: ${effect.label}`,
    ...(inputs.length ? ['Inputs:', ...inputs.map((input) =>
      `- ${input.name} (${input.type}, ${input.required ? 'required' : 'optional'}): ${input.description || 'no description'}`)] : ['Inputs: none']),
    `Outputs: ${outputs}`,
    ...[...provenance, ...limits].map(({ label, value }) => `${label}: ${value}`),
    'Not provided by this tool record:',
    ...notProvided.map((item) => `- ${item}`),
  ].join('\n')
  return { name, version, summary, effect, capabilities, inputs, outputs, provenance, limits, notProvided, text }
}

export function explainTool(tool) {
  try {
    return buildExplanation(tool)
  } catch {
    // Even an unreadable record must leave the explanation surface usable.
    return buildExplanation(null)
  }
}
