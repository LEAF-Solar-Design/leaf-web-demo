// @vitest-environment jsdom
import React from 'react'
import { TextEncoder } from 'node:util'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import BranchToolsPanel from './BranchToolsPanel.jsx'
import { buildFormModel, describeDecision, formArgs, serializeArgs, TOOL_DECISIONS } from './branchTools.js'

const tool = { name: 'panel-gap-check', version: '1.0.0',
  packageDigest: '079bc8c277c9cba64b38d881a58fdaede524d19636d7400887fc99273bcd1f72',
  source: 'downloaded', decision: 'accept', runnable: true,
  params: { type: 'object', properties: { min_gap_in: { type: 'integer', default: 18 } } } }
const accepted = { decision: 'accept', reason: null, result: { gaps: 2 }, resultTruncated: false, generation: 1, durationMs: 0 }
let bridge
beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder)
  bridge = { listTools: vi.fn().mockResolvedValue({ tools: [tool] }), runTool: vi.fn().mockResolvedValue(accepted) }
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Branch tools panel', () => {
  it('defers the mount list while shared work is pending and disables actions during sync', async () => {
    function SharedPanel({ syncing }) {
      const [working, setWorking] = React.useState(false)
      return <BranchToolsPanel bridge={bridge} working={syncing || working} setWorking={setWorking} />
    }
    const view = render(<SharedPanel syncing />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh tools' }).disabled).toBe(true))
    expect(bridge.listTools).not.toHaveBeenCalled()
    view.rerender(<SharedPanel syncing={false} />)
    const run = await screen.findByRole('button', { name: 'Run panel-gap-check' })
    await waitFor(() => expect(run.disabled).toBe(false))
    view.rerender(<SharedPanel syncing />)
    expect(run.disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Refresh tools' }).disabled).toBe(true)
    fireEvent.click(run)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh tools' }))
    expect(bridge.runTool).not.toHaveBeenCalled()
    expect(bridge.listTools).toHaveBeenCalledTimes(1)
  })

  it('omits untouched optional JSON fields and sends edited JSON', async () => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: { type: 'object', properties: {
      handles: { type: 'array' }, settings: { type: 'object', default: { enabled: true } },
    } } }] })
    render(<BranchToolsPanel bridge={bridge} />)
    const handles = await screen.findByLabelText('handles')
    expect(handles.value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(/"gaps": 2/)
    expect(bridge.runTool.mock.calls[0][0].args).toEqual({ settings: { enabled: true } })
    fireEvent.change(handles, { target: { value: '["A1"]' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await waitFor(() => expect(bridge.runTool).toHaveBeenCalledTimes(2))
    expect(bridge.runTool.mock.calls[1][0].args.handles).toEqual(['A1'])
  })

  it.each([true, false])('shows invalid JSON as a field message (whole args: %s)', async (wholeArgs) => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: wholeArgs ? undefined
      : { type: 'object', properties: { handles: { type: 'array' } } } }] })
    render(<BranchToolsPanel bridge={bridge} />)
    const label = wholeArgs ? 'Arguments JSON' : 'handles'
    fireEvent.change(await screen.findByLabelText(label), { target: { value: '{broken' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    expect(screen.getByRole('alert').textContent).toBe(`Invalid JSON in ${label}`)
    expect(screen.queryByText(/Tool refused:/)).toBeNull()
    expect(bridge.runTool).not.toHaveBeenCalled()
  })

  it('falls back to the first enum option for an invalid default', async () => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: { type: 'object', properties: {
      mode: { type: 'string', enum: ['first', 'second'], default: 'missing' },
    } } }] })
    render(<BranchToolsPanel bridge={bridge} />)
    expect((await screen.findByLabelText('mode')).value).toBe('0')
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(/"gaps": 2/)
    expect(bridge.runTool.mock.calls[0][0].args).toEqual({ mode: 'first' })
  })

  it('lists the decision, edits manifest args and shows a run result', async () => {
    render(<BranchToolsPanel bridge={bridge} />)
    const input = await screen.findByLabelText('min_gap_in')
    expect(input.value).toBe('18')
    expect(screen.getByText('Ready to run.')).toBeTruthy()
    fireEvent.change(input, { target: { value: '24' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(/"gaps": 2/)
    expect(bridge.runTool).toHaveBeenCalledWith({ name: tool.name, version: tool.version, packageDigest: tool.packageDigest, args: { min_gap_in: 24 } })
    expect(Object.keys(bridge.runTool.mock.calls[0][0]).sort()).toEqual(['args', 'name', 'packageDigest', 'version'])
  })

  it.each(['malformed', 'unknown_key', 'signature_invalid', 'digest_mismatch', 'identity_mismatch',
    'revoked', 'stale_revocation_list', 'unreviewed', 'unentitled', 'host_api_too_low',
    'capability_violation', 'timeout', 'compile_failed', 'script_error', 'revocation_invalid',
    'invalid_command', 'tool_not_installed', 'version_not_current', 'confirmation_declined',
    'run_tool_unavailable'])('shows the host refusal %s in plain words', async (reason) => {
    expect(Object.hasOwn(TOOL_DECISIONS, reason)).toBe(true)
    bridge.runTool.mockResolvedValue({ ...accepted, decision: 'refused', reason, result: null })
    render(<BranchToolsPanel bridge={bridge} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(TOOL_DECISIONS[reason])
  })

  it('shows transport failures and result truncation', async () => {
    bridge.runTool.mockRejectedValueOnce(new Error('run_tool_unavailable')).mockResolvedValueOnce({ ...accepted, resultTruncated: true })
    render(<BranchToolsPanel bridge={bridge} />)
    const run = await screen.findByRole('button', { name: 'Run panel-gap-check' })
    fireEvent.click(run)
    await screen.findByText(describeDecision('run_tool_unavailable'))
    fireEvent.click(run)
    await screen.findByText('The result was truncated by the host.')
  })

  it('refuses oversized UTF-8 arguments before calling the bridge', async () => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: { type: 'object', properties: { note: { type: 'string' } } } }] })
    render(<BranchToolsPanel bridge={bridge} />)
    fireEvent.change(await screen.findByLabelText('note'), { target: { value: 'é'.repeat(32768) } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(describeDecision('args_too_large'))
    expect(bridge.runTool).not.toHaveBeenCalled()
  })

  it('disables refused tools and retries a failed list', async () => {
    bridge.listTools.mockRejectedValueOnce(new Error('run_tool_unavailable')).mockResolvedValueOnce({ tools: [{ ...tool, runnable: false, decision: 'digest_mismatch' }] })
    render(<BranchToolsPanel bridge={bridge} />)
    await screen.findByText(describeDecision('run_tool_unavailable'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh tools' }))
    expect((await screen.findByRole('button', { name: 'Run panel-gap-check' })).disabled).toBe(true)
    expect(screen.getByText(describeDecision('digest_mismatch'))).toBeTruthy()
  })

  it('prevents duplicate runs while waiting for AutoCAD', async () => {
    let finish
    bridge.runTool.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    render(<BranchToolsPanel bridge={bridge} />)
    const run = await screen.findByRole('button', { name: 'Run panel-gap-check' })
    fireEvent.click(run)
    fireEvent.click(run)
    expect(bridge.runTool).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Refresh tools' }).disabled).toBe(true)
    finish(accepted)
    await waitFor(() => expect(run.disabled).toBe(false))
  })

  it('handles typed fields, enum values and JSON fallback', async () => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: { type: 'object', properties: {
      title: { type: 'string', default: 'roof' }, distance: { type: 'number', default: 0.5 },
      enabled: { type: 'boolean', default: true }, mode: { type: 'integer', enum: [1, 2], default: 1 },
      handles: { type: 'array', default: ['A1'] },
    } } }] })
    render(<BranchToolsPanel bridge={bridge} />)
    fireEvent.click(await screen.findByLabelText('enabled'))
    fireEvent.change(screen.getByLabelText('mode'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('handles'), { target: { value: '["B2"]' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(/"gaps": 2/)
    expect(bridge.runTool.mock.calls[0][0].args).toEqual({ title: 'roof', distance: 0.5, enabled: false, mode: 2, handles: ['B2'] })
  })

  it('uses whole-args JSON when no manifest is provided', async () => {
    bridge.listTools.mockResolvedValue({ tools: [{ ...tool, params: undefined }] })
    render(<BranchToolsPanel bridge={bridge} />)
    fireEvent.change(await screen.findByLabelText('Arguments JSON'), { target: { value: '{"min_gap_in":18}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run panel-gap-check' }))
    await screen.findByText(/"gaps": 2/)
    expect(bridge.runTool.mock.calls[0][0].args).toEqual({ min_gap_in: 18 })
  })
})

