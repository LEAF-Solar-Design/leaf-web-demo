// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const source = readFileSync(resolve(process.cwd(), 'src/site/ToolCast.jsx'), 'utf8').replace(/\r\n/g, '\n')
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)))
async function helpers() {
  const { TextEncoder, TextDecoder } = await import('node:util')
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  return import('./ToolCast.jsx')
}
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('w3 ToolCast issue sweep', () => {
  it('S1-83 labels any running tool without claiming a panel rearrangement', async () => {
    const { phaseLabel } = await helpers()
    expect(phaseLabel('running')).toBe('Running request')
    expect(phaseLabel('complete')).toBe('Drawing updated')
    expect(phaseLabel('undone')).toBe('Previous version restored')
  })

  it('S1-83 reports an upload as ready and does not invent a cat tool or preservation claim', () => {
    expect(source).toContain("drawingEvent.event === 'upload' ? 'ready' : 'complete'")
    expect(source).not.toContain("toolName: toolName || 'arrange-panels-as-cat'")
    expect(source).not.toContain('Panels preserved')
    expect(source).not.toContain('The panel run did not produce')
  })

  it('S1-84 humanizes drawing session failure and offers a connection retry', () => {
    const session = section('getSession(PUBLIC_DEMO, drawingSource)', '// showToast comes')
    expect(session).toContain('setSessionError(humanizeError(cause))')
    expect(source).not.toContain('Start the proof API')
    expect(source).toMatch(/sessionError &&[\s\S]*setSessionRetry\(\(current\) => current \+ 1\)[\s\S]*Retry connection/)
  })

  it('S1-85 humanizes string errors as well as Error objects', async () => {
    const { panelErrorText } = await helpers()
    expect(panelErrorText('GET /api/drawings/private/versions -> 500')).toBe('The service had a temporary problem. Try again.')
    expect(panelErrorText('Failed to fetch')).toBe("Couldn't reach the service. Check the connection and retry.")
  })

  it('S1-85 leaves the history failure in the history panel and sanitizes other drawing failures', () => {
    const handler = section('if (!drawingError) return', 'const {\n    seatIntake')
    expect(handler).toContain("context?.operation !== 'history'")
    expect(handler).toContain('setError(humanizeError(cause))')
    expect(handler).not.toContain('String(cause')
    expect(section('const recoverHistoricalVersion', 'const retryCatalogRun')).toContain('throw new Error(humanizeError(cause))')
  })

  it('S1-86 exposes the empty-drawing session probe failure instead of swallowing it', () => {
    const probe = section("getSession(false, 'rooftop_demo')", 'let live = true')
    expect(probe).toContain('setSessionError(humanizeError(cause))')
    expect(probe).toContain("setPhase('failed')")
  })

  it('S1-88 keeps the route Retry button without advertising an unbound R key', () => {
    const strip = section('{routeError && (', '{agentBanner && (')
    expect(strip).toContain('runRequest() }}>Retry')
    expect(strip).not.toContain('className="key">R')
  })

  it('S1-89 replaces the architecture caption with session-aware workspace guidance', () => {
    expect(source).not.toContain('Live service chain:')
    expect(source).not.toContain('controller state</span>')
    expect(source).toContain("sessionReady ? 'Your drawing and tool results stay together in this workspace.'")
    expect(source).toContain('Sign in to connect your workspace, or explore the sample drawing.')
  })

  it('S1-97 gives every unavailable workspace tab a discoverable explanation', () => {
    for (const id of ['workspace-tab-catalog', 'workspace-tab-author', 'workspace-tab-workspace', 'operations-tab-jobs', 'operations-tab-trust', 'operations-tab-versions', 'operations-tab-view']) {
      const button = section(`id="${id}"`, '</button>')
      expect(button).toContain('aria-disabled=')
      expect(button).toContain('title=')
      expect(button).not.toMatch(/\sdisabled=/)
      expect(button).toContain('setError(')
    }
  })

  it('S1-97 keeps an unavailable tab in arrow navigation and preserves focus there', async () => {
    const { moveStageTab } = await helpers()
    const activate = vi.fn()
    render(<div role="tablist" onKeyDown={moveStageTab}>
      <button role="tab">Operator</button>
      <button role="tab" aria-disabled="true" title="Sign in to open this panel." onClick={activate}>Catalog</button>
      <button role="tab">Project</button>
    </div>)
    screen.getByRole('tab', { name: 'Operator' }).focus()
    fireEvent.keyDown(document.activeElement, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Catalog' }))
    expect(activate).toHaveBeenCalledOnce()
  })

  it('S1-206 opens a usable shortcut reference from the act palette', () => {
    const palette = section('const stagePaletteActions', '// PromptBox has no')
    expect(palette).toContain('disabled: false')
    expect(palette).toContain('reason: null')
    expect(palette).toContain("title: 'Keyboard shortcuts'")
    expect(palette).toContain('Shift+Enter: add a new line')
  })

  it('S1-216 ignores the selected surface before pushing navigation history', () => {
    const select = section('const selectProductSurface', 'const openAccountDetails')
    expect(select).toContain('if (surfaceId === productSurfaceFromSearch(window.location.search)) return')
    expect(select.indexOf('return')).toBeLessThan(select.indexOf('window.history.pushState'))
  })

  it('S1-51 announces the Run refusal when Enter dispatches a blocked request', () => {
    const dispatch = section('const dispatchRequest', 'const runRequest')
    expect(dispatch).toContain('if (reason) { setError(reason); return }')
    expect(dispatch.indexOf('if (reason)')).toBeLessThan(dispatch.indexOf('catalog.actions.dispatch'))
  })

  it('S1-51 associates the Run explanation with the actual command input', () => {
    expect(source).toContain("input.setAttribute('aria-describedby', [previous, 'tc-run-reason']")
    expect(source).toContain('<p id="tc-run-reason"')
  })

  it('S1-165 resets lifecycle dialogs when the project changes', () => {
    expect(source).toMatch(/<ProjectLifecyclePanel\s+key=\{workspace.openProjectId\}\s+projectId=\{workspace.openProjectId\}/)
  })

  it('S1-212 opens loading details before awaiting the job and ignores superseded replies', () => {
    const inspect = section('const inspectHistoricalJob', '// S27: Escape')
    expect(inspect).toContain('const request = ++jobDetailsRequestRef.current')
    expect(inspect.indexOf("rows: ['Loading job details.']")).toBeLessThan(inspect.indexOf('await getJob'))
    expect(inspect.match(/if \(request !== jobDetailsRequestRef.current\) return/g)).toHaveLength(2)
    expect(inspect).toContain("label: 'Retry', onClick: () => inspectHistoricalJob(job)")
  })

  it('S1-212 invalidates pending details on dismissal so Escape cannot reopen the drawer', () => {
    expect(section('const closeDetails', 'const [uploadDragActive')).toContain('jobDetailsRequestRef.current += 1')
    expect(source).toContain('<DetailsDrawer data={drawer} onClose={closeDetails} />')
  })
})
