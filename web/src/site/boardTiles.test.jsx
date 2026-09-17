import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { BoardTiles } from './BoardTiles.jsx'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'

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
