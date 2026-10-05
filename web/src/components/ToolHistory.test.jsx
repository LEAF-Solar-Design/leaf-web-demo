import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '../api.js'
import ToolHistory, { ToolHistoryProvider } from './ToolHistory.jsx'
import NavRail from '../site/NavRail.jsx'
import CapabilityCatalog from './CapabilityCatalog.jsx'

const tool = { name: 'count-panels', description: 'Count panels', params: { type: 'object', properties: {} }, capabilities: [], kind: 'read' }
const digest = 'a'.repeat(64)
const removalId = '11111111-1111-4111-8111-111111111111'
const predecessorId = '22222222-2222-4222-8222-222222222222'
const authority = { contract: 'leaf.customization-removal-authority.v1', tool_name: tool.name,
  effective_catalog_digest: digest, target_row_count: 1, target_provenance: 'tenant_repo', removal_authorized: true }
const staged = { receipt: { state: 'staged', change_set_id: removalId }, predecessor_change_set_id: predecessorId }
const restored = { change_set_id: predecessorId, catalog_commit: 'catalog-commit', catalog_digest: digest }
const published = { publication_status: 'published' }
const families = [
  { family_id: 'custom-authored', label: 'Authored', capabilities: [tool] },
  { family_id: 'built-in', label: 'Built in', capabilities: [{ ...tool, name: 'built-in-tool' }] },
]

function response(body, status = 200) {
  return { ok: status < 400, status, json: async () => body }
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
function spies() {
  return {
    authority: vi.spyOn(api, 'getToolRemovalAuthority').mockResolvedValue(authority),
    stage: vi.spyOn(api, 'stageToolRemoval').mockResolvedValue(staged),
    publish: vi.spyOn(api, 'publishStagedAuthor').mockResolvedValue(published),
    restore: vi.spyOn(api, 'restoreToolCatalog').mockResolvedValue(restored),
  }
}
function mount(callback, enabled = true, children = <ToolHistory tool={tool} onCatalogChanged={callback} />) {
  return render(<ToolHistoryProvider enabled={enabled}>{children}</ToolHistoryProvider>)
}
function click(name) { fireEvent.click(screen.getByRole('button', { name, exact: true })) }
const refusalSentence = 'The removal request was refused. Retry to check the current catalog, or dismiss it.'
function failure(status, reason_code, body = { reason_code }) {
  return Object.assign(new Error('private failure'), { status, body })
}
function startRemoval() { click('Remove'); click('Confirm Remove') }
const railProps = { activeSurface: 'toolcast', railFamilies: [families[0]], catalogFamilyCount: 1,
  catalogSource: 'endpoint', openFamilies: { 'custom-authored': true }, onReviseTool: () => {} }
function rail(props = {}) { return <NavRail {...railProps} {...props} /> }
async function stageRetry(error, definite) {
  const calls = spies()
  calls.stage.mockRejectedValueOnce(error)
  calls.authority.mockResolvedValueOnce(authority).mockResolvedValue({ ...authority, effective_catalog_digest: 'b'.repeat(64) })
  const callback = vi.fn()
  const view = mount(callback)
  startRemoval()
  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: 'Dismiss' }) !== null).toBe(definite)
  expect(calls.publish).not.toHaveBeenCalled()
  click('Retry removal')
  await waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
  expect(calls.authority).toHaveBeenCalledTimes(definite ? 2 : 1)
  const first = calls.stage.mock.calls[0][1]
  const second = calls.stage.mock.calls[1][1]
  if (definite) {
    expect(second.idempotencyKey).not.toBe(first.idempotencyKey)
    expect(second.catalogDigest).toBe('b'.repeat(64))
  } else expect(second).toEqual(first)
  expect(calls.publish).toHaveBeenCalledTimes(1)
  view.unmount()
  vi.restoreAllMocks()
}
async function remove() {
  click('Remove')
  click('Confirm Remove')
  await screen.findByRole('button', { name: 'Restore', exact: true })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Restore', exact: true }).disabled).toBe(false))
}
async function restore() {
  click('Restore')
  click('Confirm Restore')
  await screen.findByText('Catalog snapshot restored.')
}

