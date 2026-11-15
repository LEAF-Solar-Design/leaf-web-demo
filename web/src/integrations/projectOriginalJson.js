export class DuplicateOriginalMember extends Error {
  constructor() { super('Duplicate JSON members. Nothing was imported.') }
}

// JSON.parse validates syntax but discards earlier duplicate members. Scan the
// original tokens as well, before those ambiguous values can enter a mapping.
export function parseProjectOriginalJson(text) {
  const value = JSON.parse(text)
  const objects = []
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]
    if (char === '{' || char === '[') {
      if (objects.length === 64) throw new Error('Original JSON nesting is too deep.')
      objects.push(char === '{' ? new Set() : null)
    } else if (char === '}' || char === ']') objects.pop()
    else if (char === '"') {
      const start = i
      for (i += 1; i < text.length; i += 1) {
        if (text[i] === '\\') i += 1
        else if (text[i] === '"') break
      }
      let next = i + 1
      while (' \t\r\n'.includes(text[next]) && next < text.length) next += 1
      const members = objects.at(-1)
      if (text[next] === ':' && members) {
        const key = JSON.parse(text.slice(start, i + 1))
        if (members.has(key)) throw new DuplicateOriginalMember()
        members.add(key)
      }
    }
  }
  return value
}
