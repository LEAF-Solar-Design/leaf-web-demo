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
const viewerSource = readFileSync(new URL('./components/Viewer.jsx', import.meta.url), 'utf8')

describe('W4g S08-c: the Details drawer carries sanitized diagnostics', () => {
  it('imports the diagnostics composer and request failure collector', () => {
    assert.match(appSource, new RegExp("import \\{[^}]*\\bcomposeDiagnostics\\b[^}]*\\} from './diagnostics\\.js'"))
    assert.match(appSource, new RegExp("import \\{[^}]*\\brecentRequestFailures\\b[^}]*\\} from './api\\.js'"))
  })

  it('includes served identity and sanitized diagnostics in session details', () => {
    const start = appSource.indexOf('const openSessionDetails = useCallback(')
    assert.notEqual(start, -1)
    const end = appSource.indexOf('}, [', start)
    assert.notEqual(end, -1)
    const body = appSource.slice(start, end)
    for (const literal of ['served ', 'taskRevisionOf(', 'diagnostics: composeDiagnostics({', 'collectRefusals(document)']) {
      assert.ok(body.includes(literal), `missing session diagnostics: ${literal}`)
    }
  })

  it('passes checkout failure metadata to the banner and exposes the build on Details hover', () => {
    const checkout = appSource.match(/<CheckoutControls\s[\s\S]*?\/>/)?.[0]
    assert.ok(checkout)
    assert.ok(checkout.includes('failure={checkout.failure}'))
    const details = appSource.match(new RegExp('<button[^>]*onClick=\\{openSessionDetails\\}[^>]*>Details</button>'))?.[0]
    assert.ok(details)
    assert.ok(details.includes('title={`Session details · build ${__BUILD_HASH__}`}'))
  })
})

describe('W4g S08-a: an explicit demo starts in mock', () => {
  it('gates registry and skills discovery on mock', () => {
    const start = appSource.indexOf('const [catalogSkills, setCatalogSkills]')
    const end = appSource.indexOf('}, [mock])', start)
    assert.ok(start >= 0 && end > start)
    const effect = appSource.slice(start, end + '}, [mock])'.length)
    const guard = effect.indexOf('if (mock)')
    assert.ok(guard >= 0 && guard < effect.indexOf('fetchRegistry('))
    assert.ok(guard < effect.indexOf('fetchSkills('))
    assert.ok(effect.endsWith('}, [mock])'))
  })
  it('gates PromptBox MCP discovery on mock', () => {
    const promptBox = appSource.match(/<PromptBox\s[\s\S]*?\/>/)?.[0]
    assert.ok(promptBox)
    assert.ok(promptBox.includes('mcpDiscoveryEnabled={!mock}'))
  })
  it('decides explicit demo mode in the lazy initial state', () => {
    assert.match(appSource, new RegExp('useState\\(\\(\\) => config\\.mockDefault[\\s\\S]{0,200}explicitDemo\\(\\{'))
    assert.match(appSource, new RegExp("import \\{[^}]*\\bexplicitDemo\\b[^}]*\\} from './demoState\\.js'"))
  })
  it('renders the operator probe once and only outside mock', () => {
    assert.equal(appSource.split('<OperatorEntry />').length - 1, 1)
    assert.ok(appSource.split('\n').some((line) => line.includes('{!mock && <OperatorEntry />}')))
  })
  it('preserves the live session transitions and the 401 auto-demo hatch', () => {
    for (const literal of [
      'if (!mock) sessionActions.checking()',
      'if (!mock) sessionActions.activate({ tenant: t, tier: ti, org: o })',
      'if (!mock && is401(e)) {',
      'if (shouldAutoDemo({ authRequired: true, authConfigured, mock, signedIn: isSignedIn() })) setMock(true)',
    ]) assert.ok(appSource.includes(literal), `missing session contract: ${literal}`)
  })
})

