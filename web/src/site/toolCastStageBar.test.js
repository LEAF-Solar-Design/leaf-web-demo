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
    expect(promptBox).toMatch(/projectSlot=\{<span className="bar-proj tc-bar-proj">\{activeDrawingId \|\| 'No drawing'\}<\/span>\}/)
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
