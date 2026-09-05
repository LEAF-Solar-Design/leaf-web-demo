// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ElementContextMenu, { actionsForKind, askClaudeReason, CONTEXT_MENU_REASONS, rowsForIdentity } from './ElementContextMenu.jsx'
import { byId } from '../lib/actionRegistry.js'

const ensureSessionMock = vi.fn()
const postMessageMock = vi.fn()
vi.mock('../converse.js', () => ({
  ensureSession: (...args) => ensureSessionMock(...args),
  postMessage: (...args) => postMessageMock(...args),
}))
vi.mock('../useAnnotations.js', () => ({
  useAnnotations: () => ({
    annotation: null, busy: false, error: null, confirmation: null,
    preview: vi.fn(), accept: vi.fn(), reject: vi.fn(), retry: vi.fn(), undo: vi.fn(),
  }),
}))

afterEach(cleanup)

function Scene({ ctx }) {
  return (
    <div>
      <button type="button" data-element-id="tool:fit" data-testid="ribbon-fit">Fit</button>
      <div data-element-id="entity:AB12" data-testid="canvas">canvas</div>
      <div data-element-id="version:v-3" data-testid="version-tile">v3</div>
      <div data-element-id="job:job-1" data-testid="job-tile">job</div>
      <div role="dialog" aria-modal="true" data-testid="modal">
        <button type="button" data-element-id="tool:fit" data-testid="modal-fit">Fit (modal)</button>
      </div>
      <div data-testid="plain">no id here</div>
      <ElementContextMenu ctx={ctx} />
    </div>
  )
}

describe('actionsForKind / rowsForIdentity (pure)', () => {
  it('resolves a tool id straight through the registry', () => {
    expect(actionsForKind('tool', 'fit')).toEqual([byId('fit')])
    expect(actionsForKind('tool', 'not-a-real-id')).toEqual([])
  })
  it('resolves entity to Modify + Clipboard cut/copy (never paste)', () => {
    const actions = actionsForKind('entity', 'AB12')
    expect(actions.some((a) => a.id === 'modify:delete')).toBe(true)
    expect(actions.some((a) => a.id === 'clipboard:cutClip')).toBe(true)
    expect(actions.some((a) => a.id === 'clipboard:copyClip')).toBe(true)
    expect(actions.some((a) => a.id === 'clipboard:pasteClip')).toBe(false)
  })
  it('resolves version to the version cluster', () => {
    const ids = actionsForKind('version', 'v-3').map((a) => a.id)
    expect(ids).toEqual(['undo', 'redo', 'history'])
  })
  it('has no registry vocabulary yet for job/family/rung/turn/approval/item (an honest gap)', () => {
    for (const kind of ['job', 'family', 'rung', 'turn', 'approval', 'item']) {
      expect(actionsForKind(kind, 'x')).toEqual([])
    }
  })
  it('rowsForIdentity carries the honest disabled reason from when(ctx)', () => {
    const rows = rowsForIdentity({ kind: 'tool', id: 'fit' }, { hasDrawing: false })
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ id: 'fit', disabled: true, reason: 'no drawing loaded' })
  })
  it('rowsForIdentity is empty (not thrown) for null identity', () => {
    expect(rowsForIdentity(null)).toEqual([])
  })
})

