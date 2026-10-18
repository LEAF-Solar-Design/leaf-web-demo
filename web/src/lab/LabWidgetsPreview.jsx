import { useId, useState } from 'react'
import WidgetsView from './widgets/WidgetsView.jsx'
import { resolveLabPreviewScope } from './labPreview.js'
import './labPreview.css'

// App keeps this instance in its existing PersistentSeat, outside profile panels.
export default function LabWidgetsPreview({ context }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const { scopeKey, reason } = resolveLabPreviewScope(context)
  if (context?.enabled !== true) return null
  return (
    <section className="lab-preview" aria-label="Lab">
      <button type="button" className="lab-preview-toggle" aria-expanded={open}
        aria-controls={`${id}-panel`} onClick={() => setOpen((value) => !value)}>Lab</button>
      <div id={`${id}-panel`} className="lab-preview-panel" hidden={!open}>
        <p>Local development preview. Drafts last while this workspace is mounted.</p>
        <div role="tablist" aria-label="Lab sections">
          <button type="button" role="tab" id={`${id}-widgets`} aria-selected="true"
            aria-controls={`${id}-widgets-panel`}>Widgets</button>
        </div>
        <div role="tabpanel" id={`${id}-widgets-panel`} aria-labelledby={`${id}-widgets`}>
          {reason && <p role="status">{reason}</p>}
          <WidgetsView scopeKey={scopeKey} hidden={Boolean(reason)} />
        </div>
      </div>
    </section>
  )
}
