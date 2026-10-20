import { useId, useLayoutEffect, useRef, useState } from 'react'

export default function SolarFlowSeat({ seated, activeForm = null, formRef, children }) {
  const [open, setOpen] = useState(false)
  const userOpen = useRef(false)
  const contentId = useId()
  // A ribbon form temporarily reveals the seat without replacing the user's
  // toggle preference. Changing tools reveals it again after a manual collapse.
  useLayoutEffect(() => {
    setOpen(activeForm ? true : userOpen.current)
  }, [activeForm])
  useLayoutEffect(() => {
    if (!activeForm || (seated && !open)) return
    const form = formRef?.current
    const control = form?.querySelector('input:not([type="hidden"]):not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled), [tabindex="0"]')
    ;(control || form)?.focus()
  }, [activeForm, open, seated, formRef])
  if (!seated) return children
  return <div className="solar-flow-seat" data-testid="solar-flow-seat" data-expanded={open}>
    <button type="button" className="solar-flow-seat-toggle" aria-expanded={open}
      aria-controls={contentId} onClick={() => {
        userOpen.current = !open
        setOpen(userOpen.current)
      }}>
      Solar flow tools <span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    <div id={contentId} className="solar-flow-seat-content" data-testid="solar-flow-seat-content">
      {children}
    </div>
  </div>
}
