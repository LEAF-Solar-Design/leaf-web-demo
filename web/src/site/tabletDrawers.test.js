// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const app = read('../App.jsx')
const cockpit = read('./cockpit.css').replace(/\/\*[\s\S]*?\*\//g, '')
const query = '(max-width: 600px), (max-width: 980px) and (min-height: 500px)'
const scope = '.studio-shell .app[data-studio-shell="cockpit"][data-drawer][data-surface]'
const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const mediaBodies = (query) => [...cockpit.matchAll(new RegExp(
  `@media ${escape(query)}\\s*\\{((?:[^{}]|\\{[^{}]*\\})*)\\}`, 'g',
))].map((match) => match[1])
const ruleBody = (css, selector) => css.match(new RegExp(
  `(?:^|\\})\\s*${escape(selector)}\\s*\\{([^{}]*)\\}`,
))?.[1]

describe('tablet Studio drawers', () => {
  it('uses the drawer frame query for both viewport state and its listener', () => {
    expect(app).toContain(`const DRAWER_FRAME_QUERY = '${query}'`)
    expect(app).toContain('const [drawerViewport, setDrawerViewport] = useState(')
    expect(app.match(/window\.matchMedia\(DRAWER_FRAME_QUERY\)/g)).toHaveLength(2)
    expect(app).not.toMatch(/const\s*\[\s*phoneViewport[^\]]*\]\s*=\s*useState/)
    expect(app).not.toMatch(/matchMedia\(\s*['"]\(max-width: 600px\)['"]\s*\)/)
    expect(app).toContain('phoneViewport: drawerViewport')
  })

  it('shares the tabs and closed Plan exclusion across phone and tablet frames', () => {
    const blocks = mediaBodies(query)
    expect(blocks).toHaveLength(1)
    const tabs = ruleBody(blocks[0], `${scope} .studio-drawer-tabs`)
    expect(tabs).toMatch(/display:\s*grid;/)
    expect(tabs).toMatch(/grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\);/)
    expect(blocks[0]).toContain(`${scope}:not([data-drawer="plan"]) :is(.ent-panel, .studio-profile-info) { display: none !important; }`)
  })

  it('reserves the tablet dock and drawer row below the drawing', () => {
    const blocks = mediaBodies('(min-width: 601px) and (max-width: 980px) and (min-height: 500px)')
    const frame = blocks.find((body) => ruleBody(body, scope)?.includes('--ck-drawer:'))
    expect(frame).toBeDefined()
    const variables = ruleBody(frame, scope)
    expect(variables).toMatch(/--ck-drawer:\s*44px;/)
    expect(variables).toContain('--ck-dock: var(--studio-dock-rise, calc(66px + var(--tb-h, 32px) + var(--ck-status-rise, 0px)));')
    expect(ruleBody(frame, `${scope} .center-col`)).toContain('bottom: calc(var(--ck-dock) + var(--ck-drawer) + env(safe-area-inset-bottom, 0px));')
    expect(ruleBody(frame, `${scope} .studio-drawer-tabs`)).toContain('bottom: calc(var(--ck-dock) + env(safe-area-inset-bottom, 0px));')
  })
})
