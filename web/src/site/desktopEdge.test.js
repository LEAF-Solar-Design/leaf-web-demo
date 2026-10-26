// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const sources = [
  './landing.css',
  './cockpit.css',
  './studioShell.css',
  '../styles.css',
  '../structural.css',
  './cadOverview.css',
  './drawingNavigation.css',
  '../cadedit/drawingObjectList.css',
  '../demo/coach.css',
  '../solar/solarFlow.css',
  '../App.jsx',
  '../lib/useDockRise.js',
  './DraftingRibbon.jsx',
  './CadOverview.jsx',
  '../demo/FirstRunCoach.jsx',
  '../solar/SolarFlowSeat.jsx',
]

const phoneAndWideSources = [
  '../App.jsx',
  '../lib/useDockRise.js',
  './cockpit.css',
  './studioShell.css',
  './cadOverview.css',
  '../components/CostTransparencyPanel.css',
  '../integrations/ops-workspace.css',
  './projectBoard.css',
  '../styles.css',
]

describe('Studio desktop edge', () => {
  it('landing.css retires the previous phone and wide bands', () => {
    expect(read('./landing.css')).not.toMatch(/max-width\s*:\s*(?:600|1279|1200)px\s*\)/)
  })

  it.each(phoneAndWideSources)('%s retires the previous phone, wide and special bands', (path) => {
    const source = read(path)
    expect(source).not.toMatch(/max-width\s*:\s*600px\s*\)/)
    expect(source).not.toMatch(/(?:min|max)-width\s*:\s*(?:601|1199|1499|1500|1799)px\s*\)/)
  })

  it.each(sources)('%s retires the previous tablet/desktop edge', (path) => {
    // Landing's historical comments remain outside the breakpoint-only migration.
    const source = path === './landing.css'
      ? read(path).replace(/\/\*[\s\S]*?\*\//g, '')
      : read(path)
    expect(source).not.toMatch(/98[01]px/)
    expect(source).not.toMatch(/innerWidth\s*<=\s*980\b/)
    expect(source).not.toMatch(/innerWidth\s*>=\s*981\b/)
  })

  it('uses 1024px for the wide viewport state and listener', () => {
    const app = read('../App.jsx')
    expect(app).toMatch(/const \[wideViewport, setWideViewport\] = useState\(\(\) => \{\s*try \{ return window\.matchMedia\('\(min-width: 1024px\)'\)\.matches/)
    expect(app).toContain("try { mq = window.matchMedia('(min-width: 1024px)') } catch { return undefined }")
    expect(app.match(/window\.matchMedia\('\(min-width: 1024px\)'\)/g)).toHaveLength(2)
  })
})
