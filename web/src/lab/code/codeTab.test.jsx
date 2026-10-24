import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CodeTab from './CodeTab.jsx'
import BlockChecks from './BlockChecks.jsx'
import { manifests } from '../canvas/registry.js'

const manifest = {
  ...manifests.find(item => item.id === 'adjust-none'),
  inputs: {
    chance: { mode: 'scalar', schema: { type: 'number', minimum: 0, maximum: 1 }, unit: 'ratio', nullable: false },
    trades: { mode: 'stream', schema: { type: 'integer' }, unit: 'count', nullable: true },
  },
  outputs: { size: { mode: 'scalar', schema: { type: 'number' }, unit: 'count', nullable: false } },
  ui_schema: { order: ['max_tokens'], labels: { max_tokens: 'Token budget' } },
}
const block = { id: 'adjust-none-1', settings: { max_tokens: 128 } }
const digest = 'a'.repeat(64)
const previousDigest = 'b'.repeat(64)
const passed = () => Object.fromEntries(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'].map(key => [key, { status: 'pass' }]))
const evidenceOf = overrides => ({ draft: { crate: 'sizing', library_digest: digest }, digest, checks: passed(), ...overrides })
let fetchSpy
let clipboard
beforeEach(() => {
  fetchSpy = vi.fn(() => { throw new Error('Network forbidden') })
  clipboard = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('fetch', fetchSpy)
  vi.stubGlobal('navigator', { clipboard: { writeText: clipboard } })
})
afterEach(() => {
  cleanup()
  expect(fetchSpy).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})
const mount = (evidence = null, onSettings = vi.fn()) => render(<CodeTab block={block} manifest={manifest} evidence={evidence} onSettings={onSettings} />)
const checkRows = () => within(screen.getByRole('region', { name: 'Checks before it can be used' })).getAllByRole('listitem')

describe('Block Code tab', () => {
  it.each(['Built-in', 'Settings only', 'Custom Rust'])('shows the exact manifest contract and commands in %s', mode => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: mode, exact: true }))
    expect(screen.getByRole('button', { name: mode, exact: true })).toHaveAttribute('aria-pressed', 'true')
    const typed = screen.getByText(/in: chance:/)
    expect(typed).toHaveTextContent('chance: number [ratio], non-nullable (scalar); schema {"maximum":1,"minimum":0,"type":"number"}')
    expect(typed).toHaveTextContent('trades: integer [count], nullable (stream)')
    expect(typed).toHaveTextContent('out: size: number [count], non-nullable')
    expect(typed).toHaveTextContent('budget: p50 at most 10 µs · p99 at most 100 µs')
    for (const command of ['export-contract', 'validate-settings', 'new-rust', 'check', 'request-use']) {
      expect(screen.getByRole('button', { name: `Copy ${command}`, exact: true }).previousElementSibling)
        .toHaveTextContent(`python -m leafhft.studio_blocks.cli ${command} --kind 'adjust-none' --version '1.0.0'`)
    }
    expect(screen.getByLabelText('Ask Claude')).toHaveValue('Size by Kelly as now, but halve it in the last 2 minutes before close.')
  })

  it('keeps all six ordered checks pending and Use disabled without evidence', () => {
    mount()
    expect(checkRows()).toHaveLength(6)
    expect(checkRows().map(row => row.dataset.check)).toEqual(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'])
    for (const row of checkRows()) expect(row).toHaveAttribute('data-status', 'pending')
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
  })

  it.each([null, previousDigest])('requires the check evidence digest to match the draft (%s)', evidenceDigest => {
    mount(evidenceOf({ digest: evidenceDigest }))
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
  })

  it('enables Use only with matching evidence and reveals its digest-bound command', () => {
    mount(evidenceOf())
    const use = screen.getByRole('button', { name: 'Use as a Change' })
    expect(use).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Copy Use as a Change command' })).not.toBeInTheDocument()
    fireEvent.click(use)
    expect(screen.getByRole('button', { name: 'Copy Use as a Change command' }).previousElementSibling)
      .toHaveTextContent(`request-use --kind 'adjust-none' --version '1.0.0' --digest '${digest}'`)
  })

  it.each(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'])('blocks Use when %s fails or is missing', key => {
    const checks = { ...passed(), [key]: { status: 'fail' } }
    const view = mount(evidenceOf({ checks }))
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(checkRows().find(row => row.dataset.check === key)).toHaveAttribute('data-status', 'fail')
    delete checks[key]
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ checks })} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(checkRows().find(row => row.dataset.check === key)).toHaveAttribute('data-status', 'pending')
  })

  it('shows sample counts, measured speed against budget, corpus count and first differences', () => {
    mount(evidenceOf({ checks: { ...passed(),
      tests: { status: 'pass', passed: 12, total: 12 },
      speed: { status: 'pass', p50_us: 2, p99_us: 8 },
      corpus: { status: 'fail', difference_count: 3, differences: [{ trade: 'trade-1', live: 4, draft: 2 }], detail: 'Sizing changed' },
    } }))
    expect(screen.getByText('12 of 12 passed')).toBeVisible()
    expect(screen.getByText(/p50 2 µs; p99 8 µs/)).toHaveTextContent('budget p50 10 µs, p99 100 µs')
    expect(screen.getByText('3 differ')).toBeVisible()
    expect(screen.getByRole('list', { name: 'First differences' })).toHaveTextContent('trade-1')
    expect(screen.getByText('Sizing changed')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
  })

  it('shows live, pending and previous versions and stages the prior digest for Undo', () => {
    mount(evidenceOf({ versions: [
      { version: 'v3', kind: 'rust', state: 'pending', library_digest: digest },
      { version: 'v2', kind: 'settings', state: 'active' },
      { version: 'v1', kind: 'builtin', state: 'prior', library_digest: previousDigest },
    ] }))
    expect(screen.getByText(/v3 rust · pending restart/)).toBeVisible()
    expect(screen.getByText('v2 settings · live')).toBeVisible()
    expect(screen.getByText('v1 builtin · previous')).toBeVisible()
    expect(screen.getByText('Takes effect at the next restart')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Undo at restart' }))
    expect(screen.getByRole('button', { name: 'Copy Undo at restart command' }).previousElementSibling)
      .toHaveTextContent(`--digest '${previousDigest}'`)
  })

  it('discards the local draft and its Use command without discarding history', () => {
    const evidence = evidenceOf({ versions: [{ version: 'v1', kind: 'builtin', state: 'active' }] })
    const view = mount(evidence)
    fireEvent.click(screen.getByRole('button', { name: 'Custom Rust', exact: true }))
    expect(screen.getByText(`Draft: sizing · ${digest}`)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Use as a Change' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    expect(screen.getByRole('status')).toHaveTextContent('Draft discarded')
    expect(screen.queryByText(`Draft: sizing · ${digest}`)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy Use as a Change command' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(screen.getByText('v1 builtin · live')).toBeVisible()
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={{ ...evidence }} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
  })

  it('validates settings JSON before saving through onSettings', () => {
    const onSettings = vi.fn()
    mount(null, onSettings)
    fireEvent.click(screen.getByRole('button', { name: 'Settings only', exact: true }))
    const schemas = within(screen.getByText('Settings schema and UI schema').closest('details'))
    expect(schemas.getByText(/"labels":\{"max_tokens":"Token budget"\}/)).toBeInTheDocument()
    const json = screen.getByLabelText('Settings JSON')
    for (const invalid of ['{', '{"max_tokens":-1}', '{"api_token":"no"}']) {
      fireEvent.change(json, { target: { value: invalid } })
      fireEvent.click(screen.getByRole('button', { name: 'Apply settings JSON' }))
      expect(onSettings).not.toHaveBeenCalled()
    }
    fireEvent.change(json, { target: { value: '{"max_tokens":256}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings JSON' }))
    expect(onSettings).toHaveBeenCalledExactlyOnceWith({ max_tokens: 256 })
    expect(screen.getByRole('status')).toHaveTextContent('Settings saved')
  })

  it('uses UI schema labels for editable settings without inserting defaults', () => {
    const onSettings = vi.fn()
    mount(null, onSettings)
    fireEvent.click(screen.getByRole('button', { name: 'Settings only', exact: true }))
    fireEvent.change(screen.getByLabelText('Token budget (optional)'), { target: { value: '64' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(onSettings).toHaveBeenCalledExactlyOnceWith({ max_tokens: 64 })
  })

  it('withdraws the Use command if evidence changes after it was opened', () => {
    const view = mount(evidenceOf())
    fireEvent.click(screen.getByRole('button', { name: 'Use as a Change' }))
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ digest: previousDigest })} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Copy Use as a Change command' })).not.toBeInTheDocument()
  })

  it('copies prompts and commands locally without contacting a model or network', async () => {
    mount()
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'Use a smaller size' } })
    fireEvent.click(screen.getByRole('button', { name: 'Copy Ask Claude prompt' }))
    expect(clipboard).toHaveBeenCalledWith(expect.stringContaining('Use a smaller size\n\nBlock adjust-none@1.0.0'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy export-contract', exact: true }))
    expect(clipboard).toHaveBeenCalledWith("python -m leafhft.studio_blocks.cli export-contract --kind 'adjust-none' --version '1.0.0'")
    await waitFor(() => expect(screen.getAllByText('Copied')).toHaveLength(2))
  })

  it('treats unrecognized check states as pending', () => {
    render(<BlockChecks checks={{ builds: { status: 'passed' } }} />)
    expect(checkRows()[0]).toHaveAttribute('data-status', 'pending')
  })
})
