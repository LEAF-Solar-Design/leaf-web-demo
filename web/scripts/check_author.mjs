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

// (2b) The GENERATED CODE must carry the same parsed distance as the params —
// a preview that says 60.0 while the tool runs at 18 is a visible contradiction.
assert(
  b.code.includes('"distance_in" 18.0'),
  `generated code must use the parsed distance, got: ${JSON.stringify(b.code.split('\n')[2])}`
)
// An unparsed description keeps the 60.0 default in both places.
const b60 = authorMock('highlight panels near the roof edge')
assert(b60.tool.params.properties.distance_in.default === 60, 'unparsed distance should default to 60')
assert(b60.code.includes('"distance_in" 60.0'), 'unparsed distance should render 60.0 in the code')

// (2c) Slug names the TOOL, not the act of authoring: a leading imperative phrase
// is stripped and the name never ends on a stopword.
const e = authorMock('build a tool that flags panels within 18 in of the roof edge')
assert(!e.tool.name.startsWith('build-'), `slug must strip the leading imperative phrase, got "${e.tool.name}"`)
assert(e.tool.name.startsWith('flags-panels'), `slug should name what the tool does, got "${e.tool.name}"`)
const STOPWORDS = new Set(['the', 'a', 'an', 'of', 'in', 'on', 'to', 'that', 'by', 'for', 'and', 'with', 'within', 'per'])
for (const desc of [longDesc, 'count panels within 18 inches of the edge', 'build a tool that flags panels within 18 in of the roof edge']) {
  const n = authorMock(desc).tool.name
  const tail = n.split('-').pop()
  assert(!STOPWORDS.has(tail), `slug for "${desc}" ends on stopword "${tail}": "${n}"`)
}

// (2d) Provenance the authored card renders: agent-authored, with created +
// grants + a run id, so the UI reads "Authored by agent" and can show the block.
assert(b.source === 'harness', `mock authoring must report source 'harness', got ${JSON.stringify(b.source)}`)
assert(typeof b.run_id === 'string' && b.run_id.length > 0, 'mock authoring must carry a run_id')
assert(b.tool.provenance.author === 'agent', 'provenance.author must be agent')
assert(!Number.isNaN(Date.parse(b.tool.provenance.created)), `provenance.created must be an ISO timestamp, got ${b.tool.provenance.created}`)
assert(
  Array.isArray(b.tool.provenance.grants) && b.tool.provenance.grants.includes('drawing.read'),
  `provenance.grants must list the capabilities, got ${JSON.stringify(b.tool.provenance.grants)}`
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
