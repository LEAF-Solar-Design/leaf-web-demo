import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import WidgetPreview from './WidgetPreview.jsx'
import { createTextElement, validateElement, validateElements } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const add = () => fireEvent.click(screen.getByRole('button', { name: 'Add text' }))
const text = (value) => fireEvent.change(screen.getByLabelText('Text'), { target: { value } })
const preview = () => screen.getByRole('region', { name: 'Preview' })

describe('scoped widget designer', () => {
  it('edits and previews literal text and removes it', () => {
    render(<WidgetsView scopeKey="a/p" />)
    expect(preview().querySelectorAll('[data-element-id]')).toHaveLength(0)
    add()
    const literal = '<img onerror="bad()"> & text'
    text(literal)
    expect(within(preview()).getByText(literal)).toBeInTheDocument()
    expect(preview().querySelector('img, script')).toBeNull()
    expect(preview().querySelectorAll('[data-element-id]')).toHaveLength(1)
    expect(screen.getByLabelText('Text')).toHaveAttribute('maxlength', '40')
    fireEvent.click(screen.getByRole('button', { name: 'Remove', exact: true }))
    expect(within(preview()).queryByText(literal)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('restores drafts and selection per scope, including hidden scopes, without network calls', () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const { rerender, unmount } = render(<WidgetsView scopeKey="alice/a" />)
    add(); text('Alice first'); add(); text('Alice second')
    fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name: 'Text 1: Alice first' }))
    const ids = [...preview().querySelectorAll('[data-element-id]')].map((e) => e.dataset.elementId)
    for (const scopeKey of ['bob/a', 'alice/b', '__proto__']) {
      rerender(<WidgetsView scopeKey={scopeKey} />)
      expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
      add(); text(scopeKey)
    }
    rerender(<WidgetsView scopeKey="alice/a" hidden />)
    expect(screen.queryByRole('region', { name: 'Text widget editor' })).not.toBeInTheDocument()
    rerender(<WidgetsView scopeKey="alice/a" />)
    expect(screen.getByLabelText('Text')).toHaveValue('Alice first')
    expect([...preview().querySelectorAll('[data-element-id]')].map((e) => e.dataset.elementId)).toEqual(ids)
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    unmount(); render(<WidgetsView scopeKey="alice/a" />)
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('starts the authenticated designer only after explicit document entry', () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />)
    expect(screen.getByRole('status')).toHaveTextContent('Choose a project')
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'my-widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    add()
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('limits each format to 32 elements and permits adding after removal', () => {
    render(<WidgetsView scopeKey="a/p" />)
    for (let n = 0; n < 32; n += 1) add()
    const elements = preview().querySelectorAll('[data-element-id]')
    expect(new Set([...elements].map((e) => e.dataset.elementId)).size).toBe(32)
    expect(screen.getByRole('button', { name: 'Add text' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove', exact: true }))
    expect(screen.getByRole('button', { name: 'Add text' })).toBeEnabled()
  })
})

describe('existing text primitive boundary', () => {
  it('still validates generic defaults and refuses executable fields and duplicate ids', () => {
    expect(createTextElement('text-1').text).toBe('Your text')
    for (const field of ['action', 'html', 'url', 'provider']) expect(() => validateElement({ ...createTextElement('a'), [field]: 'untrusted' })).toThrow('Unsupported')
    expect(() => validateElements([createTextElement('a'), createTextElement('a')])).toThrow('unique')
    expect(() => validateElements(new Array(1))).toThrow()
    expect(() => validateElements(Array.from({ length: 33 }, (_, n) => createTextElement(String(n))))).toThrow('32')
  })
  it.each([{ x: NaN }, { y: Infinity }, { w: -1 }, { h: 0 }, { x: -1 }, { y: 145 }, { x: 242 }, { weight: 501 }, { color: 'url(evil)' }, { visible: 'true' }, { text: 'x'.repeat(513) }])('rejects invalid model values %j', (patch) => {
    expect(() => validateElement({ ...createTextElement('a'), ...patch })).toThrow()
  })
  it('retains the standalone preview boundary and visibility behavior', () => {
    render(<WidgetPreview elements={validateElements([{ ...createTextElement('a'), x: 241, y: 144, text: 'Boundary' }, { ...createTextElement('b'), visible: false }])} />)
    expect(preview().querySelectorAll('[data-element-id]')).toHaveLength(1)
    expect(within(preview()).getByText('Boundary')).toBeInTheDocument()
  })
})
