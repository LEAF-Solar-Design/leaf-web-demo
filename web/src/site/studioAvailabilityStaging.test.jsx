// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://platform-staging.leafdesign.ai/app" }
//
// The availability manifest as a visitor on STAGING meets it: this file's
// document is served from the staging host, so every row whose evidence speaks
// for staging is in force. Each test renders the real control and checks the
// three things the mandate asks for: it is visibly off, it says why in the
// manifest's own sentence, and it does nothing when pressed.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ navigate: vi.fn() }))
vi.mock('./router.js', () => ({ navigate: mocks.navigate, useRoute: () => ({ path: '/', hash: '' }) }))
vi.mock('./intakeCache.js', () => ({ loadDemoSolve: () => new Promise(() => {}) }))
vi.mock('../api.js', async (importOriginal) => ({ ...(await importOriginal()), submitDemandCapture: vi.fn() }))

import AuthorPanel from '../components/AuthorPanel.jsx'
import RoutePanel from '../components/RoutePanel.jsx'
import ToolsPanel from '../components/ToolsPanel.jsx'
import LocalCadConnect from '../localCad/LocalCadConnect.jsx'
import LandingCast from './LandingCast.jsx'
import { STUDIO_AVAILABILITY, availabilityEnv } from './studioAvailability.js'
import { deriveWorkspaceProjectState } from './workspaceProjectState.js'

const sentence = (id) => STUDIO_AVAILABILITY[id].disclaimer

afterEach(() => {
  cleanup()
  mocks.navigate.mockReset()
  window.localStorage.removeItem('leaf.availability.walk')
})

describe('the availability manifest on the staging host', () => {
  it('is running on the host the staging rows speak for', () => {
    expect(availabilityEnv()).toEqual({ hostname: 'platform-staging.leafdesign.ai', walk: false, investorDemo: false })
  })

  it('disables the landing sheet tabs with one visible sentence and keeps Cover and the entry buttons', () => {
    render(<LandingCast onTryTool={() => {}} onTryDemo={() => {}} />)
    const note = document.getElementById('lp-sheets-note')
    expect(note.textContent).toBe(sentence('site.sheets'))
    for (const name of ['02 The problem', '03 How Branch works', '04 Proof', '05 Pricing', '06 Docs']) {
      const tab = screen.getByRole('button', { name })
      expect(tab.getAttribute('aria-disabled')).toBe('true')
      expect(tab.getAttribute('aria-describedby')).toBe('lp-sheets-note')
      expect(tab.getAttribute('title')).toBe(sentence('site.sheets'))
      fireEvent.click(tab)
    }
    expect(mocks.navigate).not.toHaveBeenCalled()
    expect(document.querySelector('.lp-price').textContent).toBe('Free to try. Pricing is not yet available.')
    expect(screen.getByRole('button', { name: 'Try Branch on the sample rooftop' }).getAttribute('aria-disabled')).toBeNull()
    expect(screen.getByRole('button', { name: /^(Open workspace|Sign in \/ Create account)$/ }).getAttribute('aria-disabled')).toBeNull()
  })

  it('takes Generate away in the author panel and says authoring is not yet available', () => {
    const onAuthor = vi.fn()
    const { container } = render(<AuthorPanel onAuthor={onAuthor} buildEntitled />)
    const gate = container.querySelector('.author-gate[data-availability="unavailable"]')
    expect(gate.textContent).toBe(sentence('shared.author-tool'))
    fireEvent.change(container.querySelector('textarea'), { target: { value: 'Count the panels on the roof.' } })
    const generate = screen.getByRole('button', { name: 'Generate tool' })
    expect(generate.disabled).toBe(true)
    fireEvent.click(generate)
    expect(onAuthor).not.toHaveBeenCalled()
  })

  it('disables Review and run for a catalog tool the manifest takes away, and only that tool', () => {
    const onRequestRun = vi.fn()
    const tools = [
      { name: 'arlo-design', description: 'Feeder proposals.', params: { type: 'object', properties: {}, required: [] } },
      { name: 'count-by-layer', description: 'Counts entities.', params: { type: 'object', properties: {}, required: [] } },
    ]
    render(<ToolsPanel tools={tools} running={false} onRequestRun={onRequestRun} runDisabled={false} />)
    fireEvent.click(screen.getByText('arlo-design'))
    const blocked = screen.getByRole('button', { name: 'Review & run' })
    expect(blocked.disabled).toBe(true)
    expect(blocked.getAttribute('title')).toBe(sentence('solar.arlo-design'))
    expect(screen.getByRole('note').textContent).toBe(sentence('solar.arlo-design'))
    fireEvent.click(blocked)
    expect(onRequestRun).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('count-by-layer'))
    const open = screen.getByRole('button', { name: 'Review & run' })
    expect(open.disabled).toBe(false)
    expect(screen.queryByRole('note')).toBeNull()
    fireEvent.click(open)
    expect(onRequestRun).toHaveBeenCalledTimes(1)
  })

  it('never confirms a run of a tool the manifest takes away, from any proposal', () => {
    const onConfirmIntent = vi.fn()
    const mount = (tool) => render(
      <RoutePanel
        route={{ lane: 'run', tool, confidence: 0.99, runIntent: { tool, params: {} } }}
        tools={[{ name: tool, capabilities: ['drawing.read'], params: { properties: {} } }]}
        running={false}
        writeLocked={false}
        onConfirmIntent={onConfirmIntent}
        onPickAlternative={vi.fn()}
        onOpenAuthor={vi.fn()}
        onDismiss={vi.fn()}
      />,
    )
    const blocked = mount('arlo-design')
    const run = screen.getByRole('button', { name: 'Run arlo-design' })
    expect(run.disabled).toBe(true)
    expect(run.getAttribute('title')).toBe(sentence('solar.arlo-design'))
    expect(blocked.container.textContent).toContain(sentence('solar.arlo-design'))
    expect(blocked.container.textContent).not.toContain('you confirm before it runs')
    fireEvent.click(run)
    expect(onConfirmIntent).not.toHaveBeenCalled()
    blocked.unmount()
    mount('count-by-layer')
    const open = screen.getByRole('button', { name: 'Run count-by-layer' })
    expect(open.disabled).toBe(false)
    fireEvent.click(open)
    expect(onConfirmIntent).toHaveBeenCalledTimes(1)
  })

  it('lifts every disable for a verification walk', () => {
    window.localStorage.setItem('leaf.availability.walk', '1')
    const { container } = render(<AuthorPanel onAuthor={() => {}} buildEntitled />)
    expect(container.querySelector('.author-gate[data-availability="unavailable"]')).toBeNull()
    fireEvent.change(container.querySelector('textarea'), { target: { value: 'Count the panels on the roof.' } })
    expect(screen.getByRole('button', { name: 'Generate tool' }).disabled).toBe(false)
  })

  it('says what Connect AutoCAD needs before the click and leaves it enabled', () => {
    render(<LocalCadConnect status="stopped" onConnect={() => {}} onDisconnect={() => {}} />)
    const connect = screen.getByTestId('local-cad-connect')
    expect(connect.disabled).toBe(false)
    expect(connect.getAttribute('title')).toBe(STUDIO_AVAILABILITY['cad.connect-autocad'].note)
  })
})

