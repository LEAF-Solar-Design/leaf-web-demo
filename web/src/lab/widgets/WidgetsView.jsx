import { useEffect, useId, useRef, useState } from 'react'
import WidgetPreview from './WidgetPreview.jsx'
import { createTextElement, MAX_ELEMENTS, textInputs, validateTextInputs } from './widgetDoc.js'
import './widgets.css'

const emptyDraft = { entries: [], selectedId: null }

export function WidgetsView({ scopeKey, hidden = false }) {
  const [drafts, setDrafts] = useState(() => new Map())
  const sequence = useRef(0)
  const prefix = useId()
  const addRef = useRef(null)
  const selectedRef = useRef(null)
  const focusAfterRemove = useRef(false)
  const hasScope = typeof scopeKey === 'string' && scopeKey.trim().length > 0
  const draft = hasScope ? drafts.get(scopeKey) || emptyDraft : emptyDraft
  const selected = draft.entries.find(({ element }) => element.id === draft.selectedId)

  useEffect(() => {
    if (focusAfterRemove.current) {
      focusAfterRemove.current = false
      const target = selectedRef.current || addRef.current
      target?.focus()
    }
  }, [draft])

  function updateDraft(change) {
    if (!hasScope) return
    setDrafts((previous) => {
      const next = new Map(previous)
      next.set(scopeKey, change(previous.get(scopeKey) || emptyDraft))
      return next
    })
  }

  function addText() {
    const element = createTextElement(`${prefix}-text-${++sequence.current}`)
    updateDraft((current) => current.entries.length >= MAX_ELEMENTS ? current : {
      entries: [...current.entries, { element, input: textInputs(element), errors: {} }], selectedId: element.id,
    })
  }

  function edit(field, value) {
    updateDraft((current) => ({ ...current, entries: current.entries.map((entry) => {
      if (entry.element.id !== current.selectedId) return entry
      const input = { ...entry.input, [field]: value }
      const result = validateTextInputs(entry.element, input)
      return { input, errors: result.errors, element: result.element || entry.element }
    }) }))
  }

  function removeText() {
    focusAfterRemove.current = true
    updateDraft((current) => {
      const index = current.entries.findIndex(({ element }) => element.id === current.selectedId)
      const entries = current.entries.filter(({ element }) => element.id !== current.selectedId)
      return { entries, selectedId: entries[Math.min(index, entries.length - 1)]?.element.id || null }
    })
  }

  const controlId = (field) => `${prefix}-${field}`
  return (
    <section className="widgets-view" hidden={hidden} aria-label="Text widget editor">
      <h2>Text widget</h2>
      {!hasScope ? <p role="status">Choose a project to create a text widget.</p> : (
        <div className="widgets-layout">
          <div className="widgets-editor">
            <button ref={addRef} type="button" onClick={addText} disabled={draft.entries.length >= MAX_ELEMENTS}>Add text</button>
            <p role="status">{draft.entries.length} of 32 text elements. Limit: 32 elements per draft.</p>
            {draft.entries.length === 0 && <p>No text yet. Use Add text to start your preview.</p>}
            <ul className="widgets-element-list" aria-label="Text elements">
              {draft.entries.map(({ element }, index) => (
                <li key={element.id}>
                  <button type="button" ref={element.id === draft.selectedId ? selectedRef : null}
                    aria-pressed={element.id === draft.selectedId}
                    onClick={() => updateDraft((current) => ({ ...current, selectedId: element.id }))}>
                    Text {index + 1}: {element.text || '(empty)'}
                  </button>
                </li>
              ))}
            </ul>
            {selected && (
              <div className="widgets-fields">
                <label htmlFor={controlId('text')}>Text</label>
                <textarea id={controlId('text')} value={selected.input.text} onChange={(event) => edit('text', event.target.value)}
                  aria-invalid={Boolean(selected.errors.text)} aria-describedby={controlId('errors')} rows={3} />
                <p>Up to 512 characters. Text stays on one line in the preview.</p>
                {['x', 'y'].map((axis) => (
                  <label key={axis} htmlFor={controlId(axis)}>{axis.toUpperCase()} position (points)
                    <input id={controlId(axis)} type="text" inputMode="decimal" value={selected.input[axis]}
                      onChange={(event) => edit(axis, event.target.value)} aria-invalid={Boolean(selected.errors[axis])}
                      aria-describedby={controlId('errors')} />
                  </label>
                ))}
                <label htmlFor={controlId('align')}>Alignment</label>
                <select id={controlId('align')} value={selected.input.align} onChange={(event) => edit('align', event.target.value)}>
                  <option value="left">Left</option><option value="center">Center</option><option value="right">Right</option>
                </select>
                <div id={controlId('errors')} role="alert" aria-atomic="true">
                  {Object.values(selected.errors).map((error) => <p key={error}>{error}</p>)}
                  {Object.keys(selected.errors).length > 0 && <p>Preview keeps the last valid values until these errors are fixed.</p>}
                </div>
                <button type="button" onClick={removeText}>Remove</button>
              </div>
            )}
          </div>
          <WidgetPreview elements={draft.entries.map(({ element }) => element)} />
        </div>
      )}
    </section>
  )
}

export default WidgetsView
