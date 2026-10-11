const FIELDS = ['kind', 'organization_id', 'project_id', 'drawing_id', 'input_version_id']
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export function createSolarDrawingBinding(value) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype ||
      Reflect.ownKeys(value).length !== FIELDS.length) throw new TypeError('Invalid Solar binding')
  const result = {}
  for (const field of FIELDS) {
    const descriptor = Object.getOwnPropertyDescriptor(value, field)
    if (!descriptor || !Object.hasOwn(descriptor, 'value') ||
        (field === 'kind' ? descriptor.value !== 'project' :
          typeof descriptor.value !== 'string' || !UUID.test(descriptor.value))) {
      throw new TypeError('Invalid Solar binding')
    }
    result[field] = descriptor.value
  }
  return Object.freeze(result)
}

export function solarDrawingBindingFromRunContext(context) {
  if (context?.drawingVersion !== null) throw new TypeError('Canonical version required')
  return createSolarDrawingBinding({ kind: 'project', organization_id: context.orgId,
    project_id: context.projectId, drawing_id: context.drawingArtifactId,
    input_version_id: context.drawingId })
}

function equal(a, b, fields) {
  try {
    const left = createSolarDrawingBinding(a)
    const right = createSolarDrawingBinding(b)
    return fields.every((field) => left[field] === right[field])
  } catch { return false }
}

export const sameSolarDrawingBinding = (a, b) => equal(a, b, FIELDS)
export const sameSolarCheckoutBinding = (a, b) => equal(a, b, FIELDS.slice(0, 4))
export function solarDrawingBindingKey(binding) {
  const value = createSolarDrawingBinding(binding)
  return 'solar-binding|' + FIELDS.map((field) => value[field]).join('|')
}
export function solarCheckoutBindingKey(binding) {
  const value = createSolarDrawingBinding(binding)
  return 'solar-checkout|' + FIELDS.slice(0, 4).map((field) => value[field]).join('|')
}
