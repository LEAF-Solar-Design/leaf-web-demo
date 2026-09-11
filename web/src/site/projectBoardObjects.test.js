import { describe, expect, it } from 'vitest'
import { deriveProjectBoardObjects } from './projectBoardObjects.js'

describe('deriveProjectBoardObjects', () => {
  it('returns no records for absent sources', () => {
    expect(deriveProjectBoardObjects()).toEqual([])
    expect(deriveProjectBoardObjects({})).toEqual([])
    expect(deriveProjectBoardObjects({ drawing: null, workspace: null, catalog: null })).toEqual([])
  })

  it('uses only source identities, encodes keys and sorts by kind then lexical id', () => {
    const drawing = { drawing_id: 'd/1', id: 'ignored' }
    const workspace = {
      drawing_versions: [{ version_id: '2' }, { version_id: '10' }],
      jobs: [{ job_id: 'z' }, { job_id: 'A' }],
      built_tools: [{ tool_id: 't', name: 'ignored' }, { name: 'Build roof' }],
    }
    const catalog = { families: [{ family_id: 'f:1' }] }
    const records = deriveProjectBoardObjects({ drawing, workspace, catalog })
    expect(records.map(({ key }) => key)).toEqual([
      'drawing:d%2F1', 'version:10', 'version:2', 'job:A', 'job:z',
      'tool:Build%20roof', 'tool:t', 'family:f%3A1',
    ])
    expect(records[0]).toEqual({ key: 'drawing:d%2F1', kind: 'drawing', sourceId: 'd/1', source: drawing })
    expect(records[0].source).toBe(drawing)
    expect(records[1].source).toBe(workspace.drawing_versions[1])
    expect(records[7].source).toBe(catalog.families[0])
  })

  it('falls back to existing drawing id and real tool name when primary ids are absent', () => {
    expect(deriveProjectBoardObjects({
      drawing: { drawing_id: '', id: 'legacy' },
      workspace: { built_tools: [{ tool_id: null, name: 'roof' }] },
    }).map(record => record.key)).toEqual(['drawing:legacy', 'tool:roof'])
  })

  it('omits missing identities instead of using unrelated fields', () => {
    const unrelated = { id: 'wrong', name: 'wrong', profile: 'cad', position: 2 }
    expect(deriveProjectBoardObjects({
      drawing: { name: 'drawing', profile: 'cad' },
      workspace: {
        drawing_versions: [unrelated, null], jobs: [unrelated, {}],
        built_tools: [{ id: 'wrong' }, { name: '   ' }, { tool_id: {} }],
      },
      catalog: { families: [unrelated, { family_id: '' }] },
    })).toEqual([])
  })

  it('deduplicates within each kind with the first source winning', () => {
    const first = Object.freeze({ job_id: 'same', status: 'first' })
    const jobs = Object.freeze([first, Object.freeze({ job_id: 'same', status: 'second' })])
    const workspace = Object.freeze({ jobs, built_tools: Object.freeze([Object.freeze({ tool_id: 'same' })]) })
    const input = Object.freeze({ workspace })
    const records = deriveProjectBoardObjects(input)
    expect(records.map(record => record.key)).toEqual(['job:same', 'tool:same'])
    expect(records[0].source).toBe(first)
    expect(workspace.jobs).toBe(jobs)
    expect(jobs.map(job => job.status)).toEqual(['first', 'second'])
    expect(deriveProjectBoardObjects(input)).toEqual(records)
  })

  it('keeps identities stable across reorder and profile changes', () => {
    const a = { job_id: 'a' }
    const b = { job_id: 'b' }
    const derive = (jobs, profile) => deriveProjectBoardObjects({ workspace: { jobs, profile } })
    expect(derive([b, a], 'cad')).toEqual(derive([a, b], 'board'))
  })
})
