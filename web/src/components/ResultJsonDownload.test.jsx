import React from 'react'
import { Blob as NodeBlob } from 'node:buffer'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ResultJsonDownload from './ResultJsonDownload.jsx'
import ResultPanel from './ResultPanel.jsx'

const report = () => ({
  ok: true, tool: 'inspect-extents', version: '2.3.4', timing_ms: 17,
  project_context: { project_id: 'synthetic-project', input_version_id: 'synthetic-version', intake_sha256: 'a'.repeat(64) },
  result: { rows: [
    { layer: 'Large', area: 123.123456789, min_x: -0.000000123456 },
    { layer: 'Small', area: 7.987654321, min_x: 0.123456789 },
  ] },
})

let createObjectURL
let revokeObjectURL
const blobs = new Map()
const jsonLink = () => screen.getByRole('link', { name: 'Download result JSON' })
const csvLink = () => screen.getByRole('link', { name: /^Download rows CSV$/i })

beforeEach(() => {
  createObjectURL = vi.fn((blob) => {
    const href = `blob:result-${blobs.size}`
    blobs.set(href, blob)
    return href
  })
  revokeObjectURL = vi.fn()
  vi.stubGlobal('Blob', NodeBlob)
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  blobs.clear()
})

async function exported() {
  const blob = blobs.get(jsonLink().getAttribute('href'))
  expect(blob.type).toBe('application/json')
  return JSON.parse(await blob.text())
}

describe('generic result JSON download', () => {
  it('exports the actual row report with precision, order and returned identities through ResultPanel', async () => {
    const result = report()
    const before = JSON.stringify(result)
    render(<ResultPanel running={false} result={result} />)
    expect(jsonLink()).toHaveAttribute('download', 'result.json')
    const csv = blobs.get(csvLink().getAttribute('href'))
    expect(csv.type).toBe('text/csv;charset=utf-8')
    expect(await csv.text()).toContain('"Large","123.123456789","-1.23456e-7"')
    expect(await exported()).toEqual(result)
    expect(JSON.stringify(result)).toBe(before)
  })

  it('exports every returned row even when the panel paginates them', async () => {
    const result = report()
    result.result.rows = Array.from({ length: 107 }, (_, i) => ({ layer: `Layer ${i}`, area: i + 0.123456789 }))
    const view = render(<ResultPanel running={false} result={result} />)
    expect(view.container.querySelectorAll('table.counts tbody tr')).toHaveLength(100)
    expect((await exported()).result.rows).toEqual(result.result.rows)
  })

  it('replaces the file on a filtered rerun and releases URLs on replacement and scope clear', async () => {
    const first = report()
    const view = render(<ResultPanel running={false} result={first} />)
    const firstJson = jsonLink().getAttribute('href')
    const firstCsv = csvLink().getAttribute('href')
    const next = { ...first, result: { rows: [first.result.rows[1]] } }
    view.rerender(<ResultPanel running={false} result={next} />)
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(revokeObjectURL).toHaveBeenCalledWith(firstJson)
    expect(revokeObjectURL).toHaveBeenCalledWith(firstCsv)
    const nextJson = jsonLink().getAttribute('href')
    const nextCsv = csvLink().getAttribute('href')
    expect(new Set([firstJson, firstCsv, nextJson, nextCsv]).size).toBe(4)
    expect(await exported()).toEqual(next)
    view.rerender(<ResultPanel running={false} result={null} />)
    expect(screen.queryByRole('link', { name: 'Download result JSON' })).toBeNull()
    expect(screen.queryByRole('link', { name: /^Download rows CSV$/i })).toBeNull()
    expect(revokeObjectURL).toHaveBeenCalledTimes(4)
    expect(revokeObjectURL).toHaveBeenCalledWith(nextJson)
    expect(revokeObjectURL).toHaveBeenCalledWith(nextCsv)
  })

  it('uses the shared redaction policy for credential-shaped fields without changing drawing values', async () => {
    const result = report()
    result.result.rows[0].token = 'synthetic-sensitive-value'
    result.project_context['authorization'] = ['Bearer', 'synthetic-value'].join(' ')
    render(<ResultJsonDownload result={result} />)
    const copy = await exported()
    expect(copy.result.rows[0].token).toBe('[redacted]')
    expect(copy.project_context.authorization).toBe('[redacted]')
    expect(copy.result.rows[0].area).toBe(result.result.rows[0].area)
    expect(result.result.rows[0].token).toBe('synthetic-sensitive-value')
  })

  it('offers no file for failures or output that cannot be serialized', () => {
    const view = render(<ResultJsonDownload result={{ ...report(), ok: false }} />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(createObjectURL).not.toHaveBeenCalled()
    const cycle = report()
    cycle.result.self = cycle.result
    view.rerender(<ResultJsonDownload result={cycle} />)
    expect(screen.getByRole('status')).toHaveTextContent('Result download is unavailable.')
    expect(screen.queryByRole('link')).toBeNull()
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('releases the current file when the panel unmounts', () => {
    const view = render(<ResultJsonDownload result={report()} />)
    const href = jsonLink().getAttribute('href')
    view.unmount()
    expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith(href)
  })
})


describe('X02 deterministic placement result', () => {
  const placement = () => ({
    ok: true, tool: 'solar-add-all-inverters', job_id: 'synthetic-job',
    result: { graph_commit: {
      tool: 'solar-add-all-inverters', engine: 'deterministic_fallback',
      engine_label: 'Deterministic fallback', engine_backend: 'local_python',
      producing_attempt: { job_id: 'synthetic-job', attempt: 1 },
      graph_sha256: 'a'.repeat(64), request_sha256: 'b'.repeat(64),
    } },
  })

  it('shows the producing engine and exports the same selected run metadata', async () => {
    const result = placement()
    render(<ResultPanel result={result} />)
    expect(screen.getByText('Deterministic fallback')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Download result JSON' })).toBeVisible()
    expect((await exported()).result.graph_commit).toEqual(result.result.graph_commit)
  })

  it('rejects provenance from a different job', () => {
    const result = placement()
    result.result.graph_commit.producing_attempt.job_id = 'other-job'
    render(<ResultPanel result={result} />)
    expect(screen.queryByText('Deterministic fallback')).toBeNull()
    expect(screen.queryByRole('link', { name: 'Download result JSON' })).toBeNull()
  })
})
