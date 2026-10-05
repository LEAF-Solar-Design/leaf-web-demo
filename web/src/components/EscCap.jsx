import { forwardRef } from 'react'

const EscCap = forwardRef(function EscCap({ label, type, ...props }, ref) {
  return (
    <button ref={ref} {...(type === undefined ? {} : { type })} className="key hot" {...props} aria-label={label}>Esc</button>
  )
})

export default EscCap
