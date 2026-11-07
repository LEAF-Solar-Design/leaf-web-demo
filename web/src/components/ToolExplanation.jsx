import { useState } from 'react'
import { explainTool } from '../lib/toolExplanation.js'

export default function ToolExplanation({ tool }) {
  const explanation = explainTool(tool)
  const [copyState, setCopyState] = useState('Copy explanation')
  const canCopy = typeof navigator !== 'undefined' && typeof navigator.clipboard?.writeText === 'function'

  async function copy() {
    try {
      await navigator.clipboard.writeText(explanation.text)
      setCopyState('Copied')
    } catch {
      setCopyState('Copy failed; try again')
    }
  }

  return (
    <details className="tool-explain">
      <summary>How this tool works</summary>
      <p>{explanation.summary}</p>
      <p className={`tool-explain-effect ${explanation.effect.editsDrawing ? 'write' : 'read'}`}>{explanation.effect.label}</p>
      <h4>Inputs</h4>
      {explanation.inputs.length ? <ul>{explanation.inputs.map((input) => (
        <li key={input.name}>{input.name} ({input.type}, {input.required ? 'required' : 'optional'}): {input.description || 'no description'}
          {input.default !== undefined && <span> Default: {JSON.stringify(input.default)}</span>}
        </li>
      ))}</ul> : <p>No inputs</p>}
      <p>Outputs: {explanation.outputs}</p>
      <h4>Provenance</h4>
      <dl>{explanation.provenance.map(({ label, value }) => (
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
      ))}</dl>
      {explanation.limits.length > 0 && <>
        <h4>Limits</h4>
        <dl>{explanation.limits.map(({ label, value }) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}</dl>
      </>}
      <h4>Not provided by this record</h4>
      <ul>{explanation.notProvided.map((item) => <li key={item}>{item}</li>)}</ul>
      <button type="button" className="chip-act" disabled={!canCopy}
        title={canCopy ? undefined : 'Copy is not available here'} onClick={copy}>{copyState}</button>
    </details>
  )
}
