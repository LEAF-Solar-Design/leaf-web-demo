// Source-shape contract for the no-drawing Catalog browse (reconciliation row
// B2: the continuity rail advertises "44 tools" before a drawing exists, so
// the Catalog tab must be openable then; Author and Project stay gated on a
// drawing, since they mutate/select one). ToolCast.jsx is too large to mount
// here, same rationale as toolCastScopeWiring.test.js.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (name) => readFileSync(new URL(name, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const toolCast = read('./ToolCast.jsx')

describe('no-drawing Catalog browse wiring', () => {
  it('wires the session controller into the persistent product-nav sign-out control', () => {
    // Slice 4a: the <ProductSurfaceTabs> element moved into
    // site/SurfaceFrame.jsx, so the stage passes these two on its ONE
    // <SurfaceFrame> mount instead. Same regex shape, same two bindings,
    // pointed at the element that now carries them — plus the forwarding leg,
    // so passing them into a frame that dropped them cannot read green.
    //
    // Bounded to the open tag, not [\s\S]{0,3000}: an unbounded span could
    // walk clean past </SurfaceFrame> into an unrelated sibling that happens
    // to carry its own onSignOut prop and still read green. `[^>]` alone
    // over-tightens, though: the frame's own props include arrow functions
    // (`toast={{ ..., onDone: (id) => setToast(...) }}`) whose `=>` is a
    // literal `>` mid-attribute, so a bare `[^>]` class stops the match
    // there and never reaches signedIn at all. The `(?<=\=)>` branch lets
    // exactly that arrow through while any other `>` — in particular the
    // tag's own closing bracket — still ends the span.
    expect(toolCast).toMatch(
      /<SurfaceFrame(?:[^>]|(?<=\=)>){0,2000}signedIn=\{isSignedIn\(\)\}(?:[^>]|(?<=\=)>){0,100}onSignOut=\{platformSession\.actions\.signOut\}/,
    )
    // Slice 4b: the frame no longer hands these to <ProductSurfaceTabs>. It
    // publishes them to the ContinuityStore, which renders the sign-out
    // control the nav adopts. The forwarding leg therefore pins the publish
    // call, and the tabs must NOT still receive them (a second path would be
    // a second owner).
    const surfaceFrame = read('./SurfaceFrame.jsx')
    expect(surfaceFrame).toMatch(
      /useContinuityPublish\(\{[^}]{0,200}\bsignedIn\b[^}]{0,60}\bonSignOut\b[^}]{0,20}\}\)/,
    )
    expect(surfaceFrame).not.toMatch(/<ProductSurfaceTabs[\s\S]{0,500}signedIn=/)
    const store = read('./ContinuityStore.jsx')
    expect(store).toMatch(/<AccountSignOut signedIn=\{snapshot\.signedIn\} onSignOut=\{snapshot\.onSignOut\} \/>/)
  })

  it('gates the Catalog tab on session readiness alone, not on having a drawing open', () => {
    expect(toolCast).toMatch(/<button id="workspace-tab-catalog"[^>]*disabled=\{!sessionReady\}/)
  })
  it('keeps Author and Project gated on canOperate (session + drawing)', () => {
    expect(toolCast).toMatch(/<button id="workspace-tab-author"[^>]*disabled=\{!canOperate\}/)
    expect(toolCast).toMatch(/<button id="workspace-tab-workspace"[^>]*disabled=\{!canOperate\}/)
    expect(toolCast).toMatch(/const reviseAuthoredTool = useCallback\(\(tool\) => \{\n    if \(!canOperate \|\| !tool\?\.name \|\| authorStage\.pointer\) return/)
  })
  it('threads the no-drawing run reason from ToolCast into CapabilityCatalog', () => {
    expect(toolCast).toMatch(/<CapabilityCatalog[\s\S]{0,1000}runDisabled=\{!hasDrawing\}[\s\S]{0,100}runDisabledNote=\{CATALOG_NEEDS_DRAWING_NOTE\}/)
    expect(toolCast).toMatch(/const CATALOG_NEEDS_DRAWING_NOTE = '.+'/)
  })
})

