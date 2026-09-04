// @vitest-environment jsdom
//
// Standardization slice 5a: ONE PromptBox everywhere. The stage (/try) mounts
// this component where its hand-rolled .tc-bar block stood, and 36 e2e rows
// key off that block's classes (aria-label="Command bar", .tc-bar-input,
// .tc-run, .tc-bar-proj, .tc-bar-key) and its behaviour (the Run ladder, a
// static ⌘K keycap, no G2 drop catcher). Every stage prop is optional, and
// the console passes none of them, so the FIRST suite here is the console's
// guarantee: the default render is byte-identical to the element sequence
// captured from origin/main's PromptBox BEFORE this slice touched the file.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PromptBox from './PromptBox.jsx'
import { subscribeUnauthorized } from '../api.js'

// tag|class|testid, depth first: the same probe surfaceFrame.render.test.jsx
// uses, so a class rename, a moved node or a dropped testid all read as a
// changed sequence.
function sequence(node) {
  const out = []
  const walk = (el) => {
    for (const child of el.children) {
      out.push([
        child.tagName.toLowerCase(),
        child.getAttribute('class') || '',
        child.getAttribute('data-testid') || '',
      ].join('|'))
      walk(child)
    }
  }
  walk(node)
  return out
}

// Captured on origin/main 8ff0c601 with PromptBox.jsx untouched (this file
// was written and run BEFORE the slice-5a edit, which is what makes the pin
// evidence rather than a restatement of the new code).
const CONSOLE_SEQUENCE = [
  'div|bar|',
  'div|bar-input|',
  'span|bar-caret|',
  'textarea|bar-field|command-bar',
  'div|bar-controls|',
  'button|bar-add|',
  'button|bar-scope|',
  'span|bar-proj|',
  'span|key|',
  'button|chip-act|',
]

const noop = () => {}

function mount(props = {}) {
  return render(
    <div data-testid="host">
      <PromptBox value="" onChange={noop} onDispatch={noop} projectName="cat-panels" {...props} />
    </div>,
  )
}

beforeEach(() => {
  // PromptBox loads the MCP server list on mount; the picker is not under
  // test here, and a network call from a unit test is a defect.
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) })))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the console render (no stage props) is byte-identical to before slice 5a', () => {
  it('renders the captured element sequence', () => {
    mount()
    expect(sequence(screen.getByTestId('host'))).toEqual(CONSOLE_SEQUENCE)
  })

  it('keeps its own Run ladder: routing or an empty prompt disables the chip, and no title is written', () => {
    const { rerender } = mount()
    const run = screen.getByRole('button', { name: 'Run', exact: true })
    expect(run).toBeDisabled()
    expect(run).not.toHaveAttribute('title')
    rerender(
      <div data-testid="host">
        <PromptBox value="count panels" onChange={noop} onDispatch={noop} projectName="cat-panels" />
      </div>,
    )
    expect(screen.getByRole('button', { name: 'Run', exact: true })).toBeEnabled()
  })

  it('keeps the G2 drop catcher: a drop lands the honest failure strip', () => {
    const { container } = mount()
    fireEvent.drop(container.querySelector('.bar'), { dataTransfer: { files: [new File(['x'], 'm.json')] } })
    expect(container.querySelector('.strip-failed')).not.toBeNull()
    expect(container.querySelector('.strip-failed').textContent).toContain('m.json')
  })
})

