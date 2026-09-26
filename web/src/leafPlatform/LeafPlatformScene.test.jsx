// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getStoredOrgId, listProjects, openProject } from '../api.js'
import { isSignedIn, login } from '../auth.js'
import { getLeafHostBridge } from './hostBridge.js'
import LeafPlatformScene from './LeafPlatformScene.jsx'

vi.mock('../api.js', () => ({ getStoredOrgId: vi.fn(), listProjects: vi.fn(), openProject: vi.fn() }))
vi.mock('../auth.js', () => ({ isSignedIn: vi.fn(), login: vi.fn() }))
vi.mock('./hostBridge.js', () => ({ getLeafHostBridge: vi.fn() }))

const orgId = '11111111-1111-4111-8111-111111111111'
const projectId = '22222222-2222-4222-8222-222222222222'
const drawingId = '33333333-3333-4333-8333-333333333333'
const versionId = '44444444-4444-4444-8444-444444444444'
const sessionKey = 'secret-session-key-never-render-this'
const ready = { platformTenantId: orgId, projectId, drawingId, drawingVersionId: versionId, sessionKey }
const version = { org_id: orgId, project_id: projectId, drawing_id: drawingId, version_id: versionId, seq: 3 }

let bridge, listener, state
function setBridgeState(next) {
  act(() => { state = next; listener(next) })
}

beforeEach(() => {
  vi.resetAllMocks()
  state = { status: 'unavailable', ready: null, selectedObjectId: null, selectedHandles: null, lastCommand: null, helloSentAt: null }
  bridge = {
    subscribe: vi.fn((fn) => { listener = fn; fn(state); return vi.fn() }),
    start: vi.fn(), stop: vi.fn(), bindDrawing: vi.fn().mockResolvedValue(undefined),
    focusObject: vi.fn().mockResolvedValue(undefined),
    retryHello: vi.fn(() => { state = { ...state, helloSentAt: Date.now() }; listener(state) }),
  }
  getLeafHostBridge.mockReturnValue(bridge)
  getStoredOrgId.mockReturnValue(orgId)
  isSignedIn.mockReturnValue(true)
  login.mockResolvedValue(undefined)
  listProjects.mockResolvedValue([{ project_id: projectId, name: 'Roof project' }])
  openProject.mockResolvedValue({ drawing_versions: [version] })
})
afterEach(() => { cleanup(); vi.useRealTimers() })

function mountUnbound() {
  state = { status: 'unbound', ready: { sessionKey }, selectedObjectId: null, bindingResult: null }
  return render(<LeafPlatformScene />)
}

function expectStudioRecovery() {
  expect(screen.getByRole('link', { name: 'Open Leaf Automation Studio' }).getAttribute('href')).toBe('/app')
  expect(screen.getByText(/Then run LEAFPLATFORM again in AutoCAD to come back\./)).toBeTruthy()
}