describe('Branch tool argument helpers', () => {
  it('keeps JSON fields required without inventing null defaults', () => {
    const model = buildFormModel({ type: 'object', properties: { handles: { type: 'array' } }, required: ['handles'] })
    expect(model[0]).toMatchObject({ required: true, initial: '' })
    expect(() => formArgs(model, { handles: '' })).toThrow('Invalid JSON in handles')
    expect(formArgs(model, { handles: '[]' })).toEqual({ handles: [] })
  })
  it('counts encoded bytes at the exact limit', () => {
    expect(serializeArgs({ note: 'x'.repeat(65525) })).toHaveLength(65536)
    expect(() => serializeArgs({ note: 'x'.repeat(65526) })).toThrow('args_too_large')
    expect(() => serializeArgs({ note: 'é'.repeat(32768) })).toThrow('args_too_large')
  })
  it('refuses invalid integer and non-object args', () => {
    const model = buildFormModel(tool.params)
    expect(() => formArgs(model, { min_gap_in: '1.5' })).toThrow('invalid_command')
    expect(() => formArgs(buildFormModel(null), { $args: '[]' })).toThrow('invalid_command')
  })
  it.each(Object.keys(TOOL_DECISIONS))('describes %s in plain words', (code) => {
    expect(describeDecision(code)).not.toContain('_')
  })
})
