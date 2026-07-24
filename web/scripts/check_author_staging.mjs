import { readFileSync } from 'node:fs'

import { authorMock } from '../src/mock/mockAuthor.js'
import { runMock } from '../src/mock/mockEngine.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const authored = authorMock('build a tool that selects every tenth panel')
assert(!authored.unsupported, 'every-tenth authoring must be supported')
assert(authored.tool.engine_op === 'select_every_nth', 'every-tenth must use select_every_nth')
assert(authored.tool.params.properties.stride.default === 10, 'stride must be 10')
assert(authored.code.includes('"stride" 10'), 'generated code must carry stride 10')

const intake = JSON.parse(readFileSync(new URL('../public/sample.intake.json', import.meta.url), 'utf8'))
const result = runMock(authored.tool, { layer: 'Panels', stride: 10 }, intake)
assert(result.ok, `every-tenth tool must run: ${result.error || 'unknown error'}`)
assert(result.result.selected === 234, `expected 234 selected panels, got ${result.result.selected}`)
assert(result.overlay.highlight_handles.length === 234, 'overlay must highlight the same 234 panels')

const unsupported = authorMock('make the roof look more balanced and elegant')
assert(unsupported.unsupported === true, 'unsupported requests must decline')
assert(!unsupported.tool, 'unsupported requests must not create a runnable tool')

const destructive = authorMock('delete every tenth panel')
assert(destructive.unsupported === true, 'compound destructive stride requests must decline')
assert(!destructive.tool, 'compound destructive stride requests must not create a tool')

console.log('AUTHOR_STAGING_OK')
