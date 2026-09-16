// Source-shape contract for standardization slice 5a: the stage's command bar
// is the console's PromptBox. ToolCast.jsx is too large to mount here (same
// rationale as toolCastCatalogGating.test.js), so these rows read the source
// and pin the wiring the e2e rows depend on:
//   - PromptBox is mounted INSIDE the stage's own .tc-bar, after RoutePanel,
//     with the stage aliases, the caller-owned Run ladder, attachments off,
//     the G2 drop catcher off, and the route-active Enter guard.
//   - The stage's DWG/DXF drop handler still lives on .tc-bar (a drop there
//     lands drawingUpload.actions.upload).
//   - The hand-rolled rows are gone: no <input className="tc-bar-input">, no
//     runOnEnter, no static "Scope · this drawing" chip, no .tc-bar-scopes.
//   - landing.css no longer styles the retired nodes and seats the well.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (name) => readFileSync(new URL(name, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const toolCast = read('./ToolCast.jsx')
const landing = read('./landing.css')
// The JSX between the .tc-bar opening tag and the end of the commandBarBlock.
const barBlock = toolCast.slice(
  toolCast.indexOf('const commandBarBlock = ('),
  toolCast.indexOf('<SurfaceFrame\n', toolCast.indexOf('const commandBarBlock = (')),
)
// The PromptBox element inside it.
const promptBox = barBlock.slice(barBlock.indexOf('<PromptBox'), barBlock.indexOf('/>', barBlock.indexOf('<PromptBox')) + 2)

