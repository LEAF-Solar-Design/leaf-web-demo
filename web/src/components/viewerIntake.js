// The server intake keys blocks by name; the engine projection lists records
// with a name field. One Viewer reads both shapes without changing the records.
// The server caps definitions at 200; 500 provides headroom, not a new policy.
export const MAX_BLOCK_DEFINITIONS = 500

function isPlainObject(value) {
  if (value === null || typeof value !== 'object') return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

export function blockDefinitions(intake) {
  const definitions = new Map()
  const blocks = intake?.blocks
  if (Array.isArray(blocks)) {
    for (let i = 0; i < Math.min(blocks.length, MAX_BLOCK_DEFINITIONS); i++) {
      const block = blocks[i]
      if (isPlainObject(block) && typeof block.name === 'string' && block.name.length > 0) {
        definitions.set(block.name.toUpperCase(), block)
      }
    }
  } else if (blocks !== null && typeof blocks === 'object') {
    let read = 0
    for (const key in blocks) {
      if (!Object.prototype.hasOwnProperty.call(blocks, key)) continue
      if (read >= MAX_BLOCK_DEFINITIONS) break
      read++
      const block = blocks[key]
      if (isPlainObject(block)) definitions.set(key.toUpperCase(), block)
    }
  }
  return definitions
}
