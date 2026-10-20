import { Children, Fragment, isValidElement, useId, useLayoutEffect, useRef, useState } from 'react'

// App's ribbon forms are direct children (possibly inside a Fragment), unlike
// the flow-rail editors and project workspace inside their own hosts. Inspect
// only that boundary so those other paths never reveal the seat.
function ribbonForm(children) {
  for (const child of Children.toArray(children)) {
    if (!isValidElement(child)) continue
    if (child.type === Fragment) {
      const form = ribbonForm(child.props.children)
      if (form) return form
    }
    // Recognize the existing form props without importing flag-gated forms:
    // importing their component types here would defeat bundle elimination.
    if (typeof child.type !== 'string' && typeof child.props.onSubmit === 'function'
      && typeof child.props.onClose === 'function') {
      if (child.props.tool) return { identity: child.props.tool, id: 'solar-tool-form' }
      if (child.props.row) return { identity: child.props.row, id: 'solar-step-editor' }
    }
    if (child.type === 'div' && child.props.id === 'solar-tool-form'
      && Children.toArray(child.props.children).some((form) => isValidElement(form)
        && typeof form.type !== 'string' && form.props.context
        && typeof form.props.onSubmit === 'function' && typeof form.props.onClose === 'function')) {
      return { identity: child.key, id: 'solar-tool-form' }
    }
  }
  return null
}

export default function SolarFlowSeat({ seated, children }) {
  const [open, setOpen] = useState(false)
  const userOpen = useRef(false)
  const contentRef = useRef(null)
  const contentId = useId()
  const form = ribbonForm(children)
  const activeForm = form?.identity ?? null
  const formId = form?.id
  // A ribbon form temporarily reveals the seat without replacing the user's
  // toggle preference. Changing tools reveals it again after a manual collapse.
  useLayoutEffect(() => {
    setOpen(activeForm ? true : userOpen.current)
  }, [activeForm])
  useLayoutEffect(() => {
    if (!activeForm || (seated && !open)) return
    const host = (contentRef.current || document).querySelector(`#${formId}`)
    const control = host?.querySelector('input:not([type="hidden"]):not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled), [tabindex="0"]')
    if (host && !control && !host.hasAttribute('tabindex')) host.tabIndex = -1
    ;(control || host)?.focus()
  }, [activeForm, formId, open, seated])
  if (!seated) return children
  return <div className="solar-flow-seat" data-testid="solar-flow-seat" data-expanded={open}>
    <button type="button" className="solar-flow-seat-toggle" aria-expanded={open}
      aria-controls={contentId} onClick={() => {
        userOpen.current = !open
        setOpen(userOpen.current)
      }}>
      Solar flow tools <span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    <div ref={contentRef} id={contentId} className="solar-flow-seat-content" data-testid="solar-flow-seat-content">
      {children}
    </div>
  </div>
}
