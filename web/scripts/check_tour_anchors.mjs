// THE TOUR-ANCHOR ORACLE (standardization slice 4b), beside check_tourscript.mjs.
//
// Slice 4b made the guided tours spotlight by `data-tour="<id>"` first
// (src/demo/DemoTour.jsx resolveTourTarget), with the step's className chain
// as the fallback, and moved the step -> anchor mapping into the Surface
// Contract (`contract.tourAnchors.{console,stage}` in src/site/productSurfaces.js)
// so the two step arrays stay byte-identical. That leaves one way to drift
// silently: an anchor id the contract names that no shell element carries any
// more (a refactor drops the attribute, or renames it). The fallback would hide
// it: the tour keeps working off className, and nobody learns the anchor is
// dead. This gate fails on that drift.
//
// What it checks, all from SOURCE, nothing rendered:
//   1. VOCABULARY. Every anchor id the contract names, on any surface, in
//      either shell, is one of the declared ids (ANCHOR_IDS below), and every
//      id is a slug DemoTour's ANCHOR_ID pattern accepts.
//   2. STEP IDS. Every key of a shell's map is a step id that shell's tour
//      array actually has: TOUR_STEPS (imported headless, it is pure data) for
//      the console, UNIFIED_TOUR_STEPS for the stage (ToolCast.jsx imports
//      React, so its step ids are read out of the source text).
//   3. PRESENCE. Every anchor id a shell's map references is present as a
//      literal `data-tour="<id>"` attribute in that shell's own source files
//      (CONSOLE_SOURCES / STAGE_SOURCES). A console anchor found only in a
//      stage file, or the reverse, fails: each shell must carry its own.
//   4. SHAPE. `tourAnchors` is `{ console, stage }` on every record, each side
//      an object or null, and the stage map is non-null exactly where the
//      stage mounts a walk (the cad arm: `chrome.stageBranch === 'cad'`).
//   5. RESOLUTION ORDER. DemoTour.jsx resolves `[data-tour=` before the
//      className chain (a source pin), and both mounts pass `anchors=` from
//      the contract (App.jsx `.tourAnchors?.console`, ToolCast.jsx
//      `.tourAnchors?.stage`).
//   6. NO ORPHANED VOCABULARY (the reverse of check 3). Every `data-tour="<id>"`
//      a shell's own source carries is referenced by SOME surface's step map
//      for that shell. Check 3 alone lets an attribute rot silently: an id a
//      refactor stops mapping, or a copy-paste that lands an anchor on an
//      element no step ever targets, still passes 3 (nothing references it,
//      so nothing can be missing) and never shows up anywhere else, because
//      the className fallback does not read `data-tour` at all. This check
//      is what catches that: it fails on the attribute existing with zero
//      consumers, which is exactly the drift `left-rail` was (NavRail.jsx
//      `aside.nav`, ToolCast.jsx `.tc-operator-rail` x2) before it was
//      removed — see `orphanedAnchors` below.
//   7. POSITIVE CONTROL. Fixture contracts with a dangling anchor, an unknown
//      step id, an invalid slug, a mis-shelled anchor and an orphaned source
//      anchor each drive the same functions red, so a broken scan cannot
//      report green.
//
// Static only, and honest about it: this proves the attribute exists in the
// file and is consumed by a step, not that the element renders on a given
// surface, or that the RIGHT element is what actually lights up in a browser.
// That second, narrower claim — an anchored step resolves to the SAME element
// its className chain would have picked, i.e. the anchor was not mis-placed
// on a sibling — is pinned by web/src/demo/demoTourAnchors.test.jsx: a jsdom
// suite that mounts each shell's real markup shape, calls resolveTourTarget
// once WITH the contract's anchors and once with anchors withheld (className
// chain only) for every mapped step, and asserts they resolve to the same
// node. Its own positive control proves that pairing is a real tripwire, not
// a vacuous one, by moving a `data-tour` attribute onto an unrelated sibling
// and showing the two resolutions then diverge. The e2e walk
// (e2e/unified-walk.spec.mjs) drives the tour end to end through the real
// app and asserts on step copy, the command bar, and app state as the walk
// proceeds; it never reads `.tour-spot`'s geometry or which node is under it,
// so it is not, and was never, a check on which element the spotlight lands
// on — the line that used to claim otherwise was wrong, not just imprecise.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'
import { describe, it } from 'node:test'

