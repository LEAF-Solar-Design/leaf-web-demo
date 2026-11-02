import { describe, expect, it } from 'vitest'
import { confirmedLayout, headerField, headerPosition, patchHeader, requestedHeaderPosition, sameHeaderField } from './workspaceLayout.js'

describe('bounded workspace header layout', () => {
  it('recognizes only a clear header-position request and normalizes unknown values', () => {
    expect(requestedHeaderPosition('Move the workspace header below the tools')).toBe('below-tools')
    expect(requestedHeaderPosition('Put the header above the CAD ribbon')).toBe('above-tools')
    for (const text of ['Move panels below roof', 'header above and below tools', 'color:red', 'highlight panels']) expect(requestedHeaderPosition(text)).toBeNull()
    expect(headerPosition('position:fixed')).toBe('above-tools')
  })
  it('uses the desired clause when the current header position is also named', () => {
    expect(requestedHeaderPosition('the header below the cad tools should be above it')).toBe('above-tools')
    expect(requestedHeaderPosition('the header above the cad tools should be below it')).toBe('below-tools')
    expect(requestedHeaderPosition('the header below the cad tools should be above and below it')).toBeNull()
  })
  it('patches and reverses only the selected profile field while preserving newer unrelated settings', () => {
    const base = { cad: { toolbar: { home: 'draw' }, rails: { right: 'job-spine' } }, solar: { authoring: true } }
    const before = headerField(base, 'cad')
    const applied = patchHeader(base, 'cad', { present: true, value: 'below-tools' })
    expect(base.cad.toolbar).toEqual({ home: 'draw' })
    const newer = { ...applied, cad: { ...applied.cad, authoring: false } }
    expect(patchHeader(newer, 'cad', before)).toEqual({ ...base, cad: { ...base.cad, authoring: false } })
    expect(patchHeader({ cad: { toolbar: { workspaceHeader: 'below-tools' } } }, 'cad', { present: false })).toEqual({})
    expect(() => patchHeader(base, 'cad', { present: true, value: '<style>' })).toThrow()
    expect(() => patchHeader(base, 'browser', { present: true, value: 'above-tools' })).toThrow()
    expect(() => patchHeader({ cad: { toolbar: null } }, 'cad', { present: true, value: 'above-tools' })).toThrow()
    expect(sameHeaderField(before, { present: true, value: 'above-tools' })).toBe(false)
  })
  it('requires the receipt digest and complete submitted overlay to match readback', () => {
    const receipt = { sha256: 'a'.repeat(64), authored_at: '2026-11-01T12:00:00Z' }
    const surfaces = { cad: { toolbar: { workspaceHeader: 'below-tools' } } }
    expect(confirmedLayout(receipt, { source: receipt, surfaces }, surfaces)).toBe(true)
    expect(confirmedLayout(receipt, { source: { ...receipt, sha256: 'b'.repeat(64) }, surfaces }, surfaces)).toBe(false)
    expect(confirmedLayout(receipt, { source: receipt, surfaces: {} }, surfaces)).toBe(false)
    const reordered = { cad: { toolbar: { home: 'draw', workspaceHeader: 'below-tools' } }, solar: { authoring: true } }
    expect(confirmedLayout(receipt, { source: receipt, surfaces: reordered }, { solar: { authoring: true }, cad: { toolbar: { workspaceHeader: 'below-tools', home: 'draw' } } })).toBe(true)
  })
})
