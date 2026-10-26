import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { readController } from './controller.js'
import TestsKeepersRail from './TestsKeepersRail.jsx'
import LivePredictionGap from './LivePredictionGap.jsx'

afterEach(cleanup)
const at = '2026-10-26T12:00:00Z'
const trial = { rollout_id: 'trial-1', state: 'screened', at,
  screen: { verdict: 'pass', reason: 'Screen completed', net_dollars: 1.25, episodes_completed: 8, scope_count: 2, kind: 'screen', complete: true } }
const keeper = { feature: 'Sizing', scope_count: 2, rollout_id: 'trial-1', window: 'last 4 hours',
  episodes_completed: 8, net_dollars: '2.50', added_at: at }
const report = (patch = {}) => ({ kind: 'controller', received_at: at, report_only: true, truncated: false,
  screen_trials: [trial], keepers: [keeper], versions: { champion_digest: 'a'.repeat(64), latest: {
    rollout_id: 'trial-1', kind: 'screen', feature: 'Sizing', snapshot_digest: 'b'.repeat(64), live_at: at,
    state: 'live', closed_at: null, rollback_pending: false, source_pending: false,
  } }, ...patch })
const provider = (...rows) => ({ rowsOf: source => source === 'evidence' ? rows : [] })
const controller = (patch = {}, flags = {}) => readController(provider({ data: report(patch), ...flags }))
const banner = 'Report only. Keep, Undo and combine run in the champion rule controller, not here.'

describe('controller evidence', () => {
  it('selects the newest report by received time without mutating host order', () => {
    const newest = { data: report(), stale: true, truncated: true }
    const older = { data: report({ received_at: '2026-10-25T12:00:00Z', keepers: [] }) }
    const host = provider(newest, { data: { kind: 'receipt' } }, older)
    expect(readController(host)).toMatchObject({ received_at: at, stale: true, truncated: true, keepers: [keeper] })
    expect(host.rowsOf('evidence')[0]).toBe(newest)
  })

  it('bounds both lists to twenty and drops malformed entries and untrusted fields', () => {
    const result = controller({ screen_trials: [null, {}, ...Array(25).fill(trial)], keepers: [null, {}, ...Array(25).fill(keeper)], secret: 'omit' })
    expect(result.screen_trials).toHaveLength(20)
    expect(result.keepers).toHaveLength(20)
    expect(result.truncated).toBe(true)
    expect(result).not.toHaveProperty('secret')
    expect(controller({ screen_trials: [{ ...trial, screen: { verdict: {}, net_dollars: Infinity, complete: 'true', episodes_completed: -1 } }] }).screen_trials[0].screen)
      .toMatchObject({ verdict: null, net_dollars: null, complete: null, episodes_completed: null })
    expect(controller({ versions: { champion_digest: 'not a digest', latest: {} } }).versions)
      .toEqual({ champion_digest: null, latest: null })
  })

  it('returns null for unavailable or malformed reports and never revives an older report', () => {
    for (const host of [undefined, {}, { rowsOf: () => { throw Error('offline') } }, { rowsOf: () => null },
      provider({ data: null }), provider({ data: report({ report_only: false }) }),
      provider({ data: report({ screen_trials: {} }) }), provider({ data: report({ keepers: null }) }),
      provider({ data: report({ versions: null }) }), provider({ data: report({ received_at: 'bad' }) })]) {
      expect(readController(host)).toBeNull()
    }
    expect(readController(provider({ data: report() }, { data: report({ received_at: '2026-10-27T12:00:00Z', report_only: false }) }))).toBeNull()
  })
})

