export const MAX_TOOL_ARGS_BYTES = 64 * 1024
const plain = (value) => !!value && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const exact = (value, keys) => plain(value) && Reflect.ownKeys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$/
export const TOOL_DECISIONS = {
  accept: 'Ready to run.',
  malformed: 'The tool package is malformed.',
  unknown_key: 'The package signing key is unknown.',
  signature_invalid: 'The package signature is invalid.',
  identity_mismatch: 'The package identity does not match this tool.',
  revoked: 'This tool has been revoked.',
  stale_revocation_list: 'The host needs an updated revocation list.',
  unreviewed: 'This tool has not been reviewed.',
  unentitled: 'Your account does not have access to this tool.',
  host_api_too_low: 'This tool requires a newer host API.',
  capability_violation: 'The tool requested a capability it is not allowed to use.',
  timeout: 'The tool exceeded its time limit.',
  compile_failed: 'The tool could not be compiled.',
  script_error: 'The tool encountered a script error.',
  revocation_invalid: 'The revocation information is invalid.',
  invalid_command: 'The tool request is invalid.',
  tool_not_installed: 'This tool is not installed.',
  digest_mismatch: 'The installed package does not match this tool.',
  version_not_current: 'This tool version is no longer current. Refresh the tool list.',
  confirmation_declined: 'Confirmation was declined in AutoCAD.',
  pick_cancelled: 'Cancelled in AutoCAD: no object was picked. Nothing changed.',
  run_tool_unavailable: 'This host cannot run tools.',
  args_too_large: 'Arguments exceed the 64 KiB limit.',
  invalid_response: 'The host returned an invalid tool response.',
}
const hostRefusals = new Set(['invalid_command', 'tool_not_installed', 'digest_mismatch',
  'version_not_current', 'confirmation_declined', 'pick_cancelled', 'run_tool_unavailable',
  'malformed', 'unknown_key', 'signature_invalid', 'identity_mismatch', 'revoked',
  'stale_revocation_list', 'unreviewed', 'unentitled', 'host_api_too_low',
  'capability_violation', 'timeout', 'compile_failed', 'script_error', 'revocation_invalid'])
export function describeDecision(code) {
  return TOOL_DECISIONS[code] ?? `Tool refused: ${String(code).replaceAll('_', ' ')}.`
}
export function serializeArgs(args) {
  let json
  try {
    if (!plain(args)) throw new Error()
    json = JSON.stringify(args)
    if (!json || !plain(JSON.parse(json))) throw new Error()
  } catch { throw new Error('invalid_command') }
  if (new TextEncoder().encode(json).byteLength > MAX_TOOL_ARGS_BYTES) throw new Error('args_too_large')
  return json
}
export function validateToolPayload(payload) {
  if (!exact(payload, ['name', 'version', 'packageDigest', 'args']) ||
      typeof payload.name !== 'string' || payload.name !== payload.name.trim() || !/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(payload.name) ||
      typeof payload.version !== 'string' || payload.version !== payload.version.trim() || !semver.test(payload.version) ||
      typeof payload.packageDigest !== 'string' || payload.packageDigest.length !== 64 || !/^[a-f0-9]{64}$/.test(payload.packageDigest)) throw new Error('invalid_command')
  serializeArgs(payload.args)
}
export function isToolIdentity(value) {
  try { validateToolPayload({ name: value.name, version: value.version, packageDigest: value.packageDigest, args: {} }); return true } catch { return false }
}
export function isToolResponse(action, value) {
  if (action === 'list_tools') return exact(value, ['tools']) && Array.isArray(value.tools) && value.tools.every((tool) =>
    plain(tool) && Object.keys(tool).every((key) => ['name', 'version', 'packageDigest', 'source', 'decision', 'runnable', 'params', 'manifest'].includes(key)) &&
    isToolIdentity(tool) && ['downloaded', 'builtin', 'local'].includes(tool.source) &&
    (tool.decision === 'accept' || hostRefusals.has(tool.decision)) && typeof tool.runnable === 'boolean' &&
    (tool.decision === 'accept' || !tool.runnable))
  if (!exact(value, ['decision', 'reason', 'result', 'resultTruncated', 'generation', 'durationMs']) ||
      !['accept', 'refused'].includes(value.decision) || typeof value.resultTruncated !== 'boolean' ||
      !Number.isSafeInteger(value.generation) || value.generation < 0 ||
      !Number.isFinite(value.durationMs) || value.durationMs < 0) return false
  try {
    return (value.decision === 'accept' ? value.reason === null && plain(value.result)
      : hostRefusals.has(value.reason) && value.result === null) &&
      new TextEncoder().encode(JSON.stringify(value.result)).byteLength <= 256 * 1024
  } catch { return false }
}
export function buildFormModel(params) {
  if (!plain(params) || params.type !== 'object' || !plain(params.properties) ||
      Object.keys(params).some((key) => !['type', 'properties', 'required', 'additionalProperties', 'title', 'description', '$schema'].includes(key))) {
    return [{ name: '$args', label: 'Arguments JSON', type: 'json', initial: '{}', wholeArgs: true }]
  }
  // R2-11: AutoCAD asks the user to pick an x-leaf-pick input in the drawing, so the form never shows one.
  return Object.entries(params.properties).filter(([, schema]) => !(plain(schema) && Object.hasOwn(schema, 'x-leaf-pick'))).map(([name, schema]) => {
    const supported = plain(schema) && Object.keys(schema).every((key) =>
      ['type', 'enum', 'default', 'title', 'description', 'minimum', 'maximum', 'minLength', 'maxLength'].includes(key))
    const type = supported && Array.isArray(schema.enum) && schema.enum.length ? 'enum'
      : supported && ['string', 'integer', 'number', 'boolean'].includes(schema.type) ? schema.type : 'json'
    const initial = type === 'enum'
      ? (schema.enum.some((option) => Object.is(option, schema.default)) ? schema.default : schema.enum[0])
      : schema?.default ?? (type === 'boolean' ? false : '')
    return { name, label: typeof schema?.title === 'string' ? schema.title : name, type,
      options: type === 'enum' ? schema.enum : null, required: params.required?.includes(name) === true,
      initial: type === 'json' ? (Object.hasOwn(schema ?? {}, 'default') ? JSON.stringify(schema.default) : '') : initial }
  })
}
export function formArgs(model, values) {
  function parseJson(value, field) {
    try { return JSON.parse(value) } catch {
      const error = new Error(`Invalid JSON in ${field.label}`)
      error.field = field.name
      throw error
    }
  }
  if (model.length === 1 && model[0].wholeArgs === true) {
    const args = parseJson(values.$args, model[0])
    serializeArgs(args)
    return args
  }
  const args = Object.create(null)
  for (const field of model) {
    const value = values[field.name]
    if (value === '' && !field.required) continue
    if (field.type === 'integer' || field.type === 'number') {
      const number = Number(value)
      if ((typeof value === 'string' && value.trim() === '') || !Number.isFinite(number) || (field.type === 'integer' && !Number.isSafeInteger(number))) throw new Error('invalid_command')
      args[field.name] = number
    } else args[field.name] = field.type === 'json' ? parseJson(value, field) : value
  }
  serializeArgs(args)
  return args
}
