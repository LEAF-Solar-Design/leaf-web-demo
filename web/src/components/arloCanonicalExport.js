const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)

// Compare the parsed transport views without depending on object key order.
function sameJson(left, right) {
  if (left === right) return true
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
      || Array.isArray(left) !== Array.isArray(right)) return false
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length
    && keys.every(key => Object.hasOwn(right, key) && sameJson(left[key], right[key]))
}

export async function canonicalExportJson(envelope, canonicalJson, context, disclaimer) {
  const parts = {}
  for (const [field, hashField] of [['solver_input', 'input_sha256'], ['solver_result', 'result_sha256']]) {
    const text = canonicalJson?.[field]
    if (typeof text !== 'string' || !/^[a-f0-9]{64}$/i.test(envelope?.[hashField] || '')) {
      throw new Error('Producer canonical JSON or hashes are unavailable. Reopen this job to export it.')
    }
    const parsed = JSON.parse(text)
    if (!object(parsed) || !sameJson(parsed, envelope[field])) {
      throw new Error('Producer canonical JSON does not match the stored result. Reopen this job.')
    }
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
    const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
    if (hash !== envelope[hashField].toLowerCase()) throw new Error('Producer hash verification failed. Reopen this job.')
    parts[field] = text
  }
  const input = JSON.parse(parts.solver_input)
  if (input.organization_id !== context.org_id || input.project_id !== context.project_id
      || input.input_version_id !== context.input_version_id) throw new Error('Producer input does not match this project job.')
  // Serialize each ordinary field separately. Only validated complete JSON values
  // are inserted as values; user strings can never act as replacement tokens.
  const fields = Object.entries(envelope).filter(([, value]) => value !== undefined).map(([key, value]) =>
    `${JSON.stringify(key)}: ${Object.hasOwn(parts, key) ? parts[key] : JSON.stringify(value, null, 2)}`)
  return `{
  "job_context": ${JSON.stringify(context, null, 2)},
  "envelope": {${fields.join(',\n')}},
  "product_disclaimer": ${JSON.stringify(disclaimer)}
}`
}