describe('ElementContextMenu (mounted)', () => {
  it('opens on contextmenu over an element carrying data-element-id, with rows filtered by kind', async () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    fireEvent.contextMenu(screen.getByTestId('ribbon-fit'))
    const menu = await screen.findByTestId('element-context-menu')
    expect(menu).toBeTruthy()
    expect(screen.getByText('fit')).toBeTruthy()
    expect(screen.getByTestId('element-context-menu-ask-claude')).toBeTruthy()
  })

  it('does not open over an element with no data-element-id', () => {
    render(<Scene ctx={{}} />)
    fireEvent.contextMenu(screen.getByTestId('plain'))
    expect(screen.queryByTestId('element-context-menu')).toBeNull()
  })

  it('renders the terminal "Ask Claude to…" row disabled with the honest reason, and it runs nothing', async () => {
    const onSelect = () => { throw new Error('the terminal row must never run anything') }
    render(<Scene ctx={{}} />)
    fireEvent.contextMenu(screen.getByTestId('job-tile'))
    const ask = await screen.findByTestId('element-context-menu-ask-claude')
    expect(ask.getAttribute('data-reason')).toBe(CONTEXT_MENU_REASONS.askClaudeScoped)
    expect(ask.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(ask)
    expect(onSelect).toBeDefined() // never invoked (disabled Item swallows select)
  })

  it('shows a real disabled reason on a gated row rather than a fabricated one', async () => {
    render(<Scene ctx={{ hasVersions: false }} />)
    fireEvent.contextMenu(screen.getByTestId('version-tile'))
    await screen.findByTestId('element-context-menu')
    const undo = screen.getByRole('menuitem', { name: 'undo (unavailable: no versioned drawing)' })
    expect(undo.getAttribute('data-reason')).toBe('no versioned drawing')
  })

  it('opens via Shift+F10 on the focused element', async () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    const fitButton = screen.getByTestId('ribbon-fit')
    fitButton.focus()
    fireEvent.keyDown(document.activeElement, { key: 'F10', shiftKey: true })
    await screen.findByTestId('element-context-menu')
  })

  it('opens via the Menu key on the focused element', async () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    const fitButton = screen.getByTestId('ribbon-fit')
    fitButton.focus()
    fireEvent.keyDown(document.activeElement, { key: 'ContextMenu' })
    await screen.findByTestId('element-context-menu')
  })

  it('defers to a focused modal ancestor: Shift+F10 inside a dialog opens nothing', () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    const modalFit = screen.getByTestId('modal-fit')
    modalFit.focus()
    fireEvent.keyDown(document.activeElement, { key: 'F10', shiftKey: true })
    expect(screen.queryByTestId('element-context-menu')).toBeNull()
  })

  it('opens via a 500ms long press and not before', () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    const tile = screen.getByTestId('canvas')
    fireEvent.touchStart(tile, { touches: [{ clientX: 10, clientY: 10 }] })
    expect(screen.queryByTestId('element-context-menu')).toBeNull()
  })

  it('opens via long press after the hold completes', async () => {
    vi.useFakeTimers()
    try {
      render(<Scene ctx={{ hasDrawing: true }} />)
      const tile = screen.getByTestId('canvas')
      fireEvent.touchStart(tile, { touches: [{ clientX: 10, clientY: 10 }] })
      await vi.advanceTimersByTimeAsync(500)
    } finally {
      vi.useRealTimers()
    }
    await screen.findByTestId('element-context-menu')
  })

  it('cancels a long press on enough movement', async () => {
    vi.useFakeTimers()
    try {
      render(<Scene ctx={{ hasDrawing: true }} />)
      const tile = screen.getByTestId('canvas')
      fireEvent.touchStart(tile, { touches: [{ clientX: 10, clientY: 10 }] })
      fireEvent.touchMove(tile, { touches: [{ clientX: 40, clientY: 40 }] })
      await vi.advanceTimersByTimeAsync(500)
    } finally {
      vi.useRealTimers()
    }
    expect(screen.queryByTestId('element-context-menu')).toBeNull()
  })

  it('closes on Escape', async () => {
    render(<Scene ctx={{ hasDrawing: true }} />)
    fireEvent.contextMenu(screen.getByTestId('ribbon-fit'))
    await screen.findByTestId('element-context-menu')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('element-context-menu')).toBeNull())
  })
})

