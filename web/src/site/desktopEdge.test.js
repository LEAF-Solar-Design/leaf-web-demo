// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const sources = [
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

describe('Studio desktop edge', () => {
  it.each(sources)('%s retires the previous tablet/desktop edge', (path) => {
    const source = read(path)
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