import { TOUR_STEPS } from '../src/demo/tourScript.js'
import { PRODUCT_SURFACES } from '../src/site/productSurfaces.js'

const here = dirname(fileURLToPath(import.meta.url))
const WEB = join(here, '..')
const SRC = join(WEB, 'src')
const rel = (p) => relative(WEB, p).split('\\').join('/')

// The shared anchor vocabulary, as a LITERAL: a fifth id added to the contract
// without a row here fails check 1, by design, so the vocabulary cannot grow
// without someone naming what the new anchor is. `left-rail` lived here
// through slice 4b's first cut and was removed with its (unreferenced)
// attributes rather than kept as a reservation — see productSurfaces.js.
export const ANCHOR_IDS = Object.freeze(['shell', 'viewer', 'command-bar', 'right-rail'])

// The same slug rule DemoTour.jsx applies before querySelector.
export const ANCHOR_ID = /^[a-z][a-z0-9-]{0,63}$/

// Where each shell's elements live. A shared component that BOTH shells mount
// (Viewer.jsx, JobRail.jsx) is listed under the console only: the stage's own
// wrapper (`.stage-viewer`, `.tc-rail-r`) carries the stage's anchor, and tree
// order makes that wrapper win at runtime (see DemoTour.resolveTourTarget).
export const CONSOLE_SOURCES = Object.freeze([
  'src/App.jsx',
  'src/components/Viewer.jsx',
  'src/components/PromptBox.jsx',
  'src/components/JobRail.jsx',
  'src/site/NavRail.jsx',
])
export const STAGE_SOURCES = Object.freeze([
  'src/site/StageScene.jsx',
  'src/site/StageLayer.jsx',
  'src/site/ToolCast.jsx',
])

const read = (p) => readFileSync(join(WEB, p), 'utf8')

/** Every literal data-tour id in a source text. */
export function anchorsInSource(source) {
  const out = new Set()
  for (const m of String(source).matchAll(/data-tour="([^"]*)"/g)) out.add(m[1])
  return out
}

/**
 * The stage's step ids, read out of ToolCast.jsx's `UNIFIED_TOUR_STEPS`
 * literal. Bounded to that one array: the scan starts at the declaration and
 * stops at the first `]` that closes it at column 0, so a step id elsewhere in
 * the file cannot leak in.
 */
export function stageStepIds(toolCastSource) {
  const start = toolCastSource.indexOf('const UNIFIED_TOUR_STEPS = [')
  if (start === -1) return []
  const end = toolCastSource.indexOf('\n]', start)
  const block = toolCastSource.slice(start, end === -1 ? undefined : end)
  return [...block.matchAll(/^\s*id: '([^']+)',$/gm)].map((m) => m[1])
}

/**
 * Validates one surface's `tourAnchors` against the two step-id lists and the
 * two per-shell anchor sets. Returns a list of violation strings; empty means
 * the record holds. Pure, so the positive control can drive it with fixtures.
 */
