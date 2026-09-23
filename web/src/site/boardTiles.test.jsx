import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, createEvent, fireEvent, render, screen } from '@testing-library/react'
import { BOARD_TRANSFER_TYPE, encodeVersionRef } from '../lib/boardTransfer.js'
import { BoardTiles } from './BoardTiles.jsx'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'
import SurfaceGrounds from './SurfaceGrounds.jsx'

afterEach(cleanup)

const version = { version_id: 'v1', drawing_id: 'drawing1', seq: 1 }
const job = { job_id: 'j1', tool_name: 'Measure', status: 'succeeded' }
const tool = { tool_id: 't1', name: 'Count' }
const family = { family_id: 'f1', label: 'Measure family' }
const props = {
  workspace: { drawing_versions: [version], jobs: [job], built_tools: [tool] },
  drawing: { drawing_id: 'drawing1', name: 'Roof', polylines: 12, layers: 2 },
  catalog: { families: [family] },
}
const makeActions = () => Object.fromEntries(['onOpenDrawing', 'onOpenVersion', 'onOpenJob', 'onOpenTool', 'onOpenFamily', 'onOpenCapability'].map((name) => [name, vi.fn()]))

const makeTransferActions = () => ({ ...makeActions(), transferProjectId: 'project1', onTransferVersion: vi.fn() })
const versionRef = encodeVersionRef({ projectId: 'project1', drawingId: version.drawing_id, versionId: version.version_id, seq: version.seq })
const MAIN_BOARD_HTML = '<section class="ground-tile" data-tile="drawing" aria-label="Drawing"><h3>Drawing</h3><button type="button" class="ground-row-action" data-action="drawing" data-id="drawing1">Roof</button><p>12 polylines · 2 layers</p></section><section class="ground-tile" data-tile="versions" aria-label="Versions"><h3>Versions</h3><strong>1 drawing version</strong><ul><li data-element-id="version:v1"><button type="button" class="ground-row-action" data-action="version" data-id="v1">v1 · drawing1</button></li></ul></section><section class="ground-tile" data-tile="jobs" aria-label="Jobs"><h3>Jobs</h3><ul><li data-element-id="job:j1"><button type="button" class="ground-row-action" data-action="job" data-id="j1"><strong>Measure</strong> · succeeded</button></li></ul></section><section class="ground-tile" data-tile="tools" aria-label="Built tools"><h3>Built tools</h3><ul><li data-element-id="tool:t1"><button type="button" class="ground-row-action" data-action="tool" data-id="t1">Count</button></li></ul></section><section class="ground-tile" data-tile="catalog" aria-label="Catalog"><h3>Catalog</h3><strong>1 family · 0 tools</strong><ul><li data-element-id="family:f1"><button type="button" class="ground-row-action" data-action="family" data-id="f1">Measure family</button></li></ul></section><section class="ground-tile" data-tile="shared" aria-label="Shared everywhere"><h3>Shared everywhere</h3><ul><li><button type="button" class="ground-row-action" data-action="capability" data-id="conversation" aria-label="Open conversation">conversation</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="annotations" aria-label="Open annotations">annotations</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="authoring" aria-label="Open authoring">authoring</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="approvals" aria-label="Open approvals">approvals</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="versions" aria-label="Open versions">versions</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="receipts" aria-label="Open receipts">receipts</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="marathons" aria-label="Open marathons">marathons</button></li><li><button type="button" class="ground-row-action" data-action="capability" data-id="one-shot execution" aria-label="Open one-shot execution">one-shot execution</button></li></ul></section>'

it('SSD1-24D leaves the board unchanged without a transfer callback', () => {
  const actions = makeActions()
  const { container, rerender } = render(<BoardTiles {...props} actions={actions} />)
  expect(container.innerHTML).toBe(MAIN_BOARD_HTML)
  expect(container.querySelector('[draggable]')).toBeNull()
  expect(screen.queryByText('Preview in drawing')).toBeNull()
  expect(container.querySelector('[data-drop-target]')).toBeNull()
  rerender(<BoardTiles {...props} actions={{ ...actions, transferStatus: 'Hidden', transferProjectId: 'project1' }} />)
  expect(container.innerHTML).toBe(MAIN_BOARD_HTML)
})

