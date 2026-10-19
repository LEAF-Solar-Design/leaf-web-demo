import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import useConverseSessionController from '../controllers/useConverseSessionController.js'
import { ensureSession, postMessage } from '../converse.js'
import { getSession, getTools, isMockConversation, publishStagedAuthor, runTool, stageAuthorTool } from '../api.js'
import { listMockCatalogTools, resetMockCatalog } from './mockCatalog.js'
import * as mockAuthority from './mockStageAuthority.js'

vi.mock('../converse.js', () => ({
  ensureSession: vi.fn(), postMessage: vi.fn(), resetSession: vi.fn(),
  classifyAgentError: () => 'unreachable', projectActivityProjection: (value) => value,
}))
vi.mock('../telemetry.js', () => ({ track: vi.fn(), trackErrorShown: vi.fn() }))

const hint = { source: 'author_panel', purpose: 'stage_authority' }
const description = 'build a tool that flags panels within 24 in of the roof edge'
const immediate = { requireImmediateTurn: true }
let mint

beforeEach(async () => {
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ intake: { polylines: [], layers: [] } }),
  })))
  ensureSession.mockResolvedValue({ session_id: 'live-session' })
  postMessage.mockResolvedValue({ turn_id: 'live-turn', status: 'started' })
  mint = vi.spyOn(mockAuthority, 'startMockStageAuthority')
  await getSession(false)
  fetch.mockClear()
  resetMockCatalog()
})

afterEach(async () => {
  cleanup()
  vi.useRealTimers()
  await getSession(false)
  vi.unstubAllGlobals()
  resetMockCatalog()
})

async function setMode(mock) {
  await getSession(mock)
  fetch.mockClear()
}

