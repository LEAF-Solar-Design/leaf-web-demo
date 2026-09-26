import { useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import { solarFormKeys, solarView } from './solarView.js'

export default function SolarToolForm({ tool, onSubmit, onClose }) {
  const { view } = solarView(tool)
  const schema = {
    ...tool.params,
    properties: Object.fromEntries(solarFormKeys(tool, view).map((key) => [key, tool.params.properties[key]])),
  }
  const [values, setValues] = useState(() => defaultsOf(schema))
  const text = tool.label || tool.name

  return (
    <section
      id="solar-tool-form"
      className="solar-tool-form tool-body"
      aria-label={`${text} parameters`}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          onClose()
        }
      }}
    >
      <h3>{text}</h3>
      <SchemaForm schema={schema} values={values} onChange={setValues} />
      <button type="button" className="chip-act" onClick={() => { onSubmit(tool, values); onClose() }}>
        Review & run
      </button>
      <button type="button" className="chip-act" onClick={onClose}>Cancel</button>
    </section>
  )
}