it('SSD1-24D drags a version with the bounded type and copy effect', () => {
  const { container } = render(<BoardTiles {...props} actions={makeTransferActions()} />)
  const button = container.querySelector('[data-action="version"]')
  const dataTransfer = { setData: vi.fn(), effectAllowed: 'none' }
  expect(button).toHaveAttribute('draggable', 'true')
  fireEvent.dragStart(button, { dataTransfer })
  expect(dataTransfer.setData).toHaveBeenCalledWith(BOARD_TRANSFER_TYPE, versionRef)
  expect(dataTransfer.effectAllowed).toBe('copy')
})

it('SSD1-24D accepts only the version type during drag over', () => {
  const { container } = render(<BoardTiles {...props} actions={makeTransferActions()} />)
  const drawing = container.querySelector('[data-drop-target="version"]')
  for (const [type, accepted] of [[BOARD_TRANSFER_TYPE, true], ['text/plain', false]]) {
    const dataTransfer = { types: [type], dropEffect: 'none' }
    const event = createEvent.dragOver(drawing, { dataTransfer })
    fireEvent(drawing, event)
    expect(event.defaultPrevented).toBe(accepted)
    expect(dataTransfer.dropEffect).toBe(accepted ? 'copy' : 'none')
  }
})

it('SSD1-24D drops the payload onto the drawing', () => {
  const actions = makeTransferActions()
  const { container } = render(<BoardTiles {...props} actions={actions} />)
  const drawing = container.querySelector('[data-drop-target="version"]')
  const getData = vi.fn(() => versionRef)
  const event = createEvent.drop(drawing, { dataTransfer: { types: [BOARD_TRANSFER_TYPE], getData } })
  fireEvent(drawing, event)
  expect(event.defaultPrevented).toBe(true)
  expect(getData).toHaveBeenCalledWith(BOARD_TRANSFER_TYPE)
  expect(actions.onTransferVersion).toHaveBeenCalledTimes(1)
  expect(actions.onTransferVersion).toHaveBeenCalledWith(versionRef)
})

it('SSD1-24D ignores a drop without the version type', () => {
  const actions = makeTransferActions()
  const { container } = render(<BoardTiles {...props} actions={actions} />)
  const drawing = container.querySelector('[data-drop-target="version"]')
  const getData = vi.fn(() => versionRef)
  const event = createEvent.drop(drawing, { dataTransfer: { types: ['text/plain'], getData } })
  fireEvent(drawing, event)
  expect(event.defaultPrevented).toBe(false)
  expect(actions.onTransferVersion).not.toHaveBeenCalled()
  expect(getData).not.toHaveBeenCalled()
})

it('SSD1-24D previews through the accessible button', () => {
  const actions = makeTransferActions()
  render(<BoardTiles {...props} actions={actions} />)
  const button = screen.getByRole('button', { name: 'Preview v1 in the drawing' })
  expect(button).toHaveTextContent('Preview in drawing')
  fireEvent.click(button)
  expect(actions.onTransferVersion).toHaveBeenCalledTimes(1)
  expect(actions.onTransferVersion).toHaveBeenCalledWith(versionRef)
  expect(actions.onOpenVersion).not.toHaveBeenCalled()
})

it('SSD1-24D renders transfer status in the drawing', () => {
  render(<BoardTiles {...props} actions={{ ...makeTransferActions(), transferStatus: 'Previewing v1 in the drawing' }} />)
  const status = screen.getByTestId('board-transfer-status')
  expect(status).toHaveAttribute('role', 'status')
  expect(status).toHaveTextContent('Previewing v1 in the drawing')
  expect(status.closest('[data-tile]')).toHaveAttribute('data-tile', 'drawing')
})

it('SSD1-24D prevents an invalid reference drag and omits its preview button', () => {
  const actions = { ...makeTransferActions(), transferProjectId: '' }
  const { container } = render(<BoardTiles {...props} actions={actions} />)
  const button = container.querySelector('[data-action="version"]')
  const dataTransfer = { setData: vi.fn() }
  const event = createEvent.dragStart(button, { dataTransfer })
  fireEvent(button, event)
  expect(event.defaultPrevented).toBe(true)
  expect(dataTransfer.setData).not.toHaveBeenCalled()
  expect(screen.queryByText('Preview in drawing')).toBeNull()
})