export function anchorViolations({ id, contract }, { consoleSteps, stageSteps, consoleAnchors, stageAnchors }) {
  const out = []
  const ta = contract?.tourAnchors
  if (!ta || typeof ta !== 'object' || Array.isArray(ta)) {
    out.push(`${id}: tourAnchors must be { console, stage }, got ${JSON.stringify(ta)}`)
    return out
  }
  const keys = Object.keys(ta).sort()
  if (keys.join(',') !== 'console,stage') out.push(`${id}: tourAnchors keys must be exactly console,stage (got ${keys.join(',')})`)
  const shells = [
    ['console', ta.console, consoleSteps, consoleAnchors],
    ['stage', ta.stage, stageSteps, stageAnchors],
  ]
  for (const [shell, map, steps, present] of shells) {
    if (map === null) continue
    if (!map || typeof map !== 'object' || Array.isArray(map)) {
      out.push(`${id}.${shell}: must be an object or null, got ${JSON.stringify(map)}`)
      continue
    }
    if (Object.keys(map).length === 0) out.push(`${id}.${shell}: an empty map declares nothing; use null`)
    for (const [stepId, anchorId] of Object.entries(map)) {
      if (!steps.includes(stepId)) out.push(`${id}.${shell}.${stepId}: no such step in the ${shell} tour`)
      if (typeof anchorId !== 'string' || !ANCHOR_ID.test(anchorId)) {
        out.push(`${id}.${shell}.${stepId}: anchor must be a slug, got ${JSON.stringify(anchorId)}`)
        continue
      }
      if (!ANCHOR_IDS.includes(anchorId)) out.push(`${id}.${shell}.${stepId}: "${anchorId}" is not a declared anchor id (${ANCHOR_IDS.join(', ')})`)
      if (!present.has(anchorId)) out.push(`${id}.${shell}.${stepId}: no ${shell} source carries data-tour="${anchorId}"`)
    }
  }
  // The stage declares a map exactly where it mounts a walk: the cad arm.
  const stageMounts = contract?.chrome?.stageBranch === 'cad'
  if (stageMounts && ta.stage === null) out.push(`${id}: the stage mounts its walk here (stageBranch cad) but declares no anchors`)
  if (!stageMounts && ta.stage !== null) out.push(`${id}: the stage mounts no walk here (stageBranch ${JSON.stringify(contract?.chrome?.stageBranch)}) but declares anchors`)
  return out
}

/** Every anchor id any surface's map (for one shell) actually references. */
export function referencedAnchors(surfaces, shell) {
  const out = new Set()
  for (const s of surfaces) {
    const map = s.contract?.tourAnchors?.[shell]
    if (map && typeof map === 'object') for (const v of Object.values(map)) out.add(v)
  }
  return out
}

/**
 * The reverse of the PRESENCE check: anchor ids a shell's source carries that
 * NO surface's map references. Check 3 (anchorViolations) only asks whether a
 * mapped id exists in source, so an id that exists in source but that nothing
 * maps is invisible to it — the class of drift `left-rail` was. Returns the
 * orphaned ids, sorted; empty means clean. Pure, so the positive control can
 * drive it with fixtures.
 */
export function orphanedAnchors(sourceAnchors, referenced) {
  return [...sourceAnchors].filter((id) => !referenced.has(id)).sort()
}

// --- the real tree ---------------------------------------------------------
const consoleSteps = TOUR_STEPS.map((s) => s.id)
const toolCast = read('src/site/ToolCast.jsx')
const stageSteps = stageStepIds(toolCast)
const consoleAnchors = new Set()
for (const p of CONSOLE_SOURCES) for (const a of anchorsInSource(read(p))) consoleAnchors.add(a)
const stageAnchors = new Set()
for (const p of STAGE_SOURCES) for (const a of anchorsInSource(read(p))) stageAnchors.add(a)
const consoleReferenced = referencedAnchors(PRODUCT_SURFACES, 'console')
const stageReferenced = referencedAnchors(PRODUCT_SURFACES, 'stage')
const facts = { consoleSteps, stageSteps, consoleAnchors, stageAnchors }

