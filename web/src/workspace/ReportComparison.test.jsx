import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
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

function selectSeries(jobs) {
  for (const [index, label] of ['First report', 'Second report', 'Third report'].entries()) {
    fireEvent.change(picker(label), { target: { value: jobs[index].job_id } })
  }
}

const series = [
  areaReport(first, [['Roof', 10], ['Canopy', 10], ['Garage', 5]]),
  areaReport(report('job-middle', 'drawing-v2'), [['Roof', 10], ['Canopy', 10], ['Garage', 15]]),
  areaReport(report('job-last', 'drawing-v3'), [['Roof', 20], ['Garage', 15], ['Patio', 5]]),
]

it.each([
  ['forward', series, {
    Roof: ['1', 'Down 1', '2', 'Up 1', '1'],
    Canopy: ['1', 'Down 1', '2', 'Removed', 'Absent'],
    Garage: ['3', 'Up 2', '1', 'Down 1', '2'],
    Patio: ['Absent', 'Absent', 'Absent', 'Added', '3'],
  }],
  ['reverse', [...series].reverse(), {
    Roof: ['1', 'Down 1', '2', 'Up 1', '1'],
    Canopy: ['Absent', 'Added', '2', 'Up 1', '1'],
    Garage: ['2', 'Up 1', '1', 'Down 2', '3'],
    Patio: ['3', 'Removed', 'Absent', 'Absent', 'Absent'],
  }],
])('compares and exports three saved drawing versions in %s order', async (_label, order, ranks) => {
  render(<ReportComparison jobs={series} projectId={projectId} />)
  selectSeries(order)

  const versions = screen.getByRole('table', { name: 'Saved report versions in selected order' })
  expect(within(versions).getAllByRole('cell').map(cell => cell.textContent)).toEqual(
    order.flatMap(job => [job.job_id, 'site-a', job.input_version_id, 'Layer report', 'v1']),
  )
  expect(within(picker('Third report')).getAllByRole('option').map(option => option.value))
    .toEqual(['', order[2].job_id])

  const table = screen.getByRole('table', { name: 'Area rank changes across saved reports' })
  expect(Object.fromEntries(within(table).getAllByRole('rowheader').map(header => [
    header.textContent,
    within(header.closest('tr')).getAllByRole('cell').map(cell => cell.textContent),
  ]))).toEqual(ranks)

  const blob = await downloadReport('Download area rank table', 'saved-report-ranking-changes.html')
  expect(blob.type).toBe('text/html')
  const html = new DOMParser().parseFromString(await readText(blob), 'text/html')
  expect([...html.querySelectorAll('table')].map(element => element.outerHTML))
    .toEqual([versions.outerHTML, table.outerHTML])
})

