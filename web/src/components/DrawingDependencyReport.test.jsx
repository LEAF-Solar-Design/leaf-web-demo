import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import DrawingDependencyReport from './DrawingDependencyReport.jsx'

afterEach(cleanup)

it('shows each dependency disposition, font fallback and coverage limit', () => {
  render(<DrawingDependencyReport report={{ coverage_note: 'Converted records only.', dependencies: [
    { kind: 'font', name: 'notes.shx', disposition: 'font remapped', remapped_to: 'sans-serif' },
    { kind: 'xref', name: 'external.dwg', disposition: 'detached' },
  ] }} />)
  expect(screen.getByText('Converted records only.')).toBeTruthy()
  expect(screen.getByText('notes.shx')).toBeTruthy()
  expect(screen.getByText('font remapped to sans-serif')).toBeTruthy()
  expect(screen.getByText('external.dwg')).toBeTruthy()
  expect(screen.getByText('detached')).toBeTruthy()
})

it('does not claim no dependencies when the report is absent', () => {
  const view = render(<DrawingDependencyReport />)
  expect(view.container.textContent).toBe('')
  view.rerender(<DrawingDependencyReport report={{ dependencies: [] }} />)
  expect(screen.getByText('No dependencies were present in the local intake records.')).toBeTruthy()
})
