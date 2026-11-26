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

function areaReport(base, entries, { drawing = 'site-a', filter = 'roof', exact = true } = {}) {
  return {
    ...base,
    params: { exact_match: exact },
    result: {
      ...base.result,
      project_context: { ...base.result.project_context, drawing_id: drawing },
      result: { layer_count: entries.length, layer_filter: filter,
        layers: entries.map(([layer, area]) => ({ layer, area })) },
    },
  }
}

function compare(left, right) {
  render(<ReportComparison jobs={[left, right]} projectId={projectId} />)
  fireEvent.change(picker('First report'), { target: { value: left.job_id } })
  fireEvent.change(picker('Second report'), { target: { value: right.job_id } })
}

it.each([
  ['filter only', { filter: null, exact: false }, { filter: 'Panels', exact: false },
    ['All layers', 'Panels', 'Changed'], ['Pattern match', 'Pattern match', 'Same']],
  ['matching mode only', { filter: 'Feeder', exact: false }, { filter: 'Feeder', exact: true },
    ['Feeder', 'Feeder', 'Same'], ['Pattern match', 'Exact match', 'Changed']],
  ['both options', { filter: 'Roof', exact: true }, { filter: 'Canopy', exact: false },
    ['Roof', 'Canopy', 'Changed'], ['Exact match', 'Pattern match', 'Changed']],
  ['equal options', { filter: 'Drain', exact: true }, { filter: 'Drain', exact: true },
    ['Drain', 'Drain', 'Same'], ['Exact match', 'Exact match', 'Same']],
  ['invalid options', { filter: 17, exact: 'true' }, { filter: 'Civil', exact: true },
    ['Unknown', 'Civil', 'Unavailable'], ['Unknown', 'Exact match', 'Unavailable']],
])('compares saved options for %s', (_label, a, b, filter, matching) => {
  const left = areaReport(first, [['Example', 10]], a)
  const right = areaReport(third, [['Example', 20]], b)
  right.params.unrelated_option = 'unrelated saved value'
  compare(left, right)

  const options = screen.getByRole('table', { name: 'Saved layer report options' })
  expect(within(options).getByRole('row', { name: /Layer filter/ })).toHaveTextContent(filter.join(''))
  expect(within(options).getByRole('row', { name: /Layer matching/ })).toHaveTextContent(matching.join(''))
  expect(options).not.toHaveTextContent('unrelated saved value')
})

it('distinguishes an omitted exact-match option from missing saved params', () => {
  const left = areaReport(first, [])
  const right = areaReport(third, [])
  left.params = {}
  delete right.params
  delete right.result.result.layer_filter
  compare(left, right)

  const options = screen.getByRole('table', { name: 'Saved layer report options' })
  expect(within(options).getByRole('row', { name: /Layer filter/ })).toHaveTextContent('roofUnknownUnavailable')
  expect(within(options).getByRole('row', { name: /Layer matching/ })).toHaveTextContent('Pattern matchUnknownUnavailable')
})

it('omits the option table when the report tools or versions differ', () => {
  compare(areaReport(first, []), areaReport(second, []))

  expect(screen.getByText('Different tool versions')).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Saved layer report options' })).toBeNull()
})

it.each([
  ['case-distinct roof layers reverse', [['Roof', 16], ['ROOF', 4]], [['ROOF', 25], ['Roof', 9]],
    'Layer area ranking changed', false, 'Roof', ['1', '2']],
  ['civil layer areas change with stable ranks', [['Pad', 20], ['Access', 3]], [['Access', 6], ['Pad', 30]],
    'No rank changes among shared layers', false, 'Pad', ['1', '1']],
  ['tied ranks stay stable when row order changes', [['North', 8], ['South', 8]], [['South', 12], ['North', 12]],
    'No rank changes among shared layers', false, 'South', ['1', '1']],
  ['one electrical layer disappears', [['Feeder', 20], ['Branch', 10]], [['Feeder', 25]],
    'No rank changes among shared layers', true, 'Branch', ['2', 'Absent']],
  ['a new larger layer shifts absolute ranks only', [['Roof', 16], ['Canopy', 4]],
    [['Site', 80], ['Roof', 16], ['Canopy', 4]],
    'No rank changes among shared layers', true, 'Roof', ['1', '2']],
  ['removing a tied layer leaves the shared ranking intact', [['Pad', 20], ['Road', 20], ['Access', 3]],
    [['Pad', 20], ['Access', 3]], 'No rank changes among shared layers', true, 'Access', ['3', '2']],
  ['a new larger layer does not hide a reversal', [['Roof', 16], ['Canopy', 4]],
    [['Site', 80], ['Canopy', 25], ['Roof', 9]], 'Layer area ranking changed', true, 'Roof', ['1', '3']],
  ['each report has a different extra layer', [['Roof', 16], ['Canopy', 4], ['North', 20]],
    [['Roof', 16], ['Canopy', 4], ['South', 2]],
    'No rank changes among shared layers', true, 'Roof', ['2', '1']],
])('%s', (_label, a, b, status, matchesChanged, name, expected) => {
  compare(areaReport(first, a), areaReport({ ...third, input_version_id: 'drawing-v2',
    result: { ...third.result, project_context: { project_id: projectId, input_version_id: 'drawing-v2' } } }, b))

  expect(screen.getByText(status)).toBeVisible()
  expect(Boolean(screen.queryByText('Matching layers changed'))).toBe(matchesChanged)
  expect(Boolean(screen.queryByText(/Other matching layers can shift/))).toBe(matchesChanged)
  const row = screen.getByRole('rowheader', { name, exact: true }).closest('tr')
  expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual(expected)
})

it.each([
  ['different drawing', { drawing: 'site-b' }, [['Roof', 4]]],
  ['different filter', { filter: 'canopy' }, [['Roof', 4]]],
  ['different filter mode', { exact: false }, [['Roof', 4]]],
  ['ambiguous layer identity', {}, [['Roof', 4], ['Roof', 8]]],
  ['unusable area', {}, [['Roof', '4']]],
])('omits ranking for %s', (_label, options, layers) => {
  compare(areaReport(first, [['Roof', 16]]), areaReport(third, layers, options))

  expect(screen.getByText('Same tool version')).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Area rank within each report' })).toBeNull()
})

it('omits ranking when a report only returns part of its match set', () => {
  const partial = areaReport(third, [['Roof', 4]])
  partial.result.result.layer_count = 2
  compare(areaReport(first, [['Roof', 16], ['Canopy', 3]]), partial)

  expect(screen.getByText('Same tool version')).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Area rank within each report' })).toBeNull()
  expect(screen.queryByText('Matching layers changed')).toBeNull()
})