describe('W4g bleed-2b: profile presentation preserves the engine document', () => {
  it('mounts the head opener after the engine document under its own studio and engine gates', () => {
    const ribbonEnd = appSource.indexOf('</DraftingRibbon>')
    const engine = appSource.indexOf('<EngineDocumentView')
    const opener = appSource.indexOf('<EngineHeadOpener')
    const guard = appSource.lastIndexOf('{ENV_CAD_EDIT && studioGround && (', opener)
    assert.equal(appSource.split('<EngineHeadOpener').length - 1, 1)
    assert.ok(ribbonEnd >= 0 && engine >= 0)
    assert.ok(opener > ribbonEnd && opener > engine)
    assert.ok(guard >= 0)
    assert.doesNotMatch(appSource.slice(guard, opener), /</)
  })
  it('mounts the engine document after the drafting ribbon under the studio and engine gates', () => {
    const ribbonStart = appSource.indexOf('{studioGround && drafting && (')
    const ribbonEnd = appSource.indexOf('</DraftingRibbon>', ribbonStart)
    const engine = appSource.indexOf('<EngineDocumentView')
    assert.ok(ribbonStart >= 0 && ribbonEnd > ribbonStart)
    assert.ok(engine > ribbonEnd)
    assert.match(appSource.slice(ribbonEnd, engine), new RegExp('\\)\\}\\s*[\\s\\S]*?\\{ENV_CAD_EDIT && studioGround && \\(\\s*$'))
    assert.equal(appSource.split('<EngineDocumentView').length - 1, 1)
  })
  it('closes Start before requesting a profile and commits the URL with the state', () => {
    const selectStart = appSource.indexOf('const onSelectSurface =')
    const selectEnd = appSource.indexOf('[returnToDrawing, request])', selectStart)
    assert.ok(selectStart >= 0 && selectEnd > selectStart)
    const select = appSource.slice(selectStart, selectEnd)
    assert.match(select, /returnToDrawing\(\)\s+request\(id\)/)
    assert.doesNotMatch(select, /setActiveSurface|replaceState/)
    const commitStart = appSource.indexOf('const onCommit =')
    const commitEnd = appSource.indexOf('}, [])', commitStart)
    assert.ok(commitStart >= 0 && commitEnd > commitStart)
    const commit = appSource.slice(commitStart, commitEnd)
    assert.match(commit, /setActiveSurface\(id\)/)
    assert.match(commit, /searchForProductSurface\(window\.location\.search, id\)/)
    assert.match(commit, /window\.history\.replaceState/)
    assert.match(appSource, /committed: activeSurface, onCommit, isDrafting: groundShowsDrawing/)
  })
  it('settles presentation at drawing actions and exposes phases only in the studio', () => {
    assert.match(appSource, new RegExp('<EngineSessionProvider[^>]+onBeforeEdit=\\{closeStartForChange\\}\\s+onBeforeArm=\\{onBeforeArm\\}'))
    assert.match(appSource, new RegExp('const \\{ phase, request, settle, exitPending \\} = useStudioTransition\\('))
    const clickStart = appSource.indexOf('onClickCapture=')
    const clickEnd = appSource.indexOf('onChangeCapture=', clickStart)
    assert.ok(clickStart >= 0 && clickEnd > clickStart)
    const click = appSource.slice(clickStart, clickEnd)
    assert.match(click, new RegExp('if \\(exitPending\\(\\)\\) \\{\\s*event\\.preventDefault\\(\\);\\s*event\\.stopPropagation\\(\\);\\s*return\\s*\\}\\s*settle\\(\\)\\s+returnToDrawing\\(\\)'))
    assert.match(click, /settle\(\)\s+returnToDrawing\(\)/)
    const armStart = appSource.indexOf('const onBeforeArm = useCallback(')
    const armEnd = appSource.indexOf('}, [', armStart)
    assert.ok(armStart >= 0 && armEnd > armStart)
    assert.match(appSource.slice(armStart, armEnd), /if \(exitPending\(\)\) return false\s+settle\(\)\s+return true/)
    const start = appSource.indexOf('const closeStartForChange =')
    const end = appSource.indexOf('const onReturnToDrawing =', start)
    assert.match(appSource.slice(start, end), /settle\(\)/)
    assert.match(appSource, new RegExp("data-studio-transition=\\{studioGround && phase !== 'idle' \\? phase : undefined\\}"))
    assert.match(appSource, /const effectiveGround = studioGround \? \(boardVisible \? 'board' : surfaceGround\(activeSurface\)\) : null/)
    assert.match(appSource, /const leavingGround = useLeavingGround\(effectiveGround\)/)
    assert.match(appSource, new RegExp('leavingGround=\\{leavingGround\\}'))
    const viewer = appSource.slice(appSource.indexOf('? createPortal(<div className="studio-ground-viewer"'), appSource.indexOf(': viewerEl', appSource.indexOf('? createPortal(<div className="studio-ground-viewer"')))
    assert.match(viewer, /data-ground-phase=/)
    assert.match(viewer, /aria-hidden=/)
    assert.match(viewer, /inert=/)
  })
  it('leaving studio chrome takes no pointer', () => {
    const css = readFileSync(new URL('./site/landing.css', import.meta.url), 'utf8')
    const selector = '.studio-shell .app[data-studio-transition="out"] :is('
    const start = css.indexOf(selector)
    assert.ok(start >= 0)
    const open = css.indexOf('{', start)
    const close = css.indexOf('}', open)
    assert.ok(open > start && close > open)
    assert.match(css.slice(open + 1, close), /pointer-events:\s*none\s*;/)
  })
})

