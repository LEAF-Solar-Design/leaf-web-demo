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
    expect(toolCast).toMatch(
      /<ProductSurfaceTabs[\s\S]{0,500}signedIn=\{isSignedIn\(\)\}[\s\S]{0,100}onSignOut=\{platformSession\.actions\.signOut\}/,
    )
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
