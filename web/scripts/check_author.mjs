// check_author.mjs — asserts the M1 authoring-quality fixes recompute from the
// real mockAuthor module (word-boundary slug, parsed distance param, write-verb
// honesty branch). Headless: mockAuthor.js is pure (no import.meta / React).
import { authorMock } from '../src/mock/mockAuthor.js'

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg)
    process.exit(1)
  }
}

// (1) Word-boundary truncation: a long description slugs to a name <=40 chars
// whose last hyphen segment is a whole word from the description (never mid-word,
// never a trailing '-').
const longDesc = 'count the total number of photovoltaic panels grouped by their assigned layer designation'
const a = authorMock(longDesc)
assert(typeof a.tool.name === 'string' && a.tool.name.length <= 40, `name should be <=40 chars, got ${a.tool.name.length}: "${a.tool.name}"`)
assert(!a.tool.name.endsWith('-'), `name should not end with '-': "${a.tool.name}"`)
const words = longDesc.toLowerCase().match(/[a-z0-9]+/g)
const segs = a.tool.name.split('-')
const lastSeg = segs[segs.length - 1]
assert(words.includes(lastSeg), `last slug segment "${lastSeg}" must be a whole word from the description`)
// The raw untruncated slug is >40 chars, so truncation genuinely fired.
assert(words.join('-').length > 40, 'test description should be long enough to force truncation')

// (2) Numeric param parse: distance comes from the description, not hardcoded 60.
const b = authorMock('count panels within 18 inches of the edge')
assert(
  b.tool.params.properties.distance_in.default === 18,
  `distance_in.default should be 18, got ${b.tool.params.properties.distance_in.default}`
)

// (3) Write-verb honesty branch: delete requests are drawing.write and the
// preview must NOT claim read-only.
const c = authorMock('delete panels near the roof edge')
assert(Array.isArray(c.tool.capabilities) && c.tool.capabilities.includes('drawing.write'), `delete request must include 'drawing.write', got ${JSON.stringify(c.tool.capabilities)}`)
assert(!/read-only/i.test(c.preview), `write preview must not contain "read-only": ${JSON.stringify(c.preview)}`)

// (4) Genuine read requests keep exactly ['drawing.read'] and the read-only preview.
const d = authorMock('count panels per layer')
assert(JSON.stringify(d.tool.capabilities) === JSON.stringify(['drawing.read']), `read request should keep ['drawing.read'], got ${JSON.stringify(d.tool.capabilities)}`)
assert(/read-only/i.test(d.preview), `read preview should say read-only: ${JSON.stringify(d.preview)}`)

// Shape intact.
assert(a.tool.provenance.author === 'agent', 'provenance.author must be agent')
assert(typeof a.code === 'string' && a.code.length > 0, 'code must be a non-empty string')

console.log('AUTHOR_QUALITY_OK')
