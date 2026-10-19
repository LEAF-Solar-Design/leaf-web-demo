import assert from 'node:assert/strict'
import { performance } from 'node:perf_hooks'
import { repoPath, verifiedFixture } from './fixtures.mjs'

export const STEP_VERBS = Object.freeze([
  'goto', 'click', 'fill', 'upload', 'press', 'wait_text', 'assert_text',
  'scroll_to', 'hover', 'pause',
])

const nonempty = (value) => typeof value === 'string' && value.trim().length > 0
const milliseconds = (value) => Number.isSafeInteger(value) && value >= 0

function keys(value, expected, label) {
  assert.ok(value && typeof value === 'object' && !Array.isArray(value), `${label}: expected object`)
  assert.deepEqual(Object.keys(value).sort(), [...expected].sort(), `${label}: unexpected or missing fields`)
}

function targetKeys(target) {
  if (target && Object.hasOwn(target, 'testid')) return ['testid']
  if (target && Object.hasOwn(target, 'role')) return ['role', 'name']
  return ['text']
}

function validateTarget(target, label) {
  const fields = targetKeys(target)
  keys(target, fields, label)
  for (const field of fields) assert.ok(nonempty(target[field]), `${label}.${field}: expected nonempty string`)
}

export function validateBeat(beat) {
  keys(beat, ['schema', 'beat_id', 'env', 'origin', 'requires_signed_in', 'fixtures', 'steps', 'expect_text'], 'beat')
  assert.equal(beat.schema, 'leaf.studio-beats.v1', 'beat.schema')
  assert.ok(nonempty(beat.beat_id), 'beat.beat_id')
  assert.equal(beat.env, 'staging', 'beat.env')
  assert.equal(beat.origin, 'https://platform-staging.leafdesign.ai', 'beat.origin')
  assert.equal(typeof beat.requires_signed_in, 'boolean', 'beat.requires_signed_in')
  assert.ok(Array.isArray(beat.fixtures), 'beat.fixtures: expected array')
  const fixturePaths = new Set()
  for (const fixture of beat.fixtures) {
    keys(fixture, ['path', 'sha256', 'bytes'], 'fixture')
    const uploadIndex = Array.isArray(beat.steps)
      ? beat.steps.findIndex((step) => step?.verb === 'upload' && step.fixture === fixture.path) : -1
    assert.ok(repoPath(fixture.path), `${uploadIndex < 0 ? '' : `steps[${uploadIndex}] `}fixture.path: expected repo-relative POSIX path`)
    assert.ok(!fixturePaths.has(fixture.path), `duplicate fixture: ${fixture.path}`)
    fixturePaths.add(fixture.path)
    assert.ok(typeof fixture.sha256 === 'string' && /^[a-f0-9]{64}$/.test(fixture.sha256), 'fixture.sha256')
    assert.ok(milliseconds(fixture.bytes), 'fixture.bytes: expected nonnegative integer')
  }
  assert.ok(Array.isArray(beat.steps) && beat.steps.length > 0, 'beat.steps: expected nonempty array')
  for (const [index, step] of beat.steps.entries()) {
    const label = `steps[${index}]`
    assert.ok(STEP_VERBS.includes(step?.verb), `${label}: unknown verb ${step?.verb}`)
    switch (step.verb) {
      case 'goto':
        keys(step, ['verb', 'path'], label)
        assert.ok(nonempty(step.path) && /^\/(?!\/)/.test(step.path)
          && !/[\\\u0000-\u0020\u007f]/.test(step.path), `${label}.path: expected origin-relative path`)
        break
      case 'click': {
        const { verb, ...target } = step
        validateTarget(target, label)
        break
      }
      case 'fill':
      case 'upload': {
        const field = step.verb === 'fill' ? 'value' : 'fixture'
        keys(step, ['verb', 'target', field], label)
        validateTarget(step.target, `${label}.target`)
        if (field === 'value') assert.equal(typeof step.value, 'string', `${label}.value`)
        else assert.ok(fixturePaths.has(step.fixture), `${label}.fixture: undeclared fixture`)
        break
      }
      case 'press':
        keys(step, ['verb', 'key'], label)
        assert.ok(nonempty(step.key), `${label}.key`)
        break
      case 'wait_text':
      case 'assert_text':
        keys(step, step.verb === 'wait_text' ? ['verb', 'text', 'timeout_ms'] : ['verb', 'text'], label)
        assert.ok(nonempty(step.text), `${label}.text`)
        if (step.verb === 'wait_text') assert.ok(milliseconds(step.timeout_ms) && step.timeout_ms > 0, `${label}.timeout_ms`)
        break
      case 'scroll_to':
      case 'hover':
        keys(step, ['verb', 'target'], label)
        validateTarget(step.target, `${label}.target`)
        break
      case 'pause':
        keys(step, ['verb', 'ms'], label)
        assert.ok(milliseconds(step.ms), `${label}.ms`)
        break
    }
  }
  assert.ok(Array.isArray(beat.expect_text), 'beat.expect_text: expected array')
  assert.deepEqual(beat.expect_text, beat.steps.filter((step) => step.verb === 'assert_text').map((step) => step.text),
    'beat.expect_text must list exactly the asserted strings in order')
}

function locate(page, target, formControl = false) {
  if (Object.hasOwn(target, 'testid')) return page.getByTestId(target.testid)
  if (Object.hasOwn(target, 'role')) return page.getByRole(target.role, { name: target.name, exact: true })
  return formControl
    ? page.getByLabel(target.text, { exact: true })
    : page.getByText(target.text, { exact: true })
}

async function innerWidth(page) {
  const width = await page.evaluate(() => window.innerWidth)
  assert.ok(Number.isFinite(width) && width >= 981, `Studio take requires window.innerWidth >= 981; got ${width}`)
  return width
}

export async function runBeat(page, beat, params = {}) {
  // Preflight the entire vocabulary and shape before any browser interaction.
  validateBeat(beat)
  const t_start_ms = performance.timeOrigin + performance.now()
  await innerWidth(page)
  for (const [index, step] of beat.steps.entries()) {
    try {
      switch (step.verb) {
        case 'goto': await page.goto(step.path); break
        case 'click': await locate(page, step).click(); break
        case 'fill': await locate(page, step.target, true).fill(step.value); break
        case 'upload': {
          const fixture = beat.fixtures.find((entry) => entry.path === step.fixture)
          const payload = await verifiedFixture(fixture)
          await locate(page, step.target, true).setInputFiles(payload)
          break
        }
        case 'press': await page.keyboard.press(step.key); break
        case 'wait_text':
        case 'assert_text':
          await page.getByText(step.text, { exact: true }).filter({ visible: true }).first()
            .waitFor({ state: 'visible', timeout: step.verb === 'wait_text' ? step.timeout_ms : 30_000 })
          break
        case 'scroll_to': await locate(page, step.target).scrollIntoViewIfNeeded(); break
        case 'hover': await locate(page, step.target).hover(); break
        case 'pause': await page.waitForTimeout(step.ms); break
      }
    } catch (error) {
      throw new Error(`Beat ${beat.beat_id} steps[${index}] (${step.verb}) failed: ${error?.message ?? error}`, { cause: error })
    }
  }
  const inner_width = await innerWidth(page)
  return {
    beat_id: beat.beat_id,
    sync_id: params.sync_id ?? null,
    t_start_ms,
    t_end_ms: performance.timeOrigin + performance.now(),
    expect_text: [...beat.expect_text],
    inner_width,
  }
}