it('shows three saved versions without ranking incompatible or incomplete layer reports', () => {
  const malformed = areaReport(series[2], [['Roof', 20]], { drawing: { name: 'site-a' } })
  const { rerender } = render(<ReportComparison jobs={[series[0], series[1], malformed]} projectId={projectId} />)
  selectSeries(series)
  expect(screen.getByText('Area ranking requires complete layer area data in all three reports.')).toBeVisible()
  expect(screen.getByText('Not recorded')).toBeVisible()

  const revisedTool = { ...series[2], result: { ...series[2].result, version: 'v2' } }
  rerender(<ReportComparison jobs={[series[0], series[1], revisedTool]} projectId={projectId} />)
  expect(screen.getByText('Area ranking requires three reports from the same known tool version.')).toBeVisible()
  expect(screen.getByRole('table', { name: 'Saved report versions in selected order' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Download area rank table' })).toBeNull()
})

it('resets all three selections when changing project even if the new project has the same job IDs', () => {
  const { rerender } = render(<ReportComparison jobs={series} projectId={projectId} />)
  selectSeries(series)
  const otherProject = 'project-two'
  const otherJobs = series.map(job => ({
    ...job, result: { ...job.result, project_context: { ...job.result.project_context, project_id: otherProject } },
  }))
  rerender(<ReportComparison jobs={otherJobs} projectId={otherProject} />)
  for (const label of ['First report', 'Second report', 'Third report']) expect(picker(label)).toHaveValue('')
  expect(screen.queryByRole('table', { name: 'Saved report versions in selected order' })).toBeNull()
})

it.each([
  ['filter only', { filter: null, exact: false }, { filter: 'Panels', exact: false },
    ['All layers', 'Panels', 'Changed'], ['Off', 'Off', 'Same']],
  ['matching mode only', { filter: 'Feeder', exact: false }, { filter: 'Feeder', exact: true },
    ['Feeder', 'Feeder', 'Same'], ['Off', 'On', 'Changed']],
  ['both options', { filter: 'Roof', exact: true }, { filter: 'Canopy', exact: false },
    ['Roof', 'Canopy', 'Changed'], ['On', 'Off', 'Changed']],
  ['equal options', { filter: 'Drain', exact: true }, { filter: 'Drain', exact: true },
    ['Drain', 'Drain', 'Same'], ['On', 'On', 'Same']],
  ['invalid options', { filter: 17, exact: 'true' }, { filter: 'Civil', exact: true },
    ['Unknown', 'Civil', 'Unavailable'], ['Unknown', 'On', 'Unavailable']],
])('compares saved options for %s', (_label, a, b, filter, matching) => {
  const left = areaReport(first, [['Example', 10]], a)
  const right = areaReport(third, [['Example', 20]], b)
  right.params.unrelated_option = 'unrelated saved value'
  compare(left, right)

  const options = screen.getByRole('table', { name: 'Saved layer report options' })
  expect(within(options).getByRole('row', { name: /Layer filter/ })).toHaveTextContent(filter.join(''))
  expect(within(options).getByRole('row', { name: /Exact match/ })).toHaveTextContent(matching.join(''))
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
  expect(within(options).getByRole('row', { name: /Exact match/ })).toHaveTextContent('OffUnknownUnavailable')
})

it.each([
  ['versions differ', { version: 'v2' }, 'Different tool versions'],
  ['tool identity differs', { tool: 'Other layer report' }, 'Different tools'],
  ['version is missing', { version: undefined }, 'Tool version could not be verified'],
])('explains why saved options are unavailable when %s', (_label, identity, result) => {
  const right = areaReport(third, [])
  Object.assign(right.result, identity)
  compare(areaReport(first, []), right)

  expect(screen.getByText(result)).toBeVisible()
  expect(screen.getByText('Comparing saved options requires two reports from the same known tool version.')).toBeVisible()
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
  ['different drawing', { drawing: 'site-b' }, [['Roof', 4]], /for the same drawing/],
  ['different filter', { filter: 'canopy' }, [['Roof', 4]], /the same layer filter and exact-match setting/],
  ['different filter mode', { exact: false }, [['Roof', 4]], /the same layer filter and exact-match setting/],
  ['ambiguous layer identity', {}, [['Roof', 4], ['Roof', 8]], /complete layer area data/],
  ['unusable area', {}, [['Roof', '4']], /complete layer area data/],
])('explains why ranking is unavailable for %s', (_label, options, layers, reason) => {
  compare(areaReport(first, [['Roof', 16]]), areaReport(third, layers, options))

  expect(screen.getByText('Same tool version')).toBeVisible()
  expect(screen.getByText(reason)).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Area rank within each report' })).toBeNull()
})

it.each(['tool', 'version'])('explains why ranking is unavailable when %s is missing', field => {
  const incomplete = areaReport(third, [['Roof', 4]])
  delete incomplete.result[field]
  compare(areaReport(first, [['Roof', 16]]), incomplete)

  expect(screen.getByText(/Area ranking requires two reports from the same known tool version/)).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Area rank within each report' })).toBeNull()
})

it('keeps rank messages out of unrelated report comparisons', () => {
  const unrelated = job => ({ ...job, result: { ...job.result, result: { stations: [] } } })
  compare(unrelated(first), unrelated(third))

  expect(screen.getByText('Same tool version')).toBeVisible()
  expect(screen.queryByText(/Area ranking requires/)).toBeNull()
})

it('omits ranking when a report only returns part of its match set', () => {
  const partial = areaReport(third, [['Roof', 4]])
  partial.result.result.layer_count = 2
  compare(areaReport(first, [['Roof', 16], ['Canopy', 3]]), partial)

  expect(screen.getByText('Same tool version')).toBeVisible()
  expect(screen.queryByRole('table', { name: 'Area rank within each report' })).toBeNull()
  expect(screen.queryByText('Matching layers changed')).toBeNull()
  expect(screen.getByText(/complete layer area data/)).toBeVisible()
})

function countReport(base, {
  sets = [['roof', 1]], handles = ['A'], drawing = 'site-a',
  intake = 'a', entry = 'b', geometry = 'c', count = 1,
} = {}) {
  return {
    ...base,
    kind: 'run', org_id: 'org-one', project_id: projectId, tool_name: 'count-modules', status: 'succeeded',
    result: {
      ...base.result,
      tool: 'count-modules',
      project_context: {
        ...base.result.project_context, organization_id: 'org-one', drawing_id: drawing, intake_sha256: intake.repeat(64),
      },
      rule_reference: { schema: 'leaf.count-rule.v1', entry_sha256: entry.repeat(64), geometry_sha256: geometry.repeat(64) },
      result: { selection_required: false, count, selected_sets: sets.map(([set_id, count]) => ({ set_id, count })) },
      overlay: { highlight_handles: handles },
    },
  }
}

it.each([['entry', { entry: 'd' }], ['geometry', { geometry: 'e' }]])(
  'exports a %s rule change with the same drawing and selection', async (_source, rule) => {
    compare(countReport(first), countReport(third, rule))

    expect(screen.getByText('Same drawing version')).toBeVisible()
    expect(screen.getByText('Count rule changed')).toBeVisible()
    expect(screen.getByText('No saved selection changes')).toBeVisible()
    expect(await downloadSelectionComparison()).toMatchObject({
      rule_changed: true, drawing_revision_changed: false, changes: [],
    })
  },
)

it('exports a drawing revision change and a changed count with the same rule', async () => {
  compare(countReport(first), countReport(second, { intake: 'f', sets: [['roof', 2]], count: 2 }))

  expect(screen.getByText('Different drawing versions')).toBeVisible()
  expect(screen.getByText('Same count rule')).toBeVisible()
  const row = screen.getByRole('row', { name: /Selected set roof/ })
  expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['1 Reference', '2 Reference'])
  expect(await downloadSelectionComparison()).toMatchObject({
    rule_changed: false, drawing_revision_changed: true,
    changes: expect.arrayContaining([{ kind: 'selected_set', id: 'roof', before: 1, after: 2,
      before_selection: first.job_id, after_selection: second.job_id }]),
  })
})

async function downloadSelectionComparison() {
  return readJson(await downloadReport('Download selection comparison', 'selection-comparison.json'))
}

async function readJson(blob) {
  return JSON.parse(await readText(blob))
}

function readText(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = reject
    reader.readAsText(blob)
  })
}

