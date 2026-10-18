import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import WidgetPreview from './WidgetPreview.jsx'
import {
  CANVAS_HEIGHT, CANVAS_WIDTH, createTextElement, MAX_ELEMENTS,
  validateElement, validateElements,
} from './widgetDoc.js'

afterEach(cleanup)

const add = () => fireEvent.click(screen.getByRole('button', { name: 'Add text' }))
const edit = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const preview = () => screen.getByRole('region', { name: 'Preview' })
const layer = (name) => within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name })
const previewElements = () => [...preview().querySelectorAll('[data-element-id]')]

describe('scoped text widget editor', () => {
  it('adds, edits, selects and removes independent elements with useful focus', () => {
    render(<WidgetsView scopeKey="principal-a/project-a" />)
    expect(screen.getByText(/No text yet/)).toBeInTheDocument()
    add()
    edit('Text', 'First draft')
    const firstId = previewElements()[0].dataset.elementId
    expect(within(preview()).getByText('First draft')).toBeInTheDocument()
    add()
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    edit('Text', 'Second draft')
    const secondId = previewElements()[1].dataset.elementId
    expect(secondId).not.toBe(firstId)
    fireEvent.click(layer('Text 1: First draft'))
    expect(screen.getByLabelText('Text')).toHaveValue('First draft')
    edit('Text', 'First revised')
    expect(previewElements().map((element) => element.dataset.elementId)).toEqual([firstId, secondId])
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(within(preview()).queryByText('First revised')).not.toBeInTheDocument()
    expect(within(preview()).getByText('Second draft')).toBeInTheDocument()
    expect(screen.getByLabelText('Text')).toHaveValue('Second draft')
    expect(layer('Text 1: Second draft')).toHaveFocus()
    expect(previewElements()[0].dataset.elementId).toBe(secondId)
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getByRole('button', { name: 'Add text' })).toHaveFocus()
    expect(previewElements()).toHaveLength(0)
  })

  it('updates position and alignment immediately without changing selection or IDs', () => {
    render(<WidgetsView scopeKey="a/p" />)
    add()
    const id = previewElements()[0].dataset.elementId
    edit('X position (points)', '241')
    edit('Y position (points)', '144')
    edit('Alignment', 'right')
    edit('Text', 'At the edge')
    const element = previewElements()[0]
    expect(element).toHaveTextContent('At the edge')
    expect(element).toHaveStyle({ textAlign: 'right' })
    expect(parseFloat(element.style.left)).toBeCloseTo(241 / 361 * 100)
    expect(parseFloat(element.style.top)).toBeCloseTo(90)
    expect(element.dataset.elementId).toBe(id)
    expect(layer('Text 1: At the edge')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Text')).toHaveValue('At the edge')
  })

  it('isolates principal/project drafts, restores selection, and retains hidden drafts', () => {
    const { rerender } = render(<WidgetsView scopeKey="alice/project-a" />)
    add()
    edit('Text', 'Alice first')
    add()
    edit('Text', 'Alice second')
    fireEvent.click(layer('Text 1: Alice first'))
    const aliceIds = previewElements().map((element) => element.dataset.elementId)
    for (const scopeKey of ['bob/project-a', 'alice/project-b']) {
      rerender(<WidgetsView scopeKey={scopeKey} />)
      expect(previewElements()).toHaveLength(0)
      add()
      edit('Text', scopeKey)
      expect(within(preview()).queryByText('Alice first')).not.toBeInTheDocument()
    }
    rerender(<WidgetsView scopeKey="alice/project-a" hidden />)
    expect(screen.queryByRole('region', { name: 'Text widget editor' })).not.toBeInTheDocument()
    rerender(<WidgetsView scopeKey="alice/project-a" />)
    expect(screen.getByLabelText('Text')).toHaveValue('Alice first')
    expect(previewElements().map((element) => element.dataset.elementId)).toEqual(aliceIds)
    rerender(<WidgetsView scopeKey="bob/project-a" />)
    expect(screen.getByLabelText('Text')).toHaveValue('bob/project-a')
  })

  it('cannot create or edit without a scope and discards drafts on unmount', () => {
    const { rerender, unmount } = render(<WidgetsView />)
    for (const scopeKey of [undefined, '', '  ', null, 42]) {
      rerender(<WidgetsView scopeKey={scopeKey} />)
      expect(screen.getByRole('status')).toHaveTextContent('Choose a project')
      expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    }
    rerender(<WidgetsView scopeKey="__proto__" />)
    add()
    edit('Text', 'Mounted only')
    rerender(<WidgetsView />)
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    rerender(<WidgetsView scopeKey="__proto__" />)
    expect(screen.getByLabelText('Text')).toHaveValue('Mounted only')
    unmount()
    render(<WidgetsView scopeKey="__proto__" />)
    expect(previewElements()).toHaveLength(0)
  })

  it('renders HTML-looking input as exact literal React text', () => {
    render(<WidgetsView scopeKey="a/p" />)
    add()
    const literal = '<img src=x onerror="alert(1)"><script>bad()</script> & text'
    edit('Text', literal)
    expect(within(preview()).getByText(literal)).toBeInTheDocument()
    expect(previewElements()[0].textContent).toBe(literal)
    expect(preview().querySelector('img, script')).toBeNull()
  })

  it.each(['', '-', 'Infinity', 'NaN', '1e999', '0x10', '-1', '241.1'])('retains invalid X input %j and the last valid preview', (value) => {
    render(<WidgetsView scopeKey="a/p" />)
    add()
    edit('Text', 'Valid preview')
    edit('X position (points)', '12.5')
    const left = previewElements()[0].style.left
    edit('X position (points)', value)
    expect(screen.getByLabelText('X position (points)')).toHaveValue(value)
    expect(screen.getByLabelText('X position (points)')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent('X must be a finite number from 0 to 241.')
    expect(previewElements()[0].style.left).toBe(left)
    expect(previewElements()[0]).toHaveTextContent('Valid preview')
    edit('X position (points)', '24')
    expect(screen.getByRole('alert')).toBeEmptyDOMElement()
    expect(parseFloat(previewElements()[0].style.left)).toBeCloseTo(24 / 361 * 100)
  })

  it('keeps oversized text and invalid Y input per element and recovers after correction', () => {
    render(<WidgetsView scopeKey="a/p" />)
    add()
    edit('Text', 'Last valid')
    const oversized = 'x'.repeat(513)
    edit('Text', oversized)
    edit('Y position (points)', '145')
    expect(screen.getByLabelText('Text')).toHaveValue(oversized)
    expect(screen.getByRole('alert')).toHaveTextContent('at most 512 characters')
    expect(screen.getByRole('alert')).toHaveTextContent('Y must be a finite number from 0 to 144.')
    expect(previewElements()[0].textContent).toBe('Last valid')
    add()
    edit('Text', 'Other element')
    fireEvent.click(layer('Text 1: Last valid'))
    expect(screen.getByLabelText('Text')).toHaveValue(oversized)
    expect(screen.getByLabelText('Y position (points)')).toHaveValue('145')
    edit('Y position (points)', '144')
    expect(previewElements()[0].textContent).toBe('Last valid')
    edit('Text', 'x'.repeat(512))
    expect(previewElements()[0].textContent).toBe('x'.repeat(512))
    expect(previewElements()[1].textContent).toBe('Other element')
    expect(screen.getByRole('alert')).toBeEmptyDOMElement()
  })

  it('visibly bounds the draft at 32 unique stable IDs and permits adding after removal', () => {
    render(<WidgetsView scopeKey="a/p" />)
    for (let index = 0; index < MAX_ELEMENTS; index += 1) add()
    const ids = previewElements().map((element) => element.dataset.elementId)
    expect(new Set(ids).size).toBe(32)
    expect(screen.getByRole('status')).toHaveTextContent('32 of 32 text elements. Limit: 32')
    expect(screen.getByRole('button', { name: 'Add text' })).toBeDisabled()
    add()
    expect(previewElements()).toHaveLength(32)
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getByRole('button', { name: 'Add text' })).toBeEnabled()
    add()
    const nextIds = previewElements().map((element) => element.dataset.elementId)
    expect(nextIds.slice(0, 31)).toEqual(ids.slice(0, 31))
    expect(ids).not.toContain(nextIds[31])
  })
})