describe('tour anchors: the contract names anchors that exist (slice 4b)', () => {
  it('reads both step arrays and both shells (the scan is not vacuous)', () => {
    assert.ok(consoleSteps.length >= 7, `console tour has ${consoleSteps.length} steps`)
    assert.ok(stageSteps.length >= 5, `stage walk has ${stageSteps.length} steps`)
    assert.ok(consoleAnchors.size >= 3, `console sources carry ${consoleAnchors.size} anchors`)
    assert.ok(stageAnchors.size >= 3, `stage sources carry ${stageAnchors.size} anchors`)
  })

  it('every anchor either shell carries is in the declared vocabulary', () => {
    for (const [shell, set] of [['console', consoleAnchors], ['stage', stageAnchors]]) {
      for (const id of set) {
        assert.ok(ANCHOR_ID.test(id), `${shell}: data-tour="${id}" is not a slug`)
        assert.ok(ANCHOR_IDS.includes(id), `${shell}: data-tour="${id}" is not a declared anchor id`)
      }
    }
  })

  for (const surface of PRODUCT_SURFACES) {
    it(`${surface.id}: every referenced anchor exists in its shell's source, every key is a real step`, () => {
      assert.deepEqual(anchorViolations(surface, facts), [])
    })
  }

  it('at least one surface declares a stage map and every studio surface declares the console map', () => {
    const stage = PRODUCT_SURFACES.filter((s) => s.contract.tourAnchors.stage !== null).map((s) => s.id)
    assert.deepEqual(stage, ['cad'])
    for (const s of PRODUCT_SURFACES.filter((s) => s.contract.scene === 'app')) {
      assert.ok(s.contract.tourAnchors.console && Object.keys(s.contract.tourAnchors.console).length > 0, `${s.id} console map`)
    }
  })

  it('DemoTour resolves the anchor before the className chain, and both mounts pass the contract map', () => {
    const demoTour = read('src/demo/DemoTour.jsx')
    const anchorAt = demoTour.indexOf('document.querySelector(`[data-tour="${anchorId}"]`)')
    const chainAt = demoTour.indexOf('return resolveSelectorChain(step.target)')
    assert.ok(anchorAt > -1, 'DemoTour queries [data-tour=...]')
    assert.ok(chainAt > anchorAt, 'the className chain is the FALLBACK, after the anchor lookup')
    assert.match(read('src/App.jsx'), /<DemoTour[\s\S]{0,400}anchors=\{surfaceSlots\.tourAnchors\?\.console \?\? null\}/)
    assert.match(toolCast, /<DemoTour[\s\S]{0,600}anchors=\{surfaceContract\(activeSurface\)\.tourAnchors\?\.stage \?\? null\}/)
  })

  it('the step arrays are untouched by the anchor work (no step carries an anchor field)', () => {
    for (const s of TOUR_STEPS) assert.equal('anchor' in s, false, `console step ${s.id}`)
    const block = toolCast.slice(toolCast.indexOf('const UNIFIED_TOUR_STEPS = ['), toolCast.indexOf('\n]', toolCast.indexOf('const UNIFIED_TOUR_STEPS = [')))
    assert.doesNotMatch(block, /\banchor:/)
  })

  it('no shell carries a data-tour id that no step map references (no orphaned vocabulary)', () => {
    assert.deepEqual(orphanedAnchors(consoleAnchors, consoleReferenced), [], 'console')
    assert.deepEqual(orphanedAnchors(stageAnchors, stageReferenced), [], 'stage')
  })
})