describe('AutoCAD palette scene in Studio', () => {
  it('explains how to open the palette when WebView is unavailable', () => {
    render(<LeafPlatformScene />)
    expect(screen.getByText('This page connects an AutoCAD drawing to a drawing version in your Leaf Automation Studio workspace.')).toBeTruthy()
    expect(screen.getByText(/LEAFPLATFORM command/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Back to Studio' }).getAttribute('href')).toBe('/app')
    expect(bridge.start).toHaveBeenCalledTimes(1)
  })

  it('shows a connecting status', () => {
    state = { status: 'connecting', ready: null, selectedObjectId: null }
    render(<LeafPlatformScene />)
    expect(screen.getByRole('status').textContent).toBe('Waiting for AutoCAD.')
  })

  it('offers sign in for an unbound DWG when signed out', async () => {
    isSignedIn.mockReturnValue(false)
    mountUnbound()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(login).toHaveBeenCalledTimes(1))
    expect(listProjects).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('waits for Connect drawing before sending one binding request with the chosen UUIDs', async () => {
    mountUnbound()
    const project = await screen.findByLabelText('Project')
    expect(listProjects).toHaveBeenCalledWith(orgId)
    fireEvent.change(project, { target: { value: projectId } })
    const versions = await screen.findByLabelText('Drawing version')
    expect(openProject).toHaveBeenCalledWith(projectId, orgId)
    fireEvent.change(versions, { target: { value: versionId } })
    await act(async () => {})
    expect(bridge.bindDrawing).not.toHaveBeenCalled()
    const connect = screen.getByRole('button', { name: 'Connect drawing' })
    expect(connect.disabled).toBe(false)
    fireEvent.click(connect)
    fireEvent.click(connect)
    await waitFor(() => expect(bridge.bindDrawing).toHaveBeenCalledTimes(1))
    expect(bridge.bindDrawing).toHaveBeenCalledWith({ platformTenantId: orgId, projectId, drawingId, drawingVersionId: versionId })
    setBridgeState({ ...state, bindingResult: 'Waiting for confirmation in AutoCAD.' })
    expect(screen.getByText('Waiting for confirmation in AutoCAD.')).toBeTruthy()
    expect(connect.disabled).toBe(true)
    screen.getByRole('heading', { level: 1 }).focus()
    setBridgeState({ ...state, bindingResult: 'DWG connection was not changed (cancelled).' })
    expect(screen.getByText('DWG connection was not changed (cancelled).')).toBeTruthy()
    expect(connect.disabled).toBe(false)
    expect(document.activeElement).toBe(connect)
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('offers only complete UUID versions in the selected workspace and project', async () => {
    openProject.mockResolvedValue({ drawing_versions: [
      version, { ...version, version_id: 'not-a-uuid' },
      { ...version, drawing_id: undefined },
      { ...version, org_id: projectId }, { ...version, project_id: orgId },
    ] })
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    const picker = await screen.findByLabelText('Drawing version')
    expect(picker.options.length).toBe(2)
    expect(picker.options[1].value).toBe(versionId)
  })

  it.each([
    ['Roof plan', '2026-09-25T12:34:56Z', 'Roof plan, 2026-09-25'],
    ['Roof plan', undefined, 'Roof plan'],
    [undefined, '2026-09-25T12:34:56Z', 'Drawing 33333333, 2026-09-25'],
    [undefined, undefined, 'Drawing 33333333'],
    [42, 'invalid-date', 'Drawing 33333333'],
    ['', null, 'Drawing 33333333'],
  ])('labels versions with drawing name %s and date %s', async (name, created_at, label) => {
    openProject.mockResolvedValue({
      drawing_versions: [{ ...version, created_at }],
      drawing_artifacts: [{ drawing_id: projectId, name: 'Other drawing' }, { drawing_id: drawingId, name }],
    })
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    const picker = await screen.findByLabelText('Drawing version')
    expect(picker.options[1].textContent).toBe(`Version 3: ${label}`)
    expect(picker.options[1].value).toBe(versionId)
  })

  it.each([
    ['Roof project', 'Roof plan', 'Roof project / Roof plan'],
    [null, null, 'Untitled project / Drawing 33333333'],
  ])('summarizes the chosen connection with project %s and drawing %s', async (name, drawing, summary) => {
    listProjects.mockResolvedValue([{ project_id: projectId, name }])
    openProject.mockResolvedValue({ drawing_versions: [version], drawing_artifacts: [{ drawing_id: drawingId, name: drawing }] })
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    const picker = await screen.findByLabelText('Drawing version')
    expect(screen.queryByText(/You cannot change this from the palette later/)).toBeNull()
    fireEvent.change(picker, { target: { value: versionId } })
    const text = screen.getByText(`Connect this DWG to ${summary} / Version 3. You cannot change this from the palette later.`)
    expect(text.nextElementSibling).toBe(screen.getByRole('button', { name: 'Connect drawing' }))
    fireEvent.change(picker, { target: { value: '' } })
    expect(screen.queryByText(/You cannot change this from the palette later/)).toBeNull()
  })

  it('loads the bound drawing identity once and does not move focus for selection or catalog updates', async () => {
    let finish
    openProject.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    const select = screen.getByRole('button', { name: 'Select' })
    select.focus()
    await screen.findByText('Connected to Roof project.', { selector: 'section p' })
    expect(document.activeElement).toBe(select)
    await act(async () => { finish({
      drawing_versions: [{ ...version, version_id: orgId, seq: 9 }, version],
      drawing_artifacts: [{ drawing_id: drawingId, name: 'Roof plan' }],
    }) })
    expect(screen.getByText('Roof plan, Version 3')).toBeTruthy()
    expect(document.activeElement).toBe(select)
    setBridgeState({ ...state, ready: { ...ready }, selectedObjectId: 'panel:A2' })
    await act(async () => {})
    expect(document.activeElement).toBe(select)
    expect(openProject).toHaveBeenCalledTimes(1)
    expect(openProject).toHaveBeenCalledWith(projectId, orgId)
  })

  it.each(['failure', 'missing'])('omits the bound version line quietly on %s', async (result) => {
    if (result === 'failure') openProject.mockRejectedValue(new Error('private catalog error'))
    else openProject.mockResolvedValue({ drawing_versions: [{ ...version, version_id: orgId }] })
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    await act(async () => {})
    expect(screen.queryByText(/, Version/)).toBeNull()
    expect(screen.getByRole('status').textContent).toBe('DWG connected.')
    expect(document.body.textContent).not.toContain('private catalog error')
  })

  it('focuses the heading only after a user-initiated bind becomes connected', async () => {
    mountUnbound()
    const region = screen.getByRole('status')
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    fireEvent.change(await screen.findByLabelText('Drawing version'), { target: { value: versionId } })
    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading.getAttribute('tabindex')).toBe('-1')
    fireEvent.click(screen.getByRole('button', { name: 'Connect drawing' }))
    await act(async () => {})
    setBridgeState({ ...state, bindingResult: 'DWG connected. Starting the signed cross-probe session.' })
    expect(document.activeElement).not.toBe(heading)
    setBridgeState({ status: 'connected', ready, selectedObjectId: null })
    expect(document.activeElement).toBe(heading)
    await act(async () => {})
    expect(document.activeElement).toBe(heading)
    expect(screen.getAllByText('Connected to Roof project.')).toHaveLength(1)
    expect(screen.getByRole('status')).toBe(region)
    expect(region.textContent).toBe('DWG connected.')
  })

  it('disables selection actions until an object is selected', async () => {
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(true)
    setBridgeState({ ...state, selectedObjectId: 'panel:A1' })
    expect(screen.getByText('panel:A1')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    await waitFor(() => expect(bridge.focusObject).toHaveBeenCalledWith('panel:A1', 'select'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom to' }))
    await waitFor(() => expect(bridge.focusObject).toHaveBeenCalledWith('panel:A1', 'focus'))
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it.each([
    [['2F4A'], 'Selected object: 2F4A'],
    [['2F4A', '2F4B', '2F4C'], '3 objects selected (2F4A, 2F4B, 2F4C)'],
    [['2F4A', '2F4B', '2F4C', '2F4D', '2F4E'], '5 objects selected (2F4A, 2F4B, 2F4C, and 2 more)'],
  ])('shows handle selection %j and sends handles for drawing actions', async (objectHandles, label) => {
    state = { status: 'connected', ready, selectedObjectId: null, selectedHandles: objectHandles }
    render(<LeafPlatformScene />)
    expect(screen.getByText((_, element) => element.tagName === 'P' && element.textContent === label)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Zoom to' }))
    await waitFor(() => expect(bridge.focusObject).toHaveBeenCalledWith({ objectHandles }, 'focus'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    await waitFor(() => expect(bridge.focusObject).toHaveBeenCalledWith({ objectHandles }, 'select'))
    setBridgeState({ ...state, selectedHandles: null })
    expect(screen.getByText('Select objects in your AutoCAD drawing to use these buttons.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(true)
  })

  it('offers no actions for a DWG in another workspace', () => {
    state = { status: 'connected', ready: { ...ready, platformTenantId: projectId }, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    expect(screen.getByText(/belongs to another workspace/)).toBeTruthy()
    expectStudioRecovery()
    expect(openProject).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it.each([null, orgId, projectId])('offers sign in for a connected DWG when signed out with stored org %s', async (storedOrgId) => {
    isSignedIn.mockReturnValue(false)
    getStoredOrgId.mockReturnValue(storedOrgId)
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    expect(screen.getByText('This DWG is connected. Sign in to use this connection.')).toBeTruthy()
    expect(screen.queryByText(/belongs to another workspace/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Select' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Zoom to' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(login).toHaveBeenCalledTimes(1))
    expect(listProjects).not.toHaveBeenCalled()
    expect(bridge.focusObject).not.toHaveBeenCalled()
    expect(openProject).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it.each([null, '', 'invalid'])('asks for a workspace for a connected DWG with invalid stored org %s', (storedOrgId) => {
    getStoredOrgId.mockReturnValue(storedOrgId)
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    expect(screen.getByText('Choose a workspace in Studio, then reopen this palette.')).toBeTruthy()
    expectStudioRecovery()
    expect(openProject).not.toHaveBeenCalled()
    expect(screen.queryByText(/belongs to another workspace/)).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(listProjects).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('loads the bound project name for a connected DWG in the current workspace', async () => {
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    expect(await screen.findByText('Connected to Roof project.', { selector: 'section p' })).toBeTruthy()
    expect(listProjects).toHaveBeenCalledWith(orgId)
    expect(openProject).toHaveBeenCalledTimes(1)
    expect(openProject).toHaveBeenCalledWith(projectId, orgId)
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('keeps the short drawing id when the bound project is not found', async () => {
    listProjects.mockResolvedValue([{ project_id: orgId, name: 'Another project' }])
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    await act(async () => {})
    expect(listProjects).toHaveBeenCalledWith(orgId)
    expect(screen.getByText(`Connected to ${drawingId.slice(0, 8)}.`, { selector: 'section p' })).toBeTruthy()
    expect(screen.queryByText('Connected to Another project.')).toBeNull()
  })

  it('explains a missing workspace without requesting projects', () => {
    getStoredOrgId.mockReturnValue(null)
    mountUnbound()
    expect(screen.getByText(/Choose a workspace in Studio/)).toBeTruthy()
    expectStudioRecovery()
    expect(listProjects).not.toHaveBeenCalled()
  })

  it.each([[[]], [null]])('handles an empty or missing project list', async (projects) => {
    listProjects.mockResolvedValue(projects)
    mountUnbound()
    expect(await screen.findByText('This workspace has no projects yet.')).toBeTruthy()
    expectStudioRecovery()
  })

  it('keeps API error details out of the palette', async () => {
    listProjects.mockRejectedValue(new Error('private raw stack detail'))
    mountUnbound()
    expect(await screen.findByText(/Projects could not be loaded/)).toBeTruthy()
    expect(document.body.textContent).not.toContain('private raw stack detail')
  })

  it.each([{}, { drawing_versions: [] }])('handles missing or empty versions', async (result) => {
    openProject.mockResolvedValue(result)
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    expect(await screen.findByText(/no drawing versions available/)).toBeTruthy()
    expectStudioRecovery()
  })

  it('handles version request failure without raw errors', async () => {
    openProject.mockRejectedValue(new Error('private response'))
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    expect(await screen.findByText(/Drawing versions could not be loaded/)).toBeTruthy()
    expect(document.body.textContent).not.toContain('private response')
  })

  it('retains the version and allows retry after a failed bind', async () => {
    bridge.bindDrawing.mockRejectedValue(new Error('private host error'))
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    fireEvent.change(await screen.findByLabelText('Drawing version'), { target: { value: versionId } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect drawing' }))
    expect(await screen.findByText(/drawing could not be connected/)).toBeTruthy()
    expect(screen.getByLabelText('Drawing version').value).toBe(versionId)
    expect(screen.getByRole('button', { name: 'Connect drawing' }).disabled).toBe(false)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Connect drawing' }))
    expect(document.body.textContent).not.toContain('private host error')
  })

  it('unsubscribes and stops when the scene leaves', () => {
    const unsubscribe = vi.fn()
    bridge.subscribe.mockImplementation((fn) => { fn(state); return unsubscribe })
    const { unmount } = render(<LeafPlatformScene />)
    unmount()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(bridge.stop).toHaveBeenCalledTimes(1)
  })

  it('keeps one polite status region mounted through every connection state', async () => {
    render(<LeafPlatformScene />)
    const region = screen.getByRole('status')
    expect(region.tagName).toBe('P')
    expect(region.className).toBe('leaf-platform-status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toBe('')
    for (const next of [
      { status: 'connecting', ready: null, helloSentAt: Date.now() },
      { status: 'unbound', ready: { sessionKey }, bindingResult: null },
      { status: 'connected', ready, selectedObjectId: null },
      { status: 'unavailable', ready: null },
    ]) {
      setBridgeState(next)
      await act(async () => {})
      expect(screen.getAllByRole('status')).toHaveLength(1)
      expect(screen.getByRole('status')).toBe(region)
    }
    expect(region.textContent).toBe('')
  })

  it('times out hello after ten seconds and restarts the timer on Try again', async () => {
    vi.useFakeTimers()
    state = { status: 'connecting', ready: null, helloSentAt: Date.now() }
    render(<LeafPlatformScene />)
    await act(async () => { vi.advanceTimersByTime(9999) })
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(screen.getByRole('status').textContent).toBe('AutoCAD has not answered yet. Check that AutoCAD is open, then try again.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(bridge.retryHello).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe('Waiting for AutoCAD.')
    await act(async () => { vi.advanceTimersByTime(9999) })
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    setBridgeState({ status: 'connected', ready, selectedObjectId: null })
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.getByRole('status').textContent).toBe('DWG connected.')
  })

  it('retries projects after a catalog error', async () => {
    listProjects.mockRejectedValueOnce(new Error('offline'))
    mountUnbound()
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    expect(await screen.findByLabelText('Project')).toBeTruthy()
    expect(listProjects).toHaveBeenCalledTimes(2)
    expect(listProjects).toHaveBeenLastCalledWith(orgId)
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('retries versions for the selected project without toggling the select', async () => {
    openProject.mockRejectedValueOnce(new Error('offline'))
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    expect(await screen.findByLabelText('Drawing version')).toBeTruthy()
    expect(screen.getByLabelText('Project').value).toBe(projectId)
    expect(openProject).toHaveBeenCalledTimes(2)
    expect(openProject).toHaveBeenLastCalledWith(projectId, orgId)
    expect(listProjects).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('times out a bind, retains the choice and resyncs once without repeating the bind', async () => {
    bridge.bindDrawing.mockImplementation(() => new Promise(() => {}))
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    fireEvent.change(await screen.findByLabelText('Drawing version'), { target: { value: versionId } })
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Connect drawing' }))
    await act(async () => { vi.advanceTimersByTime(59_999) })
    expect(screen.getByRole('button', { name: 'Connect drawing' }).disabled).toBe(true)
    expect(bridge.retryHello).not.toHaveBeenCalled()
    screen.getByRole('heading', { level: 1 }).focus()
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(screen.getByRole('status').textContent).toBe('AutoCAD did not answer. Look for a confirmation window in AutoCAD, then try again.')
    expect(screen.getByRole('button', { name: 'Connect drawing' }).disabled).toBe(false)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Connect drawing' }))
    expect(screen.getByLabelText('Project').disabled).toBe(false)
    expect(screen.getByLabelText('Drawing version').value).toBe(versionId)
    expect(bridge.retryHello).toHaveBeenCalledTimes(1)
    setBridgeState({ ...state, ready: { ...state.ready }, bindingResult: null })
    expect(screen.getByRole('status').textContent).toBe('AutoCAD did not answer. Look for a confirmation window in AutoCAD, then try again.')
    await act(async () => { vi.advanceTimersByTime(120_000) })
    expect(bridge.retryHello).toHaveBeenCalledTimes(1)
    expect(bridge.bindDrawing).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Connect drawing' }))
    expect(bridge.bindDrawing).toHaveBeenCalledTimes(2)
  })

  it('cancels the bind timeout when a result arrives', async () => {
    mountUnbound()
    fireEvent.change(await screen.findByLabelText('Project'), { target: { value: projectId } })
    fireEvent.change(await screen.findByLabelText('Drawing version'), { target: { value: versionId } })
    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Connect drawing' }))
    await act(async () => {})
    setBridgeState({ ...state, bindingResult: 'DWG connection was not changed (cancelled).' })
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(bridge.retryHello).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toBe('DWG connection was not changed (cancelled).')
  })

  it.each([
    ['applied', null, 'Zoomed to the selection in AutoCAD.'],
    ['stale', 'stale_document', 'This drawing changed in AutoCAD. Select the objects again.'],
    ['rejected', 'selection_apply_failed', 'AutoCAD could not find those objects. Select them again in AutoCAD.'],
    ['rejected', 'no_active_document', 'AutoCAD did not run that request (no active document).'],
    ['unknown', 'timeout', 'AutoCAD did not answer. Check AutoCAD, then try again.'],
    ['superseded', null, ''],
  ])('keeps Zoom pending until the %s outcome and announces the result', async (status, reason, message) => {
    let finish
    bridge.focusObject.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    await screen.findByText('Connected to Roof project.', { selector: 'section p' })
    fireEvent.click(screen.getByRole('button', { name: 'Zoom to' }))
    expect(screen.getByRole('button', { name: 'Zooming...' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Zooming...' }))
    expect(bridge.focusObject).toHaveBeenCalledTimes(1)
    await act(async () => { finish({ action: 'focus', status, reason }) })
    expect(screen.getByRole('status').textContent).toBe(message)
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Select' }).disabled).toBe(false)
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it.each([
    ['Roof project', 'Connected to Roof project.'],
    [null, `Connected to ${drawingId.slice(0, 8)}.`],
  ])('keeps the connection label visible after rejected Zoom with project name %s', async (name, connectionText) => {
    listProjects.mockResolvedValue([{ project_id: projectId, name }])
    bridge.focusObject.mockResolvedValue({ action: 'focus', status: 'rejected', reason: 'selection_apply_failed' })
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    await act(async () => {})
    const connection = screen.getByText(connectionText, { selector: 'section p' })
    const region = screen.getByRole('status')
    fireEvent.click(screen.getByRole('button', { name: 'Zoom to' }))
    await waitFor(() => expect(region.textContent).toBe('AutoCAD could not find those objects. Select them again in AutoCAD.'))
    expect(screen.getByText(connectionText)).toBe(connection)
    expect(connection.nextElementSibling.textContent).toBe('Drawing 33333333, Version 3')
    expect(connection.nextElementSibling.nextElementSibling.textContent).toBe('Selected object: panel:A1')
    expect(region).not.toBe(connection)
    for (const element of [connection, region]) {
      expect(element.hidden).toBe(false)
      expect(getComputedStyle(element).display).not.toBe('none')
      expect(getComputedStyle(element).visibility).toBe('visible')
    }
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('shows Selecting until the host applies selection', async () => {
    let finish
    bridge.focusObject.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    state = { status: 'connected', ready, selectedHandles: ['2F4A'] }
    render(<LeafPlatformScene />)
    await screen.findByText('Connected to Roof project.', { selector: 'section p' })
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    expect(screen.getByRole('button', { name: 'Selecting...' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(true)
    await act(async () => { finish({ action: 'select', status: 'applied', reason: null }) })
    expect(screen.getByRole('status').textContent).toBe('Selected in AutoCAD.')
  })

  it('reports action errors in the persistent region and releases the controls', async () => {
    bridge.focusObject.mockRejectedValue(new Error('private signing error'))
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    await screen.findByText('Connected to Roof project.', { selector: 'section p' })
    fireEvent.click(screen.getByRole('button', { name: 'Zoom to' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('The request could not be completed. Please try again.'))
    expect(screen.getByRole('button', { name: 'Zoom to' }).disabled).toBe(false)
    expect(document.body.textContent).not.toContain('private signing error')
  })
})