// Slice 7a (standardization, D3 close): the workspace rail (Operator /
// Catalog / Author / Project) used to mount only inside the `stageBranch ===
// 'cad'` arm (docs/convergence/SURFACE-CONTRACT.md's D3 row). It now mounts
// wherever the contract declares `authoring` on the surface, ios excepted
// (the ship lane is checked first and keeps its own untouched rail).
describe('the workspace rail mounts on every authoring surface, not only cad', () => {
  // Slice 7b: `surfaceSlots` is the ONE `useSurfaceContract(activeSurface,
  // transportMock)` call this file also pins below (byte-identical to the
  // bare `surfaceContract(activeSurface)` for a tenant with no overlay), so
  // every declared-slot read in this describe block, including this one,
  // goes through it instead of re-reading the bare contract per call site.
  const AUTHORING_ON_STAGE_DECLARATION = /const authoringOnStage = surfaceSlots\.authoring === true/
  const AUTHORING_ON_STAGE_BARE_CONTRACT = /const authoringOnStage = surfaceContract\(activeSurface\)\.authoring === true/

  it('derives authoringOnStage from the ONE declared authoring slot, through the overlay hook', () => {
    expect(toolCast).toMatch(AUTHORING_ON_STAGE_DECLARATION)
    // A regression back to the pre-7b bare read would fail this, not just
    // silently pass a widened pattern: the two regexes are mutually exclusive.
    expect(toolCast).not.toMatch(AUTHORING_ON_STAGE_BARE_CONTRACT)
  })

  it('reads surfaceSlots off useSurfaceContract, not the bare contract function', () => {
    expect(toolCast).toMatch(/const surfaceSlots = useSurfaceContract\(activeSurface, transportMock\)/)
  })

  it('checks the ios ship-lane arm before the authoring arm, so ios never reaches it', () => {
    // Order matters: ios's OWN `authoring` slot is `true` (it really is
    // reachable from the console's un-gated rail), so the only thing that
    // keeps ios's stage on its own ship-lane rail instead of the workspace
    // rail below is evaluating `stageBranch === 'ios'` FIRST.
    expect(toolCast).toMatch(/\{stageBranch === 'ios' \? \(/)
    const iosArmIndex = toolCast.search(/\{stageBranch === 'ios' \? \(/)
    const authoringArmIndex = toolCast.search(/\) : authoringOnStage \? \(/)
    expect(iosArmIndex).toBeGreaterThan(0)
    expect(authoringArmIndex).toBeGreaterThan(iosArmIndex)
  })

  it('the workspace rail arm reads authoringOnStage, not the old cad-literal gate', () => {
    expect(toolCast).toMatch(/\) : authoringOnStage \? \(/)
    // The stage's Author tab button sits inside that arm on every surface
    // that reaches it (solar and browser included, now that the arm is not
    // cad-only), unchanged in its own disabled ladder (canOperate).
    expect(toolCast).toMatch(/id="workspace-tab-author"/)
  })

  it('the guided tour stays cad-only inside the widened arm: STAGE_TOUR_ANCHORS is null on every other surface', () => {
    expect(toolCast).toMatch(/stageBranch === 'cad' && LIVE_TOUR_REQUESTED && sessionReady && !tourOn && shouldStartTour/)
    expect(toolCast).toMatch(/\{stageBranch === 'cad' && tourOn && sessionReady && \(/)
  })

  it('falsification: a source that reverted the arm to the old cad-literal gate would not match the current probe', () => {
    // Built from fragments so this control itself cannot trip the probe above.
    const reverted = toolCast.replace(
      /\) : authoringOnStage \? \(/,
      `) : stageBranch ${'==='} 'cad' ? (`,
    )
    expect(reverted).not.toMatch(/\) : authoringOnStage \? \(/)
    expect(reverted).not.toEqual(toolCast)
  })
})
