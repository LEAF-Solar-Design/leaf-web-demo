import test from 'node:test'
import assert from 'node:assert/strict'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { holdJobRoutes, openHistory, previewVersion, startPendingRun, runProbe, assertEffect } from './fixtures.mjs'

// Exercise the real runner seams without booting the worker-stack fixture.
const expect = (value) => ({
  toBe: (expected) => assert.equal(value, expected),
  toBeTruthy: () => assert.ok(value),
  toBeGreaterThan: (expected) => assert.ok(value > expected),
  toBeVisible: async () => assert.equal(value.visible, true),
  toBeHidden: async () => assert.equal(value.visible, false),
  toHaveCount: async (expected) => assert.equal(value.countValue, expected),
})

function fakePage() {
  const events = []
  let historyOpen = false
  let previewed = false
  const locator = (role, options = {}) => {
    const name = options.name
    return {
      getByRole: locator,
      get visible() {
        if (role === 'dialog') return historyOpen
        if (name === 'More panels') return false
        if (role === 'text') return previewed && name.test('Viewing v1 of 2, read-only preview')
        return true
      },
      isVisible: async () => name !== 'More panels',
      click: async (bounds) => {
        assert.equal(bounds.timeout, 15_000)
        events.push({ role, name, bounds })
        if (name === 'history') historyOpen = true
        if (name instanceof RegExp) {
          assert.ok(name.test('v1'))
          assert.ok(!name.test('v2'))
          previewed = true
        }
        if (name === 'Close version history') historyOpen = false
      },
    }
  }
  let reads = 0
  const reply = (body) => ({ ok: () => true, json: async () => body })
  const page = {
    events, getByRole: locator, getByText: (name) => locator('text', { name }),
    reload: async (bounds) => { assert.equal(bounds.timeout, 60_000); events.push({ reload: true }) },
    request: {
      get: async (url, bounds) => {
        assert.equal(bounds.timeout, 15_000)
        assert.equal(url, '/api/drawings/private/versions')
        return reply(++reads === 1 ? { head: 1 } : { head: 2, versions: [{ v: 2 }, { v: 1 }] })
      },
      post: async (url, bounds) => {
        assert.equal(bounds.timeout, 15_000)
        assert.equal(url, '/api/drawings/private/versions/1/restore')
        return reply({ head: 2, new_version: { version: 2 }, restored_from: 1 })
      },
    },
  }
  return page
}

test('read-only uses the registry History name, explicitly previews non-head and restores the tab', async () => {
  const page = fakePage()
  const runtime = { page, evidence: {}, drawingId: 'private', ribbonTab: 'Manage' }
  await previewVersion({}, runtime, expect, async () => {})
  assert.equal(page.events.find((event) => event.role === 'button' && event.name === 'history').name,
    accessibleName(ACTIONS.find((action) => action.id === 'history').label))
  assert.deepEqual(runtime.evidence.versionPreview, { head: 2, preview: 1, restoredFrom: 1 })
  assert.equal(runtime.ribbonTab, 'Manage')
  assert.equal(page.events.at(-1).name, 'Manage')
  assert.ok(page.events.find((event) => event.reload))
})

test('History failure is bounded and stops setup before preview', async () => {
  const page = fakePage()
  const original = page.getByRole
  page.getByRole = (role, options) => {
    const result = original(role, options)
    if (role === 'toolbar') result.getByRole = (_, button) => {
      assert.equal(button.name, 'history')
      return { click: async (bounds) => {
        assert.equal(bounds.timeout, 15_000)
        throw new Error('History click timed out')
      } }
    }
    return result
  }
  await assert.rejects(openHistory({}, { page, evidence: {} }, expect), /timed out/)
})

