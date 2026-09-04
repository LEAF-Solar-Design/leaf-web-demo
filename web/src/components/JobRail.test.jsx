// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { fromBrokerJob } from '../lib/buildQueue.js'
import JobRail from './JobRail.jsx'

afterEach(cleanup)

const doneJob = Object.freeze({
  job_id: 'job-verified',
  tool: 'count-by-layer',
  status: 'complete',
  created_at: 1725400000,
})

const terminalRecord = fromBrokerJob({
  ...doneJob,
  receipts: [{ kind: 'terminal', ref: 'receipts/job-verified/receipt.json', at: 1725400001 }],
})

describe('JobRail build-record reconciliation', () => {
  it('enriches one live broker row with its terminal receipt without duplicating it', () => {
    const { container } = render(
      <JobRail mock jobs={[doneJob]} builds={[terminalRecord]} currentJob={null} />,
    )

    const cards = container.querySelectorAll('.bq-card')
    expect(cards).toHaveLength(1)
    expect(cards[0].getAttribute('data-verified')).toBe('1')
    expect(cards[0].querySelector('.bq-receipts')?.textContent).toContain('1 receipt')
  })

  it('keeps a broker build that is absent from the recent-jobs list', () => {
    const { container } = render(
      <JobRail mock jobs={[]} builds={[terminalRecord]} currentJob={null} />,
    )

    expect(container.querySelectorAll('.bq-card')).toHaveLength(1)
    expect(container.querySelector('.bq-card')?.getAttribute('data-lane')).toBe('broker')
  })

  it('does not let an older build poll replace a newer job state', () => {
    const runningJob = { ...doneJob, status: 'running' }
    const { container } = render(
      <JobRail mock jobs={[runningJob]} builds={[terminalRecord]} currentJob={null} />,
    )

    const card = container.querySelector('.bq-card')
    expect(card?.getAttribute('data-state')).toBe('running')
    expect(card?.getAttribute('data-verified')).toBe('0')
  })
})
