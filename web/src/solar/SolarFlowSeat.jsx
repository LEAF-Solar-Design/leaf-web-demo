import { useId, useState } from 'react'

export default function SolarFlowSeat({ seated, children }) {
  const [open, setOpen] = useState(false)
  const contentId = useId()
  if (!seated) return children
  return <div className="solar-flow-seat" data-testid="solar-flow-seat" data-expanded={open}>
    <button type="button" className="solar-flow-seat-toggle" aria-expanded={open}
      aria-controls={contentId} onClick={() => setOpen((value) => !value)}>
      Solar flow tools <span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    <div id={contentId} className="solar-flow-seat-content" data-testid="solar-flow-seat-content">
      {children}
    </div>
  </div>
}
