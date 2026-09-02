// Source-shape contract for the scope-locked shell (tenant tool scope), in the
// same style as appCheckoutWiring.test.js: ToolCast.jsx is too large to mount
// here, so pin the wiring that makes a scoped tenant's shell single-purpose.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (name) => readFileSync(new URL(name, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const toolCast = read('./ToolCast.jsx')
const siteRoot = read('./SiteRoot.jsx')

describe('scope-locked shell wiring', () => {
  it('derives the lock from /api/entitlements through the one parser', () => {
    expect(toolCast).toMatch(/const tenantScope = useMemo\(\(\) => scopeFromEntitlements\(platform\.entitlements\)/)
    expect(toolCast).toMatch(/const scopeLocked = tenantScope !== null/)
  })
  it('drops the product-surface tabs, the marketing exits, and the Esc hint when locked', () => {
    expect(toolCast).toMatch(/\{!scopeLocked && \(\s*<ProductSurfaceTabs/)
    const guarded = toolCast.match(/\{!scopeLocked && \(\s*<button type="button" className="tc-back" onClick=\{\(\) => navigate\('\/'\)\}>Back to the site<\/button>/g) || []
    const total = toolCast.match(/>Back to the site<\/button>/g) || []
    expect(guarded.length).toBeGreaterThanOrEqual(2)
    expect(total.length).toBe(guarded.length)
    expect(toolCast).toMatch(/\{!scopeLocked && <span className="key">Esc<\/span>\}/)
  })
  it('normalizes a scoped deep link to CAD before choosing a product surface', () => {
    expect(toolCast).toMatch(/const visibleSurface = scopeLocked \? 'cad' : activeSurface/)
    expect(toolCast).toMatch(/if \(!scopeLocked \|\| activeSurface === 'cad'\) return[\s\S]{0,300}setActiveSurface\('cad'\)/)
    expect(toolCast).toMatch(/\{visibleSurface === 'cad' \? \(/)
    expect(toolCast).toMatch(/\) : visibleSurface === 'ios' \? \(/)
  })
  it('keeps only the Operator tab and renders the DOM lock marker on the rail title', () => {
    expect(toolCast).toMatch(/\{!scopeLocked && \(\s*<>\s*<button id="workspace-tab-catalog"/)
    expect(toolCast).toMatch(/<button id="workspace-tab-author"[\s\S]{0,600}<button id="workspace-tab-workspace"[\s\S]{0,400}<\/>\s*\)\}/)
    expect(toolCast).toMatch(/\{\.\.\.\(scopeLocked \? \{ \[LOCKED_SCOPE_ATTR\]: '1' \} : \{\}\)\}/)
  })
  it('swaps the conversational panel for the scoped panel, run through the slash dispatch', () => {
    expect(toolCast).toMatch(/\) : sessionId && scopeLocked \? \(\s*<ScopedToolPanel[\s\S]{0,400}onRun=\{\(tool\) => dispatchRequest\(`\/\$\{tool\.name\}`\)\}/)
    expect(toolCast).toMatch(/\) : sessionId \? \(\s*<ConversePanel/)
  })
  it('never lets Esc eject a locked tenant to the marketing cover', () => {
    expect(siteRoot).toMatch(/if \(!ownedSurface && !scopeLockActive\(\)\) navigate\('\/'\)/)
    expect(siteRoot).toMatch(/import \{ scopeLockActive \} from '\.\/tenantScope\.js'/)
  })

  // 2026-09-02 reconciliation (row B11): the deployed /try console had no
  // reachable sign-out control, only a toast buried behind Trust panel ->
  // Account details. ProductSurfaceTabs' own render is covered by
  // ProductSurfaceTabs.test.jsx; this pins that ToolCast actually WIRES the
  // real controller into it (the same platformSession.actions.signOut path
  // the Trust panel drawer already calls — never raw logout()).
  it('wires the real session controller into the persistent nav sign-out control', () => {
    expect(toolCast).toMatch(
      /<ProductSurfaceTabs\s[\s\S]{0,400}signedIn=\{isSignedIn\(\)\}\s*\n\s*onSignOut=\{platformSession\.actions\.signOut\}/,
    )
  })
})
