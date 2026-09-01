import React from 'react'
import { createRoot } from 'react-dom/client'

import CadEditSurface from '../../src/cadedit/CadEditSurface.jsx'
import { ENV_CAD_EDIT } from '../../src/cadedit/flag.js'

// F-3 closeout fixture. This is the real surface and its default real Worker.
// Keep the flag as the first operand so an off build can remove the surface.
createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {ENV_CAD_EDIT && <CadEditSurface />}
  </React.StrictMode>,
)
