import { forwardRef } from 'react'

const EscCap = forwardRef(function EscCap({ label = 'Close', type, className = '', style, ...props }, ref) {
  return (
    <button ref={ref} type={type ?? 'button'} className={`key hot esc-cap dp-focus ${className}`.trim()} style={{ borderRadius: 'var(--leaf-radius-key)', ...style }} {...props} aria-label={label}>Esc</button>
  )
})

export default EscCap
