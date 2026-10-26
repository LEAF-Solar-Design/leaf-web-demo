import { expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { BOARD_TRANSFER_TYPE, MAX_TRANSFER_CHARS, createPreviewRunner, describePreviewOutcome, encodeVersionRef, resolveVersionTransfer } from './boardTransfer.js'

const ref = { projectId: 'project1', drawingId: 'drawing1', versionId: 'version1', seq: 2 }
const payload = { kind: 'version', ...ref }
const context = { projectId: ref.projectId, drawingId: ref.drawingId, versions: [{ version_id: ref.versionId, drawing_id: ref.drawingId, seq: ref.seq }] }

// Exercise App's actual drop callback without mounting the drawing renderer.
const appSource = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const appTree = parse(appSource, { sourceType: 'module', plugins: ['jsx'] })
function transferCallback(node = appTree) {
  if (!node || typeof node.type !== 'string') return null
  if (node.type === 'ObjectProperty' && node.key.name === 'onTransferVersion') return node.value.consequent
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      if (child && typeof child.type === 'string') {
        const found = transferCallback(child)
        if (found) return found
      }
    }
  }
  return null
}
function dropHarness(resolveTransfer = resolveVersionTransfer) {
  const boardTransferScopeRef = { current: { projectId: ref.projectId, drawingId: ref.drawingId } }
  const onPreviewVersionTracked = vi.fn(() => ({ version: ref.seq }))
  const setBoardTransferStatus = vi.fn()
  const bindings = {
    openProjectId: ref.projectId, workspace: { drawing_versions: context.versions },
    drawingState: { drawing_id: ref.drawingId }, boardTransferScopeRef,
    boardPreviewRunRef: { current: null }, resolveVersionTransfer: resolveTransfer,
    createPreviewRunner, onPreviewVersionTracked, setBoardTransferStatus,
  }
  const callback = transferCallback()
  expect(callback).not.toBeNull()
  const drop = new Function(...Object.keys(bindings), `return (${appSource.slice(callback.start, callback.end)})`)(...Object.values(bindings))
  return { drop, boardTransferScopeRef, onPreviewVersionTracked, setBoardTransferStatus }
}

it('ignores a successful board drop resolved after switching project or drawing', async () => {
  for (const change of [{ projectId: 'project2' }, { drawingId: 'drawing2' }]) {
    let finish
    const resolveTransfer = vi.fn(() => new Promise(resolve => { finish = resolve }))
    const h = dropHarness(resolveTransfer)
    const pending = h.drop(encodeVersionRef(ref))
    expect(resolveTransfer).toHaveBeenCalledTimes(1)
    h.boardTransferScopeRef.current = { ...h.boardTransferScopeRef.current, ...change }
    finish({ ok: true, seq: ref.seq })
    await pending
    await Promise.resolve()
    expect(h.onPreviewVersionTracked).not.toHaveBeenCalled()
    expect(h.setBoardTransferStatus).not.toHaveBeenCalled()
  }
})

it('ignores a failed board drop resolved after switching project or drawing', async () => {
  for (const change of [{ projectId: 'project2' }, { drawingId: 'drawing2' }]) {
    let finish
    const h = dropHarness(() => new Promise(resolve => { finish = resolve }))
    const pending = h.drop(encodeVersionRef(ref))
    h.boardTransferScopeRef.current = { ...h.boardTransferScopeRef.current, ...change }
    finish({ ok: false, message: 'Choose v2 from the current project to preview it.' })
    await pending
    expect(h.onPreviewVersionTracked).not.toHaveBeenCalled()
    expect(h.setBoardTransferStatus).not.toHaveBeenCalled()
  }
})

it('previews and reports board drops while their scope is still current', async () => {
  const h = dropHarness()
  await h.drop(encodeVersionRef(ref))
  expect(h.onPreviewVersionTracked).toHaveBeenCalledTimes(1)
  expect(h.onPreviewVersionTracked).toHaveBeenCalledWith(ref.seq)
  await vi.waitFor(() => expect(h.setBoardTransferStatus).toHaveBeenLastCalledWith('Previewing v2 in the drawing'))
  await h.drop('{')
  expect(h.setBoardTransferStatus).toHaveBeenLastCalledWith('Drag a version from the Versions card again.')
})

it('does not start a queued preview after its project scope changes', async () => {
  const h = dropHarness()
  const pending = h.drop(encodeVersionRef(ref))
  // The result is accepted first, but the preview runner starts in a later microtask.
  await Promise.resolve()
  h.boardTransferScopeRef.current = { projectId: 'project2', drawingId: 'drawing2' }
  h.setBoardTransferStatus.mockClear()
  await pending
  await Promise.resolve()
  await Promise.resolve()
  expect(h.onPreviewVersionTracked).not.toHaveBeenCalled()
  expect(h.setBoardTransferStatus).not.toHaveBeenCalled()
})

it('SSD1-24D caps an encoded reference at the transfer limit', () => {
  const escaped = '\\'.repeat(200)
  expect(encodeVersionRef({ projectId: escaped, drawingId: escaped, versionId: escaped, seq: 2 })).toBeNull()
  const plain = 'x'.repeat(200)
  const raw = encodeVersionRef({ projectId: plain, drawingId: plain, versionId: plain, seq: 2 })
  expect(raw).not.toBeNull()
  expect(raw.length).toBeLessThanOrEqual(MAX_TRANSFER_CHARS)
  expect(resolveVersionTransfer(raw, {
    projectId: plain, drawingId: plain, versions: [{ version_id: plain, drawing_id: plain, seq: 2 }],
  }).ok).toBe(true)
})

