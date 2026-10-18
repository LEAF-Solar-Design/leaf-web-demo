export const CANVAS_WIDTH = 361
export const CANVAS_HEIGHT = 160
export const MAX_ELEMENTS = 32
export const MAX_TEXT_LENGTH = 512

const fields = new Set(['id', 'kind', 'x', 'y', 'w', 'h', 'scale', 'weight', 'color', 'align', 'visible', 'text'])
const finite = (value) => typeof value === 'number' && Number.isFinite(value)

// A closed text-only boundary: unknown properties are refused, including executable data.
export function validateElement(element) {
  if (!element || typeof element !== 'object' || Array.isArray(element)) throw new Error('Expected a text element.')
  if (Reflect.ownKeys(element).some((key) => !fields.has(key))) throw new Error('Unsupported element field.')
  if ([...fields].some((key) => !Object.hasOwn(element, key))) throw new Error('All text element fields are required.')
  if (element.kind !== 'text') throw new Error('Only text elements are supported.')
  if (typeof element.id !== 'string' || !element.id.trim()) throw new Error('A stable element ID is required.')
  if (typeof element.text !== 'string' || element.text.length > MAX_TEXT_LENGTH) throw new Error('Text must be at most 512 characters.')
  const { x, y, w, h, scale, weight } = element
  if (![x, y, w, h].every(finite) || x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > CANVAS_WIDTH || y + h > CANVAS_HEIGHT) {
    throw new Error('Geometry must be finite and inside the 361 × 160 canvas.')
  }
  if (!finite(scale) || scale < 0.4 || scale > 2) throw new Error('Scale must be between 0.4 and 2.')
  if (![400, 500, 600, 700, 800, 900].includes(weight)) throw new Error('Unsupported text weight.')
  if (!['ink', 'leaf', 'slate', 'loss'].includes(element.color)) throw new Error('Unsupported text color.')
  if (!['left', 'center', 'right'].includes(element.align)) throw new Error('Unsupported alignment.')
  if (typeof element.visible !== 'boolean') throw new Error('Visibility must be a boolean.')
  return { ...element }
}

export function validateElements(elements) {
  if (!Array.isArray(elements) || elements.length > MAX_ELEMENTS) throw new Error('A draft supports at most 32 text elements.')
  const validated = Array.from(elements, validateElement)
  if (new Set(validated.map(({ id }) => id)).size !== validated.length) throw new Error('Element IDs must be unique.')
  return validated
}

export function createTextElement(id) {
  return validateElement({ id, kind: 'text', x: 16, y: 16, w: 120, h: 16, scale: 1, weight: 500, color: 'slate', align: 'left', visible: true, text: 'Your text' })
}

export function textInputs(element) {
  return { text: element.text, x: String(element.x), y: String(element.y), align: element.align }
}

// Inputs stay strings until validated, so blank/invalid numeric edits survive in the editor.
export function validateTextInputs(element, input) {
  const errors = {}
  if (typeof input.text !== 'string' || input.text.length > MAX_TEXT_LENGTH) errors.text = 'Text must be at most 512 characters.'
  for (const [axis, maximum] of [['x', CANVAS_WIDTH - element.w], ['y', CANVAS_HEIGHT - element.h]]) {
    const raw = input[axis]
    if (typeof raw !== 'string' || !/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(raw.trim()) || !Number.isFinite(Number(raw)) || Number(raw) < 0 || Number(raw) > maximum) {
      errors[axis] = `${axis.toUpperCase()} must be a finite number from 0 to ${maximum}.`
    }
  }
  if (!['left', 'center', 'right'].includes(input.align)) errors.align = 'Choose left, center or right alignment.'
  if (Object.keys(errors).length) return { errors, element: null }
  return { errors, element: validateElement({ ...element, ...input, x: Number(input.x), y: Number(input.y) }) }
}
