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

// SEATSTART2: scroll the seat (never the page) so a hidden marked Start becomes visible: first align its form's top
// with the seat's top edge, then, if Start still is not inside the seat, bring Start's own edge in.
function revealSettingsStart(seat, start) {
  const view = seat.getBoundingClientRect()
  const top = view.top + seat.clientTop
  const bottom = top + seat.clientHeight
  const inside = (rect) => rect.top >= top && rect.bottom <= bottom
  if (inside(start.getBoundingClientRect())) return
  const form = start.closest('form')
  if (form) seat.scrollTop += form.getBoundingClientRect().top - top
  const after = start.getBoundingClientRect()
  if (after.bottom > bottom) seat.scrollTop += after.bottom - bottom
  else if (after.top < top) seat.scrollTop += after.top - top
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
  // SEATSTART2: on the desktop cockpit, an open settings form's Start is brought into the seat once per mount or
  // expansion, so the user never has to scroll the seat to find it. Focus and page scroll are untouched.
  useLayoutEffect(() => {
    const seat = contentRef.current
    if (!seated || !open || !seat || typeof window.matchMedia !== 'function'
      || !window.matchMedia('(min-width: 981px)').matches
      || !seat.closest('.studio-shell .workspace-card')) return undefined
    let revealed = null
    const reveal = () => {
      const start = seat.querySelector('[data-seat-start]')
      if (!start || start === revealed) return
      revealed = start
      revealSettingsStart(seat, start)
    }
    reveal()
    const observer = typeof MutationObserver === 'function' ? new MutationObserver(reveal) : null
    observer?.observe(seat, { childList: true, subtree: true })
    return () => observer?.disconnect()
  }, [seated, open])
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