beforeEach(() => { localStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('tool management transport', () => {
  it('W22D1-01 authority wire', async () => {
    const fetch = vi.fn().mockResolvedValue(response(authority))
    vi.stubGlobal('fetch', fetch)
    expect(await api.getToolRemovalAuthority(false, tool.name)).toEqual(authority)
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/api/author/removals/authority')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ tool_name: tool.name })
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(opts.headers['X-Tenant-Id']).toBeTruthy()
  })
  it('W22D1-02 stage wire', async () => {
    const fetch = vi.fn().mockResolvedValue(response(staged))
    vi.stubGlobal('fetch', fetch)
    expect(await api.stageToolRemoval(false, { toolName: tool.name, catalogDigest: digest, idempotencyKey: 'remove-key' })).toEqual(staged)
    expect(fetch.mock.calls[0][0]).toBe('/api/author/removals')
    expect(fetch.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ tool_name: tool.name, expected_catalog_digest: digest, idempotency_key: 'remove-key' })
  })
  it('W22D1-03 restore wire', async () => {
    const fetch = vi.fn().mockResolvedValue(response(restored))
    vi.stubGlobal('fetch', fetch)
    expect(await api.restoreToolCatalog(false, { changeSetId: predecessorId, idempotencyKey: 'restore-key' })).toEqual(restored)
    expect(fetch.mock.calls[0][0]).toBe('/api/author/rollback')
    expect(fetch.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ change_set_id: predecessorId, idempotency_key: 'restore-key' })
  })
  it('W22D1-04 mock refusal', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    for (const promise of [api.getToolRemovalAuthority(true, tool.name), api.stageToolRemoval(true, {}), api.restoreToolCatalog(true, {})]) {
      await expect(promise).rejects.toThrow('Tool management is unavailable in demo mode.')
    }
    expect(fetch).not.toHaveBeenCalled()
  })
  it('W22D1-05 HTTP refusal', async () => {
    const body = { error: { code: 'tenant_role_denied' } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(body, 403)))
    await expect(api.getToolRemovalAuthority(false, tool.name)).rejects.toMatchObject({ status: 403, body })
  })
  it('W22D1-06 mismatched authority', async () => {
    const fetch = vi.fn().mockResolvedValue(response({ ...authority, tool_name: 'other-tool' }))
    vi.stubGlobal('fetch', fetch)
    await expect(api.getToolRemovalAuthority(false, tool.name)).rejects.toThrow('authority')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('W22D1-07 unauthorized row', async () => {
    const fetch = vi.fn().mockResolvedValue(response({ ...authority, target_row_count: 0 }))
    vi.stubGlobal('fetch', fetch)
    await expect(api.getToolRemovalAuthority(false, tool.name)).rejects.toThrow('authority')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('W22D1-08 invalid staged receipt', async () => {
    const fetch = vi.fn().mockResolvedValue(response({ ...staged, receipt: { ...staged.receipt, state: 'published' } }))
    vi.stubGlobal('fetch', fetch)
    await expect(api.stageToolRemoval(false, { toolName: tool.name, catalogDigest: digest, idempotencyKey: 'key' })).rejects.toThrow('receipt')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('W22D1-09 invalid rollback receipt', async () => {
    for (const field of ['catalog_commit', 'catalog_digest']) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...restored, [field]: '' })))
      await expect(api.restoreToolCatalog(false, { changeSetId: predecessorId, idempotencyKey: 'key' })).rejects.toThrow('receipt')
    }
  })
})

