import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import { stageAuthorTool } from '../api.js'
import { readInflightAuthor } from '../authorStagePointer.js'
import useAuthorStageController from './useAuthorStageController.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const changeSetId = '6f0c2a91-8d3b-4e57-9a1c-2b7d4e8f0a13'
const pollUrl = `/api/author/stages/${changeSetId}`
const description = 'Count the panels on each roof plane.'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

function accepted() {
  return new Response(JSON.stringify({
    change_set_id: changeSetId, poll_url: pollUrl, status: 'accepted', retry_after_ms: 1,
  }), { status: 202, headers: { 'Content-Type': 'application/json' } })
}

// A status response whose body stream fails part way, as a read does when the
// page reloads or the connection drops after the headers arrived.
function interruptedBody(status, error) {
  const body = new ReadableStream({ start(controller) { controller.error(error) } })
  return new Response(body, { status, headers: { 'Content-Type': 'application/json' } })
}

function textBody(status, text) {
  return new Response(text, { status, headers: { 'Content-Type': 'text/html' } })
}

function jsonBody(status, value) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

async function stageWith(pollResponse) {
  const storage = memoryStorage()
  const responses = [accepted(), pollResponse]
  const fetch = vi.fn(async () => responses.shift())
  vi.stubGlobal('fetch', fetch)
  const hook = renderHook(() => useAuthorStageController({ storage, stageAuthorTool }))
  await act(async () => { await hook.result.current.stage(description) })
  return { storage, fetch, hook }
}

describe('a poll whose status body cannot be read', () => {
  it('F338-01 keeps the saved request when an HTTP 200 status body read is interrupted', async () => {
    const { storage, fetch, hook } = await stageWith(
      interruptedBody(200, new DOMException('The operation was aborted.', 'AbortError')))
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(hook.result.current.phase).toBe('interrupted')
    expect(hook.result.current.resumable).toBe(true)
    expect(hook.result.current.failedRequest).toBeNull()
    expect(hook.result.current.error?.authorTerminal).not.toBe(true)
    expect(readInflightAuthor(storage)).toMatchObject({ change_set_id: changeSetId, poll_url: pollUrl, description })
  })

  it('F338-02 keeps the saved request when an HTTP 200 status body is not JSON', async () => {
    const { storage, fetch, hook } = await stageWith(textBody(200, '<html>gateway</html>'))
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(hook.result.current.phase).toBe('interrupted')
    expect(hook.result.current.resumable).toBe(true)
    expect(hook.result.current.error?.authorTerminal).not.toBe(true)
    expect(readInflightAuthor(storage)).toMatchObject({ change_set_id: changeSetId, poll_url: pollUrl })
  })

  it('F338-03 still ends the request when a readable HTTP 200 status names another change set', async () => {
    const { storage, hook } = await stageWith(jsonBody(200, {
      change_set_id: '0b6e4d2a-1c3f-4a5b-8d7e-9f0a1b2c3d4e', status: 'running',
    }))
    expect(hook.result.current.phase).toBe('failed')
    expect(hook.result.current.error?.authorTerminal).toBe(true)
    expect(readInflightAuthor(storage)).toBeNull()
  })

  it('F338-04 keeps the status of a refused poll whose body is not JSON', async () => {
    const { storage, hook } = await stageWith(textBody(503, '<html>unavailable</html>'))
    expect(hook.result.current.error?.status).toBe(503)
    expect(hook.result.current.error?.name).not.toBe('SyntaxError')
    expect(hook.result.current.phase).toBe('interrupted')
    expect(readInflightAuthor(storage)).toMatchObject({ change_set_id: changeSetId, poll_url: pollUrl })
  })

  it('F338-05 keeps the status of a refused poll whose body read is interrupted', async () => {
    const { hook } = await stageWith(
      interruptedBody(503, new DOMException('The operation was aborted.', 'AbortError')))
    expect(hook.result.current.error?.status).toBe(503)
    expect(hook.result.current.phase).toBe('interrupted')
  })

  // A guard written for 200 alone would read a 202 whose body cannot be read as
  // a status that names no change set and end the request. Every success code
  // keeps the saved request, the same as 200 does.
  it('F338-06 keeps the saved request when another success status has an unreadable body', async () => {
    for (const response of [
      interruptedBody(202, new DOMException('The operation was aborted.', 'AbortError')),
      textBody(202, '<html>gateway</html>'),
    ]) {
      const { storage, fetch, hook } = await stageWith(response)
      expect(fetch).toHaveBeenCalledTimes(2)
      expect(hook.result.current.phase).toBe('interrupted')
      expect(hook.result.current.resumable).toBe(true)
      expect(hook.result.current.error?.authorTerminal).not.toBe(true)
      expect(readInflightAuthor(storage)).toMatchObject({ change_set_id: changeSetId, poll_url: pollUrl, description })
      hook.unmount()
    }
  })
})
