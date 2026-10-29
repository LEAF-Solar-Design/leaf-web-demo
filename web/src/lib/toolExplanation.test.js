import { describe, expect, it } from 'vitest'
import { explainTool, NOT_PROVIDED } from './toolExplanation.js'
import { WRITE_CAPABILITY } from './toolRecord.js'

describe('explainTool', () => {
  it('describes a read tool using the existing description and output schema', () => {
    const capabilities = ['drawing.read']
    const result = explainTool({ name: 'count', version: 2, description: 'count entities. Keeps their layers.', capabilities,
      returns_schema: { description: 'Entity count' } })
    expect(result.name).toBe('count')
    expect(result.version).toBe('2')
    expect(result.summary).toBe('Count entities. Keeps their layers.')
    expect(result.effect).toEqual({ editsDrawing: false, label: 'Reads the drawing; does not change it' })
    expect(result.outputs).toBe('Entity count')
    expect(result.capabilities).toEqual(capabilities)
    expect(result.capabilities).not.toBe(capabilities)
    expect(result.text.startsWith('count v2\n')).toBe(true)
  })

  it('uses the shared write capability to identify drawing edits', () => {
    expect(explainTool({ capabilities: [WRITE_CAPABILITY] }).effect).toEqual({
      editsDrawing: true, label: 'Edits the open drawing',
    })
    expect(explainTool({ capabilities: 'drawing.write' }).effect.editsDrawing).toBe(false)
  })

  it('preserves declared input order, required membership, union types and defaults', () => {
    const result = explainTool({ params: { required: ['scale'], properties: {
      scale: { type: ['number', 'null'], description: 'Scale factor', default: null },
      enabled: { type: 'boolean', default: false },
      count: { type: 'integer', default: 0 },
      other: {},
    } } })
    expect(result.inputs).toEqual([
      { name: 'scale', type: 'number or null', required: true, description: 'Scale factor', default: null },
      { name: 'enabled', type: 'boolean', required: false, description: null, default: false },
      { name: 'count', type: 'integer', required: false, description: null, default: 0 },
      { name: 'other', type: 'any', required: false, description: null, default: undefined },
    ])
    expect(result.text).toContain('Inputs:\n- scale (number or null, required): Scale factor\n- enabled (boolean, optional): no description')
  })

  it.each([undefined, null, 'x', {}, 42, false])('returns the full fallback object for %j', (tool) => {
    const result = explainTool(tool)
    expect(result).toEqual({
      name: 'Unnamed tool', version: null, summary: 'No description provided.',
      effect: { editsDrawing: false, label: 'Reads the drawing; does not change it' },
      capabilities: [], inputs: [], outputs: 'Not declared',
      provenance: [{ label: 'Source', value: 'Not declared' }, { label: 'Review status', value: 'Not declared' }],
      limits: [], notProvided: NOT_PROVIDED, text: expect.any(String),
    })
    expect(result.notProvided).not.toBe(NOT_PROVIDED)
    expect(result.text).toContain('Effect: Reads the drawing; does not change it')
    expect(result.text).toContain('Inputs: none')
    expect(result.text).toContain('Outputs: Not declared')
    expect(result.text).toContain('Not provided by this tool record:')
    for (const item of NOT_PROVIDED) expect(result.text).toContain(`- ${item}`)
    expect(result.text).not.toContain(' vnull')
  })

  it('handles malformed fields and unreadable inputs without throwing', () => {
    expect(explainTool({ name: ' ', description: {}, capabilities: {}, params: { required: 'x', properties: {
      x: null, y: { type: 12, description: false },
    } }, timeout_ms: Infinity }).inputs).toEqual([
      { name: 'x', type: 'any', required: false, description: null, default: undefined },
      { name: 'y', type: 'any', required: false, description: null, default: undefined },
    ])
    expect(explainTool({ get name() { throw new Error('unreadable') } })).toEqual(explainTool(null))
    expect(explainTool({ params: { properties: [] }, timeout_ms: '1000', idempotent: 'yes' }).limits).toEqual([])
  })

  it.each([
    [{ returns_schema: { description: 'New output' }, returns: { description: 'Legacy output' } }, 'New output'],
    [{ returns_schema: { type: 'array' }, returns: { description: 'Legacy output' } }, 'Legacy output'],
    [{ returns_schema: { type: 'array' }, returns: { type: 'object' } }, 'type array'],
    [{ returns: { type: 'number' } }, 'type number'],
  ])('uses output description and type precedence for %j', (tool, outputs) => {
    expect(explainTool(tool).outputs).toBe(outputs)
  })

  it('records agent provenance, review status, engine operation and shortened digests in order', () => {
    const result = explainTool({ provenance: { author: 'agent' }, review: { status: 'pending' },
      engine_op: 'draw:createLine', catalog_digest: '1234567890abcdef', tool_manifest_sha256: 'abcdefghijklmnop' })
    expect(result.provenance).toEqual([
      { label: 'Source', value: 'Agent-authored' },
      { label: 'Review status', value: 'pending' },
      { label: 'Engine operation', value: 'draw:createLine' },
      { label: 'Catalog digest', value: '1234567890ab' },
      { label: 'Manifest', value: 'abcdefghijkl' },
    ])
    expect(result.text).toContain('Source: Agent-authored\nReview status: pending\nEngine operation: draw:createLine\nCatalog digest: 1234567890ab\nManifest: abcdefghijkl')
    expect(explainTool({ provenance: { author: 'platform' } }).provenance[0].value).toBe('Platform built-in')
  })

  it.each([[true, 'Yes'], [false, 'No']])('renders finite time and re-run limits (%s)', (idempotent, value) => {
    const result = explainTool({ timeout_ms: 1600, idempotent })
    expect(result.limits).toEqual([{ label: 'Time limit', value: '2 s' }, { label: 'Safe to re-run', value }])
    expect(result.text).toContain(`Time limit: 2 s\nSafe to re-run: ${value}`)
    expect(explainTool({ timeout_ms: 0 }).limits).toEqual([{ label: 'Time limit', value: '0 s' }])
  })
})
