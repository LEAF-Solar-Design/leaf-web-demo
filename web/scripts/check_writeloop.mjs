// M3 oracle: the mock write -> version -> undo loop, recomputed from the REAL
// sample rooftop intake and the REAL mock engine / version chain. Nothing here
// is hardcoded: base counts come from the file, v2 counts from the engine.
//
// Run headless: `node scripts/check_writeloop.mjs` from web/.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { runMock } from '../src/mock/mockEngine.js'
import { seedBase, applyDelete, undo, redo, list, headIntake, view } from '../src/mock/mockVersions.js'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = join(here, '..')

function assert(cond, msg) {
  if (!cond) { console.error(`FAIL: ${msg}`); process.exit(1) }
}

const intake = JSON.parse(readFileSync(join(webRoot, 'public', 'sample.intake.json'), 'utf8'))
const registry = JSON.parse(readFileSync(join(webRoot, 'src', 'mock', 'registry.json'), 'utf8'))

// --- registry carries the write tool ------------------------------------
const tool = (registry.tools || []).find((t) => t.name === 'delete-marked-panel')
assert(tool, 'registry.json has no delete-marked-panel tool')
assert(tool.kind === 'script', 'delete-marked-panel kind !== script')
assert(tool.engine_op === 'delete_marked_panel', 'delete-marked-panel engine_op wrong')
assert((tool.capabilities || []).includes('drawing.write'), 'delete-marked-panel lacks drawing.write')
assert(tool.params?.properties?.handle?.type === 'string', 'delete-marked-panel has no string handle param')
assert(tool.provenance?.author === 'agent', 'delete-marked-panel provenance.author !== agent')

// --- pick a REAL Panels handle from the real intake ----------------------
const base = intake.polylines || []
const baseCount = base.length
assert(baseCount > 1, 'sample intake has too few polylines')
const panels = base.filter((p) => p.layer === 'Panels')
assert(panels.length > 0, 'sample intake has no Panels polylines')
const target = panels[Math.floor(panels.length / 2)].handle
assert(target, 'no handle on the chosen Panels polyline')

// --- run the real engine op ---------------------------------------------
const env = runMock(tool, { handle: target }, intake)
assert(env.ok === true, `engine envelope not ok: ${env.error}`)
assert(env.tool === 'delete-marked-panel', 'envelope tool name wrong')
const r = env.result
assert(r.removed === target, `removed ${r.removed} !== requested ${target}`)
assert(r.new_version && r.new_version.version === 2, `new_version.version !== 2 (${r.new_version?.version})`)
assert(r.new_version.parent === 1, `new_version.parent !== 1 (${r.new_version.parent})`)
assert(r.new_version.drawing_id === 'demo', 'new_version.drawing_id !== demo')
assert(r.panels_after === baseCount - 1, `engine post-delete count ${r.panels_after} !== ${baseCount - 1}`)
assert((r.intake.polylines || []).length === baseCount - 1, 'engine post-delete intake count wrong')
assert(!(r.intake.polylines || []).some((p) => p.handle === target), 'engine post-delete intake still has the handle')
assert((env.overlay?.highlight_handles || []).includes(target), 'overlay does not mark the removed handle')

// --- the version chain agrees, and undo/redo walk it ---------------------
seedBase(intake)
assert(headIntake().polylines.length === baseCount, 'seeded v1 count wrong')
const v2 = applyDelete(target)
assert(v2.version === 2 && v2.parent === 1, 'applyDelete did not produce v2/parent 1')
assert(v2.removed === target, 'applyDelete removed the wrong handle')
assert(headIntake().polylines.length === baseCount - 1, `v2 head count ${headIntake().polylines.length} !== ${baseCount - 1}`)
assert(!headIntake().polylines.some((p) => p.handle === target), 'v2 head still contains the deleted handle')

const afterUndo = undo()
assert(afterUndo.head === 1, `undo head ${afterUndo.head} !== 1`)
assert(headIntake().polylines.length === baseCount, `post-undo head count ${headIntake().polylines.length} !== ${baseCount}`)
assert(headIntake().polylines.some((p) => p.handle === target), 'post-undo head is missing the restored handle')

const afterRedo = redo()
assert(afterRedo.head === 2, `redo head ${afterRedo.head} !== 2`)
assert(headIntake().polylines.length === baseCount - 1, 'post-redo head count wrong')

// --- history shape VersionHistory.jsx consumes ---------------------------
const hist = list()
assert(hist.head === 2 && hist.latest === 2, 'history head/latest wrong')
assert(Array.isArray(hist.versions) && hist.versions.length === 2, 'history should list v1 and v2')
for (const row of hist.versions) {
  assert(typeof row.v === 'number', 'history row missing v')
  assert(typeof row.tool === 'string', 'history row missing tool')
  assert(typeof row.note === 'string', 'history row missing note')
  assert(typeof row.created === 'string', 'history row missing created')
  assert(typeof row.sha256 === 'string' && row.sha256.length >= 12, 'history row missing sha256')
}
assert(view(1).intake.polylines.length === baseCount, 'view(1) is not the base intake')

console.log(`base polylines: ${baseCount}; deleted handle ${target}; v2 -> ${baseCount - 1}; undo -> ${baseCount}`)
console.log('WRITELOOP_OK')