describe('the stage mount (/try): the tc-bar hooks ride on PromptBox’s own nodes', () => {
  const STAGE = {
    classNames: { wrap: 'tc-bar-input-row', input: 'tc-bar-input', run: 'tc-run' },
    projectSlot: <span className="bar-proj tc-bar-proj">No drawing</span>,
    keycap: <span className="key tc-bar-key">⌘K</span>,
    disabledReason: null,
    runLabel: 'Run',
    routingLabel: 'Routing',
    placeholder: 'Describe a change to this drawing. Nothing runs until you submit it.',
    dropIngestEnabled: false,
    imageAttachmentsEnabled: false,
    commandLine: true,
  }

  it('adds the aliases beside the box’s own classes instead of renaming them', () => {
    mount(STAGE)
    expect(sequence(screen.getByTestId('host'))).toEqual([
      'div|bar bar-command-line|',
      'div|bar-input tc-bar-input-row|',
      'span|bar-caret|',
      'textarea|bar-field tc-bar-input|command-bar',
      'div|bar-controls|',
      'button|bar-add|',
      'button|bar-scope|',
      'span|bar-proj tc-bar-proj|',
      'span|key tc-bar-key|',
      'button|chip-act tc-run|',
    ])
  })

  it('the input element carries aria-label="Command bar" and is the .tc-bar-input the e2e rows focus', () => {
    const { container } = mount(STAGE)
    const input = container.querySelector('.tc-bar-input')
    expect(input.tagName).toBe('TEXTAREA')
    expect(input).toBe(screen.getByLabelText('Command bar'))
    expect(input).toHaveAttribute('placeholder', STAGE.placeholder)
    input.focus()
    expect(input).toHaveFocus()
  })

  it('.tc-bar-key is the static ⌘K keycap, on every platform and while focused', () => {
    const { container } = mount(STAGE)
    const key = container.querySelector('.tc-bar-key')
    expect(key).toHaveTextContent('⌘K')
    container.querySelector('.tc-bar-input').focus()
    expect(container.querySelector('.tc-bar-key')).toHaveTextContent('⌘K')
    expect(container.querySelectorAll('.key')).toHaveLength(1)
  })

  it('.tc-bar-proj shows what the stage hands it', () => {
    const { container } = mount({ ...STAGE, projectSlot: <span className="bar-proj tc-bar-proj">cat-panels</span> })
    expect(container.querySelector('.tc-bar-proj')).toHaveTextContent('cat-panels')
  })

  it('.tc-run is the Run trigger: click dispatches, and the stage’s labels replace the box’s', () => {
    const onDispatch = vi.fn()
    const { container, rerender } = mount({ ...STAGE, onDispatch, runLabel: 'Send' })
    const run = container.querySelector('.tc-run')
    expect(run).toBe(screen.getByRole('button', { name: 'Send', exact: true }))
    fireEvent.click(run)
    expect(onDispatch).toHaveBeenCalledTimes(1)
    rerender(
      <div data-testid="host">
        <PromptBox value="" onChange={noop} onDispatch={onDispatch} projectName="x" {...STAGE} routing />
      </div>,
    )
    expect(container.querySelector('.tc-run')).toHaveTextContent('Routing')
  })

  it('disabledReason null enables Run even with an empty prompt (the stage’s ladder never had the empty-prompt rung)', () => {
    const { container } = mount(STAGE)
    expect(container.querySelector('.tc-run')).toBeEnabled()
    expect(container.querySelector('.tc-run')).not.toHaveAttribute('title')
  })

  it('a disabledReason string disables Run and carries the exact sentence as the title', () => {
    const reason = 'Upload a DWG or DXF before running a request.'
    const { container } = mount({ ...STAGE, value: 'count panels', disabledReason: reason })
    const run = container.querySelector('.tc-run')
    expect(run).toBeDisabled()
    expect(run).toHaveAttribute('title', reason)
  })

  it('Enter is a no-op while a route is shown (routeActive), and dispatches otherwise', () => {
    const onDispatch = vi.fn()
    const { container, rerender } = mount({ ...STAGE, value: 'count panels', onDispatch, routeActive: true })
    fireEvent.keyDown(container.querySelector('.tc-bar-input'), { key: 'Enter' })
    expect(onDispatch).not.toHaveBeenCalled()
    rerender(
      <div data-testid="host">
        <PromptBox value="count panels" onChange={noop} onDispatch={onDispatch} projectName="x" {...STAGE} routeActive={false} />
      </div>,
    )
    fireEvent.keyDown(container.querySelector('.tc-bar-input'), { key: 'Enter' })
    expect(onDispatch).toHaveBeenCalledTimes(1)
  })

  it('dropIngestEnabled=false: a drop on the well raises no failure strip, so the stage’s own upload handler owns the gesture', () => {
    const { container } = mount(STAGE)
    fireEvent.drop(container.querySelector('.bar'), { dataTransfer: { files: [new File(['x'], 'cat.dxf')] } })
    expect(container.querySelector('.strip-failed')).toBeNull()
    expect(container.querySelector('.bar-drop-hint')).toBeNull()
  })

  it('commandLine puts the reference’s prompt word in the caret (the staging marker class rides on the well)', () => {
    const { container } = mount(STAGE)
    expect(container.querySelector('.bar')).toHaveClass('bar-command-line')
    expect(container.querySelector('.bar-caret')).toHaveTextContent('Command:')
  })
})

// BLOCKER 1 (found 2026-09-04, fixed same day): mounting PromptBox on the
// public, often signed-out /try stage made the tenant-scoped
// /api/converse/mcp discovery fetch run unconditionally on every load — a
// signed-out visitor called a private endpoint, and a signed-in visitor on a
// tenant without the converse entitlement could be logged out of /try by this
// background call's 401. mcpDiscoveryEnabled gates the fetch; the discovery
// call is also unconditionally non-fatal (noteUnauthorized fatal:false) so a
// 401 on it can never wipe the stored token or fire the unauthorized
// listeners, regardless of the caller's predicate.
describe('mcpDiscoveryEnabled gates the /api/converse/mcp discovery fetch (BLOCKER 1)', () => {
  it('mcpDiscoveryEnabled=false (the signed-out stage default) makes NO network call', async () => {
    mount({ mcpDiscoveryEnabled: false })
    // Give any errant microtask a turn before asserting the negative.
    await Promise.resolve()
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('mcpDiscoveryEnabled=true (signed in and entitled) fetches the tenant MCP list', async () => {
    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ servers: [{ id: 'srv-1' }] }),
    })
    mount({ mcpDiscoveryEnabled: true })
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1))
    expect(globalThis.fetch.mock.calls[0][0]).toContain('/api/converse/mcp')
  })

  it('a 401 on the discovery call leaves leaf.jwt and the unauthorized listeners untouched', async () => {
    localStorage.setItem('leaf.jwt', 'live-token')
    const notified = []
    const unsubscribe = subscribeUnauthorized((source) => notified.push(source))
    globalThis.fetch.mockResolvedValueOnce({ ok: false, status: 401, json: () => Promise.resolve(null) })
    try {
      mount({ mcpDiscoveryEnabled: true })
      await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1))
      // Flush loadMcp's post-fetch microtasks (the noteUnauthorized call and
      // the state update both ride the same fetch resolution).
      await Promise.resolve()
      await Promise.resolve()
      expect(localStorage.getItem('leaf.jwt')).toBe('live-token')
      expect(notified).toEqual([])
    } finally {
      unsubscribe()
      localStorage.removeItem('leaf.jwt')
    }
  })
})
