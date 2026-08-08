// FIRST import on purpose: importing this module installs the global error +
// unhandledrejection listeners, and ES module evaluation runs in declaration
// order. Anything that throws while the app's own modules evaluate is then
// already covered. Every other importer of telemetry.js (api.js, converse.js,
// ErrorBoundary's dynamic import) is reached only after the app is running,
// and the marketing pages may never reach one at all.
import './telemetry.js'
import React from 'react'
import ReactDOM from 'react-dom/client'
import SiteRoot from './site/SiteRoot.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <SiteRoot />
    </ErrorBoundary>
  </React.StrictMode>,
)
