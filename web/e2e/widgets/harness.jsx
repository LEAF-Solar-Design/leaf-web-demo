import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import WidgetsView from '../../src/lab/widgets/WidgetsView.jsx'

// Browser fixture only: real editor and client, fixture document API in the spec.
// No application authentication, project creation, publication or provider calls.
function Harness() {
  const [project, setProject] = useState('regression-a')
  return <main>
    <h1>Widget browser regression fixture</h1>
    <label>Fixture project<select value={project} onChange={event => setProject(event.target.value)}>
      <option value="regression-a">Project A</option>
      <option value="regression-b">Project B</option>
    </select></label>
    <WidgetsView projectId={project} accountScope="synthetic-regression" />
  </main>
}

// Match the relevant Studio roles, deliberately leaving data-series-5 absent.
const style = document.createElement('style')
style.textContent = `:root {
  --leaf-ink: #f4f4f5; --leaf-color-foreground: #f4f4f5;
  --leaf-ground: #09090b; --leaf-line: #52525b;
  --leaf-size-dense: 14px; --leaf-radius-card: 8px;
} body { margin: 0; padding: 16px; background: var(--leaf-ground);
  color: var(--leaf-ink); font: 14px Arial, sans-serif; }
  main { min-width: 0; }`
document.head.append(style)
createRoot(document.getElementById('root')).render(<Harness />)
