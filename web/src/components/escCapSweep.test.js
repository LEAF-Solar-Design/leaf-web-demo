// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createElement } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ElementContextMenu from './ElementContextMenu.jsx'

const { ensureSession, postMessage } = vi.hoisted(() => ({
  ensureSession: vi.fn(),
  postMessage: vi.fn(),
}))
vi.mock('../converse.js', () => ({ ensureSession, postMessage }))
vi.mock('../useAnnotations.js', () => ({
  useAnnotations: () => ({
    annotation: null, busy: false, error: null, confirmation: null,
    preview: vi.fn(), accept: vi.fn(), reject: vi.fn(), retry: vi.fn(), undo: vi.fn(),
  }),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const FILES = [
  'components/DetailsDrawer.jsx',
  'components/WorkspaceSummary.jsx',
  'components/OpsDrawer.jsx',
  'components/CustomizePanel.jsx',
  'components/VersionHistory.jsx',
  'components/ClaudeAccountPanel.jsx',
  'components/LinkServiceDrawer.jsx',
  'components/ShortcutSheet.jsx',
  'components/PromptBox.jsx',
  'components/ElementContextMenu.jsx',
  'projects/ExportDialog.jsx',
]

function sourceOf(file) {
  return readFileSync(resolve(process.cwd(), 'src', file), 'utf8')
}

describe('the eleven dismiss controls share EscCap', () => {
  it.each(FILES)('%s imports and renders EscCap without a copied Esc or Close button', (file) => {
    const source = sourceOf(file)
    const importPath = file.startsWith('projects/') ? '../components/EscCap.jsx' : './EscCap.jsx'
    expect(source).toContain(`import EscCap from '${importPath}'`)
    expect(source).toMatch(/<EscCap\b/)
    const buttons = source.match(/<button\b[\s\S]*?<\/button>/g) || []
    for (const button of buttons) {
      const copiedCap = /className\s*=\s*["']key hot["']/.test(button) && />\s*Esc\s*<\/button>/.test(button)
      expect(copiedCap).toBe(false)
      expect(button).not.toMatch(/>\s*Close\s*<\/button>/)
    }
  })

  it('keeps the scoped ask dismiss testid on an explicit non-submit EscCap', () => {
    const source = sourceOf('components/ElementContextMenu.jsx')
    const close = (source.match(/<EscCap\b[\s\S]*?\/>/g) || [])
      .find((tag) => tag.includes('data-testid="ask-claude-close"'))
    expect(close).toBeTruthy()
    expect(close).toContain('type="button"')
    expect(close).toContain('label="Close"')
  })

  it('clicking ask-claude-close dismisses the real scoped form without submitting it', async () => {
    render(createElement('div', null,
      createElement('div', { 'data-element-id': 'entity:AB12', 'data-testid': 'entity' }, 'entity'),
      createElement(ElementContextMenu, { ctx: { drawingId: 'demo' } }),
    ))
    fireEvent.contextMenu(screen.getByTestId('entity'))
    fireEvent.click(await screen.findByTestId('element-context-menu-ask-claude'))
    const input = await screen.findByTestId('ask-claude-input')
    fireEvent.change(input, { target: { value: 'move this panel left' } })
    const close = screen.getByTestId('ask-claude-close')
    const form = close.closest('form')
    expect(form).not.toBeNull()
    const onSubmit = vi.fn((event) => event.preventDefault())
    form.addEventListener('submit', onSubmit)
    expect(screen.getByTestId('ask-claude-send').disabled).toBe(false)
    expect(close).toBe(screen.getByRole('button', { name: 'Close' }))
    expect(close.getAttribute('type')).toBe('button')
    expect(close.textContent).toBe('Esc')
    fireEvent.click(close)
    expect(onSubmit).not.toHaveBeenCalled()
    expect(ensureSession).not.toHaveBeenCalled()
    expect(postMessage).not.toHaveBeenCalled()
    expect(screen.queryByTestId('ask-claude-panel')).toBeNull()
  })
})