it('SSD1-24D names the version in an unreadable reference', () => {
  const result = resolveVersionTransfer(JSON.stringify({ ...payload, extra: true }), context)
  expect(result.reason).toBe('unreadable')
  expect(result.message).toBe('Drag v2 from the Versions card again.')
})

it('SSD1-24D describes the version actually shown', () => {
  expect(describePreviewOutcome(2, null)).toBe('Could not preview v2. Try again.')
  expect(describePreviewOutcome(2, undefined)).toBe('Could not preview v2. Try again.')
  expect(describePreviewOutcome(2, { version: 2 })).toBe('Previewing v2 in the drawing')
  expect(describePreviewOutcome(2, { version: 3 })).toBe('Showing v3, the latest version. v2 is no longer the latest.')
  expect(describePreviewOutcome(2, { head: 3 })).toBe('Showing v3, the latest version. v2 is no longer the latest.')
  expect(describePreviewOutcome(2, {})).toBe('Previewing v2 in the drawing')
})

it('SSD1-24D runs one preview at a time', async () => {
  const run = createPreviewRunner()
  let finish
  const preview = vi.fn(() => new Promise((resolve) => { finish = resolve }))
  const setStatus = vi.fn()
  expect(run(1, preview, setStatus)).toBe(true)
  expect(setStatus).toHaveBeenLastCalledWith('Loading v1 in the drawing')
  expect(run(2, preview, setStatus)).toBe(false)
  expect(setStatus).toHaveBeenLastCalledWith('Wait for v1 to finish loading.')
  await Promise.resolve()
  expect(preview).toHaveBeenCalledTimes(1)
  expect(preview).toHaveBeenCalledWith(1)
  finish({ version: 3 })
  await vi.waitFor(() => expect(setStatus).toHaveBeenLastCalledWith('Showing v3, the latest version. v1 is no longer the latest.'))
  expect(run(2, preview, setStatus)).toBe(true)
  await Promise.resolve()
  expect(preview).toHaveBeenCalledTimes(2)
  finish({ version: 2 })
  await vi.waitFor(() => expect(setStatus).toHaveBeenLastCalledWith('Previewing v2 in the drawing'))
})

it('SSD1-24D reports a failed preview', async () => {
  for (const preview of [() => null, () => Promise.reject(new Error('load failed')), () => { throw new Error('load failed') }]) {
    const run = createPreviewRunner()
    const setStatus = vi.fn()
    expect(run(1, preview, setStatus)).toBe(true)
    await vi.waitFor(() => expect(setStatus).toHaveBeenLastCalledWith('Could not preview v1. Try again.'))
    expect(run(1, () => ({ version: 1 }), setStatus)).toBe(true)
    await vi.waitFor(() => expect(setStatus).toHaveBeenLastCalledWith('Previewing v1 in the drawing'))
  }
})

it('SSD1-24D round trips a version reference into a frozen result', () => {
  expect(BOARD_TRANSFER_TYPE).toBe('application/x-leaf-board-ref')
  expect(MAX_TRANSFER_CHARS).toBe(1024)
  const raw = encodeVersionRef(ref)
  expect(raw).toBe(JSON.stringify(payload))
  const result = resolveVersionTransfer(raw, context)
  expect(result).toEqual({ ok: true, seq: 2, versionId: 'version1' })
  expect(Object.isFrozen(result)).toBe(true)
})

it.each([
  ['non-string', null, context, 'unreadable'],
  ['oversize', ' '.repeat(MAX_TRANSFER_CHARS + 1), context, 'unreadable'],
  ['invalid JSON', '{', context, 'unreadable'],
  ['array', '[]', context, 'unreadable'],
  ['null JSON', 'null', context, 'unreadable'],
  ['extra key', JSON.stringify({ ...payload, extra: true }), context, 'unreadable'],
  ['missing key', JSON.stringify({ ...payload, versionId: undefined }), context, 'unreadable'],
  ['job kind', JSON.stringify({ ...payload, kind: 'job' }), context, 'unsupported-kind'],
  ['empty drawing', JSON.stringify(payload), { ...context, drawingId: '' }, 'no-drawing'],
  ['another project', JSON.stringify({ ...payload, projectId: 'other' }), context, 'wrong-project'],
  ['another drawing', JSON.stringify({ ...payload, drawingId: 'other' }), context, 'wrong-drawing'],
  ['unknown version', JSON.stringify({ ...payload, versionId: 'other' }), context, 'stale-version'],
  ['another sequence', JSON.stringify({ ...payload, seq: 3 }), context, 'stale-version'],
  ['version in another drawing', JSON.stringify(payload), { ...context, versions: [{ ...context.versions[0], drawing_id: 'other' }] }, 'stale-version'],
])('SSD1-24D refuses %s with its exact reason and a frozen result', (_name, raw, current, reason) => {
  const result = resolveVersionTransfer(raw, current)
  expect(result.ok).toBe(false)
  expect(result.reason).toBe(reason)
  expect(result.message).toEqual(expect.any(String))
  expect(result.message.length).toBeGreaterThan(0)
  expect(Object.isFrozen(result)).toBe(true)
})

it.each(['projectId', 'drawingId', 'versionId'])('SSD1-24D refuses empty and oversized %s in the encoder', (key) => {
  expect(encodeVersionRef({ ...ref, [key]: '' })).toBeNull()
  expect(encodeVersionRef({ ...ref, [key]: 'x'.repeat(201) })).toBeNull()
})

it.each([0, 1.5, Number.MAX_SAFE_INTEGER + 1])('SSD1-24D refuses invalid sequence %s in the encoder', (seq) => {
  expect(encodeVersionRef({ ...ref, seq })).toBeNull()
})