describe('tool management controls', () => {
  it('W22D1-10 confirmation cancellation', () => {
    const calls = spies()
    mount()
    click('Remove'); click('Cancel')
    for (const spy of Object.values(calls)) expect(spy).not.toHaveBeenCalled()
  })
  it('W22D1-11 remove ordering', async () => {
    const calls = spies()
    const callback = vi.fn()
    mount(callback)
    await remove()
    expect(calls.authority).toHaveBeenCalledWith(false, tool.name)
    expect(calls.stage).toHaveBeenCalledWith(false, { toolName: tool.name, catalogDigest: digest, idempotencyKey: expect.any(String) })
    expect(calls.publish).toHaveBeenCalledWith(false, staged)
    expect(calls.authority.mock.invocationCallOrder[0]).toBeLessThan(calls.stage.mock.invocationCallOrder[0])
    expect(calls.stage.mock.invocationCallOrder[0]).toBeLessThan(calls.publish.mock.invocationCallOrder[0])
    expect(calls.publish.mock.invocationCallOrder[0]).toBeLessThan(callback.mock.invocationCallOrder[0])
    expect(callback).toHaveBeenCalledTimes(1)
  })
  it('W22D1-12 approval continuation', async () => {
    const calls = spies()
    calls.publish.mockResolvedValueOnce({ publication_status: 'awaiting_approval' })
    const callback = vi.fn()
    mount(callback)
    click('Remove'); click('Confirm Remove')
    await screen.findByText('Removal is awaiting approval. The catalog has not changed.')
    expect(callback).not.toHaveBeenCalled()
    click('Check approval')
    await waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
    expect(calls.stage).toHaveBeenCalledTimes(1)
    expect(calls.publish).toHaveBeenNthCalledWith(2, false, staged)
  })
  it('W22D1-13 denied publication', async () => {
    const calls = spies()
    calls.publish.mockResolvedValue({ publication_status: 'denied' })
    const callback = vi.fn()
    mount(callback)
    click('Remove'); click('Confirm Remove')
    await screen.findByText('Removal was denied. The catalog has not changed.')
    expect(callback).not.toHaveBeenCalled()
  })
  it('W22D1-14 authority failure', async () => {
    const calls = spies()
    calls.authority.mockRejectedValue(new Error('private response'))
    const callback = vi.fn()
    mount(callback)
    click('Remove'); click('Confirm Remove')
    await screen.findByRole('alert')
    expect(screen.queryByText('private response')).toBeNull()
    expect(calls.stage).not.toHaveBeenCalled()
    expect(calls.publish).not.toHaveBeenCalled()
    expect(callback).not.toHaveBeenCalled()
  })
  it('W22D1-15 ambiguous stage failure retries with the same key', async () => {
    const calls = spies()
    calls.stage.mockRejectedValue(new Error('failed'))
    const callback = vi.fn()
    mount(callback)
    click('Remove'); click('Confirm Remove')
    await screen.findByRole('alert')
    expect(calls.publish).not.toHaveBeenCalled()
    expect(callback).not.toHaveBeenCalled()
    click('Retry removal')
    await waitFor(() => expect(calls.stage).toHaveBeenCalledTimes(2))
    expect(calls.stage.mock.calls[0][1]).toEqual(calls.stage.mock.calls[1][1])
  })
  it('W22D1-16 publication retry', async () => {
    const calls = spies()
    calls.publish.mockRejectedValueOnce(new Error('failed'))
    const callback = vi.fn()
    mount(callback)
    click('Remove'); click('Confirm Remove')
    await screen.findByRole('alert')
    click('Retry removal')
    await waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
    expect(calls.stage).toHaveBeenCalledTimes(1)
    expect(calls.publish).toHaveBeenNthCalledWith(2, false, staged)
  })
  it('W22D1-17 duplicate suppression', async () => {
    const calls = spies()
    const wait = deferred()
    calls.authority.mockReturnValue(wait.promise)
    mount()
    click('Remove')
    const confirm = screen.getByRole('button', { name: 'Confirm Remove' })
    fireEvent.click(confirm); fireEvent.click(confirm)
    expect(calls.authority).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Updating the catalog. Please wait.').getAttribute('role')).toBe('status')
    await act(async () => { wait.resolve(authority) })
    await waitFor(() => expect(calls.publish).toHaveBeenCalledTimes(1))
  })
  it('W22D1-18 restore confirmation', async () => {
    const calls = spies()
    mount()
    await remove()
    click('Restore')
    expect(screen.getByText('Restore the catalog snapshot from before this removal. This can also change other tools.')).toBeTruthy()
    expect(calls.restore).not.toHaveBeenCalled()
    click('Cancel')
    expect(calls.restore).not.toHaveBeenCalled()
  })
  it('W22D1-19 restore success', async () => {
    const calls = spies()
    const callback = vi.fn()
    mount(callback)
    await remove()
    await restore()
    expect(calls.restore).toHaveBeenCalledWith(false, { changeSetId: predecessorId, idempotencyKey: expect.any(String) })
    expect(calls.restore.mock.calls[0][1].idempotencyKey).not.toBe(calls.stage.mock.calls[0][1].idempotencyKey)
    expect(callback).toHaveBeenCalledTimes(2)
  })
  it('W22D1-20 restore retry', async () => {
    const calls = spies()
    calls.restore.mockRejectedValueOnce(new Error('failed'))
    const callback = vi.fn()
    mount(callback)
    await remove()
    click('Restore'); click('Confirm Restore')
    await screen.findByRole('alert')
    expect(callback).toHaveBeenCalledTimes(1)
    click('Retry restore')
    await screen.findByText('Catalog snapshot restored.')
    expect(calls.restore.mock.calls[0][1]).toEqual(calls.restore.mock.calls[1][1])
    expect(callback).toHaveBeenCalledTimes(2)
  })
  it('W22D1-21 refresh failure', async () => {
    const calls = spies()
    const callback = vi.fn().mockRejectedValueOnce(new Error('failed')).mockResolvedValue(undefined)
    mount(callback)
    await remove()
    expect(screen.getByText('Catalog changed, but the list could not refresh.')).toBeTruthy()
    click('Retry refresh')
    await waitFor(() => expect(screen.queryByText('Catalog changed, but the list could not refresh.')).toBeNull())
    expect(callback).toHaveBeenCalledTimes(2)
    expect(calls.stage).toHaveBeenCalledTimes(1)
    expect(calls.publish).toHaveBeenCalledTimes(1)
    expect(calls.restore).not.toHaveBeenCalled()
  })
  it('W22D1-22 absent callback', async () => {
    spies()
    mount()
    await remove()
    expect(screen.getByText('Catalog changed. Refresh the catalog to update this list.')).toBeTruthy()
    await restore()
    expect(screen.getByText('Catalog changed. Refresh the catalog to update this list.')).toBeTruthy()
  })
  it('W22D1-23 last family removed', async () => {
    const calls = spies()
    let view
    const props = { catalog: { source: 'endpoint', families: [families[0]] }, openFamilies: { 'custom-authored': true }, onReviseTool: vi.fn(), tools: [] }
    const callback = vi.fn(() => view.rerender(<CapabilityCatalog {...props} catalog={{ source: 'endpoint', families: [] }} onCatalogChanged={callback} />))
    view = render(<CapabilityCatalog {...props} onCatalogChanged={callback} />)
    fireEvent.click(screen.getByText(tool.name))
    await remove()
    expect(screen.queryByText(tool.name)).toBeNull()
    await restore()
    expect(calls.restore).toHaveBeenCalledTimes(1)
  })
  it('W22D1-24 NavRail integration', () => {
    render(<NavRail activeSurface="toolcast" railFamilies={families} catalogFamilyCount={2} catalogSource="endpoint"
      openFamilies={{ 'custom-authored': true, 'built-in': true }} onReviseTool={vi.fn()} />)
    fireEvent.click(screen.getByText(tool.name))
    fireEvent.click(screen.getByText('built-in-tool'))
    expect(screen.getAllByRole('button', { name: 'Remove', exact: true })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Revise', exact: true })).toHaveLength(1)
  })
  it('W22D1-25 CapabilityCatalog integration', () => {
    render(<CapabilityCatalog catalog={{ source: 'endpoint', families }} openFamilies={{ 'custom-authored': true, 'built-in': true }} onReviseTool={vi.fn()} />)
    fireEvent.click(screen.getByText(tool.name))
    fireEvent.click(screen.getByText('built-in-tool'))
    expect(screen.getAllByRole('button', { name: 'Remove', exact: true })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Revise', exact: true })).toHaveLength(1)
  })
  it('W22D1-26 non-live catalog', () => {
    const calls = spies()
    for (const source of ['mock', 'unknown']) {
      const view = render(<CapabilityCatalog catalog={{ source, families: [families[0]] }} openFamilies={{ 'custom-authored': true }} onReviseTool={vi.fn()} />)
      fireEvent.click(screen.getByText(tool.name))
      expect(screen.getByRole('button', { name: 'Remove', exact: true }).disabled).toBe(true)
      expect(screen.getByText('Tool management requires a live catalog.')).toBeTruthy()
      click('Remove')
      view.unmount()
    }
    expect(calls.authority).not.toHaveBeenCalled()
  })
  it('W22D1-27 sibling-card serialization', async () => {
    const calls = spies()
    const wait = deferred()
    calls.authority.mockReturnValue(wait.promise)
    mount(undefined, true, <><ToolHistory tool={tool} /><ToolHistory tool={{ ...tool, name: 'other-tool' }} /></>)
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove', exact: true })[0])
    click('Confirm Remove')
    const sibling = screen.getAllByRole('button', { name: 'Remove', exact: true })[1]
    expect(sibling.disabled).toBe(true)
    fireEvent.click(sibling)
    expect(screen.getAllByText('Another catalog action is in progress.')).toHaveLength(2)
    expect(calls.authority).toHaveBeenCalledTimes(1)
    await act(async () => { wait.resolve(authority) })
  })
  it('W22D1-28 unmount cleanup', async () => {
    const calls = spies()
    const wait = deferred()
    calls.stage.mockReturnValue(wait.promise)
    const callback = vi.fn()
    const view = mount(callback)
    click('Remove'); click('Confirm Remove')
    await waitFor(() => expect(calls.stage).toHaveBeenCalledTimes(1))
    view.unmount()
    await act(async () => { wait.resolve(staged) })
    expect(calls.publish).not.toHaveBeenCalled()
    expect(callback).not.toHaveBeenCalled()
  })
  it('W22D1-29 conflict fresh retry', async () => {
    await stageRetry(failure(409, 'effective_catalog_digest_mismatch'), true)
  })
  it('W22D1-30 dismiss unlocks management', async () => {
    const calls = spies()
    mount(undefined, true, <><ToolHistory tool={tool} /><ToolHistory tool={{ ...tool, name: 'other-tool' }} /></>)
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove', exact: true })[0])
    click('Confirm Remove')
    await screen.findByRole('button', { name: 'Restore', exact: true })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore' }).disabled).toBe(false))
    calls.stage.mockRejectedValueOnce(failure(409, 'effective_catalog_digest_mismatch'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove', exact: true })[0])
    click('Confirm Remove')
    await screen.findByText(refusalSentence)
    const counts = Object.values(calls).map((spy) => spy.mock.calls.length)
    click('Dismiss')
    expect(Object.values(calls).map((spy) => spy.mock.calls.length)).toEqual(counts)
    expect(screen.queryByText(refusalSentence)).toBeNull()
    for (const button of screen.getAllByRole('button', { name: 'Remove', exact: true })) expect(button.disabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Restore' }).disabled).toBe(false)
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove', exact: true })[1])
    expect(screen.getByRole('button', { name: 'Confirm Remove' })).toBeTruthy()
  })
  it('W22D1-31 network retry identity', async () => {
    await stageRetry(new TypeError('network failure'), false)
  })
  it('W22D1-32 ambiguous response matrix', async () => {
    for (const error of [new Error('timeout'), failure(408), failure(500), failure(503),
      failure(409, undefined, null), new Error('Invalid tool removal receipt.')]) {
      await stageRetry(error, false)
    }
  })
  it('W22D1-33 authority refusal recovery', async () => {
    const calls = spies()
    const keys = vi.spyOn(globalThis.crypto, 'randomUUID')
    calls.authority.mockRejectedValueOnce(failure(409, 'catalog_unavailable'))
    const view = mount()
    startRemoval()
    await screen.findByText(refusalSentence)
    const firstKey = keys.mock.results[0].value
    expect(calls.stage).not.toHaveBeenCalled()
    click('Retry removal')
    await screen.findByRole('button', { name: 'Restore' })
    expect(calls.authority).toHaveBeenCalledTimes(2)
    expect(calls.stage.mock.calls[0][1].idempotencyKey).not.toBe(firstKey)
    view.unmount()
    mount(undefined, true, <><ToolHistory tool={tool} /><ToolHistory tool={{ ...tool, name: 'other-tool' }} /></>)
    calls.authority.mockRejectedValueOnce(failure(403, 'tenant_role_denied'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove', exact: true })[0])
    click('Confirm Remove')
    await screen.findByText(refusalSentence)
    const count = calls.authority.mock.calls.length
    click('Dismiss')
    expect(calls.authority).toHaveBeenCalledTimes(count)
    expect(screen.getAllByRole('button', { name: 'Remove', exact: true })[1].disabled).toBe(false)
  })
  it('W22D1-34 definite stage refusals', async () => {
    for (const error of [failure(403, 'tenant_role_denied'), failure(403, 'tenant_identity_invalid'),
      failure(401, undefined, { detail: 'Not authenticated' }), failure(403, undefined, { detail: 'Not authenticated' }),
      failure(403, undefined, { detail: 'token verified but missing tenant claim' + ' required for this workspace' }),
      failure(404, 'customization_publish_disabled'), failure(422, 'invalid_removal_request'),
      failure(422, undefined, { detail: [] })]) {
      await stageRetry(error, true)
      const calls = spies()
      calls.stage.mockRejectedValueOnce(error)
      const view = mount()
      startRemoval()
      await screen.findByText(refusalSentence)
      click('Dismiss')
      expect(calls.stage).toHaveBeenCalledTimes(1)
      expect(calls.authority).toHaveBeenCalledTimes(1)
      expect(calls.publish).not.toHaveBeenCalled()
      view.unmount()
      vi.restoreAllMocks()
    }
  })
  it('W22D1-35 late stage refusal retained', async () => {
    for (const error of [failure(403, 'frozen_path_changed'), failure(422, 'invalid_staged_paths'), failure(409, 'stage_not_available')]) {
      await stageRetry(error, false)
    }
  })
  it('W22D1-36 publication refusal retained', async () => {
    const calls = spies()
    calls.publish.mockRejectedValueOnce(failure(403, 'tenant_role_denied'))
    const callback = vi.fn()
    mount(callback)
    startRemoval()
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
    click('Retry removal')
    await waitFor(() => expect(callback).toHaveBeenCalledTimes(1))
    expect(calls.authority).toHaveBeenCalledTimes(1)
    expect(calls.stage).toHaveBeenCalledTimes(1)
    expect(calls.publish.mock.calls[1][1]).toBe(calls.publish.mock.calls[0][1])
  })
  it('W22D1-37 rollback refusal retained', async () => {
    const calls = spies()
    const callback = vi.fn()
    calls.restore.mockRejectedValueOnce(failure(409, 'catalog_conflict'))
    mount(callback)
    await remove()
    click('Restore'); click('Confirm Restore')
    await screen.findByRole('alert')
    expect(callback).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
    click('Retry restore')
    await waitFor(() => expect(callback).toHaveBeenCalledTimes(2))
    expect(calls.restore.mock.calls[1][1]).toEqual(calls.restore.mock.calls[0][1])
    expect(calls.restore.mock.calls[1][1].changeSetId).toBe(predecessorId)
  })
  it('W22D1-38 collapse hides confirmation', async () => {
    const calls = spies()
    const view = render(rail())
    fireEvent.click(screen.getByText(tool.name))
    await remove()
    click('Remove')
    const count = calls.authority.mock.calls.length
    view.rerender(rail({ navSpine: true }))
    for (const name of ['Confirm Remove', 'Restore', 'Retry removal', 'Retry restore', 'Retry refresh', 'Dismiss']) {
      expect(screen.queryByRole('button', { name, exact: true })).toBeNull()
      expect(screen.queryByText(name, { exact: true })).toBeNull()
    }
    expect(screen.queryByText('Remove this authored tool from the catalog after publication?')).toBeNull()
    view.rerender(rail())
    expect(screen.getByRole('button', { name: 'Confirm Remove' })).toBeTruthy()
    expect(calls.authority).toHaveBeenCalledTimes(count)
  })
  it('W22D1-39 collapse keeps history', async () => {
    const calls = spies()
    const view = render(rail())
    fireEvent.click(screen.getByText(tool.name))
    await remove()
    view.rerender(rail({ navSpine: true }))
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull()
    view.rerender(rail())
    expect(screen.getByRole('button', { name: 'Restore' }).closest('aside.nav')).toBeTruthy()
    expect(calls.stage).toHaveBeenCalledTimes(1)
    expect(calls.publish).toHaveBeenCalledTimes(1)
    expect(calls.restore).not.toHaveBeenCalled()
  })
  it('W22D1-40 rail family lifetime', async () => {
    const calls = spies()
    let view
    const callback = vi.fn(() => view.rerender(rail({ railFamilies: [], catalogFamilyCount: 0, onCatalogChanged: callback })))
    view = render(rail({ onCatalogChanged: callback }))
    fireEvent.click(screen.getByText(tool.name))
    await remove()
    expect(screen.queryByText(tool.name)).toBeNull()
    expect(screen.getByRole('button', { name: 'Restore' }).closest('aside.nav')).toBeTruthy()
    await restore()
    expect(calls.restore).toHaveBeenCalledWith(false, { changeSetId: predecessorId, idempotencyKey: expect.any(String) })
  })
  it('W22D1-41 rail adds no siblings', async () => {
    const calls = spies()
    calls.stage.mockRejectedValueOnce(failure(409, 'effective_catalog_digest_mismatch'))
    const tree = (navSpine = false) => <div data-testid="grid">{rail({ navSpine })}</div>
    const view = render(tree())
    const check = () => {
      const grid = screen.getByTestId('grid')
      expect(grid.children).toHaveLength(1)
      expect(grid.firstElementChild.matches('aside.nav')).toBe(true)
      for (const element of screen.queryAllByRole('button').concat(screen.queryAllByRole('alert'), screen.queryAllByRole('status'))) {
        expect(grid.firstElementChild.contains(element)).toBe(true)
      }
    }
    check()
    fireEvent.click(screen.getByText(tool.name))
    click('Remove'); check()
    click('Confirm Remove')
    await screen.findByText(refusalSentence)
    check()
    click('Retry removal')
    await screen.findByRole('button', { name: 'Restore' })
    check()
    view.rerender(tree(true)); check()
  })
  it('W22D1-42 uncertainty survives a later conflict', async () => {
    const calls = spies()
    calls.stage.mockRejectedValueOnce(new TypeError('network failure'))
      .mockRejectedValueOnce(failure(409, 'effective_catalog_digest_mismatch'))
    mount()
    startRemoval()
    await screen.findByRole('alert')
    click('Retry removal')
    await waitFor(() => expect(calls.stage).toHaveBeenCalledTimes(2))
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
    expect(screen.queryByText(refusalSentence)).toBeNull()
    click('Retry removal')
    await screen.findByRole('button', { name: 'Restore' })
    expect(calls.authority).toHaveBeenCalledTimes(1)
    expect(calls.stage.mock.calls[1][1]).toEqual(calls.stage.mock.calls[0][1])
    expect(calls.stage.mock.calls[2][1]).toEqual(calls.stage.mock.calls[0][1])
  })
  it('W22D1-43 default host DOM preserved', async () => {
    spies()
    const view = render(<CapabilityCatalog catalog={{ source: 'endpoint', families: [families[0]] }}
      openFamilies={{ 'custom-authored': true }} onReviseTool={() => {}} />)
    const catalog = view.container.firstElementChild
    expect(catalog.matches('.capability-catalog')).toBe(true)
    fireEvent.click(screen.getByText(tool.name))
    click('Remove')
    expect(view.container.children).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Confirm Remove' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Confirm Remove' }).parentElement).toBe(catalog.nextElementSibling)
    click('Confirm Remove')
    await screen.findByRole('button', { name: 'Restore' })
    expect(view.container.firstElementChild).toBe(catalog)
    expect(screen.getAllByLabelText('Tool removal history')).toHaveLength(1)
    expect(screen.getByLabelText('Tool removal history')).toBe(catalog.nextElementSibling)
  })
  it('W22D1-44 validation rows isolate', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(authority))
      .mockResolvedValueOnce(response(staged)).mockResolvedValueOnce(response(restored))
    vi.stubGlobal('fetch', fetch)
    expect(await api.getToolRemovalAuthority(false, tool.name)).toEqual(authority)
    expect(await api.stageToolRemoval(false, { toolName: tool.name, catalogDigest: digest, idempotencyKey: 'key' })).toEqual(staged)
    expect(await api.restoreToolCatalog(false, { changeSetId: predecessorId, idempotencyKey: 'key' })).toEqual(restored)
    expect(fetch).toHaveBeenCalledTimes(3)
  })
})