describe('closed text model boundary', () => {
  it('preserves the supplied text primitive defaults with generic copy', () => {
    expect(createTextElement('text-1')).toEqual({ id: 'text-1', kind: 'text', x: 16, y: 16, w: 120, h: 16, scale: 1, weight: 500, color: 'slate', align: 'left', visible: true, text: 'Your text' })
  })

  it.each(['action', 'actions', 'url', 'href', 'code', 'html', 'binding', 'provider'])('refuses unsupported %s fields', (field) => {
    expect(() => validateElement({ ...createTextElement('a'), [field]: 'untrusted' })).toThrow('Unsupported element field')
  })

  it('refuses unsupported kinds, duplicate IDs and excessive element counts', () => {
    expect(() => validateElement({ ...createTextElement('a'), kind: 'image' })).toThrow('Only text')
    expect(() => validateElements([createTextElement('a'), createTextElement('a')])).toThrow('unique')
    expect(() => validateElements(Array.from({ length: 33 }, (_, index) => createTextElement(String(index))))).toThrow('32')
    expect(() => validateElements(new Array(1))).toThrow('Expected a text element')
    expect(() => validateElements(null)).toThrow('32')
    expect(() => validateElement({ kind: 'text' })).toThrow('required')
  })

  it.each([
    { x: NaN }, { y: Infinity }, { w: -1 }, { h: 0 }, { x: '0' }, { x: -1 },
    { y: 145 }, { x: 242 }, { w: 362 }, { h: 161 }, { scale: Infinity },
    { scale: 0 }, { weight: 501 }, { color: 'url(evil)' }, { align: 'evil' },
    { visible: 'true' }, { text: 'x'.repeat(513) }, { text: null }, { id: '' },
  ])('rejects invalid model values %j', (patch) => {
    expect(() => validateElement({ ...createTextElement('a'), ...patch })).toThrow()
  })

  it('accepts finite in-canvas boundary geometry and presents visibility without mutation', () => {
    const elements = validateElements([
      { ...createTextElement('a'), x: 241, y: 144, text: 'Boundary' },
      { ...createTextElement('b'), x: 0, y: 0, w: CANVAS_WIDTH, h: CANVAS_HEIGHT, visible: false },
    ])
    for (const element of elements) {
      expect([element.x, element.y, element.w, element.h].every(Number.isFinite)).toBe(true)
      expect(element.x + element.w).toBeLessThanOrEqual(CANVAS_WIDTH)
      expect(element.y + element.h).toBeLessThanOrEqual(CANVAS_HEIGHT)
    }
    render(<WidgetPreview elements={elements} />)
    expect(previewElements()).toHaveLength(1)
    expect(previewElements()[0].textContent).toBe('Boundary')
    expect(elements[1].visible).toBe(false)
  })
})