describe('W4g bleed-2a: one canvas across CAD and Solar CAD', () => {
  it('passes a palette revision to the studio viewer', () => {
    const viewer = appSource.slice(appSource.indexOf('const viewerEl = ('), appSource.indexOf('const legendEl ='))
    assert.match(viewer, /paletteRevision=\{studioGround \? \(surfaceSlots\.groundMaterial\.layerAccent === 'solar' \? 'solar' : 'base'\) : undefined\}/)
    assert.match(viewer, /colorForLayer=\{studioGround \? studioColorForLayer : surfaceColorForLayer\}/)
  })
  it('keeps the studio colour callback stable through its render-time ref', () => {
    assert.match(appSource, /surfaceColorForLayerRef\.current = surfaceColorForLayer/)
    assert.match(appSource, /const studioColorForLayer = useCallback\(\(layer\) => surfaceColorForLayerRef\.current\(layer\), \[\]\)/)
  })
  it('only rebuilds for callback identity when no revision is supplied', () => {
    assert.ok(!viewerSource.includes('[activeIntake, colorForLayer, background, panelSculpture]'))
    assert.ok(viewerSource.includes('paletteRevision === undefined'))
  })
  it('exports the in-place layer recolouring helper', () => {
    assert.match(viewerSource, /export function recolorLayerGroups\(/)
  })
  it('ignores zero-size resize notifications before resizing the renderer', () => {
    assert.match(viewerSource, new RegExp('function onResize\\(\\)\\s*\\{\\s*const w = mount\\.clientWidth, h = mount\\.clientHeight\\s*if \\(!\\(w > 0\\) \\|\\| !\\(h > 0\\)\\) return\\s*renderer\\.setSize\\('))
  })
})

const appNoComments = decomment(appSource)
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code
describe('studio unobstructed drawing viewport', () => {
  it('passes the measured safe rectangle only in the studio', () => {
    assert.match(appNoComments, new RegExp('safeRect=\\{studioGround \\? drawingViewport : null\\}'))
    assert.ok(appNoComments.includes('useDrawingViewport(studioGround && groundShowsDrawing(activeSurface) ? studioGround : null, STUDIO_DRAWING_OCCLUDERS)'))
    assert.ok(appNoComments.includes('drawingViewportRef.current = drawingViewport'))
  })
  it('uses safe bounds for result visibility and Show result framing', () => {
    const start = appNoComments.indexOf('const maxInvalidFrames =')
    const show = appNoComments.indexOf('const showCreatedResult =', start)
    const end = appNoComments.indexOf('const seatVersion =', show)
    const visibility = appNoComments.slice(start, show)
    const framing = appNoComments.slice(show, end)
    assert.ok(visibility.includes('drawingViewportRef.current'))
    assert.ok(visibility.includes('canvasRect.left + safe.left'))
    assert.ok(visibility.includes('< 8'))
    assert.ok(framing.includes('drawingViewportRef.current'))
    assert.ok(framing.includes('viewer.frame('))
    assert.ok(framing.includes('viewer.setView('))
  })
  it('names all eight occluders and excludes growing command chrome and the view cube', () => {
    const start = appNoComments.indexOf('const STUDIO_DRAWING_OCCLUDERS = Object.freeze(')
    assert.ok(start >= 0)
    const list = appNoComments.slice(start, appNoComments.indexOf('])', start))
    for (const selector of ['header.top', '#drafting-ribbon', '.viewer-toolbar', '[data-testid="cockpit-view"]',
      '.properties-dock', '.bar.bar-command-line', 'footer.foot-bar', '.rail-stack']) assert.ok(list.includes(selector), selector)
    assert.ok(list.includes('reserve: 50'))
    assert.ok(!list.includes("'.bar-dock'"))
    assert.ok(!list.includes('cockpit-prompt'))
    assert.ok(!list.includes('cockpit-cube'))
  })
})
describe('Start is a view inside the current workspace profile', () => {
  it('wires all three Start controls to one memory-only handler', () => {
    assert.match(appSource, new RegExp('className="doc-tab-start"\\s+onClick=\\{onOpenStart\\}'))
    assert.match(appSource, new RegExp('className="doc-tab-close"[\\s\\S]*?onClick=\\{onOpenStart\\}'))
    assert.match(appSource, new RegExp('<StatusTabs[^>]+onStart=\\{onOpenStart\\}'))
    assert.doesNotMatch(appNoComments, /onSelectSurface\('browser'\)/)
    const start = appNoComments.indexOf('const onOpenStart =')
    const end = appNoComments.indexOf('const returnToDrawing =', start)
    const handler = appNoComments.slice(start, end)
    assert.match(handler, /if\s*\(startOpenRef\.current\)\s*return\s+startOpenerRef\.current\s*=/)
    assert.match(handler, /setStartOpen\(true\)/)
    assert.doesNotMatch(handler, /onSelectSurface|setActiveSurface|history\.|dispatchEvent|setOpenProject/)
    assert.match(appNoComments, /\[startOpen, setStartOpen\] = useState\(false\)/)
  })

  it('requests heading focus from the board only when Start opens', () => {
    assert.match(appNoComments, /\[startFocusRequest, setStartFocusRequest\] = useState\(0\)/)
    const start = appNoComments.indexOf('const onOpenStart =')
    const end = appNoComments.indexOf('const returnToDrawing =', start)
    assert.match(appNoComments.slice(start, end), /setStartFocusRequest\(\(request\) => request \+ 1\)/)
    assert.equal((appNoComments.match(/setStartFocusRequest\(/g) || []).length, 1)
    assert.match(appNoComments, /<SurfaceGrounds\s[\s\S]*?startFocusRequest=\{startFocusRequest\}/)
    assert.doesNotMatch(appNoComments, /boardHeadingRef\.current\?\.focus\(\)/)
  })

  it('returns to the drawing at engine, catalog and version sinks', () => {
    assert.match(appNoComments, new RegExp('<EngineSessionProvider[^>]+onBeforeEdit=\\{closeStartForChange\\}'))
    const runStart = appNoComments.indexOf('const onRun = useCallback')
    const runEnd = appNoComments.indexOf('const onConfirmCatalogRun =', runStart)
    assert.notEqual(runStart, -1)
    assert.notEqual(runEnd, -1)
    assert.match(appNoComments.slice(runStart, runEnd), /if\s*\(writeLocked && isWrite\)\s*return null\s+closeStartForChange\(\)/)
    for (const [name, target] of [
      ['onPreviewVersionTracked', 'onPreviewVersion'],
      ['onBackToHeadTracked', 'onBackToHead'],
      ['onRestoredTracked', 'onRestoreCommitted'],
    ]) {
      const start = appNoComments.indexOf(`const ${name} = useCallback`)
      const end = appNoComments.indexOf('])', start)
      assert.notEqual(start, -1)
      assert.notEqual(end, -1)
      const callback = appNoComments.slice(start, end + 2)
      assert.match(callback, new RegExp('closeStartForChange\\(\\)[\\s\\S]*?return ' + target + '\\(\\.\\.\\.args\\)'))
      assert.match(callback, new RegExp('\\[' + target + ', closeStartForChange\\]'))
    }
    for (const [prop, callback] of [
      ['onPreview', 'onPreviewVersionTracked'],
      ['onBackToHead', 'onBackToHeadTracked'],
      ['onRestored', 'onRestoredTracked'],
    ]) {
      assert.match(appNoComments, new RegExp('<VersionHistory\\s[^>]*' + prop + '=\\{' + callback + '\\}'))
    }
  })

  it('closes Start for changes without a synchronous flush or focus move', () => {
    const start = appNoComments.indexOf('const closeStartForChange = useCallback')
    const end = appNoComments.indexOf('const onReturnToDrawing =', start)
    assert.notEqual(start, -1)
    assert.notEqual(end, -1)
    const close = appNoComments.slice(start, end)
    assert.match(close, /if\s*\(!startOpenRef\.current\)\s*return/)
    assert.match(close, /startOpenRef\.current\s*=\s*false/)
    assert.match(close, /setStartOpen\(false\)/)
    assert.doesNotMatch(close, /flushSync|focus/)
    for (const [component, prop] of [
      ['EngineSessionProvider', 'onBeforeEdit'],
      ['ConversePanel', 'onBeforeWriteApproval'],
      ['VersionHistory', 'onBeforeRestore'],
    ]) {
      const mountStart = appNoComments.indexOf('<' + component)
      assert.notEqual(mountStart, -1)
      const mountEnd = appNoComments.indexOf('/>', mountStart)
      assert.notEqual(mountEnd, -1)
      assert.ok(appNoComments.slice(mountStart, mountEnd).includes(prop + '={closeStartForChange}'))
    }
  })

  it('closes Start before either viewer recovery request', () => {
    assert.match(appNoComments, /retryRefresh:\s*onRetryViewerRefreshRaw/)
    assert.match(appNoComments, /retryUnreadableHead:\s*retryUnreadableHeadRaw/)
    for (const name of ['onRetryViewerRefresh', 'retryUnreadableHead']) {
      const start = appNoComments.indexOf('const ' + name + ' = useCallback')
      const end = appNoComments.indexOf('])', start)
      assert.notEqual(start, -1)
      assert.notEqual(end, -1)
      const callback = appNoComments.slice(start, end + 2)
      assert.match(callback, new RegExp('closeStartForChange\\(\\)\\s+return '
        + name + 'Raw\\(\\.\\.\\.args\\)'))
      assert.match(callback, new RegExp('\\[' + name + 'Raw, closeStartForChange\\]'))
    }
  })
  it('shows the Run and Build gloss beside the shared prompt for every visible board', () => {
    const dock = appNoComments.indexOf('<div className="bar-dock">')
    assert.notEqual(dock, -1)
    const gloss = appNoComments.slice(dock, dock + 280)
    assert.match(gloss, /boardVisible &&/)
    assert.match(gloss, /START_BOARD_COPY\.gloss/)
    assert.match(gloss, /mock &&/)
    assert.doesNotMatch(gloss, /openProjectId/)
    assert.match(appNoComments, new RegExp('const shell = \\{\\s*startOpen,'))
    assert.match(appNoComments, /onCloseStart: onReturnToDrawing/)
    assert.match(appNoComments, new RegExp('boardVisible=\\{boardVisible\\}'))
  })
})
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
  it('names the drawing refresh wait when interrupting a finished job', () => {
    const start = stripped.indexOf('onInterruptRun: () => {')
    const end = stripped.indexOf('onClearSelection:', start)
    assert.ok(start >= 0 && end > start, 'interrupt handler must survive the transform')
    const handler = stripped.slice(start, end)
    assert.match(handler, new RegExp('showToast\\(result != null\\s*\\?\\s*\\{\\s*text:\\s*`Stopped waiting for the drawing to refresh after \\$\\{tool\\}\\.`'))
    assert.match(stripped, /interruptRun,\s*currentJob\?\.tool,\s*currentJob\?\.job_id,\s*result,\s*mock,\s*showToast/)
  })

  it('acknowledges interrupting live and mock runs after detaching', () => {
    const start = stripped.indexOf('onInterruptRun: () => {')
    const end = stripped.indexOf('onClearSelection:', start)
    assert.ok(start >= 0 && end > start, 'interrupt handler must survive the transform')
    const handler = stripped.slice(start, end)
    assert.match(handler, /interruptRun\(\);[\s\S]*showToast\(/)
    assert.match(handler, /typeof currentJob\?\.tool === "string" && currentJob\.tool\.trim\(\) \? currentJob\.tool : "the run"/)
    assert.match(handler, new RegExp('text:\\s*mock\\s*\\?\\s*`Stopped waiting for \\$\\{tool\\}\\.`\\s*:\\s*currentJob\\?\\.job_id\\s*\\?\\s*`Stopped following \\$\\{tool\\}\\. It keeps running; find it in Jobs\\.`\\s*:\\s*`Stopped waiting for \\$\\{tool\\} to be accepted\\. If the server accepts it, it will appear in Jobs\\.`'))
  })

  it('names the quick tools as version history actions', () => {
    assert.match(stripped, /id: "quick-undo",\s*label: "Undo version"/)
    assert.match(stripped, /id: "quick-redo",\s*label: "Redo version"/)
  })

  it('only swallows parseable unarmed points, so comma prose reaches the catalog', () => {
    assert.match(stripped, /if \(parsePointExpression\(text\) !== null\)/)
    assert.doesNotMatch(stripped, /isPointExpression\(text\)/)
  })
  describe('S03 one drawing truth', () => {
    // The engine paints the viewer while shown; the console intake remains
    // the exact fallback when the consumer closes or unmounts.
    it('binds selection, geometry, legend, dock totals and status to the active intake', () => {
      assert.match(stripped, /const drawingIntake = activeIntake \|\| shown/)
      assert.match(stripped, /countEntitiesByLayer\(drawingIntake\)/)
      assert.match(stripped, /selectEntity\(drawingIntake, selectedHandle,/)
      assert.match(stripped, /drawingIntake\.polylines \|\| \[\]/)
      assert.match(stripped, /drawingExtents\(drawingIntake\.polylines\)/)
      assert.match(appNoComments, new RegExp('<Legend\\s+layers=\\{drawingIntake.layers\\}'))
      assert.match(appNoComments, new RegExp('<CockpitStatus[^>]*shown=\\{drawingIntake\\}'))
      assert.match(appNoComments, new RegExp('onShown=\\{\\(intake, history\\) => \\{\\s*setActiveIntake\\(intake\\)'))
      assert.match(appNoComments, new RegExp('onHidden=\\{\\(\\) => \\{\\s*setActiveIntake\\(null\\)'))
      assert.match(appNoComments, /undoDepth: history.undoDepth, redoDepth: history.redoDepth/)
    })
    it('drops a result from a different document before resolving its handle', () => {
      assert.match(appNoComments, new RegExp('if \\(!resultCandidate \\|\\| !activeIntake \\|\\| resultCandidate.documentId !== activeIntake.documentId\\) return null'))
      assert.match(appNoComments, new RegExp('if \\(!intake \\|\\| \\(resultCandidate && resultCandidate.documentId !== intake.documentId\\)\\) \\{\\s*setResultCandidate\\(null\\)\\s*setOffscreenResult\\(null\\)'))
      assert.match(appNoComments, new RegExp('if \\(intake && history\\?\\.createdResult\\?\\.documentId === intake.documentId\\)'))
    })
    it('passes the visibility result and viewer pose handler to the dock', () => {
      assert.match(appNoComments, new RegExp('offscreenResult=\\{offscreenResult\\}\\s+onShowResult=\\{showCreatedResult\\}'))
      assert.match(stripped, new RegExp('viewer\\.setView\\(\\{\\s*center:'))
      assert.match(stripped, /pose\.zoom \* 0\.4 \/ span/)
    })
  })

  describe('W4g selection mirror back', () => {
    it('mounts StatusModesBridge under the engine flag', () => {
      assert.match(appSource, /\{ENV_CAD_EDIT && <StatusModesBridge \/>\}/)
    })
    it('passes the console selection setter to EngineDocumentView', () => {
      assert.match(appSource, /<EngineDocumentView\b(?:(?!\/>)[\s\S])*?\bonSelectedHandleChange=\{setSelectedHandle\}/)
    })
  })

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
    // Falsification: remove the state setter used by the author-panel wrapper.
    const mutated = appSource.replace(
      /const \[authorOpen, setAuthorOpenState\] = useState\(false\)/, '')
    assert.notEqual(mutated, appSource, 'the falsification mutation must apply')
    assert.deepEqual(orphanSetters(mutated), ['setAuthorOpenState'])
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
      const body = stripped.slice(start, start + 4400)
      assert.match(body, /save: async \(bytes, parent, digest, plan = null, onStatus = null\)/)
      // W1: a hand import gets the real parent, and an unknown job keeps its fence.
      assert.match(body, /if \(parent == null\)\s*parent = \(await getDrawingVersions\(false, REQUESTED_DRAWING_ID\)\)\.head/)
      assert.match(body, /keepLock = error\?\.outcomeUnknown === true/)
      assert.match(body, /if \(acquired && !keepLock\)/)
      // W2: an unknown save reuses its own checkout until a terminal outcome.
      assert.match(stripped, /pendingSavesRef = useRef\((?:\/\*[^*]*\*\/\s*)?new Map\(\)\)/)
      assert.match(body, /pendingSavesRef\.current\.get\(REQUESTED_DRAWING_ID\)/)
      assert.match(body, /pendingSavesRef\.current\.set\(REQUESTED_DRAWING_ID/)
      assert.match(body, /pendingSavesRef\.current\.delete\(REQUESTED_DRAWING_ID\)/)
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

  it('limits studio point picking and cursor readout to the viewer canvas', () => {
    for (const component of ['CanvasPointPicker', 'CockpitStatus']) {
      const mount = appNoComments.match(new RegExp('<' + component + '\\s[\\s\\S]*?/>'))?.[0]
      assert.ok(mount, component + ' mount exists')
      assert.match(mount, /canvasSelector="\.viewer-canvas"/)
      assert.match(mount, new RegExp('ground=\\{studioGround\\}'))
    }
  })

  it('opts studio frame and grounds into customer copy with explicit mock state', () => {
    const grounds = appNoComments.match(/<SurfaceGrounds\s[\s\S]*?\/>/)?.[0]
    assert.ok(grounds, 'SurfaceGrounds mount exists')
    assert.match(grounds, new RegExp('occluders=\\{STUDIO_DRAWING_OCCLUDERS\\}'))
    for (const component of ['SurfaceFrame', 'SurfaceGrounds']) {
      const mount = new RegExp('<' + component + '\\s[\\s\\S]*?/>|<' + component + '\\s[\\s\\S]*?>')
      const source = appNoComments.match(mount)?.[0]
      assert.ok(source, component + ' mount exists')
      assert.match(source, new RegExp('studioPresentation=\\{Boolean\\(studioGround\\)\\}'))
      assert.match(source, new RegExp('mock=\\{mock\\}'))
    }
  })
})
