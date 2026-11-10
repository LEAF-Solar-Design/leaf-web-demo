import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { boardDrawingFacts } from './App.jsx'
import { BoardTiles } from './site/BoardTiles.jsx'
import WorkspaceSummary from './components/WorkspaceSummary.jsx'

const VERSION = 'bf18c7ad-4777-4849-be8c-98ea6fc62afe'
const DRAWING = '7a3b7a56-1111-4222-8333-444444444444'
const version = { version_id: VERSION, drawing_id: DRAWING, project_id: 'p1', org_id: 'o1', seq: 1 }
const binding = { input_version_id: VERSION, drawing_id: DRAWING }
// The standalone demo drawing that used to leak onto a project's board.
const standalone = { polylines: Array.from({ length: 2345 }, () => ({})), layers: ['a', 'b', 'c', 'd'] }
const projectIntake = { dwg: 'distinctive-panel.dxf', polylines: [{}], layers: ['Panels'] }
const facts = (changes = {}) => boardDrawingFacts({ projectMode: true, shown: standalone, standaloneName: 'rooftop_demo',
  selectedVersion: version, binding, projectIntake, ...changes })

function board(drawing) {
  const view = render(<BoardTiles workspace={{ drawing_versions: [version] }} drawing={drawing} catalog={null} />)
  return within(view.container.querySelector('[data-tile="drawing"]'))
}

describe('project board drawing facts', () => {
  it('shows the selected project version, never the standalone drawing', () => {
    const tile = board(facts())
    expect(tile.getByText('distinctive-panel.dxf')).toBeTruthy()
    expect(tile.getByText('1 polylines · 1 layers')).toBeTruthy()
    expect(tile.getByText(VERSION)).toBeTruthy()
    expect(tile.queryByText('rooftop_demo')).toBeNull()
    expect(tile.queryByText('2345 polylines · 4 layers')).toBeNull()
  })

  it('says so when the selected version has no projection yet', () => {
    const tile = board(facts({ projectIntake: null }))
    expect(tile.getByText("This version's drawing facts are not available yet.")).toBeTruthy()
    expect(tile.getByText(VERSION)).toBeTruthy()
    expect(tile.queryByText(/polylines ·/)).toBeNull()
    expect(tile.queryByText('rooftop_demo')).toBeNull()
  })

  it('drops the tile when no version is selected or the binding names another version', () => {
    expect(facts({ selectedVersion: undefined })).toBeNull()
    expect(facts({ binding: { ...binding, input_version_id: DRAWING } })).toBeNull()
    expect(facts({ binding: null })).toBeNull()
    expect(board(null).getByText('No drawing mounted')).toBeTruthy()
  })

  it('keeps the standalone tile unchanged outside a project', () => {
    const drawing = facts({ projectMode: false })
    expect(drawing).toEqual({ name: 'rooftop_demo', polylines: 2345, layers: 4 })
    const tile = board(drawing)
    expect(tile.getByText('2345 polylines · 4 layers')).toBeTruthy()
    expect(tile.queryByText(/^Version/)).toBeNull()
  })

  it('labels the canonical version selector with the version id the CAD tab shows', () => {
    render(<WorkspaceSummary workspace={{ project: { name: 'P' }, drawing_versions: [version], jobs: [], built_tools: [] }}
      selectedVersionId={VERSION} onSelectVersion={() => {}} onClose={() => {}} />)
    const option = screen.getByRole('option', { name: 'bf18c7ad · v1' })
    expect(option.value).toBe(VERSION)
    expect(screen.queryByRole('option', { name: '7a3b7a56 · v1' })).toBeNull()
  })
})