describe('mock stage authority boundary', () => {
  it('mints fresh demo ids for mock stage turns without attaching a conversation', async () => {
    await setMode(true)
    const { result } = renderHook(() => useConverseSessionController({ drawingId: 'rooftop_demo' }))
    let first, second
    await act(async () => {
      first = await result.current.startTurn(description, hint, immediate)
      second = await result.current.startTurn(description, hint, immediate)
    })
    for (const response of [first, second]) {
      expect(response).toEqual({
        session_id: expect.stringMatching(/^demo-session-.+/),
        turn_id: expect.stringMatching(/^demo-turn-\d+$/),
        status: 'started',
      })
    }
    expect(second.session_id).not.toBe(first.session_id)
    expect(second.turn_id).not.toBe(first.turn_id)
    expect(mint).toHaveBeenCalledTimes(2)
    expect(ensureSession).not.toHaveBeenCalled()
    expect(postMessage).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(result.current.sessionId).toBeNull()
    expect(result.current.turns).toEqual([])
  })

  it('leaves other mock turns on their existing transport and state path', async () => {
    await setMode(true)
    const { result } = renderHook(() => useConverseSessionController({ drawingId: 'rooftop_demo' }))
    const ordinaryHint = { source: 'command_bar', purpose: 'chat' }
    let response
    await act(async () => { response = await result.current.startTurn('hello', ordinaryHint) })
    expect(mint).not.toHaveBeenCalled()
    expect(ensureSession).toHaveBeenCalledWith('rooftop_demo', null)
    expect(postMessage).toHaveBeenCalledWith('live-session', {
      text: 'hello', classifier_hint: ordinaryHint, allowSecretOnce: false,
    })
    expect(response).toEqual({ session_id: 'live-session', turn_id: 'live-turn', status: 'started' })
    expect(result.current.sessionId).toBe('live-session')
    expect(result.current.turns).toEqual([
      { turnId: 'live-turn', requestId: null, status: 'started', text: 'hello' },
    ])
    expect(mockAuthority.startMockStageAuthority('hello', ordinaryHint)).toBeNull()
  })

  it('cannot reach the mint in live mode, even with the stage purpose', async () => {
    const { result } = renderHook(() => useConverseSessionController({ drawingId: 'drawing-a' }))
    act(() => result.current.setProjectContext('project-a'))
    let response
    await act(async () => {
      response = await result.current.startTurn(description, hint, { ...immediate, allowSecretOnce: true })
    })
    expect(mint).not.toHaveBeenCalled()
    expect(ensureSession).toHaveBeenCalledWith('drawing-a', 'project-a')
    expect(postMessage).toHaveBeenCalledWith('live-session', {
      text: description, classifier_hint: hint, allowSecretOnce: true,
      queue: false, request_id: expect.any(String),
    })
    expect(response).toMatchObject({ session_id: 'live-session', turn_id: 'live-turn' })
  })

  it('uses a live session after leaving mock, without reusing demo ids', async () => {
    await setMode(true)
    const { result } = renderHook(() => useConverseSessionController({ drawingId: 'rooftop_demo' }))
    await act(async () => { await result.current.startTurn(description, hint, immediate) })
    mint.mockClear()
    await setMode(false)
    let response
    await act(async () => { response = await result.current.startTurn(description, hint, immediate) })
    expect(mint).not.toHaveBeenCalled()
    expect(ensureSession).toHaveBeenCalledTimes(1)
    expect(postMessage).toHaveBeenCalledWith('live-session', expect.any(Object))
    expect(response.session_id).toBe('live-session')
  })

  it.each(['clear', 'project', 'drawing', 'unmount', 'live'])('invalidates a pending mock mint on %s', async (change) => {
    await setMode(true)
    const hook = renderHook(({ drawingId }) => useConverseSessionController({ drawingId }), {
      initialProps: { drawingId: 'drawing-a' },
    })
    let pending, modeChange
    act(() => {
      pending = hook.result.current.startTurn(description, hint, immediate).catch((error) => error)
      if (change === 'clear') hook.result.current.clear()
      if (change === 'project') hook.result.current.setProjectContext('project-b')
      if (change === 'drawing') hook.rerender({ drawingId: 'drawing-b' })
      if (change === 'unmount') hook.unmount()
      if (change === 'live') modeChange = getSession(false)
    })
    await act(async () => {
      await modeChange
      expect(await pending).toBeInstanceOf(Error)
    })
    expect(mint).not.toHaveBeenCalled()
    expect(ensureSession).not.toHaveBeenCalled()
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('does not restore mock mode when an older intake finishes after a live switch', async () => {
    let finishMock
    fetch.mockImplementationOnce(() => new Promise((resolve) => { finishMock = resolve }))
    const pending = getSession(true)
    expect(isMockConversation()).toBe(true)
    await getSession(false)
    finishMock({ ok: true, json: async () => ({ polylines: [] }) })
    await pending
    expect(isMockConversation()).toBe(false)
  })

  it('tracks catalog mode changes even when a workspace skips intake', async () => {
    vi.useFakeTimers()
    const loading = getTools(true)
    expect(isMockConversation()).toBe(true)
    await vi.runAllTimersAsync()
    await loading
    await getTools(false)
    expect(isMockConversation()).toBe(false)
    const { result } = renderHook(() => useConverseSessionController({ drawingId: null }))
    await act(async () => { await result.current.startTurn(description, hint, immediate) })
    expect(mint).not.toHaveBeenCalled()
    expect(postMessage).toHaveBeenCalledWith('live-session', expect.any(Object))
  })

  it.each([undefined, null, {}, { purpose: 'chat' }])('does not mint without the stage purpose (%j)', (classifierHint) => {
    expect(mockAuthority.startMockStageAuthority(description, classifierHint)).toBeNull()
  })

  it.each(['sessionId', 'turnId'])('rejects demo %s on live author staging before transport', async (field) => {
    const authority = { sessionId: 'live-session', turnId: 'live-turn', [field]: 'demo-reserved' }
    await expect(stageAuthorTool(false, description, null, { authority })).rejects.toMatchObject({
      message: 'Demo authority cannot authorize live tool building.', authorTerminal: true,
    })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('stages, publishes and runs the step 6 tool entirely within the demo', async () => {
    await setMode(true)
    const { result } = renderHook(() => useConverseSessionController({ drawingId: 'rooftop_demo' }))
    let response
    await act(async () => { response = await result.current.startTurn(description, hint, immediate) })
    const authority = { sessionId: response.session_id, turnId: response.turn_id }
    vi.useFakeTimers()
    const staging = stageAuthorTool(true, description, null, { authority })
    await vi.runAllTimersAsync()
    const staged = await staging
    expect(staged).toMatchObject({ demo: true, receipt: { state: 'staged' } })
    expect(staged.tool.name).toMatch(/\S+/)
    expect(listMockCatalogTools().some((tool) => tool.name === staged.tool.name)).toBe(false)
    const publishing = publishStagedAuthor(true, staged)
    await vi.runAllTimersAsync()
    const published = await publishing
    expect(published).toMatchObject({ published: true, demo_session_only: true })
    expect(listMockCatalogTools()).toContainEqual(staged.tool)
    const running = runTool(true, published.tool, { distance_in: 24 }, {
      layers: ['Panels'],
      polylines: [{ handle: 'panel-a', layer: 'Panels', pts: [[0, 0], [10, 0], [10, 10], [0, 10]] }],
    })
    await vi.runAllTimersAsync()
    await expect(running).resolves.toMatchObject({
      ok: true, tool: staged.tool.name, result: { distance_in: 24, panels_near_edge: 1 },
    })
    expect(fetch).not.toHaveBeenCalled()
    expect(ensureSession).not.toHaveBeenCalled()
    expect(postMessage).not.toHaveBeenCalled()
  })
})
