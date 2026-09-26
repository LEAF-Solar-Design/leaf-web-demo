import React, { useRef, useState } from 'react'
import { getDiagnostics } from './diagnostics.js'

export default function DiagnosticsDetails({ diagnostics = getDiagnostics() }) {
  const [text, setText] = useState(() => diagnostics.snapshot())
  const [status, setStatus] = useState('')
  const textarea = useRef(null)
  const open = useRef(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setStatus('Copied.')
    } catch {
      textarea.current?.focus()
      textarea.current?.select()
      setStatus('Select the text above and copy it.')
    }
  }

  return <details onToggle={(event) => {
    const nextOpen = event.currentTarget.open
    if (nextOpen === open.current) return
    open.current = nextOpen
    if (nextOpen) {
      setText(diagnostics.snapshot())
      setStatus('')
    }
  }}>
    <summary>Connection details</summary>
    <textarea ref={textarea} aria-label="Connection details" readOnly value={text} rows={8} />
    <button type="button" onClick={copy}>Copy connection details</button>
    <div role="status">{status}</div>
  </details>
}