async function downloadReport(button, filename) {
  const createUrl = vi.fn(() => 'blob:selection-comparison')
  const revokeUrl = vi.fn()
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createUrl
    static revokeObjectURL = revokeUrl
  })
  try {
    fireEvent.click(screen.getByRole('button', { name: button }))
    expect(click.mock.instances[0].download).toBe(filename)
    await vi.waitFor(() => expect(revokeUrl).toHaveBeenCalledWith('blob:selection-comparison'))
    return createUrl.mock.calls[0][0]
  } finally {
    click.mockRestore()
    vi.unstubAllGlobals()
  }
}

async function downloadCountSelection(job) {
  const view = render(<ReportComparison jobs={[job]} projectId={projectId} />)
  fireEvent.click(screen.getByText('Export or compare Count selections'))
  fireEvent.change(picker('Selection to export'), { target: { value: job.job_id } })
  const blob = await downloadReport('Download Count selection', 'count-selection.json')
  view.unmount()
  return new File([blob], 'count-selection.json', { type: blob.type })
}

function importCountSelection(label, file) {
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } })
}

it.each([
  ['rule', third, { entry: 'd' }, true, false, []],
  ['revision', second, { intake: 'f', sets: [['roof', 2]], count: 2 }, false, true, ['roof', null]],
  ['mixed', second, { intake: 'f', entry: 'd', sets: [['canopy', 2]], handles: ['B'], count: 2 },
    true, true, ['roof', 'canopy', 'A', 'B', null]],
])('compares two independent %s downloads with no saved jobs loaded',
  async (_variant, afterJob, options, ruleChanged, revisionChanged, ids) => {
    const before = await downloadCountSelection(countReport(first))
    const after = await downloadCountSelection(countReport(afterJob, options))
    const { rerender } = render(<ReportComparison jobs={[]} projectId={projectId} />)
    fireEvent.click(screen.getByText('Export or compare Count selections'))
    importCountSelection('First selection file', before)
    importCountSelection('Second selection file', after)

    const comparison = await screen.findByRole('region', { name: 'Exported selection comparison' })
    expect(screen.getByText(/references come from the files and are unverified/)).toBeVisible()
    expect(within(comparison).getByText(revisionChanged ? 'Drawing revision changed' : 'Same drawing revision'))
      .toBeVisible()
    rerender(<ReportComparison jobs={[countReport(first), countReport(second)]} projectId={projectId} />)
    expect(comparison).toBeVisible()
    const result = await downloadSelectionComparison()
    expect(result).toMatchObject({
      source: 'unverified_files', rule_changed: ruleChanged, drawing_revision_changed: revisionChanged,
    })
    expect(result.changes.map(change => change.id)).toEqual(ids)
    for (const change of result.changes) {
      expect(change.before_selection).toBe(first.job_id)
      expect(change.after_selection).toBe(afterJob.job_id)
    }
    for (const link of within(comparison).queryAllByRole('link')) {
      const reference = document.getElementById(link.hash.slice(1))
      const before = link.getAttribute('aria-label').startsWith('First')
      expect(reference).toHaveTextContent(before ? first.input_version_id : afterJob.input_version_id)
      expect(reference).toHaveTextContent((before ? 'b' : options.entry || 'b').repeat(64))
    }
  },
)