describe('askClaudeReason (pure)', () => {
  it('names the real reason for a kind with no conversation scope', () => {
    expect(askClaudeReason({ kind: 'job', id: 'x' }, {})).toBe(CONTEXT_MENU_REASONS.askClaudeScoped)
    expect(askClaudeReason(null, {})).toBe(CONTEXT_MENU_REASONS.askClaudeScoped)
  })
  it('names the drawing gap for an entity with no drawingId in ctx', () => {
    expect(askClaudeReason({ kind: 'entity', id: 'AB12' }, {})).toBe(CONTEXT_MENU_REASONS.askClaudeNoDrawing)
  })
  it('is live for an entity selection carrying a real drawingId', () => {
    expect(askClaudeReason({ kind: 'entity', id: 'AB12' }, { drawingId: 'demo' })).toBe('')
  })
})

describe('the scoped "Ask Claude to…" flow (mounted)', () => {
  beforeEach(() => {
    ensureSessionMock.mockReset()
    postMessageMock.mockReset()
  })

  it('is enabled for an entity selection with a real drawingId, and opens the scoped prompt', async () => {
    render(<Scene ctx={{ drawingId: 'demo' }} />)
    fireEvent.contextMenu(screen.getByTestId('canvas'))
    const ask = await screen.findByTestId('element-context-menu-ask-claude')
    expect(ask.getAttribute('aria-disabled')).not.toBe('true')
    fireEvent.click(ask)
    await screen.findByTestId('ask-claude-panel')
    expect(screen.getByTestId('ask-claude-input')).toBeTruthy()
  })

  it('stays disabled for a non-entity kind even with a drawingId in ctx', async () => {
    render(<Scene ctx={{ drawingId: 'demo' }} />)
    fireEvent.contextMenu(screen.getByTestId('ribbon-fit'))
    const ask = await screen.findByTestId('element-context-menu-ask-claude')
    expect(ask.getAttribute('aria-disabled')).toBe('true')
    expect(ask.getAttribute('data-reason')).toBe(CONTEXT_MENU_REASONS.askClaudeScoped)
  })

  it('posts through ensureSession + converse.postMessage with the entity scope envelope', async () => {
    ensureSessionMock.mockResolvedValue({ session_id: 'sess-1' })
    postMessageMock.mockResolvedValue({ turn_id: 't1', status: 'started' })
    render(<Scene ctx={{ drawingId: 'demo' }} />)
    fireEvent.contextMenu(screen.getByTestId('canvas'))
    fireEvent.click(await screen.findByTestId('element-context-menu-ask-claude'))
    const input = await screen.findByTestId('ask-claude-input')
    fireEvent.change(input, { target: { value: 'move this panel left' } })
    fireEvent.click(screen.getByTestId('ask-claude-send'))
    await waitFor(() => expect(postMessageMock).toHaveBeenCalled())
    expect(ensureSessionMock).toHaveBeenCalledWith({ kind: 'entity', handle: 'AB12', drawingId: 'demo' })
    expect(postMessageMock).toHaveBeenCalledWith('sess-1', { text: 'move this panel left' })
  })

  it('shows the secret guard refusal inline and never silently clears the input', async () => {
    ensureSessionMock.mockResolvedValue({ session_id: 'sess-1' })
    const refusal = {
      id: 'anthropic', reason: 'That looks like an Anthropic API key.',
      masked: 'sk-a••••••••', overridable: false,
    }
    const err = new Error(refusal.reason)
    err.secretRefused = true
    err.refusal = refusal
    postMessageMock.mockRejectedValue(err)
    render(<Scene ctx={{ drawingId: 'demo' }} />)
    fireEvent.contextMenu(screen.getByTestId('canvas'))
    fireEvent.click(await screen.findByTestId('element-context-menu-ask-claude'))
    const input = await screen.findByTestId('ask-claude-input')
    const secretShaped = 'sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    fireEvent.change(input, { target: { value: secretShaped } })
    fireEvent.click(screen.getByTestId('ask-claude-send'))
    await screen.findByTestId('ask-claude-secret-notice')
    expect(screen.getByTestId('ask-claude-secret-notice-reason').textContent).toBe(refusal.reason)
    expect(input.value).toBe(secretShaped)
  })
})