describe('creating a project from the sample drawing', () => {
  const base = { drawingName: 'rooftop_demo', orgId: 'org-1' }

  it('disables the action with the manifest sentence when the drawing is not an account upload', () => {
    const state = deriveWorkspaceProjectState({ ...base, drawingAttachable: false })
    expect(state.kind).toBe('drawing-only')
    expect(state.action.disabled).toBe(true)
    expect(state.action.reason).toBe(sentence('project.create-from-sample-drawing'))
  })

  it('leaves the action alone for an uploaded drawing and for a caller that does not say', () => {
    for (const input of [{ ...base, drawingAttachable: true }, base]) {
      const state = deriveWorkspaceProjectState(input)
      expect(state.action.disabled).toBe(false)
      expect(state.action.reason).toBeNull()
    }
  })

  it('keeps the earlier, more specific blockers ahead of the sample sentence', () => {
    expect(deriveWorkspaceProjectState({ ...base, orgId: null, drawingAttachable: false }).action.reason)
      .toBe('Create a workspace first, then create a project.')
    expect(deriveWorkspaceProjectState({ ...base, mock: true, drawingAttachable: false }).action.reason)
      .toBe('This is the offline demo build; it talks to no workspace service.')
  })
})

describe('isAccountUploadDrawingId', () => {
  it('mirrors the server rule: a canonical UUID or the legacy upload receipt, nothing else', async () => {
    const { isAccountUploadDrawingId } = await import('../workspace/createProjectFromDrawing.js')
    expect(isAccountUploadDrawingId('913a2a67-4e76-4fbc-a58b-d0143425e2c5')).toBe(true)
    expect(isAccountUploadDrawingId('u-0123456789')).toBe(true)
    for (const id of ['demo', 'rooftop_demo', '', 'U-0123456789', 'u-012345678', '913A2A67-4E76-4FBC-A58B-D0143425E2C5', null, undefined, 7]) {
      expect(isAccountUploadDrawingId(id)).toBe(false)
    }
  })
})