it('clears an offline comparison when a file is replaced by invalid JSON or a different-project export', async () => {
  const before = await downloadCountSelection(countReport(first))
  const after = await downloadCountSelection(countReport(second, { intake: 'f' }))
  render(<ReportComparison jobs={[]} projectId={projectId} />)
  fireEvent.click(screen.getByText('Export or compare Count selections'))
  importCountSelection('First selection file', before)
  importCountSelection('Second selection file', after)
  await screen.findByRole('region', { name: 'Exported selection comparison' })
  importCountSelection('Second selection file', new File(['{'], 'bad.json'))
  expect(await screen.findByText(/could not be read/)).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Exported selection comparison' })).toBeNull()

  const foreign = {
    schema: 'leaf.saved-count-selection.v1', job_id: 'unrelated-job', project_id: 'unrelated-project',
    drawing_id: 'site-a', organization_id: 'org-one', count: 1,
    drawing_revision: { input_version_id: 'drawing-v2', intake_sha256: 'f'.repeat(64) },
    rule: { schema: 'leaf.count-rule.v1', entry_sha256: 'b'.repeat(64), geometry_sha256: 'c'.repeat(64) },
    selected_sets: [{ set_id: 'roof', count: 1 }], highlight_handles: ['B'],
  }
  importCountSelection('Second selection file', new File([JSON.stringify(foreign)], 'different-project.json'))
  expect(await screen.findByText(/Choose a Count selection export for this project/)).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Exported selection comparison' })).toBeNull()
})

