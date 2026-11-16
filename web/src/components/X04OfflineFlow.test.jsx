import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingDependencyReport from './DrawingDependencyReport.jsx'
import ModuleSetPicker from './ModuleSetPicker.jsx'

afterEach(cleanup)

it('keeps supplied saved reports, candidates and recorded summaries matched while changing drawings', () => {
  const recorded = vi.fn()
  const snapshots = {
    A: { report: { coverage_note: 'A decoded coverage.', dependencies: [{ kind: 'font', name: 'a.shx', disposition: 'font remapped' }] },
      result: { sets: [{ set_id: 'A-set', layer: 'East', block: 'Panel', count: 2 }] } },
    B: { report: { coverage_note: 'B decoded coverage.', dependencies: [{ kind: 'image', name: 'b.png', disposition: 'image omitted' }] },
      result: { sets: [{ set_id: 'B-set', layer: 'West', block: 'Panel', count: 3 }] } },
  }
  const original = JSON.stringify(snapshots)
  function Flow() {
    const [drawing, setDrawing] = useState('A')
    const [results, setResults] = useState({ A: snapshots.A.result, B: snapshots.B.result })
    return <>
      <button type="button" onClick={() => setDrawing(drawing === 'A' ? 'B' : 'A')}>Switch drawing</button>
      <DrawingDependencyReport report={snapshots[drawing].report} />
      <ModuleSetPicker drawingKey={drawing} data={results[drawing]} onSelect={(ids) => {
        recorded({ drawing, ids })
        const selected_sets = results[drawing].sets.filter((row) => ids.includes(row.set_id))
        setResults({ ...results, [drawing]: { ...results[drawing], selected_sets,
          count: selected_sets.reduce((sum, row) => sum + row.count, 0) } })
      }} />
    </>
  }
  render(<Flow />)
  const switchDrawing = () => fireEvent.click(screen.getByRole('button', { name: 'Switch drawing' }))
  const count = () => fireEvent.click(screen.getByRole('button', { name: 'Count selected set' }))
  const region = () => screen.getByRole('region', { name: 'Drawing intake report' })
  expect(within(region()).getByText('a.shx')).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: 'East / Panel: 2 candidates' }))
  switchDrawing()
  expect(within(region()).getByText('b.png')).toBeTruthy()
  expect(screen.queryByText('a.shx')).toBeNull()
  const west = screen.getByRole('checkbox', { name: 'West / Panel: 3 candidates' })
  expect(west.checked).toBe(false)
  fireEvent.click(west)
  count()
  expect(recorded).toHaveBeenCalledExactlyOnceWith({ drawing: 'B', ids: ['B-set'] })
  expect(screen.getByText('Counted 3 modules on West / Panel.')).toBeTruthy()
  switchDrawing()
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(within(region()).getByText('a.shx')).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: 'East / Panel: 2 candidates' }))
  count()
  expect(recorded).toHaveBeenLastCalledWith({ drawing: 'A', ids: ['A-set'] })
  expect(screen.getByText('Counted 2 modules on East / Panel.')).toBeTruthy()
  switchDrawing()
  expect(within(region()).getByText('b.png')).toBeTruthy()
  expect(screen.getByText('Counted 3 modules on West / Panel.')).toBeTruthy()
  expect(recorded).toHaveBeenCalledTimes(2)
  expect(JSON.stringify(snapshots)).toBe(original)
})
