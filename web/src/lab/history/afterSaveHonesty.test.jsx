import { readFileSync } from 'node:fs'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import AfterSavePanel from './AfterSavePanel.jsx'
import LivePredictionGap from '../changes/LivePredictionGap.jsx'
import { sentence } from '../honesty.js'
import { colorLiteralsIn, replacedWordsIn } from '../standardsChecks.js'
import { blockLine, decisionTime, formatCents, ledgerLines, OPERATION_LINE, practiceMoney, registrationLines, scheduleLine, sideFigures, withheld, WINDOW_LINE } from './afterSaveHonesty.js'
import unavailable from './__fixtures__/practice-unavailable.json'
import finished from './__fixtures__/practice-finished-synthetic.json'
import degraded from './__fixtures__/practice-degraded.json'
import zeroFee from './__fixtures__/practice-zero-fee.json'
import ledger from './__fixtures__/kashbot2-ledger.json'
import registration from './__fixtures__/kashbot2-registration.json'

afterEach(cleanup)
const view = data => ({ status: data.state, match: { data }, elapsed_ms: 0 })
const panel = () => screen.getByRole('region', { name: 'After save' })
const show = (data = finished, props = {}) => render(<AfterSavePanel view={view(data)} {...props} />)
const cleanLedgerLine = '2026-11-03 · Your account · Fills: 12 · Exchange fee: $0.07 · Exchange settled net: $611.82 · RECONCILED. Exchange basis, from the recorder. Operation verified. No result verdict before the economics horizon.'

describe('exact cents', () => {
  it.each([
    ['61182', '$611.82'], ['-61182', '-$611.82'], ['7', '$0.07'],
    ['0.74', '$0.0074'], ['0', '$0.00'], ['-0', '$0.00'], [null, 'Not reported'],
    ['9999999999999.9999', '$99999999999.999999'], ['-0.0001', '-$0.000001'],
  ])('formats %s as %s without rounding', (input, output) => {
    expect(formatCents(input)).toBe(output)
  })
  it.each(['1.0', '01', '1e3', 1, '', '1\n', '10000000000000', '0.00001'])('rejects bad cents %s', input => {
    expect(() => formatCents(input)).toThrow('bad_cents')
  })

  it.each([[7], [], {}, true, false, NaN, Infinity].map(value => [value]))('does not turn malformed Practice money %j into a measurement', value => {
    expect(practiceMoney(value)).toBe('Not reported')
    const data = { ...finished, candidate: { ...finished.candidate, net_after_fees_cents: value } }
    show(data)
    const candidate = screen.getByRole('heading', { name: 'Candidate (Simulated)' }).closest('article')
    expect(within(candidate).getByText('Net after fees').closest('div').querySelector('dd').textContent).toBe('Not reported')
  })
})

