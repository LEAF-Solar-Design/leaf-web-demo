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
  state = { status: 'unavailable', ready: null, selectedObjectId: null }
  bridge = {
    subscribe: vi.fn((fn) => { listener = fn; fn(state); return vi.fn() }),
    start: vi.fn(), stop: vi.fn(), bindDrawing: vi.fn().mockResolvedValue(undefined),
    focusObject: vi.fn().mockResolvedValue(undefined),
  }
  getLeafHostBridge.mockReturnValue(bridge)
  getStoredOrgId.mockReturnValue(orgId)
  isSignedIn.mockReturnValue(true)
  login.mockResolvedValue(undefined)
  listProjects.mockResolvedValue([{ project_id: projectId, name: 'Roof project' }])
  openProject.mockResolvedValue({ drawing_versions: [version] })
})
afterEach(cleanup)

function mountUnbound() {
  state = { status: 'unbound', ready: { sessionKey }, selectedObjectId: null, bindingResult: null }
  return render(<LeafPlatformScene />)
}

describe('AutoCAD palette scene in Studio', () => {
  it('explains how to open the palette when WebView is unavailable', () => {
    render(<LeafPlatformScene />)
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
    setBridgeState({ ...state, bindingResult: 'DWG connection was not changed (cancelled).' })
    expect(screen.getByText('DWG connection was not changed (cancelled).')).toBeTruthy()
    expect(connect.disabled).toBe(false)
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

  it('offers no actions for a DWG in another workspace', () => {
    state = { status: 'connected', ready: { ...ready, platformTenantId: projectId }, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    expect(screen.getByText(/belongs to another workspace/)).toBeTruthy()
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
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it.each([null, '', 'invalid'])('asks for a workspace for a connected DWG with invalid stored org %s', (storedOrgId) => {
    getStoredOrgId.mockReturnValue(storedOrgId)
    state = { status: 'connected', ready, selectedObjectId: 'panel:A1' }
    render(<LeafPlatformScene />)
    expect(screen.getByText('Choose a workspace in Studio, then reopen this palette.')).toBeTruthy()
    expect(screen.queryByText(/belongs to another workspace/)).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(listProjects).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('loads the bound project name for a connected DWG in the current workspace', async () => {
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    expect(await screen.findByText('Connected to Roof project.')).toBeTruthy()
    expect(listProjects).toHaveBeenCalledWith(orgId)
    expect(openProject).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(sessionKey)
  })

  it('keeps the short drawing id when the bound project is not found', async () => {
    listProjects.mockResolvedValue([{ project_id: orgId, name: 'Another project' }])
    state = { status: 'connected', ready, selectedObjectId: null }
    render(<LeafPlatformScene />)
    await act(async () => {})
    expect(listProjects).toHaveBeenCalledWith(orgId)
    expect(screen.getByText(`Connected to ${drawingId.slice(0, 8)}.`)).toBeTruthy()
    expect(screen.queryByText('Connected to Another project.')).toBeNull()
  })

  it('explains a missing workspace without requesting projects', () => {
    getStoredOrgId.mockReturnValue(null)
    mountUnbound()
    expect(screen.getByText(/Choose a workspace in Studio/)).toBeTruthy()
    expect(listProjects).not.toHaveBeenCalled()
  })

  it.each([[[]], [null]])('handles an empty or missing project list', async (projects) => {
    listProjects.mockResolvedValue(projects)
    mountUnbound()
    expect(await screen.findByText('This workspace has no projects yet.')).toBeTruthy()
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
})