describe('slice 5a: the stage mounts PromptBox where its .tc-bar rows stood', () => {
  it('imports the console PromptBox and the stage reason ladder', () => {
    expect(toolCast).toMatch(/^import PromptBox from '\.\.\/components\/PromptBox\.jsx'$/m)
    // Slice 13d added stageHelpPaletteRow to the same import (the stage's
    // declared-and-disabled Help row, stageRunReasons.js's own note).
    expect(toolCast).toMatch(/^import \{ stageRunDisabledReason, stageHelpPaletteRow \} from '\.\/stageRunReasons\.js'$/m)
  })

  it('mounts exactly one PromptBox, inside .tc-bar, after RoutePanel', () => {
    expect(barBlock.match(/<PromptBox/g)).toHaveLength(1)
    expect(barBlock.indexOf('className={`tc-bar ')).toBeGreaterThan(-1)
    expect(barBlock.indexOf('<RoutePanel')).toBeLessThan(barBlock.indexOf('<PromptBox'))
    expect(barBlock.indexOf('className={`tc-bar ')).toBeLessThan(barBlock.indexOf('<PromptBox'))
  })

  it('passes the stage aliases as one frozen module constant', () => {
    expect(toolCast).toMatch(/^const STAGE_BAR_CLASSES = Object\.freeze\(\{ wrap: 'tc-bar-input-row', input: 'tc-bar-input', run: 'tc-run' \}\)$/m)
    expect(promptBox).toMatch(/classNames=\{STAGE_BAR_CLASSES\}/)
  })

  it('keeps the project label and the static ⌘K keycap on their e2e hooks', () => {
    // Pilot round 2 (try-canvas-shows-rooftop-while-copy-says-no-drawing):
    // the slot's literal moved from `activeDrawingId || 'No drawing'` to
    // stageDrawingLabel, which says 'Sample rooftop (preview)' only while
    // signed out with nothing mounted (StageLayer paints the sample rooftop
    // there). Signed in with nothing mounted still reads 'No drawing', which
    // live-service-surface.spec.mjs pins on .tc-bar-proj.
    expect(toolCast).toMatch(/^  const stageDrawingLabel = activeDrawingId \|\| \(sessionAuthRequired \? 'Sample rooftop \(preview\)' : 'No drawing'\)$/m)
    expect(promptBox).toMatch(/projectName=\{stageDrawingLabel\}/)
    expect(promptBox).toMatch(/projectSlot=\{<span className="bar-proj tc-bar-proj">\{stageDrawingLabel\}<\/span>\}/)
    expect(toolCast).toMatch(/^const STAGE_BAR_KEYCAP = <span className="key tc-bar-key">⌘K<\/span>$/m)
    expect(promptBox).toMatch(/keycap=\{STAGE_BAR_KEYCAP\}/)
  })

  it('hands PromptBox the OLD Run ladder as one sentence, rung for rung', () => {
    expect(promptBox).toMatch(/disabledReason=\{stageRunDisabledReason\(\{\s*sessionActive: platformSession\.status === 'active',\s*hasDrawing,\s*busy,\s*jobRunning,\s*routing,\s*loading: phase === 'loading',\s*\}\)\}/)
  })

  it('RoutePanel stays the resolver: Enter is a no-op in the well while a route shows', () => {
    expect(promptBox).toMatch(/routeActive=\{!!route\}/)
    expect(promptBox).toMatch(/hintLane=\{route\?\.lane\}/)
    expect(barBlock).toMatch(/<RoutePanel\s+route=\{route\}/)
  })

  it('dispatch, change and labels are the stage’s own', () => {
    expect(promptBox).toMatch(/onDispatch=\{dispatchRequest\}/)
    expect(promptBox).toMatch(/onChange=\{changePrompt\}/)
    expect(promptBox).toMatch(/runLabel=\{PUBLIC_DEMO \? 'Send' : 'Run'\}/)
    expect(promptBox).toMatch(/routingLabel="Routing"/)
    expect(promptBox).toMatch(/sessionId=\{sessionId\}/)
  })

  it('attachments and the G2 drop catcher are off on the stage; commandLine is on', () => {
    expect(promptBox).toMatch(/imageAttachmentsEnabled=\{false\}/)
    expect(promptBox).toMatch(/dropIngestEnabled=\{false\}/)
    expect(promptBox).toMatch(/\n\s*commandLine\n/)
  })

  it('the DWG/DXF drop still lands on the stage’s own .tc-bar handler', () => {
    const tcBar = barBlock.slice(barBlock.indexOf('className={`tc-bar '), barBlock.indexOf('<RoutePanel'))
    expect(tcBar).toMatch(/onDrop=\{\(event\) => \{[\s\S]*?drawingUpload\.actions\.upload\(file\)/)
  })

  it('the hand-rolled rows are gone from the source', () => {
    expect(toolCast).not.toMatch(/className="tc-bar-input"/)
    expect(toolCast).not.toMatch(/runOnEnter/)
    expect(toolCast).not.toMatch(/<span className="tc-bar-chip">Scope · this drawing<\/span>/)
    expect(toolCast).not.toMatch(/className="tc-bar-scopes"/)
    expect(toolCast).not.toMatch(/className="tc-bar-controls"/)
    expect(toolCast).not.toMatch(/className="tc-bar-caret"/)
    // .tc-run survives only as the alias (and the iOS ship lane's own button).
    expect(toolCast).not.toMatch(/<button type="button" className="tc-run" onClick=\{runRequest\}/)
  })

  it('landing.css retired the dead selectors and seats the well inside .tc-bar', () => {
    for (const dead of ['.tc-bar-scopes', '.tc-bar-controls', '.tc-bar-caret']) {
      expect(landing.includes(dead), `${dead} should be gone from landing.css`).toBe(false)
    }
    expect(landing).toMatch(/^\.tc-bar \.bar \{ border: 0; border-radius: inherit; background: transparent; box-shadow: none; \}$/m)
    expect(landing).toMatch(/^\.tc-bar \.bar-input \.tc-bar-input \{$/m)
    expect(landing).toMatch(/^\.tc-bar \.bar \.bar-controls \.tc-run \{$/m)
    // The phone legibility floor (>= 16px) the e2e rows read off .tc-bar-input.
    expect(landing).toMatch(/^  \.tc-bar \.bar-input \.tc-bar-input \{ min-width: 0; font-size: 16px; \}$/m)
    // Dead CSS retired: .tc-bar-blink had no renderer (the static caret never
    // toggled a class), and its keyframes went with it.
    for (const dead of ['.tc-bar-blink', '@keyframes tc-blink']) {
      expect(landing.includes(dead), `${dead} should be gone from landing.css`).toBe(false)
    }
  })

  // Carried item (round 2): hiding .tc-bar-proj and .tc-bar-key at 600px used
  // to leave .tc-run right-aligned only because the now-retired
  // .tc-bar-scopes static badge carried margin-left:auto ahead of it. A
  // toolCastStageBar.test.js source-pin (this file mounts nothing — see the
  // header comment), so this asserts the CSS text itself; the SurfaceFrame
  // fixture cannot observe the stage bar at all (a sentinel render prop,
  // surfaceFrame.render.test.jsx:103), which is why the pin lives here.
  it('the phone breakpoint restores the right-edge push onto .tc-run itself', () => {
    const phoneBlock = landing.slice(landing.indexOf('@media (max-width: 600px)'), landing.indexOf('@media (max-height: 650px)'))
    expect(phoneBlock).toMatch(/^  \.tc-bar \.bar-controls \.tc-run \{ margin-left: auto; \}$/m)
  })
})

// UI pilot round 2, record R2A (findings2.json). Source pins for the same
// reason as above: ToolCast.jsx does not mount here.
describe('pilot round 2: the signed-out stage reads as a state, not a failure', () => {
  it('a 401 on the session, and the auth-required effect, set the signed-out phase, never failed', () => {
    // The session effect's 401 branch.
    expect(toolCast).toMatch(/if \(cause\?\.status === 401\) \{\s*requireAuth\('\/api\/session'\)\s*setError\(null\)\s*setPhase\('signed-out'\)\s*return\s*\}/)
    // The sessionAuthRequired effect.
    expect(toolCast).toMatch(/if \(!sessionAuthRequired\) return\s*setLeftView\('operator'\)\s*setRightView\('execution'\)\s*setError\(null\)\s*setPhase\('signed-out'\)/)
    // The label is hollow and neutral; 'failed' keeps the red face.
    expect(toolCast).toMatch(/^  if \(phase === 'signed-out'\) return 'Not signed in'$/m)
    expect(toolCast).toMatch(/^  if \(phase === 'failed'\) return 'Request failed'$/m)
    expect(toolCast).toMatch(/^  const statusClass = phase === 'failed' \? 'red' : \(phase === 'proposal' \|\| phase === 'empty' \|\| phase === 'signed-out' \? 'hollow' : 'live'\)$/m)
  })

  it('the inert bar carries a strip with the one enabling action, on the SessionGate demo handler', () => {
    expect(toolCast).toMatch(/^  const barInert = sessionAuthRequired && !hasDrawing$/m)
    expect(toolCast).toMatch(/^  const openSampleRooftop = useCallback\(\(\) => \{ window\.location\.href = '\/try\?demo=1' \}, \[\]\)$/m)
    const tcBar = barBlock.slice(barBlock.indexOf('className={`tc-bar '), barBlock.indexOf('<RoutePanel'))
    expect(tcBar).toMatch(/\$\{barInert \? ' tc-bar-inert' : ''\}/)
    expect(tcBar).toMatch(/\{barInert && \(\s*<div className="strip-decision enter" role="status" data-testid="tc-bar-inert-strip">/)
    expect(tcBar).toMatch(/<button type="button" className="chip-act" onClick=\{openSampleRooftop\}>Open the sample rooftop<\/button>/)
    // SessionGate's Explore the demo is the same handler, not a second copy.
    expect(toolCast).toMatch(/onDemo=\{openSampleRooftop\}/)
    expect(toolCast).not.toMatch(/onDemo=\{\(\) => \{ window\.location\.href/)
  })

  it('the Execution checklist waits for a drawing or a job; before that, one sentence', () => {
    const rail = toolCast.slice(toolCast.indexOf("{rightView === 'execution' && <><div className=\"tc-events\">"), toolCast.indexOf('`previewLocked` closes the surface gap'))
    expect(rail).toMatch(/\{\(hasDrawing \|\| currentJobId \|\| linkedJobId\) \? \(<>/)
    expect(rail).toMatch(/<div className="tc-event" data-testid="tc-events-empty">\s*<span className="dot hollow" \/>\s*<span className="tc-event-text">Your first run will show its steps here<\/span>/)
    // The checklist rows themselves are unchanged (cat-standards-surface and
    // the guest upload e2e rows still read them once a drawing is mounted).
    expect(rail).toMatch(/<span className="tc-event-text">Panels preserved<\/span>/)
    expect(rail).toMatch(/<span className="tc-event-text">Tool job<\/span>/)
    expect(rail).toMatch(/<span className="tc-event-text">Version head<\/span>/)
  })
})