describe('tour anchors: positive control (the gate goes red on the drift it exists for)', () => {
  const cad = PRODUCT_SURFACES.find((s) => s.id === 'cad')
  const base = () => ({ id: 'fixture', contract: { chrome: { stageBranch: 'cad' }, tourAnchors: { console: { ...cad.contract.tourAnchors.console }, stage: { ...cad.contract.tourAnchors.stage } } } })

  it('the real cad record is clean (so the mutations below are the only difference)', () => {
    assert.deepEqual(anchorViolations(base(), facts), [])
  })
  it('a dangling anchor (named, present nowhere in that shell) fails', () => {
    const f = base(); f.contract.tourAnchors.console.welcome = 'viewer'
    // present in console sources, so still clean; now name one that is not:
    const g = base(); g.contract.tourAnchors.stage.welcome = 'right-rail'
    assert.deepEqual(anchorViolations(f, facts), [])
    const fixed = { ...facts, stageAnchors: new Set([...facts.stageAnchors].filter((a) => a !== 'right-rail')) }
    assert.ok(anchorViolations(g, fixed).some((v) => v.includes('no stage source carries data-tour="right-rail"')))
  })
  it('an unknown step id fails', () => {
    const f = base(); f.contract.tourAnchors.console.nope = 'shell'
    assert.ok(anchorViolations(f, facts).some((v) => v.includes('no such step')))
  })
  it('an invalid slug never reaches the presence check', () => {
    const f = base(); f.contract.tourAnchors.console.welcome = '"] , *'
    assert.ok(anchorViolations(f, facts).some((v) => v.includes('must be a slug')))
  })
  it('an anchor outside the vocabulary fails even when a shell happens to carry it', () => {
    const f = base(); f.contract.tourAnchors.console.welcome = 'rogue'
    const withRogue = { ...facts, consoleAnchors: new Set([...facts.consoleAnchors, 'rogue']) }
    assert.ok(anchorViolations(f, withRogue).some((v) => v.includes('not a declared anchor id')))
  })
  it('an orphaned anchor (present in source, referenced by no map) fails, and mapping it clears it', () => {
    // A refactor drops the only step that referenced 'right-rail' (real:
    // 'versions' and 'trust' both do): the id stays in source, nothing named
    // it any more.
    const droppedRightRail = new Set([...stageReferenced].filter((a) => a !== 'right-rail'))
    assert.deepEqual(orphanedAnchors(facts.stageAnchors, droppedRightRail), ['right-rail'])
    // The real tree, unmutated, has a consumer for every id it carries.
    assert.deepEqual(orphanedAnchors(facts.stageAnchors, stageReferenced), [])
    assert.deepEqual(orphanedAnchors(facts.consoleAnchors, consoleReferenced), [])
    // A dangling id (present in source, e.g. a leftover from a rename) that
    // matches no live surface record is the same failure shape.
    assert.deepEqual(orphanedAnchors(new Set([...facts.consoleAnchors, 'stale']), consoleReferenced), ['stale'])
    // Nothing referenced at all (e.g. every surface record deleted) orphans
    // every id a shell's source carries, not just one.
    assert.deepEqual(orphanedAnchors(facts.stageAnchors, referencedAnchors([], 'stage')), [...facts.stageAnchors].sort())
  })
  it('a stage map on a surface whose stage arm mounts no walk fails, and the reverse', () => {
    const f = base(); f.contract.chrome.stageBranch = 'frame'
    assert.ok(anchorViolations(f, facts).some((v) => v.includes('mounts no walk here')))
    const g = base(); g.contract.tourAnchors.stage = null
    assert.ok(anchorViolations(g, facts).some((v) => v.includes('declares no anchors')))
  })
  it('a malformed tourAnchors shape fails', () => {
    for (const bad of [null, [], 'x', { console: {} }, { console: null, stage: null, extra: 1 }]) {
      const f = base(); f.contract.tourAnchors = bad
      assert.ok(anchorViolations(f, facts).length > 0, JSON.stringify(bad))
    }
  })
  it('the source scanners find what they should (so an empty scan cannot pass)', () => {
    assert.deepEqual([...anchorsInSource('<a data-tour="shell"/><b data-tour="viewer"/>')].sort(), ['shell', 'viewer'])
    assert.deepEqual(stageStepIds("const UNIFIED_TOUR_STEPS = [\n  {\n    id: 'a',\n  },\n  {\n    id: 'b',\n  },\n]\nconst other = [{ id: 'c' }]\n"), ['a', 'b'])
  })
})