describe('honest Practice result rules', () => {
  it('R1: unmeasured reached means neither block count is measured', () => {
    show(degraded)
    const blocks = within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')
    for (const [index, block] of degraded.blocks.entries()) {
      expect(blockLine(block)).toBe('Triggered: not measured. Reached: not measured.')
      expect(blocks[index]).toHaveTextContent(`${index ? 'Strategy' : 'signal'}: Triggered: not measured. Reached: not measured.`)
    }
    expect(blockLine({ ...degraded.blocks[0], reached: undefined })).toBe('Triggered: not measured. Reached: not measured.')
    expect(panel()).not.toHaveTextContent(/triggered 0 times/i)
  })

  it('R2: a measured zero is relative to reached', () => {
    show()
    expect(blockLine(finished.blocks[0])).toBe('Triggered 0 of 2 reached')
    expect(screen.getByRole('list', { name: 'Practice blocks' })).toHaveTextContent('signal: Triggered 0 of 2 reached')
    expect(within(screen.getByRole('list', { name: 'Practice blocks' })).getAllByRole('listitem')[0].textContent).toBe('signal: Triggered 0 of 2 reached')
    expect(panel()).not.toHaveTextContent(/triggered 0 times/i)
  })

  it('R3: a block that was never reached says so', () => {
    show()
    expect(blockLine(finished.blocks[1])).toBe('Never reached in the window')
    expect(screen.getByRole('list', { name: 'Practice blocks' })).toHaveTextContent('Strategy: Never reached in the window')
  })

  it('R4: no trades leaves risk and trade outcomes unmeasured', () => {
    show()
    const live = screen.getByRole('heading', { name: 'Live (Simulated)' }).closest('article')
    expect(sideFigures(finished.live)).toMatchObject({ trades: 'No trades', netPerDollar: 'not measured', worst: 'not measured', withoutBestWorst: 'not measured' })
    expect(sideFigures({ ...finished.live, trades: undefined }).trades).toBe('No trades')
    expect(within(live).getByText('No trades')).toBeVisible()
    for (const label of ['Net per dollar risked', 'Worst Trade']) {
      const row = within(live).getByText(label).closest('div')
      expect(row.querySelector('dd').textContent).toBe('not measured')
      expect(row).not.toHaveTextContent('0.00')
    }
    expect(within(live).getByText('Without best and worst: not measured')).toBeVisible()
  })

  it.each(['', false, [], {}].map(value => [value]))('R4: a malformed risk ratio %j is missing, not a measured zero', netPerDollar => {
    const data = { ...finished, candidate: { ...finished.candidate, net_per_dollar_risked: netPerDollar } }
    show(data)
    expect(sideFigures(data.candidate).netPerDollar).toBe('Not reported')
    const candidate = screen.getByRole('heading', { name: 'Candidate (Simulated)' }).closest('article')
    const ratio = within(candidate).getByText('Net per dollar risked').closest('div')
    expect(ratio.querySelector('dd').textContent).toBe('Not reported')
    expect(ratio).not.toHaveTextContent('0.00')
  })

  it('R5: fewer than three trades cannot remove best and worst', () => {
    show()
    const candidate = screen.getByRole('heading', { name: 'Candidate (Simulated)' }).closest('article')
    expect(sideFigures(finished.candidate).withoutBestWorst).toBe('not measured')
    expect(within(candidate).getByText('Without best and worst: not measured')).toBeVisible()
    expect(candidate).not.toHaveTextContent('Without best and worst: $0.00')
    expect(sideFigures({ ...finished.candidate, trades: 3, without_best_and_worst_cents: 7 }).withoutBestWorst).toBe('$0.07')
  })

  it('R6: a zero or absent p99 is unmeasured', () => {
    const mount = show()
    expect(decisionTime(finished.latency)).toBe('Decision time: not measured')
    expect(screen.getByText('Decision time: not measured')).toBeVisible()
    expect(panel()).not.toHaveTextContent('0.00 ms')
    mount.rerender(<AfterSavePanel view={view({ ...finished, latency: undefined })} />)
    expect(screen.getByText('Decision time: not measured')).toBeVisible()
    expect(decisionTime(degraded.latency)).toBe('Decision time: 0.10 ms')
  })

  it('R7: trades without fees withhold both nets, attribution and every delta', () => {
    const data = { ...zeroFee, attribution: [{ trade_id: 'hidden-trade', candidate_cents: 12345, live_cents: 23456, delta_cents: 34567 }] }
    show(data)
    expect(withheld(zeroFee)).toEqual({ code: 'zero_fee_with_trades', sentence: 'Costs missing. Result withheld.' })
    expect(screen.getByRole('alert').textContent).toBe('Costs missing. Result withheld.')
    expect(panel()).not.toHaveTextContent(/net|delta|\$0\.30|hidden-trade|\$123\.45/i)
    expect(screen.queryByRole('heading', { name: /Candidate|Live|Deltas/ })).toBeNull()
    cleanup()
    const mount = render(<LivePredictionGap comparison={zeroFee} />)
    expect(screen.getByRole('region')).not.toHaveTextContent(/net|delta|\$/i)
    const liveMissingFees = { ...finished, live: { ...finished.candidate, costs: { fees_cents: '0' } } }
    mount.rerender(<LivePredictionGap comparison={liveMissingFees} />)
    expect(screen.getByRole('region')).not.toHaveTextContent(/net|delta|\$/i)
  })

  it.each(['candidate', 'live'])('R7: a reported negative zero fee on %s still withholds both sides', side => {
    const data = { ...finished, [side]: { ...finished.candidate, costs: { fees_cents: '-0' } } }
    show(data)
    expect(withheld(data)).toEqual({ code: 'zero_fee_with_trades', sentence: sentence('zero_fee_with_trades') })
    expect(screen.getByRole('alert').textContent).toBe('Costs missing. Result withheld.')
    expect(panel()).not.toHaveTextContent(/net|delta|\$/i)
  })

  it.each(['none', 'pending', 'running', 'unavailable', 'failed', 'finished', 'expired', 'dashboard_only'])('R8: %s always labels replay as direction only', state => {
    show({ ...unavailable, state })
    expect(panel().querySelector('footer')).toHaveTextContent(`${sentence('replay_is_direction')} Simulated.`)
    expect(panel()).not.toHaveTextContent(/verdict|Keeper|Test passed/i)
  })

  it('R9: names the Practice schedule and the Exchange fee formula', () => {
    const mount = show()
    const expected = 'Practice fees: kalshi-2026-07, whole cents per contract, rounded up (an upper bound). Exchange fee: taker 0.07 x C x P x (1 - P), rounded up once per order; maker 0.'
    expect(scheduleLine(finished.fee_schedule_id)).toBe(expected)
    expect(screen.getByText(expected)).toBeVisible()
    const candidate = screen.getByRole('heading', { name: 'Candidate (Simulated)' }).closest('article')
    expect(within(candidate).getByText('Fees').closest('div')).toHaveTextContent('Fees$0.04')
    mount.rerender(<AfterSavePanel view={view(degraded)} />)
    expect(scheduleLine(undefined)).toBe('Fee schedule: not reported.')
    expect(screen.getByText('Fee schedule: not reported.')).toBeVisible()
  })

  it('R10: states the fixed, non-ranking replay window on the real unavailable receipt', () => {
    expect(unavailable).toMatchObject({ schema: 'leaf.sel112-operator-receipt.v1', state: 'unavailable', simulated_only: true })
    expect(unavailable).not.toHaveProperty('reason')
    expect(unavailable).not.toHaveProperty('candidate')
    show(unavailable)
    expect(WINDOW_LINE).toBe('Last 4 hours (fixed). A 4-hour replay ranks nothing.')
    expect(screen.getByText(WINDOW_LINE)).toBeVisible()
    expect(screen.getByText('Not reported')).toBeVisible()
    expect(screen.getByText('No statistics: Practice unavailable.')).toBeVisible()
    expect(panel().querySelector('footer')).toHaveTextContent('Direction only. Never evidence and never a gate. Simulated.')
    expect(screen.queryByRole('heading', { name: /Candidate/ })).toBeNull()
    expect(panel()).not.toHaveTextContent(/tape unavailable|\$/)
    expect(panel()).not.toHaveTextContent(unavailable.signed_by)
    expect(panel()).not.toHaveTextContent(unavailable.request_handle)
  })

  it('R11: explains unsupported blocks without an empty Candidate side', () => {
    show({ ...unavailable, reason: 'unsupported_block:signal' })
    expect(screen.getByText('No Practice replay exists for this block yet.')).toBeVisible()
    expect(screen.queryByRole('heading', { name: /Candidate/ })).toBeNull()
    expect(panel()).not.toHaveTextContent('unsupported_block:signal')
    expect(panel()).not.toHaveTextContent('No statistics:')
  })

  it('R12: uses reported registration states, UTC bounds and clean days', () => {
    show(unavailable, { registration: registration.rows })
    const lines = registrationLines(registration.rows)
    for (const state of ['open', 'closed', 'voided']) expect(screen.getByText(`Pre-registration: ${state}.`)).toBeVisible()
    expect(lines).toContain('Claimed UTC days 2026-11-06 00:00 to 2026-12-01 00:00 (end exclusive)')
    expect(lines).toContain('Claimed UTC days 2026-12-31 00:00 to 2027-01-01 00:00 (end exclusive)')
    expect(lines).toContain('Days reconciled green: 2')
    expect(lines).toContain('Clean days remaining: 8')
    expect(lines).toContain('Days reconciled green: not measured')
    expect(lines).toContain('Clean days remaining: not measured')
    for (const line of new Set(lines)) expect(screen.getAllByText(line).length).toBeGreaterThan(0)
    expect(panel()).not.toHaveTextContent(/0 days left|budget/i)
  })

  it('R13: renders exact Exchange figures from projected ledger rows', () => {
    show(unavailable, { ledger: ledger.rows })
    expect(ledgerLines(ledger.rows).lines).toEqual([cleanLedgerLine])
    expect(screen.getByText(cleanLedgerLine)).toBeVisible()
    expect(panel()).not.toHaveTextContent(/ticket-derived|venue/i)
    expect(ledgerLines([{ ...ledger.rows[2], role: 'probe' }]).lines[0]).toContain(' · Test order · ')
    const missing = ledgerLines([{ ...ledger.rows[2], fills: null, fee_cents: null, settled_net_cents: null }])
    expect(missing.lines[0]).toContain('Fills: Not reported · Exchange fee: Not reported · Exchange settled net: Not reported')
  })

  it('R14: keeps Simulated and Exchange figures separate and names the discrepancy', () => {
    show(finished, { ledger: ledger.rows })
    expect(screen.getByRole('heading', { name: 'Candidate (Simulated)' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Live (Simulated)' })).toBeVisible()
    expect(screen.getByText(cleanLedgerLine)).toBeVisible()
    expect(screen.getByRole('region', { name: 'Live versus predicted gap' })).toHaveTextContent('Simulated replay · Candidate-minus-Live net delta: $0.30 · execution discrepancy.')
    expect(panel()).not.toHaveTextContent('$612.12')
  })

  it('R15: withholds flagged days even when the producer leaves figures present', () => {
    show(unavailable, { ledger: ledger.rows })
    expect(ledgerLines(ledger.rows).withheldDays).toEqual([
      { day_utc: '2026-11-01', sentence: 'Not pre-registered. No result shown.' },
      { day_utc: '2026-11-02', sentence: 'Costs missing. Result withheld.' },
    ])
    expect(panel().querySelector('[data-day-utc="2026-11-01"]').textContent).toBe('Not pre-registered. No result shown.')
    expect(panel().querySelector('[data-day-utc="2026-11-02"]').textContent).toBe('Costs missing. Result withheld.')
    expect(panel()).not.toHaveTextContent(/987|\$9876\.54|\$987\.65/)
    expect(ledgerLines([{ ...ledger.rows[2], withheld: true, honesty_codes: [] }]).lines).toEqual([])
    expect(ledgerLines([{ ...ledger.rows[1], honesty_codes: ['replay_is_direction', 'not_preregistered', 'zero_fee_with_trades'] }]).withheldDays[0].sentence).toBe('Not pre-registered. No result shown.')
    const unknown = ledgerLines([{ ...ledger.rows[2], honesty_codes: ['code_from_a_newer_producer'] }])
    expect(unknown).toEqual({ lines: [], withheldDays: [{ day_utc: '2026-11-03', sentence: 'Result withheld.' }] })
  })

  it('R16: shows looks only when both counts were reported, including zero', () => {
    const mount = show(unavailable, { registration: registration.rows })
    expect(registrationLines(registration.rows)).toContain('Looked at 5 times by 3 people.')
    expect(screen.getAllByText(/Looked at/)).toHaveLength(1)
    expect(screen.getByText('Looked at 5 times by 3 people.')).toBeVisible()
    const rows = [{ ...registration.rows[0], looks: 0, lookers: 0 }]
    mount.rerender(<AfterSavePanel view={view(unavailable)} registration={rows} />)
    expect(screen.getByText('Looked at 0 times by 0 people.')).toBeVisible()
    expect(registrationLines([{ ...rows[0], looks: undefined }]).join(' ')).not.toContain('Looked at')
  })

  it('R17: puts the operation sentence beside every ledger figure', () => {
    show(unavailable, { ledger: ledger.rows })
    expect(OPERATION_LINE).toBe('Operation verified. No result verdict before the economics horizon.')
    const figure = screen.getByText(cleanLedgerLine)
    expect(figure.textContent).toContain(OPERATION_LINE)
    expect(figure.textContent).toContain('Exchange settled net: $611.82')
    expect(panel()).not.toHaveTextContent(/win rate|profit verdict/i)
  })
})

describe('source section states and standards', () => {
  it('distinguishes absent sources from sources reporting no days', () => {
    const mount = show(unavailable)
    expect(registrationLines(undefined)).toEqual(['Pre-registration: not reported. Clean days: not reported.'])
    expect(ledgerLines(undefined).lines).toEqual(['Exchange ledger: not reporting. Tickets are not shown as P&L.'])
    expect(screen.getByText('Pre-registration: not reported. Clean days: not reported.')).toBeVisible()
    expect(screen.getByText('Exchange ledger: not reporting. Tickets are not shown as P&L.')).toBeVisible()
    expect(panel()).not.toHaveTextContent('0 days left')
    mount.rerender(<AfterSavePanel view={view(unavailable)} registration={[]} ledger={[]} sourcesState="ready" />)
    for (const name of ['Pre-registration', 'Exchange ledger']) {
      const section = screen.getByRole('region', { name })
      expect(section).toHaveAttribute('data-state', 'empty')
      expect(within(section).getByText('No days reported yet.')).toBeVisible()
    }
  })

  it.each([['loading', 'Loading Exchange ledger'], ['error', 'The Exchange ledger could not be loaded.']])('renders %s without source figures', (state, line) => {
    show(unavailable, { ledger: ledger.rows, registration: registration.rows, sourcesState: state })
    for (const name of ['Pre-registration', 'Exchange ledger']) {
      const section = screen.getByRole('region', { name })
      expect(section).toHaveAttribute('data-state', state)
      expect(within(section).getByText(line)).toBeVisible()
      expect(section).not.toHaveTextContent(/\$|Claimed UTC|Days reconciled|Clean days remaining|Looked at|Fills:/)
    }
    expect(screen.queryByRole('button')).toBeNull()
  })

  it.each([unavailable, finished, degraded, zeroFee])('uses standard display words for $handle', data => {
    show(data, { ledger: ledger.rows, registration: registration.rows })
    expect(replacedWordsIn(panel().textContent)).toEqual([])
    for (const name of ['Pre-registration', 'Exchange ledger']) expect(screen.getByRole('region', { name })).toHaveAttribute('data-state', 'value')
  })

  it.each(['./AfterSavePanel.jsx', './afterSaveHonesty.js', '../changes/LivePredictionGap.jsx'])('uses token colors and no new CSS classes in %s', path => {
    const source = readFileSync(new URL(path, import.meta.url), 'utf8')
    expect(colorLiteralsIn(source)).toEqual([])
    expect(source).not.toContain('className=')
  })
})