describe('report-only Tests and Keepers', () => {
  it('shows versions, elapsed hours, Tests, and reported Keepers without action buttons', () => {
    render(<TestsKeepersRail controller={controller({}, { stale: true, truncated: true })} now={Date.parse(at) + 7200000} />)
    expect(screen.getByText(banner)).toBeVisible()
    expect(screen.getByText('stale')).toBeVisible()
    expect(screen.getByText('truncated')).toBeVisible()
    const versions = within(screen.getByRole('region', { name: 'Versions' }))
    expect(versions.getByText('Champion aaaaaaaaaaaa')).toBeVisible()
    expect(versions.getByText(/Latest trial-1 · screen · Sizing · live/)).toBeVisible()
    expect(versions.getByText(`Live at ${at} · 2.0 hours elapsed`)).toBeVisible()
    const tests = within(screen.getByRole('region', { name: 'Tests' }))
    expect(tests.getByText(/trial-1 · screened/)).toBeVisible()
    expect(tests.getByText(/Verdict pass/)).toBeVisible()
    expect(tests.getByText(/Net after fees \$1.25 · Episodes 8 · Scopes 2 · Kind screen · complete/)).toBeVisible()
    const keepers = within(screen.getByRole('region', { name: 'Keepers' }))
    expect(keepers.getByText('Reported, not active: ratification needed')).toBeVisible()
    expect(keepers.getByText(/Sizing · Net after fees \$2.50 · Window last 4 hours · Episodes 8/)).toBeVisible()
    expect(keepers.getByText(/Added 2026-10-26T12:00:00Z/)).toBeVisible()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('distinguishes pending restart from a closed rollback and shows incomplete screens', () => {
    const versions = report().versions
    const pending = { ...versions, latest: { ...versions.latest, state: 'rolled_back', closed_at: at, rollback_pending: true } }
    const view = render(<TestsKeepersRail controller={controller({ versions: pending })} />)
    expect(screen.getByText('Restart pending')).toBeVisible()
    expect(screen.queryByText('Undone')).toBeNull()
    view.rerender(<TestsKeepersRail controller={controller({ versions: { ...pending, latest: { ...pending.latest, rollback_pending: false } },
      screen_trials: [{ ...trial, screen: { ...trial.screen, complete: false } }] })} />)
    expect(screen.getByText('Undone')).toBeVisible()
    expect(screen.queryByText('Restart pending')).toBeNull()
    expect(screen.getByText(`Closed at ${at}`)).toBeVisible()
    expect(screen.getByText(/Kind screen · incomplete/)).toBeVisible()
    view.rerender(<TestsKeepersRail controller={controller({ versions: { ...pending,
      latest: { ...pending.latest, kind: 'rollback', state: 'closed', rollback_pending: false } } })} />)
    expect(screen.getByText('Undone')).toBeVisible()
  })

  it('keeps the banner in empty and collapsed states and reports unavailable Keepers', () => {
    const view = render(<TestsKeepersRail />)
    expect(screen.getByText(banner)).toBeVisible()
    expect(screen.getByText('No controller report yet')).toBeVisible()
    view.rerender(<TestsKeepersRail controller={controller({ screen_trials: [], keepers: [], versions: { champion_digest: null, latest: null } })} />)
    expect(screen.getByText('No Tests reported')).toBeVisible()
    expect(screen.getByText('No Keepers reported')).toBeVisible()
    expect(screen.getByText('No latest rollout')).toBeVisible()
    view.rerender(<TestsKeepersRail controller={controller({ keepers_unavailable: true })} />)
    expect(screen.getByText('Keepers unavailable')).toBeVisible()
    expect(screen.queryByText('Reported, not active: ratification needed')).toBeNull()
    view.rerender(<TestsKeepersRail controller={controller()} collapsed />)
    expect(screen.getByText(banner)).toBeVisible()
    expect(screen.getByText('Tests / Keepers').closest('details')).not.toHaveAttribute('open')
  })
})

describe('Live versus predicted gap', () => {
  it('reports no measurement and labels an existing Practice delta as simulated', () => {
    const view = render(<LivePredictionGap />)
    expect(screen.getByText('Live versus predicted gap: not measured (no matched comparison receipt)')).toBeVisible()
    expect(screen.queryByText(/Simulated replay/)).toBeNull()
    view.rerender(<LivePredictionGap comparison={{ deltas: { net_after_fees_cents: -125 } }} />)
    expect(screen.getByText('Simulated replay, not a verdict · Candidate-minus-Live net delta: -$1.25')).toBeVisible()
    view.rerender(<LivePredictionGap comparison={{ deltas: { net_after_fees_cents: 0 } }} />)
    expect(screen.getByText(/Candidate-minus-Live net delta: \$0.00/)).toBeVisible()
    view.rerender(<LivePredictionGap comparison={{ deltas: { net_after_fees_cents: {} } }} />)
    expect(screen.getByText(/Candidate-minus-Live net delta: Not reported/)).toBeVisible()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
