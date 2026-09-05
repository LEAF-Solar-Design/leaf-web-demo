// Guard against a declaration being swallowed by a comment.
//
// App.jsx carries a ~140-line commented-out legacy block
// ("Legacy inline catalog dispatch is disabled; useCatalogController owns
// it."). Inserting a hook just inside it is easy, silent, and fatal: the JSX
// still references the binding, so the component throws ReferenceError on its
// FIRST render — and `npm run build` passes, because commented-out code is
// still valid syntax. Unit tests over pure modules miss it too, since they
// never render App.
//
// esbuild strips comments, so "does this declaration survive the transform"
// is a direct, cheap answer. Verified to fail on the exact commit where the
// declaration sat inside that block.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import esbuild from 'esbuild'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code
const promptBoxSessionBinding = /React\.createElement\(\s*PromptBox,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/
const conversePanelSessionBinding = /React\.createElement\(\s*ConversePanel,\s*\{[^}]*\bsessionId:\s*agentSessionId\b/

// ---------------------------------------------------------------------------
// Orphaned-setter scan (slice 4a). Every `setX(...)` a module CALLS must have a
// declaration somewhere in that module: a useState destructure, a controller
// destructure (`const { setOverlayStale } = drawing`, including renames like
// `reportError: setRunErr`), a plain binding, a parameter, or a browser global.
//
// esbuild's output keeps ordinary comments, so a `setX(` written inside one
// would otherwise read as a live call: strip comments before scanning, or the
// pin reports a phantom orphan and gets muted by the next person.
//
// A naive `line.indexOf('//')` has the opposite failure: a string literal
// containing a URL (`'https://example.com'`) puts a `//` on the line before
// any real comment does, so the cut lands inside the string and silently
// drops every token after it — including a genuine setter call later on that
// same line.
//
// The two failures cannot be fixed as two independent regex passes run in
// either order: masking string literals BEFORE stripping comments treats
// every English contraction inside a comment ("the controller's alone",
// "this render's decision") as an unterminated string that swallows
// thousands of real characters — including live useState declarations —
// until the next stray apostrophe; stripping comments first, with a
// naive scanner, is exactly the original `//`-in-a-URL bug. Only a single
// pass that tracks "am I inside a string right now" and "am I inside a
// comment right now" as ONE mutually-exclusive state can get both right:
// a `//` is a comment only when no string is open, and a quote opens a
// string only when no comment is open (so an apostrophe on a commented-out
// line, or a `//` inside a real string, both stay inert).
// ---------------------------------------------------------------------------
const SETTER = 'set[A-Z][\\w$]*'
const BROWSER_SETTERS = new Set(['setTimeout', 'setInterval'])

function decomment(code) {
  let out = ''
  let i = 0
  const n = code.length
  while (i < n) {
    const ch = code[i]
    const next = code[i + 1]
    if (ch === '/' && next === '*') {
      i += 2
      while (i < n && !(code[i] === '*' && code[i + 1] === '/')) i++
      i += 2
      out += ' '
      continue
    }
    if (ch === '/' && next === '/') {
      i += 2
      while (i < n && code[i] !== '\n') i++
      continue // the newline itself is copied on the next loop iteration
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      const quote = ch
      out += ch
      i++
      while (i < n && code[i] !== quote) {
        if (code[i] === '\\' && i + 1 < n) {
          out += code[i] + code[i + 1]
          i += 2
          continue
        }
        out += code[i]
        i++
      }
      if (i < n) { out += code[i]; i++ } // the closing quote
      continue
    }
    out += ch
    i++
  }
  return out
}

function orphanSetters(jsxSource) {
  const code = decomment(esbuild.transformSync(jsxSource, { loader: 'jsx' }).code)
  const declared = new Set(BROWSER_SETTERS)
  let match
  // Lookahead, never consume, on the trailing delimiter: two setters adjacent
  // in one destructure share the comma between them, and a consuming match
  // would swallow it and hide the second one.
  for (const pattern of [
    new RegExp(`\\[[^\\]]*?(${SETTER})\\s*(?=[,\\]])`, 'g'),
    new RegExp(`(?:[{,:]\\s*)(${SETTER})\\s*(?=[,}=])`, 'g'),
    new RegExp(`(?:const|let|var|function)\\s+(${SETTER})`, 'g'),
    new RegExp(`[(,]\\s*(${SETTER})\\s*(?=[,)=])`, 'g'),
  ]) while ((match = pattern.exec(code))) declared.add(match[1])

  const called = new Set()
  const callPattern = new RegExp(`(?<![.\\w$])(${SETTER})\\s*\\(`, 'g')
  while ((match = callPattern.exec(code))) called.add(match[1])
  return [...called].filter((name) => !declared.has(name)).sort()
}

// Bindings App declares AND passes into JSX. Add a row whenever a new one is
// introduced; the cost is one line and the failure it catches is a white screen.
const DECLARED_AND_USED = [
  { name: 'slashCommandActions', usedAs: 'commandActions' },
  { name: 'registryEntries', usedAs: 'registryEntries' },
  { name: 'catalogSkills', usedAs: 'skills: catalogSkills' },
  { name: 'agentSessionId', usedAs: 'sessionId: agentSessionId' },
]

describe('App.jsx wiring', () => {
  for (const { name, usedAs } of DECLARED_AND_USED) {
    it(`declares ${name} in executable code, not inside a comment`, () => {
      // Matches a plain binding (`const x =`) and an array destructure
      // (`const [x, setX] =`), which is how useState results are bound.
      const declared = new RegExp(
        `(const|let|var)\\s+(\\[\\s*)?${name}\\s*[,\\]=]`).test(stripped)
        || new RegExp(
          `(const|let|var)\\s+\\{[^}]*\\b(?:\\w+\\s*:\\s*)?${name}\\s*[,}]`).test(stripped)
      assert.ok(declared,
        `${name} does not survive comment-stripping — its declaration is inside a ` +
        'comment block, so any render that reads it throws ReferenceError')
    })

    it(`actually uses ${name} (${usedAs}) after declaring it`, () => {
      assert.ok(stripped.includes(usedAs),
        `${usedAs} is not referenced in the compiled output — the wiring is dead`)
    })
  }

  // Slice 3: "run the tool I just authored" is ONE honest path on both shells.
  // App used to arm the run straight off the provisional publish response, so a
  // publish that had not settled in the runnable catalog armed anyway. These
  // pins are source-level for the same reason the ones above are: nothing here
  // is reachable without rendering App, and publishedCatalogTool.test.mjs
  // remains the single oracle for the resolver's own behaviour.
  describe('authored-tool run path', () => {
    const useAuthoredBody = () => {
      const start = stripped.indexOf('onUseAuthored = useCallback')
      assert.notEqual(start, -1, 'onUseAuthored is not declared in executable code')
      return stripped.slice(start, start + 1400)
    }

    it('refetches the runnable catalog before it resolves anything', () => {
      assert.match(useAuthoredBody(), /await\s+loadCatalogTools\(\)/)
    })

    it('resolves through publishedCatalogTool, the one oracle', () => {
      assert.ok(stripped.includes('resolvePublishedCatalogTool'),
        'App does not import/call the shared published-tool resolver')
      assert.match(useAuthoredBody(), /resolvePublishedCatalogTool\(/)
    })

    it('surfaces the resolver message instead of arming the run', () => {
      const body = useAuthoredBody()
      assert.match(body, /catch/)
      assert.match(body, /showToast\(/)
      // The bail is BEFORE the commit, or the honesty is decorative.
      assert.ok(body.indexOf('showToast(') < body.indexOf('commitCatalogDecision('),
        'the error toast must precede the commit, not follow it')
    })

    it('stamps the authored provenance ToolCast already stamps', () => {
      assert.match(useAuthoredBody(), /source:\s*"authored"/)
    })

    it('hands the refetched record to armDecision instead of the stale list', () => {
      assert.match(useAuthoredBody(), /refreshedTool:\s*runnableTool/)
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      const armBody = stripped.slice(armStart, armStart + 1200)
      assert.match(armBody, /decision\.refreshedTool\?\.name === decision\.tool/)
    })

    it('fails when the resolver call is removed', () => {
      const mutated = appSource.replace(
        /runnableTool = resolvePublishedCatalogTool\(tool, refreshedTools\)/,
        'runnableTool = tool',
      )
      assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
      const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
      const start = mutatedStripped.indexOf('onUseAuthored = useCallback')
      assert.doesNotMatch(mutatedStripped.slice(start, start + 1400),
        /resolvePublishedCatalogTool\(/)
    })
  })

  it('passes sessionId into the PromptBox element itself', () => {
    // Limit the match to the PromptBox props object, not another component
    // receiving the same session binding.
    assert.match(stripped, promptBoxSessionBinding)
  })

  it('rejects PromptBox sessionId={null} even while ConversePanel keeps its binding', () => {
    const mutated = appSource.replace(
      /(<PromptBox[\s\S]*?\bsessionId=\{)agentSessionId(\})/,
      '$1null$2',
    )
    assert.notEqual(mutated, appSource, 'the falsification mutation must target PromptBox')
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    assert.match(mutatedStripped, conversePanelSessionBinding)
    assert.doesNotMatch(mutatedStripped, promptBoxSessionBinding)
  })

  it('disables image paste in the catalog command bar instead of dropping an attachment', () => {
    assert.match(stripped, /imageAttachmentsEnabled:\s*false/)
    const promptBox = readFileSync(new URL('./components/PromptBox.jsx', import.meta.url), 'utf8')
    assert.match(promptBox, /if \(!imageAttachmentsEnabled\)/)
    assert.match(promptBox, /Image paste is available in the assistant reply box/)
  })

  it('seats live intake with the mapped store drawing and its durable version summary', () => {
    assert.match(
      stripped,
      /drawingSummary\s*=\s*await getDrawingVersions\(false,\s*REQUESTED_DRAWING_ID\)/,
    )
    assert.match(
      stripped,
      /drawingId:\s*REQUESTED_DRAWING_ID[\s\S]*drawingState:\s*drawingSummary/,
    )
    assert.match(stripped, /fallbackDrawingId:\s*REQUESTED_DRAWING_ID/)
  })

  // Standardization slice 4a wrote this after making the exact mistake it
  // catches. Lifting the nav rail into site/NavRail.jsx moved the author fold's
  // `const [authorOpen, setAuthorOpen] = useState(false)` out of App while
  // SEVEN build-lane call sites kept calling setAuthorOpen(true). `npm run
  // build` passed (an undefined identifier in a callback is a RUNTIME
  // ReferenceError, not a build error), every unit row passed, and the first
  // click that opened the author panel would have thrown.
  //
  // The rows above catch a declaration swallowed by a comment; this catches a
  // declaration DELETED while its callers stayed, which is the shape every
  // extract-a-component refactor can produce. Generic on purpose: it needs no
  // maintenance when a new piece of state arrives.
  //
  // Slice 4a split the console's shell across four files (App.jsx plus the
  // extracted site/ToolCast.jsx, site/SurfaceFrame.jsx and site/NavRail.jsx),
  // and the exact mistake this pin exists for — a useState hoisted out while
  // its call sites stayed behind — can land in any one of the four just as
  // easily as it landed in App. Loop the same scan over all four; each module
  // is scanned on its own, since a setter genuinely declared in one and
  // called from another (a prop, not a closure) is not an orphan and this pin
  // must not treat cross-module wiring as a defect.
  const ORPHAN_SETTER_SOURCES = [
    { label: 'App.jsx', path: './App.jsx' },
    { label: 'site/ToolCast.jsx', path: './site/ToolCast.jsx' },
    { label: 'site/SurfaceFrame.jsx', path: './site/SurfaceFrame.jsx' },
    { label: 'site/NavRail.jsx', path: './site/NavRail.jsx' },
  ]

  for (const { label, path } of ORPHAN_SETTER_SOURCES) {
    it(`${label} calls no useState setter it does not declare (orphaned setter after a hoist)`, () => {
      const source = readFileSync(new URL(path, import.meta.url), 'utf8')
      assert.deepEqual(orphanSetters(source), [],
        `${label} calls these setters but declares none of them: a component `
        + 'extraction took the useState with it and left the call sites behind')
    })
  }

  it('fails when a setter declaration is deleted from under its callers', () => {
    // Falsification: the exact 4a mutation, replayed.
    const mutated = appSource.replace(
      /const \[authorOpen, setAuthorOpen\] = useState\(false\)/, '')
    assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
    assert.deepEqual(orphanSetters(mutated), ['setAuthorOpen'])
  })

  it('refuses a live legacy write when version bootstrap did not produce a pin', () => {
    assert.match(
      stripped,
      /!mock\s*&&\s*isWrite\s*&&\s*catalogRunContextRef\.current\.projectId\s*==\s*null\s*&&\s*catalogRunContextRef\.current\.drawingVersion\s*==\s*null[\s\S]*setRunErr\([\s\S]*return/,
    )
  })

  it('derives the submitted drawing version from the tool capability', () => {
    assert.match(
      stripped,
      /dwgVersion:\s*drawingVersionForRun\(tool,\s*executionContext,\s*health\?\.aps_live\)/,
    )
  })

  it('binds the status bar regions to the width whose CSS styles them', () => {
    // P1 studio-shell pass. The three FootRegion wrappers are a WIDE-layout
    // construct: cockpit.css styles `.foot-region` only inside
    // `@media (min-width: 981px)`. Below that the old shell's own rules do
    // the work, and they are CHILD selectors — `footer.foot-bar > *`
    // (styles.css:776-777) — so a wrapper there would hide every segment
    // from the only rules that style it, and the narrow status bar would
    // lose its layout silently on a viewport no unit test renders at.
    //
    // The gate is therefore `wideViewport`, which is literally
    // matchMedia('(min-width: 981px)') in this file: the same breakpoint,
    // read once. Pinned in the transform (comments stripped) so the term
    // cannot be dropped as "redundant" by a later reader.
    assert.match(
      stripped,
      /footRegions\s*=\s*Boolean\(studioGround\)\s*&&\s*drafting\s*&&\s*wideViewport/,
    )
    // ...and that gate is the ONLY thing deciding it, on every FootRegion:
    // three mounts, each reading the same binding, so they cannot disagree
    // about whether the bar is regioned and leave a half-wrapped footer.
    // Built from a string, not a regex literal: the honesty gate masks
    // comments and STRINGS and then balances braces per file, and it cannot
    // see a regex literal, so an escaped `\{` with no partner in one reads to
    // it as an unclosed block and the whole file is reported as untrustworthy
    // ("braces do not balance after comment/string masking"). Same pattern,
    // same match; the braces now sit inside a masked string.
    const gated = stripped.match(new RegExp('React\\.createElement\\(\\s*FootRegion,\\s*\\{\\s*on:\\s*footRegions\\b', 'g')) || []
    assert.equal(gated.length, 3, `expected 3 FootRegion mounts on footRegions, saw ${gated.length}`)
    assert.equal((stripped.match(/React\.createElement\(\s*FootRegion\b/g) || []).length, 3)
  })

  it('keeps the wide drafting properties dock mounted before drawing data exists', () => {
    // Standardization slice 2 renamed the surface term only: the mount gate is
    // now `dockSections` (the contract's rails.dock, truthy exactly where
    // `drafting` was) instead of `drafting`. What this pin guards is unchanged:
    // the dock must mount on a wide drafting surface BEFORE any drawing data
    // exists, so the entitlement controls cannot vanish into an honest-empty
    // state. The negative below is the regression it was written for: gating
    // the mount on data (legendEl || readoutEl) is the white-screen shape.
    assert.match(
      stripped,
      // A string for the same reason as the FootRegion pin above.
      new RegExp('if \\(studioGround && dockSections && wideViewport\\) \\{\\s*return[^;]*React\\.createElement\\(\\s*PropertiesDock'),
    )
    assert.doesNotMatch(
      stripped,
      /studioGround && (?:dockSections|drafting) && wideViewport && \(legendEl \|\| readoutEl\)/,
    )
  })

  // W4g-3b (one head): a save that carries a plan (the store's diff of the
  // head document) goes to the plan route, where the SERVER picks the commit
  // leg; a hand import (no plan) keeps the F-3 sidecar route. The opener is
  // the one caller that marks its load as the head.
  describe('W4g-3b: a save with a plan takes the plan route', () => {
    it('routes by the plan, inside the same checkout the F-3 save takes', () => {
      const start = stripped.indexOf('engineSaveTarget = ')
      assert.notEqual(start, -1)
      const body = stripped.slice(start, start + 2600)
      assert.match(body, /save: async \(bytes, parent, digest, plan = null, onStatus = null\)/)
      const planCall = body.indexOf('saveDrawingVersionPlan(')
      const sidecarCall = body.indexOf('saveEditedDrawingVersion(')
      assert.notEqual(planCall, -1)
      assert.notEqual(sidecarCall, -1)
      assert.doesNotMatch(body, /save(?:DrawingVersionPlan|EditedDrawingVersion)\([^)]*chain\.head/)
      assert.match(body, /saveDrawingVersionPlan\(\s*REQUESTED_DRAWING_ID,\s*bytes,\s*parent,\s*digest,\s*plan\.mutations,\s*cap,\s*\{\s*onStatus\s*\}\s*\)/)
      assert.match(body, /saveEditedDrawingVersion\(\s*REQUESTED_DRAWING_ID,\s*bytes,\s*parent,\s*digest,\s*cap\s*\)/)
      assert.ok(planCall < sidecarCall, 'the plan route is tried first, behind the plan check')
      assert.match(body.slice(0, planCall), /if \(plan && plan\.mutations\)/)
      // Both calls sit inside the acquire -> save -> release try block.
      assert.ok(body.indexOf('try {') < planCall && body.indexOf('} finally {') > sidecarCall)
    })

    it('the opener marks its load as the head, so the store keeps a diff base', () => {
      const opener = readFileSync(new URL('./cadedit/EngineHeadOpener.jsx', import.meta.url), 'utf8')
      assert.match(opener, /openBytes\(bytes, headDocumentId\(drawingId, version\), \{ committed: true, version \}\)/)
    })
  })

  // W4g-2 (one head), kimi on #1008 finding 1: the dirty refusal must hold at
  // EXECUTION time, not only when the run is armed. The confirm strip stays
  // open while the drafter keeps drawing (engine tools never touch the run
  // intent), so onRun, every path's last stop before runJob, reads the
  // CURRENT fact from a ref and refuses before anything is submitted.
  describe('W4g-2 one head: unsaved browser edits refuse a write tool at execution time', () => {
    const onRunStart = stripped.indexOf('onRun = useCallback')
    const onRunBody = stripped.slice(onRunStart, onRunStart + 2600)

    it('onRun refuses a write tool while the engine is dirty, before runJob', () => {
      assert.notEqual(onRunStart, -1)
      const refusal = onRunBody.indexOf('engineDirtyRef.current')
      const run = onRunBody.indexOf('runJob(')
      assert.notEqual(refusal, -1, 'onRun must read engineDirtyRef.current')
      assert.notEqual(run, -1)
      assert.ok(refusal < run, 'the dirty refusal must precede runJob')
      assert.match(onRunBody.slice(refusal, run), /REASONS\.unsavedEngineEdits[\s\S]*return null/)
    })

    it('the ref and the ribbon state are written by the one dirty-change callback the provider gets', () => {
      // Read as text spans rather than one brace-carrying pattern: the
      // honesty gate's scanner masks comments and strings and then balances
      // braces per file, and an escaped `\{` inside a regex literal reads to
      // it as an unclosed block (it reported this very file rather than
      // silently trusting it).
      const start = stripped.indexOf('onEngineDirtyChange = useCallback')
      assert.notEqual(start, -1, 'App must declare onEngineDirtyChange')
      const body = stripped.slice(start, start + 220)
      assert.match(body, /engineDirtyRef\.current = !!dirty/)
      assert.match(body, /setEngineDirty\(!!dirty\)/)
      const provider = stripped.indexOf('React.createElement(EngineSessionProvider')
      const providerLoose = provider === -1 ? stripped.indexOf('EngineSessionProvider,') : provider
      assert.notEqual(providerLoose, -1, 'the provider must be mounted')
      assert.match(stripped.slice(providerLoose, providerLoose + 400), /onDirtyChange:\s*onEngineDirtyChange/)
    })

    it('armDecision and onRun share the one write predicate', () => {
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      assert.match(stripped.slice(armStart, armStart + 1600), /isWrite = isWriteTool\(catalogTool\)/)
      assert.match(onRunBody, /isWrite = isWriteTool\(tool\)/)
    })

    it('arms after the platform session resolves even when the auth-only tenant echo is absent', () => {
      const armStart = stripped.indexOf('armDecision = useCallback')
      assert.notEqual(armStart, -1)
      const armBody = stripped.slice(armStart, armStart + 5200)
      assert.ok(armBody.includes('if (!mock && session.status !== "active") return'))
      assert.equal(armBody.includes('if (!mock && !tenant) return'), false)
      const dependencyStart = armBody.indexOf('[tools, mock, session.status')
      assert.notEqual(dependencyStart, -1)
    })

    it('the ribbon memo recomputes when the engine dirty state flips', () => {
      // The deps array that closes the ribbon useMemo names engineDirty
      // (kimi on #1008 finding 2: read inside, absent from the deps).
      assert.match(stripped, /lastAuthoredTool,\s*onUseAuthored,\s*setFamilyOpen,\s*engineDirty\s*\]\)/)
    })

    it('fails when the execution-time refusal is removed', () => {
      const mutated = appSource.replace(/if \(isWrite && engineDirtyRef\.current\) \{[\s\S]*?return null\r?\n\s*\}\r?\n/, '')
      assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
      const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
      const start = mutatedStripped.indexOf('onRun = useCallback')
      assert.doesNotMatch(mutatedStripped.slice(start, start + 2600), /engineDirtyRef\.current/)
    })
  })

  // W4g-1c (engine reach on the public demo): the opener mounts for mock
  // too, with the static sample DXF as its head at version 1; live keeps the
  // DXF route and the server's head. The rail-OFF and non-drafting gates are
  // unchanged (studioGround, drafting, intake).
  it('the head opener reaches the public demo through the static sample DXF', () => {
    assert.match(
      stripped,
      /React\.createElement\(\s*EngineHeadOpener,\s*\{[^}]*enabled:\s*!!studioGround && !!drafting && !!intake/,
    )
    assert.match(stripped, /headKey:\s*drawingState\?\.head \?\? \(mock \? 1 : null\)/)
    assert.match(stripped, /fetchDxf:\s*mock \? fetchSampleDxf : fetchDrawingDxf/)
    assert.doesNotMatch(stripped, /enabled:\s*!!studioGround && !!drafting && !mock/)
  })

  it('passes the browser engine dirty state to the assistant approval card', () => {
    const binding = new RegExp('(<ConversePanel\\b(?:(?!/>)[\\s\\S])*?)\\s+engineDirty=\\{engineDirty\\}')
    assert.match(appSource, binding)
    const mutated = appSource.replace(binding, '$1')
    assert.notEqual(mutated, appSource, 'the falsification mutation must remove the ConversePanel attribute')
    assert.doesNotMatch(mutated, binding)
  })
})