it('J1 board mount delivers all six object actions through SurfaceGrounds', () => {
  const actions = makeActions()
  const { container } = render(<SurfaceGrounds surface="browser" {...props} actions={actions} />)
  for (const [kind, handler, value] of [
    ['version', 'onOpenVersion', version], ['job', 'onOpenJob', job],
    ['tool', 'onOpenTool', tool], ['family', 'onOpenFamily', family],
    ['capability', 'onOpenCapability', 'conversation'],
  ]) {
    fireEvent.click(container.querySelector(`[data-action="${kind}"]`))
    expect(actions[handler]).toHaveBeenCalledWith(value)
  }
  fireEvent.click(container.querySelector('[data-action="drawing"]'))
  expect(actions.onOpenDrawing).toHaveBeenCalledOnce()
})

it('B2 row1 preserves the markup without callbacks', () => {
  const { container, rerender } = render(<BoardTiles {...props} actions={makeActions()} />)
  expect(container.querySelectorAll('button.ground-row-action').length).toBeGreaterThan(0)
  rerender(<BoardTiles {...props} />)
  const html = container.innerHTML
  expect(container.querySelector('.ground-row-action')).toBeNull()
  rerender(<BoardTiles {...props} actions={{}} />)
  expect(container.innerHTML).toBe(html)
  expect(container.querySelector('.ground-row-action')).toBeNull()
})

it('B2 row2 opens real board objects and keeps tile identities', () => {
  const actions = makeActions()
  const { container } = render(<BoardTiles {...props} actions={actions} />)
  expect([...container.querySelectorAll('[data-tile]')].map((tile) => tile.dataset.tile))
    .toEqual(['drawing', 'versions', 'jobs', 'tools', 'catalog', 'shared'])
  for (const [kind, name, row, id] of [
    ['version', 'onOpenVersion', version, 'v1'], ['job', 'onOpenJob', job, 'j1'],
    ['tool', 'onOpenTool', tool, 't1'], ['family', 'onOpenFamily', family, 'f1'],
  ]) {
    const button = container.querySelector(`[data-action="${kind}"]`)
    expect(button.closest('li')).toHaveAttribute('data-element-id', `${kind}:${id}`)
    expect(button).toHaveAttribute('data-id', id)
    fireEvent.click(button)
    expect(actions[name]).toHaveBeenCalledWith(row)
  }
  fireEvent.click(screen.getByRole('button', { name: 'Roof' }))
  expect(actions.onOpenDrawing).toHaveBeenCalledWith()
  fireEvent.click(screen.getByRole('button', { name: 'Open conversation' }))
  expect(actions.onOpenCapability).toHaveBeenCalledWith('conversation')
})

it('B2 row3 keeps renderTile markup equal with actions', () => {
  const actions = makeActions()
  const { container, rerender } = render(<BoardTiles {...props} actions={actions} />)
  const html = container.innerHTML
  const buttonCount = container.querySelectorAll('button.ground-row-action').length
  const names = []
  rerender(<BoardTiles {...props} actions={actions} renderTile={(name, tile) => { names.push(name); return tile }} />)
  expect(names).toEqual(['drawing', 'versions', 'jobs', 'tools', 'catalog', 'shared'])
  expect(container.innerHTML).toBe(html)
  expect(container.querySelectorAll('button.ground-row-action')).toHaveLength(buttonCount)
  expect(buttonCount).toBeGreaterThan(0)
})

it('B2 row9 seats the panel above the tiles inside the same desk', () => {
  const { container, rerender } = render(<ProjectBoardGround active worldSpace={false} {...props} />)
  const html = container.innerHTML
  expect(container.querySelector('.ground-pane')).toBeNull()
  rerender(<ProjectBoardGround active worldSpace={false} {...props} panel={<section className="ground-pane">Work</section>} />)
  const panel = container.querySelector('.ground-pane')
  expect(panel.parentElement).toHaveClass('ground-desk')
  expect(panel.nextElementSibling).toHaveClass('ground-tiles')
  rerender(<ProjectBoardGround active worldSpace={false} {...props} />)
  expect(container.innerHTML).toBe(html)
})

it('B2 row10 forwards board actions to version and job tiles in flat mode', () => {
  const actions = makeActions()
  const { container } = render(<ProjectBoardGround active worldSpace={false} {...props} actions={actions} />)
  fireEvent.click(container.querySelector('button[data-action="version"]'))
  expect(actions.onOpenVersion).toHaveBeenCalledTimes(1)
  expect(actions.onOpenVersion).toHaveBeenCalledWith(version)
  fireEvent.click(container.querySelector('button[data-action="job"]'))
  expect(actions.onOpenJob).toHaveBeenCalledTimes(1)
  expect(actions.onOpenJob).toHaveBeenCalledWith(job)
})
