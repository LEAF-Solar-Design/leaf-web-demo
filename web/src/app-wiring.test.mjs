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
// ---------------------------------------------------------------------------
const SETTER = 'set[A-Z][\\w$]*'
const BROWSER_SETTERS = new Set(['setTimeout', 'setInterval'])

function decomment(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .split('\n')
    .map((line) => {
      const at = line.indexOf('//')
      return at === -1 ? line : line.slice(0, at)
    })
    .join('\n')
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
  it('calls no useState setter it does not declare (orphaned setter after a hoist)', () => {
    assert.deepEqual(orphanSetters(appSource), [],
      'App.jsx calls these setters but declares none of them: a component '
      + 'extraction took the useState with it and left the call sites behind')
  })

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
      /if \(studioGround && dockSections && wideViewport\) \{\s*return[^;]*React\.createElement\(\s*PropertiesDock/,
    )
    assert.doesNotMatch(
      stripped,
      /studioGround && (?:dockSections|drafting) && wideViewport && \(legendEl \|\| readoutEl\)/,
    )
  })
})