it.each([
  ['full restore', ['A', 'C'], ['A', 'B'], true, [
    ['highlighted_handle', 'B', true, false, true, 'restored'],
    ['highlighted_handle', 'C', false, true, false, 'restored'],
  ]],
  ['partial restore', ['A', 'C', 'D'], ['A', 'B', 'D'], false, [
    ['selected_set', 'roof', 2, 3, 3, 'still_present'],
    ['highlighted_handle', 'B', true, false, true, 'restored'],
    ['highlighted_handle', 'C', false, true, false, 'restored'],
    ['highlighted_handle', 'D', false, true, true, 'still_present'],
    ['total_count', null, 2, 3, 3, 'still_present'],
  ]],
  ['order-only edit', ['B', 'A'], ['A', 'B'], true, []],
  ['a new change during revert', ['A', 'C'], ['A', 'B', 'E'], false, [
    ['selected_set', 'roof', 2, 2, 3, 'changed_during_revert'],
    ['highlighted_handle', 'B', true, false, true, 'restored'],
    ['highlighted_handle', 'C', false, true, false, 'restored'],
    ['highlighted_handle', 'E', false, false, true, 'changed_during_revert'],
    ['total_count', null, 2, 2, 3, 'changed_during_revert'],
  ]],
])('compares three real Count exports for %s without saved jobs loaded',
  async (_variant, editedHandles, revertedHandles, matchesOriginal, expectedChanges) => {
    const edited = report('job-edited', 'drawing-v2')
    const reverted = report('job-reverted', 'drawing-v1')
    const originalFile = await downloadCountSelection(countReport(first, {
      handles: ['A', 'B'], sets: [['roof', 2]], count: 2,
    }))
    const editedFile = await downloadCountSelection(countReport(edited, {
      handles: editedHandles, sets: [['roof', editedHandles.length]], count: editedHandles.length, intake: 'f', entry: 'd',
    }))
    const revertedFile = await downloadCountSelection(countReport(reverted, {
      handles: revertedHandles, sets: [['roof', revertedHandles.length]], count: revertedHandles.length,
    }))

    render(<ReportComparison jobs={[]} projectId={projectId} />)
    fireEvent.click(screen.getByText('Export or compare Count selections'))
    importCountSelection('First selection file', originalFile)
    importCountSelection('Second selection file', editedFile)
    await screen.findByRole('region', { name: 'Exported selection comparison' })
    importCountSelection('Reverted selection file (optional)', revertedFile)

    const history = await screen.findByRole('region', { name: 'Exported selection history' })
    expect(within(history).getByText(matchesOriginal
      ? 'Reverted selection matches the original' : 'Reverted selection differs from the original')).toBeVisible()
    expect(within(history).getByText(edited.job_id)).toBeVisible()
    expect(within(history).getByText('d'.repeat(64))).toBeVisible()
    expect(within(history).queryAllByRole('row').slice(1)).toHaveLength(expectedChanges.length)
    const result = await readJson(await downloadReport('Download selection history', 'selection-history.json'))
    expect(result).toMatchObject({
      schema: 'leaf.saved-selection-history.v1', source: 'unverified_files', matches_original: matchesOriginal,
      selections: {
        original: { job_id: first.job_id, drawing_revision: { input_version_id: 'drawing-v1' } },
        edited: { job_id: edited.job_id, drawing_revision: { input_version_id: 'drawing-v2', intake_sha256: 'f'.repeat(64) },
          rule: { entry_sha256: 'd'.repeat(64) } },
        reverted: { job_id: reverted.job_id, drawing_revision: { input_version_id: 'drawing-v1' } },
      },
    })
    expect(result.changes.map(({ kind, id, original, edited, reverted, outcome }) =>
      [kind, id, original, edited, reverted, outcome])).toEqual(expectedChanges)
  },
)

