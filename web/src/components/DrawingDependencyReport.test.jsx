import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingDependencyReport from './DrawingDependencyReport.jsx'

afterEach(cleanup)

it('preserves frozen snapshots across repeated drawing changes and return', () => {
  const report = (name) => Object.freeze({ coverage_note: `${name} coverage`,
    dependencies: Object.freeze([Object.freeze({ kind: 'image', name: `${name}.png`, disposition: 'image omitted' })]) })
  const a = report('alpha')
  const b = report('beta')
  const before = JSON.stringify([a, b])
  const view = render(<DrawingDependencyReport report={a} />)
  for (const next of [a, b, b, a, b, a]) {
    view.rerender(<DrawingDependencyReport report={next} />)
    const isA = next === a
    expect(screen.getByText(isA ? 'alpha.png' : 'beta.png')).toBeTruthy()
    expect(screen.queryByText(isA ? 'beta.png' : 'alpha.png')).toBeNull()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(JSON.stringify([a, b])).toBe(before)
  }
})

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
  expect(screen.getByText('Dependency report unavailable.')).toBeTruthy()
  expect(screen.queryByText('No dependencies were present in the local intake records.')).toBeNull()
  view.rerender(<DrawingDependencyReport report={{ dependencies: [] }} />)
  expect(screen.getByText('No dependencies were present in the local intake records.')).toBeTruthy()
})

it('renders every dependency kind and HTML-like names as text with the coverage warning', () => {
  const image = '<img src="missing" onerror="throw 1">'
  const underlay = '<script>throw 2</script>'
  const proxy = '<svg onload="throw 3">'
  const shape = '<b>shape.shx</b>'
  const warning = 'Local conversion records only; raw metadata completeness is unproven.'
  const view = render(<DrawingDependencyReport report={{ coverage_note: warning, dependencies: [
    { kind: 'image', name: image, disposition: 'image omitted' },
    { kind: 'underlay', name: underlay, disposition: 'underlay omitted' },
    { kind: 'proxy', name: proxy, disposition: 'proxy omitted' },
    { kind: 'shape_font', name: shape, disposition: 'font remapped', remapped_to: 'sans-serif' },
  ] }} />)
  for (const name of [image, underlay, proxy, shape]) expect(screen.getByText(name)).toBeTruthy()
  for (const value of ['image omitted', 'underlay omitted', 'proxy omitted', 'font remapped to sans-serif']) {
    expect(screen.getByText(value)).toBeTruthy()
  }
  expect(screen.getByText(warning)).toBeTruthy()
  expect(screen.getAllByRole('row')).toHaveLength(5)
  expect(view.container.querySelector('img, script, svg, b')).toBeNull()
})

it('keeps the coverage warning on empty reports and removes the old drawing rows', () => {
  const view = render(<DrawingDependencyReport report={{ coverage_note: 'Drawing A coverage.', dependencies: [
    { kind: 'image', name: 'old-image.png', disposition: 'image omitted' },
  ] }} />)
  expect(screen.getByText('old-image.png')).toBeTruthy()
  view.rerender(<DrawingDependencyReport report={{ coverage_note: 'Drawing B coverage.', dependencies: [] }} />)
  expect(screen.getByText('Drawing B coverage.')).toBeTruthy()
  expect(screen.queryByText('old-image.png')).toBeNull()
  expect(screen.queryByText('Drawing A coverage.')).toBeNull()
  expect(screen.getByText('No dependencies were present in the local intake records.')).toBeTruthy()
})

it('keeps remapping targets inert instead of navigating or loading resources', () => {
  const targets = ['<img src="missing" onerror="throw 1">', 'javascript:alert(1)',
    'C:\\private\\font.ttf', '\\\\offline-host\\share\\font.shx']
  const before = window.location.href
  const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
  const fetched = vi.spyOn(globalThis, 'fetch').mockResolvedValue({})
  try {
    const view = render(<DrawingDependencyReport report={{ coverage_note: 'Conversion coverage only.',
      dependencies: targets.map((target, index) => ({ kind: 'font', name: `font-${index}.shx`,
        disposition: 'font remapped', remapped_to: target })) }} />)
    for (const target of targets) expect(screen.getByText(`font remapped to ${target}`)).toBeTruthy()
    expect(screen.getByText('Conversion coverage only.')).toBeTruthy()
    expect(view.container.querySelector('a, img, script, iframe, link, [src], [href]')).toBeNull()
    expect(opened).not.toHaveBeenCalled()
    expect(fetched).not.toHaveBeenCalled()
    expect(window.location.href).toBe(before)
  } finally {
    opened.mockRestore()
    fetched.mockRestore()
  }
})

it('reports unsupported disposition strings as unknown without claiming coverage', () => {
  render(<DrawingDependencyReport report={{ dependencies: [
    { kind: 'xref', name: 'unknown.dwg', disposition: 'teleported' },
  ] }} />)
  expect(screen.getAllByText('Unknown disposition')).toHaveLength(1)
  expect(screen.getByText('Raw dependency coverage is not confirmed.')).toBeTruthy()
  expect(screen.queryByText('No dependencies were present in the local intake records.')).toBeNull()
})

it.each([null, [], 'invalid', 4, { dependencies: {} }, { dependencies: [null] },
  { dependencies: [{ kind: {}, name: 'bad', disposition: 'detached' }] },
  { dependencies: [{ kind: 'xref', name: {}, disposition: 'detached' }] },
  { dependencies: [{ kind: 'xref', name: 'bad', disposition: {} }] },
  { dependencies: [{ kind: 'font', name: 'bad', disposition: 'font remapped', remapped_to: {} }] },
])('shows actionable unavailable for malformed report %#', (report) => {
  render(<DrawingDependencyReport report={report} />)
  expect(screen.getByText('Dependency report unavailable.')).toBeTruthy()
  expect(screen.getByText(/Run drawing intake again/)).toBeTruthy()
  expect(screen.getByText('Raw dependency coverage is not confirmed.')).toBeTruthy()
  expect(screen.queryByRole('table')).toBeNull()
  expect(screen.queryByText('No dependencies were present in the local intake records.')).toBeNull()
})