test('job setup requires accepted submission and visible pending UI, then restores the feature tab', async () => {
  const page = fakePage()
  page.request.get = async () => ({ ok: () => true, json: async () => ({ families: [{ capabilities: [{ name: 'count-by-layer' }] }] }) })
  page.route = async () => {}
  page.unroute = async () => {}
  let status = 202
  let pending = true
  page.waitForResponse = (predicate, bounds) => {
    assert.equal(bounds.timeout, 15_000)
    const response = { request: () => ({ method: () => 'POST' }), url: () => 'http://localhost/api/run',
      status: () => status, json: async () => ({ job_id: 'real-job' }) }
    assert.equal(predicate(response), true)
    return Promise.resolve(response)
  }
  page.locator = (selector) => {
    assert.equal(selector, '.strip-running')
    return { get visible() { return pending } }
  }
  const runtime = { page, evidence: {}, cleanup: [], ribbonTab: 'View', workerFacts: {} }
  await startPendingRun({}, runtime, expect)
  assert.deepEqual(runtime.evidence.pendingRun, { tool: 'count-by-layer', jobId: 'real-job', status: 202, pendingVisible: true })
  assert.equal(runtime.ribbonTab, 'View')
  await runtime.cleanup.pop()()
  status = 200
  await assert.rejects(startPendingRun({}, runtime, expect))
  await runtime.cleanup.pop()()
  status = 202
  pending = false
  await assert.rejects(startPendingRun({}, runtime, expect))
  await runtime.cleanup.pop()()
})

test('cleanup drains several outstanding routes exactly once before unroute, including pass-through arrivals', async () => {
  let handler
  let unroutes = 0
  const counts = [0, 0, 0, 0]
  let finish
  const slow = new Promise((resolve) => { finish = resolve })
  const page = {
    route: async (_, callback) => { handler = callback },
    unroute: async (_, callback) => {
      assert.equal(callback, handler)
      assert.deepEqual(counts, [1, 1, 1, 1])
      unroutes++
    },
  }
  const cleanup = await holdJobRoutes(page)
  const route = (index) => ({ continue: async () => {
    assert.equal(++counts[index], 1)
    if (index === 0) await slow
  } })
  const outstanding = [0, 1, 2].map((index) => handler(route(index)))
  assert.deepEqual(counts, [0, 0, 0, 0])
  const draining = cleanup()
  assert.equal(cleanup(), draining, 'cleanup is idempotent')
  await handler(route(3))
  assert.equal(unroutes, 0)
  finish()
  await draining
  await Promise.all(outstanding)
  assert.equal(unroutes, 1)
})

test('submission setup failure still drains routes through runProbe finally', async () => {
  let continued = 0
  let unroute = 0
  let outstanding
  const page = fakePage()
  page.request.get = async () => ({ ok: () => true, json: async () => ({ families: [{ capabilities: [{ name: 'count-by-layer' }] }] }) })
  page.route = async (_, handler) => { outstanding = handler({ continue: async () => { continued++ } }) }
  page.unroute = async () => { assert.equal(continued, 1); unroute++ }
  const original = page.getByRole
  page.getByRole = (role, options) => {
    if (options.name === 'Run count-by-layer') return { click: async (bounds) => {
      assert.equal(bounds.timeout, 15_000)
      throw new Error('submission failed')
    } }
    return original(role, options)
  }
  page.waitForResponse = (_, bounds) => {
    assert.equal(bounds.timeout, 15_000)
    return Promise.reject(new Error('response timed out'))
  }
  const probe = { kind: 'action', featureId: 'action:undo', state: 'job-running',
    setup: { steps: [{ kind: 'start-pending-run' }] }, assertion: { assertionId: 'undo/running' } }
  await assert.rejects(runProbe(probe, { page, evidence: { steps: [] }, testInfo: { annotations: [] },
    recipeAssertions: expect, runStep: async (_, callback) => callback() }), /submission failed/)
  await outstanding
  assert.equal(continued, 1)
  assert.equal(unroute, 1)
})

test('running Escape requires the detach message and removal of the running strip', async () => {
  const runtime = { evidence: { pendingRun: {} }, page: {
    getByText: (text, options) => {
      assert.equal(text, 'Stopped following count-by-layer. It keeps running; find it in Jobs.')
      assert.equal(options.exact, true)
      return { visible: true }
    },
    locator: (selector) => { assert.equal(selector, '.strip-running'); return { countValue: 0 } },
  } }
  await assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect)
  assert.equal(runtime.evidence.pendingRun.detached, true)
  runtime.page.locator = () => ({ countValue: 1 })
  await assert.rejects(assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect))
})
