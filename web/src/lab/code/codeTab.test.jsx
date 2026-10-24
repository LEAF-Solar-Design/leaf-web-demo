import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CodeTab from './CodeTab.jsx'
import BlockChecks from './BlockChecks.jsx'
import { evidenceFor } from './hostEvidence.js'
import { manifests } from '../canvas/registry.js'
import BlockPanel from '../canvas/BlockPanel.jsx'

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
const passed = () => ({ ...Object.fromEntries(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'].map(key => [key, { status: 'pass' }])),
  tests: { status: 'pass', passed: 12, total: 12 }, speed: { status: 'pass', p99_us: 8 }, corpus: { status: 'pass', difference_count: 0 } })
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

describe('host evidence boundary', () => {
  const selected = { registry: { id: 'adjust-none', version: '1.0.0' } }
  const row = overrides => ({ data: { kind: 'adjust-none', version: '1.0.0', checks: passed(), digest, draftDigest: digest, ...overrides } })
  const providerOf = (checks, versions = []) => ({ rowsOf: name => name === 'block-checks' ? checks : versions })

  it('normalizes only matching, valid evidence and bounded version history', () => {
    const checks = passed()
    checks.tests = { state: 'pass', passed: 12, total: 12 }
    const result = evidenceFor(providerOf([row({ kind: 'another' }), row({ checks })], [
      { data: { kind: 'adjust-none', version: '1.0.0', versions: [
        { state: 'pending', version: 'v3', restart_required: true, digest },
        { state: 'prior', version: 'v1', restart_required: 'false' },
      ] } },
    ]), selected)
    expect(result).toEqual({ checks: { ...passed(), tests: { status: 'pass', passed: 12, total: 12 } }, digest, draftDigest: digest,
      versions: [{ state: 'pending', version: 'v3', restart_required: true, digest }] })
  })

  it.each([
    { kind: 'another' }, { version: '2.0.0' },
  ])('ignores a malformed or unrelated check row (%j)', invalid => {
    expect(evidenceFor(providerOf([row(invalid)]), selected)).toBeNull()
  })

  it('reads no more than 32 announced rows per source and handles absent or refused sources', () => {
    expect(evidenceFor(providerOf([row(), ...Array.from({ length: 32 }, () => row({ kind: 'another' }))]), selected)).toBeNull()
    expect(evidenceFor(providerOf([row({ checks: { ...passed(), builds: { status: 'pass', detail: 'é'.repeat(1024) } } })]), selected).checks.builds.detail).toHaveLength(1024)
    expect(evidenceFor(providerOf([]), selected)).toBeNull()
    expect(evidenceFor({ rowsOf: () => { throw new Error('unavailable') } }, selected)).toBeNull()
    expect(evidenceFor(null, selected)).toBeNull()
  })
})

describe('Block Code tab', () => {
  it('C1 displays real CLI command flags and gates request-use with its visible evidence digest', () => {
    mount(evidenceOf())
    const prefix = 'python -m leafhft.studio_blocks.cli '
    const selector = "--kind 'adjust-none' --version '1.0.0'"
    for (const [label, command] of [
      ['export-contract', `export-contract ${selector}`],
      ['validate-settings', `validate-settings ${selector} --settings ./settings.json`],
      ['new-rust', `new-rust ${selector} --out ./studio-block`],
      ['check', `check --crate ./studio-block ${selector} --out ./studio-block-check.json`],
      ['check corpus', `check corpus --crate ./studio-block ${selector} --journal-dir ./journal --out ./studio-block-check.json`],
    ]) expect(screen.getByRole('button', { name: `Copy ${label}`, exact: true }).previousElementSibling.textContent).toBe(prefix + command)
    expect(document.body).not.toHaveTextContent('request-use')
    fireEvent.click(screen.getByRole('button', { name: 'Use as a Change' }))
    expect(screen.getByRole('button', { name: 'Copy Use as a Change command' }).previousElementSibling.textContent)
      .toBe(prefix + 'request-use --manifest ./studio-block-check.json')
    expect(screen.getByText(/Evidence digest:/)).toHaveTextContent(digest)
    expect(document.body).not.toHaveTextContent('--digest')
  })

  it('C3 requires complete consistent checks, counts, speed, corpus and lowercase digests', () => {
    const view = mount(evidenceOf())
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeEnabled()
    const invalid = [
      [{ tests: { status: 'pass' } }, /Tests passed and total/],
      [{ tests: { status: 'pass', passed: 2, total: 3 } }, /Tests passed and total/],
      [{ tests: { status: 'pass', passed: 4, total: 3 } }, /Tests passed and total/],
      [{ speed: { status: 'pass' } }, /Speed p99 must/],
      [{ speed: { status: 'pass', p99_us: 101 } }, /Speed p99 must/],
      [{ corpus: { status: 'pass' } }, /Corpus difference count/],
      [{ corpus: { status: 'pass', difference_count: 1 } }, /Corpus difference count/],
      [{ corpus: { status: 'pass', difference_count: 0, differences: ['contradiction'] } }, /Corpus difference count/],
    ]
    for (const [change, reason] of invalid) {
      view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ checks: { ...passed(), ...change } })} />)
      expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
      expect(screen.getByText(reason)).toBeVisible()
    }
    for (const bad of [null, 'short', 'A'.repeat(64), previousDigest]) {
      view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ digest: bad })} />)
      expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
      expect(screen.getByText(/Evidence and draft digests must match/)).toBeVisible()
    }
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ checks: { ...passed(), speed: { status: 'pass', p99_us: 100 } } })} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeEnabled()
  })

  it('C5 shows checks, counts and differences with either or both digests absent', () => {
    const view = mount()
    for (const [evidenceDigest, draftDigest] of [[digest, null], [null, digest], [null, null]]) {
      const selected = { registry: { id: manifest.id, version: manifest.version.semver } }
      const evidence = evidenceFor({ rowsOf: name => name === 'block-checks' ? [{ data: {
        kind: manifest.id, version: manifest.version.semver, digest: evidenceDigest, draftDigest,
        checks: { ...passed(), corpus: { status: 'fail', difference_count: 1, differences: ['different trade'] } },
      } }] : [] }, selected)
      view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidence} />)
      expect(screen.getByText('12 of 12 passed')).toBeVisible()
      expect(screen.getByText('1 differ')).toBeVisible()
      expect(screen.getByRole('list', { name: 'First differences' })).toHaveTextContent('different trade')
      expect(checkRows()[0]).toHaveAttribute('data-status', 'pass')
      expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    }
  })

  it('C6 follows Configure and Setup saves, prefers new block settings on conflict and resyncs complex JSON', () => {
    const selected = { ...block, registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }
    const entries = { [`${manifest.id}@${manifest.version.semver}`]: manifest }
    function Inspector() {
      const [current, setCurrent] = useState(selected)
      return <BlockPanel block={current} document={{ blocks: [current], pipes: [] }} entries={entries}
        onSettings={settings => setCurrent(value => ({ ...value, settings }))} />
    }
    const panel = render(<Inspector />)
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    fireEvent.click(screen.getByRole('button', { name: 'Settings only', exact: true }))
    fireEvent.click(screen.getByRole('tab', { name: 'Configure' }))
    fireEvent.click(screen.getByText('Submit settings JSON'))
    const configure = screen.getAllByLabelText('Settings JSON')[0]
    fireEvent.change(configure, { target: { value: '{"max_tokens":256}' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Apply settings JSON' })[0])
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    expect(screen.getByLabelText('Token budget (optional)')).toHaveValue(256)
    fireEvent.change(screen.getByLabelText('Token budget (optional)'), { target: { value: '257' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Setup' }))
    const setup = within(screen.getByRole('tabpanel'))
    fireEvent.change(setup.getByRole('spinbutton', { name: 'Token budget (optional)' }), { target: { value: '512' } })
    fireEvent.click(setup.getByRole('button', { name: 'Save settings', exact: true }))
    fireEvent.click(screen.getByRole('tab', { name: 'Code' }))
    expect(screen.getByLabelText('Token budget (optional)')).toHaveValue(512)
    expect(screen.getByText('Newer block settings replaced unsaved local edits.')).toBeVisible()
    panel.unmount()
    const complex = { ...manifest, settings_schema: { type: 'object', properties: {
      options: { type: 'object' }, weights: { type: 'array', items: { type: 'number' } },
    } }, ui_schema: {} }
    const view = render(<CodeTab block={{ ...block, settings: { options: { a: 1 }, weights: [1] } }} manifest={complex} onSettings={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Settings only', exact: true }))
    fireEvent.change(screen.getByLabelText('Settings JSON'), { target: { value: '{"options":{"a":2},"weights":[2,3]}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply settings JSON' }))
    expect(screen.getByLabelText('options (optional)')).toHaveValue('{"a":2}')
    expect(screen.getByLabelText('weights (optional)')).toHaveValue('[2,3]')
    fireEvent.change(screen.getByLabelText('options (optional)'), { target: { value: '{' } })
    view.rerender(<CodeTab block={{ ...block, settings: { options: { a: 4 }, weights: [4] } }} manifest={complex} />)
    expect(screen.getByLabelText('options (optional)')).toHaveValue('{"a":4}')
    expect(screen.getByLabelText('weights (optional)')).toHaveValue('[4]')
    expect(screen.getByLabelText('options (optional)')).toBeValid()
    expect(screen.getByText('Newer block settings replaced unsaved local edits.')).toBeVisible()
  })

  it('C7 remembers all discarded identities across remounts and block switches', () => {
    const first = 'd'.repeat(64)
    const second = 'e'.repeat(64)
    const draftEvidence = value => evidenceOf({ draft: { library_digest: value }, digest: value })
    let view = mount(draftEvidence(first))
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    view.unmount()
    view = mount(draftEvidence(first))
    expect(screen.getByRole('button', { name: 'Discard draft' })).toBeDisabled()
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={draftEvidence(second)} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    view.rerender(<CodeTab block={block} manifest={{ ...manifest, id: 'other-kind' }} evidence={draftEvidence(first)} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeEnabled()
    for (const value of [first, second]) {
      view.rerender(<CodeTab block={block} manifest={manifest} evidence={draftEvidence(value)} />)
      expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Discard draft' })).toBeDisabled()
    }
  })

  it('C8 explains owner-staged Undo at restart with or without a prior digest', () => {
    const view = mount()
    expect(screen.queryByText(/Undo at restart/)).toBeNull()
    for (const prior of [{ state: 'prior', version: '1' }, { state: 'prior', version: 'v1', digest: previousDigest }]) {
      view.rerender(<CodeTab block={block} manifest={manifest} evidence={{ versions: [prior] }} />)
      expect(screen.getByText('Undo at restart: the owner stages v1 through the owner rail; it takes effect at the next restart')).toBeVisible()
      expect(screen.queryByRole('button', { name: /Undo/ })).toBeNull()
      expect(document.body).not.toHaveTextContent('cli undo')
    }
  })

  it('C12 copies only the prompt and allowlisted typed contract metadata', () => {
    const secret = 'DO-NOT-COPY'
    const poisoned = { ...manifest, sample_output: { secret }, sample_input: { secret }, title: secret,
      other_host_value: secret, settings_schema: { ...manifest.settings_schema, description: secret },
      inputs: { ...manifest.inputs, chance: { ...manifest.inputs.chance, default: secret,
        schema: { ...manifest.inputs.chance.schema, description: secret, examples: [secret], const: secret } } } }
    render(<CodeTab block={block} manifest={poisoned} evidence={evidenceOf({ host_value: secret })} />)
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'Make it faster' } })
    fireEvent.click(screen.getByRole('button', { name: 'Copy Ask Claude prompt' }))
    expect(clipboard).toHaveBeenCalledWith('Make it faster\n\nin: chance: number [ratio], non-nullable\ntrades: integer [count], nullable\nout: size: number [count], non-nullable\nbudget: p50 at most 10 µs · p99 at most 100 µs')
    expect(clipboard.mock.calls[0][0]).not.toContain(secret)
  })

  it('C13 clears Copied feedback when its copied text changes', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Copy Ask Claude prompt' }))
    await screen.findByText('Copied')
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'A new prompt' } })
    expect(screen.queryByText('Copied')).toBeNull()
  })

  it('C14 focuses and selects a read-only keyboard fallback when clipboard access is denied', async () => {
    clipboard.mockRejectedValue(new Error('denied'))
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Copy export-contract', exact: true }))
    const fallback = await screen.findByRole('textbox', { name: 'Copy export-contract manually' })
    expect(fallback).toHaveAttribute('readonly')
    expect(fallback).toHaveFocus()
    expect(fallback.selectionStart).toBe(0)
    expect(fallback.selectionEnd).toBe(fallback.value.length)
    expect(fallback.value).toBe("python -m leafhft.studio_blocks.cli export-contract --kind 'adjust-none' --version '1.0.0'")
  })

  it.each(['Built-in', 'Settings only', 'Custom Rust'])('shows the exact manifest contract and commands in %s', mode => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: mode, exact: true }))
    expect(screen.getByRole('button', { name: mode, exact: true })).toHaveAttribute('aria-pressed', 'true')
    const typed = screen.getAllByText(/in: chance:/)[0]
    expect(typed).toHaveTextContent('chance: number [ratio], non-nullable (scalar); schema {"maximum":1,"minimum":0,"type":"number"}')
    expect(typed).toHaveTextContent('trades: integer [count], nullable (stream)')
    expect(typed).toHaveTextContent('out: size: number [count], non-nullable')
    expect(typed).toHaveTextContent('budget: p50 at most 10 µs · p99 at most 100 µs')
    for (const command of ['export-contract', 'validate-settings', 'new-rust']) {
      expect(screen.getByRole('button', { name: `Copy ${command}`, exact: true }).previousElementSibling)
        .toHaveTextContent(`python -m leafhft.studio_blocks.cli ${command} --kind 'adjust-none' --version '1.0.0'`)
    }
    expect(document.body).not.toHaveTextContent('request-use')
    expect(screen.getByLabelText('Ask Claude')).toHaveValue('Size by Kelly as now, but halve it in the last 2 minutes before close.')
  })

  it('keeps all six ordered checks pending and Use disabled without evidence', () => {
    mount()
    expect(checkRows()).toHaveLength(6)
    expect(checkRows().map(row => row.dataset.check)).toEqual(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'])
    for (const row of checkRows()) expect(row).toHaveAttribute('data-status', 'pending')
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(document.body).not.toHaveTextContent('request-use')
  })

  it.each([null, previousDigest])('requires the check evidence digest to match the draft (%s)', evidenceDigest => {
    mount(evidenceOf({ digest: evidenceDigest }))
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(document.body).not.toHaveTextContent('request-use')
  })

  it('does not expose request-use when passed checks arrive without a draft', () => {
    mount({ digest, checks: passed(), versions: [{ state: 'prior', version: 'v1', digest: previousDigest }] })
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(screen.getByText(/Undo at restart: the owner stages v1/)).toBeVisible()
    expect(document.body).not.toHaveTextContent('request-use')
  })

  it('enables Use only with matching evidence and reveals its digest-bound command', () => {
    mount(evidenceOf())
    const use = screen.getByRole('button', { name: 'Use as a Change' })
    expect(use).toBeEnabled()
    expect(document.body).not.toHaveTextContent('request-use')
    expect(screen.queryByRole('button', { name: 'Copy Use as a Change command' })).not.toBeInTheDocument()
    fireEvent.click(use)
    expect(screen.getByRole('button', { name: 'Copy Use as a Change command' }).previousElementSibling)
      .toHaveTextContent('request-use --manifest ./studio-block-check.json')
    expect(screen.getByText(/Evidence digest:/)).toHaveTextContent(digest)
  })

  it.each(['builds', 'safety', 'contract', 'tests', 'speed', 'corpus'])('blocks Use when %s fails or is missing', key => {
    const checks = { ...passed(), [key]: { status: 'fail' } }
    const view = mount(evidenceOf({ checks }))
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(checkRows().find(row => row.dataset.check === key)).toHaveAttribute('data-status', 'fail')
    expect(document.body).not.toHaveTextContent('request-use')
    delete checks[key]
    view.rerender(<CodeTab block={block} manifest={manifest} evidence={evidenceOf({ checks })} />)
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(checkRows().find(row => row.dataset.check === key)).toHaveAttribute('data-status', 'pending')
    expect(document.body).not.toHaveTextContent('request-use')
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

  it('shows live, pending and previous versions and explains owner-staged Undo', () => {
    mount(evidenceOf({ versions: [
      { version: 'v3', kind: 'rust', state: 'pending', library_digest: digest },
      { version: 'v2', kind: 'settings', state: 'active' },
      { version: 'v1', kind: 'builtin', state: 'prior', library_digest: previousDigest },
    ] }))
    expect(screen.getByText(/v3 rust · pending restart/)).toBeVisible()
    expect(screen.getByText('v2 settings · live')).toBeVisible()
    expect(screen.getByText('v1 builtin · previous')).toBeVisible()
    expect(screen.getByText('Takes effect at the next restart')).toBeVisible()
    expect(screen.getByText('Undo at restart: the owner stages v1 through the owner rail; it takes effect at the next restart')).toBeVisible()
    expect(document.body).not.toHaveTextContent('request-use')
  })

  it('discards the local draft and its Use command without discarding history', () => {
    const discardedDigest = 'c'.repeat(64)
    const evidence = evidenceOf({ draft: { crate: 'sizing', library_digest: discardedDigest }, digest: discardedDigest,
      versions: [{ version: 'v1', kind: 'builtin', state: 'active' }] })
    const view = mount(evidence)
    fireEvent.click(screen.getByRole('button', { name: 'Custom Rust', exact: true }))
    expect(screen.getByText(`Draft: sizing · ${discardedDigest}`)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Use as a Change' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    expect(screen.getByRole('status')).toHaveTextContent('Draft discarded')
    expect(screen.queryByText(`Draft: sizing · ${discardedDigest}`)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy Use as a Change command' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use as a Change' })).toBeDisabled()
    expect(screen.getByText('v1 builtin · live')).toBeVisible()
    expect(document.body).not.toHaveTextContent('request-use')
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
    expect(document.body).not.toHaveTextContent('request-use')
  })

  it('copies prompts and commands locally without contacting a model or network', async () => {
    mount()
    fireEvent.change(screen.getByLabelText('Ask Claude'), { target: { value: 'Use a smaller size' } })
    fireEvent.click(screen.getByRole('button', { name: 'Copy Ask Claude prompt' }))
    expect(clipboard).toHaveBeenCalledWith(expect.stringContaining('Use a smaller size\n\nin: chance: number'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy export-contract', exact: true }))
    expect(clipboard).toHaveBeenCalledWith("python -m leafhft.studio_blocks.cli export-contract --kind 'adjust-none' --version '1.0.0'")
    await waitFor(() => expect(screen.getAllByText('Copied')).toHaveLength(2))
  })

  it('treats unrecognized check states as pending', () => {
    render(<BlockChecks checks={{ builds: { status: 'passed' } }} />)
    expect(checkRows()[0]).toHaveAttribute('data-status', 'pending')
  })
})
