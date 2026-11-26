import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import ReportComparison from './ReportComparison.jsx'

afterEach(cleanup)

const projectId = 'project-one'

function report(jobId, drawingVersion, toolVersion = 'v1') {
  return {
    job_id: jobId,
    input_version_id: drawingVersion,
    status: 'complete',
    result: {
      ok: true,
      tool: 'Layer report',
      version: toolVersion,
      result: { layers: [] },
      project_context: { project_id: projectId, input_version_id: drawingVersion },
    },
  }
}

const first = report('job-first', 'drawing-v1')
const second = report('job-second', 'drawing-v2', 'v2')
const third = report('job-third', 'drawing-v1')

function picker(label) {
  return screen.getByRole('combobox', { name: label })
}

it('keeps both reports selected across a refresh that preserves their identities', () => {
  const { rerender } = render(<ReportComparison jobs={[first, second]} projectId={projectId} />)
  fireEvent.change(picker('First report'), { target: { value: first.job_id } })
  fireEvent.change(picker('Second report'), { target: { value: second.job_id } })

  rerender(<ReportComparison jobs={[{ ...second }, { ...first }, third]} projectId={projectId} />)

  expect(picker('First report')).toHaveValue(first.job_id)
  expect(picker('Second report')).toHaveValue(second.job_id)
  expect(screen.getByText('Different drawing versions')).toBeVisible()
  expect(screen.getByText('Different tool versions')).toBeVisible()
})

it('requires a fresh selection when a removed report returns', () => {
  const { rerender } = render(<ReportComparison jobs={[first, second]} projectId={projectId} />)
  fireEvent.change(picker('First report'), { target: { value: first.job_id } })
  fireEvent.change(picker('Second report'), { target: { value: second.job_id } })

  rerender(<ReportComparison jobs={[first]} projectId={projectId} />)
  expect(screen.queryByRole('region', { name: 'Compare saved reports' })).toBeNull()
  rerender(<ReportComparison jobs={[first, second]} projectId={projectId} />)

  expect(picker('First report')).toHaveValue(first.job_id)
  expect(picker('Second report')).toHaveValue('')
  expect(screen.queryByText('Different drawing versions')).toBeNull()
  fireEvent.change(picker('Second report'), { target: { value: second.job_id } })
  expect(screen.getByText('Different drawing versions')).toBeVisible()
})

const duplicate = report(first.job_id, 'drawing-v3')

it.each([
  ['original first', [first, duplicate]],
  ['conflicting record first', [duplicate, first]],
  ['three records', [first, duplicate, first]],
])('excludes every record for an ambiguous job ID (%s)', (_case, ambiguous) => {
  render(<ReportComparison jobs={[...ambiguous, second, third]} projectId={projectId} />)

  for (const label of ['First report', 'Second report']) {
    const options = within(picker(label)).getAllByRole('option')
    expect(options.map(option => option.value)).toEqual(['', second.job_id, third.job_id])
  }
})