it('blocks three-file history when edited and reverted fingerprints conflict and recovers the pair when cleared', async () => {
  const original = await downloadCountSelection(countReport(first))
  const edited = await downloadCountSelection(countReport(second, { intake: 'f' }))
  const reverted = await downloadCountSelection(countReport(report('job-reverted', 'drawing-v2'), { intake: 'e' }))
  render(<ReportComparison jobs={[]} projectId={projectId} />)
  fireEvent.click(screen.getByText('Export or compare Count selections'))
  importCountSelection('Reverted selection file (optional)', reverted)
  importCountSelection('First selection file', original)
  importCountSelection('Second selection file', edited)
  expect(await screen.findByText(/Selection history found conflicting intake fingerprints/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Download selection history' })).toBeNull()

  fireEvent.change(screen.getByLabelText('Reverted selection file (optional)'), { target: { files: [] } })
  expect(await screen.findByRole('region', { name: 'Exported selection comparison' })).toBeVisible()
})

it('exports mixed selection changes with references to both saved snapshots', async () => {
  compare(countReport(first), countReport(second, { intake: 'f', entry: 'd', sets: [['canopy', 2]], handles: ['B'], count: 2 }))

  const table = screen.getByRole('table', { name: 'Saved selection changes' })
  for (const row of within(table).getAllByRole('row').slice(1)) {
    const [before, after] = within(row).getAllByRole('link')
    const beforeRef = document.getElementById(before.hash.slice(1))
    const afterRef = document.getElementById(after.hash.slice(1))
    expect(beforeRef).toHaveTextContent('drawing-v1')
    expect(beforeRef).toHaveTextContent('b'.repeat(64))
    expect(afterRef).toHaveTextContent('drawing-v2')
    expect(afterRef).toHaveTextContent('d'.repeat(64))
  }

  const comparison = await downloadSelectionComparison()
  expect(comparison).toMatchObject({ schema: 'leaf.saved-selection-comparison.v1', rule_changed: true, drawing_revision_changed: true })
  const byJob = new Map(comparison.selections.map(s => [s.job_id, s]))
  expect(comparison.changes.map(({ kind, id, before, after }) => [kind, id, before, after])).toEqual([
    ['selected_set', 'roof', 1, null], ['selected_set', 'canopy', null, 2],
    ['highlighted_handle', 'A', true, false], ['highlighted_handle', 'B', false, true],
    ['total_count', null, 1, 2],
  ])
  for (const change of comparison.changes) {
    expect(byJob.get(change.before_selection)).toMatchObject({
      drawing_revision: { input_version_id: 'drawing-v1', intake_sha256: 'a'.repeat(64) },
      rule: { entry_sha256: 'b'.repeat(64), geometry_sha256: 'c'.repeat(64) },
    })
    expect(byJob.get(change.after_selection)).toMatchObject({
      drawing_revision: { input_version_id: 'drawing-v2', intake_sha256: 'f'.repeat(64) },
      rule: { entry_sha256: 'd'.repeat(64), geometry_sha256: 'c'.repeat(64) },
    })
  }
})

it('exports handle-only changes when the sets and total match', async () => {
  compare(countReport(first, { handles: ['A', 'B'] }), countReport(third, { handles: ['B', 'C'] }))

  const { changes } = await downloadSelectionComparison()
  expect(changes.map(({ kind, id, before, after }) => [kind, id, before, after])).toEqual([
    ['highlighted_handle', 'A', true, false], ['highlighted_handle', 'C', false, true],
  ])
})

it('treats reordered sets and handles as the same selection', async () => {
  compare(
    countReport(first, { sets: [['roof', 1], ['canopy', 2]], handles: ['A', 'B'], count: 3 }),
    countReport(third, { sets: [['canopy', 2], ['roof', 1]], handles: ['B', 'A'], count: 3 }),
  )

  expect(screen.getByText('No saved selection changes')).toBeVisible()
  expect(await downloadSelectionComparison()).toMatchObject({
    changes: [], drawing_revision_changed: false, rule_changed: false,
  })
})

it('identifies the first report when its provenance is missing', () => {
  const incomplete = countReport(first)
  delete incomplete.result.rule_reference
  compare(incomplete, countReport(third))

  expect(screen.getByText(/The first report needs a saved selection/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Download selection comparison' })).toBeNull()
})


it.each([
  ['another drawing', job => { job.result.project_context.drawing_id = 'site-b' }, /same drawing/],
  ['an incomplete rule', job => { delete job.result.rule_reference }, /The second report needs a saved selection/],
  ['a set listing', job => { job.result.result.selection_required = true }, /The second report needs a saved selection/],
  ['a conflicting revision', job => { job.result.project_context.intake_sha256 = 'f'.repeat(64) }, /conflicting intake fingerprints/],
])('explains why selection export is unavailable for %s', (_label, update, reason) => {
  const other = countReport(third)
  update(other)
  compare(countReport(first), other)

  expect(screen.getByText(reason)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Download selection comparison' })).toBeNull()
})
